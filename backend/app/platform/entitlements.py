"""Entitlement persistence — reading the allowance, recording consumption.

The kernel decides *what a plan permits*; this module decides *where the numbers
come from and where consumption goes*. Splitting them is what lets every rule in
``kernel.entitlements`` be tested without a database.

🔒 **The read is O(1)** (DDR-14). :func:`load_allowance` is one indexed lookup on
``usage_counters`` plus one join to the plan — never a scan of the thing being
metered. The exception is ``active_clients``, which DB §14.4 counts live from
``clients``; that table does not exist until S2, so :func:`load_allowance` takes
the count as an argument rather than reaching across a module boundary to get it
(Arch R6). ⚠️ A caller that omits ``live_used`` for that resource gets an
indeterminate allowance and a fail-safe denial, not a silent zero.

🔒 **Consumption is two writes that must not come apart.** :func:`record_usage`
appends a ``usage_events`` row *and* upserts the counter in the same statement
pair, inside the caller's transaction. The event log is what a drifted counter is
recovered from (DDR-14), so a counter incremented without its event is worse than
neither: it is a number nothing explains.

🔒 **Failure gives quota back as a negative event, never a decrement**
(EC-M10-04). :func:`release_usage` is the only correction path, and it appends
rather than edits — the trigger in migration 0007 refuses anything else.

⚠️ **Reading is never gated.** FR-M0-046 requires that when entitlement state is
indeterminate, existing data stays readable and only new metered actions are
blocked. Nothing in this module is called on a read path, and nothing here should
be. See the warning in ``kernel.entitlements``.

⚠️ Functions take an ``AsyncSession`` rather than opening one. The transaction
belongs to the request pipeline (ADR-04); a repository that committed
independently would record consumption for an action that then rolled back.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Final, cast

from sqlalchemy import Table, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.entitlements import Allowance, ResourceCode, check
from app.kernel.models import (
    PlanDefinition,
    Subscription,
    SubscriptionEvent,
    SubscriptionEventType,
    UsageCounter,
    UsageEvent,
)
from app.platform.logging import get_logger

logger = get_logger(__name__)

#: Core constructs for the two append-only tables, matching ``platform.consent``.
#: The write is a plain dict against ``insert()`` rather than an ORM instance, so
#: a row cannot be loaded, mutated and flushed — the ORM's normal behaviour, and
#: the one thing an append-only table must not permit.
_USAGE_EVENT_TABLE: Final[Table] = cast(Table, UsageEvent.__table__)
_SUBSCRIPTION_EVENT_TABLE: Final[Table] = cast(Table, SubscriptionEvent.__table__)
_USAGE_COUNTER_TABLE: Final[Table] = cast(Table, UsageCounter.__table__)


def now() -> datetime:
    """Timezone-aware current time.

    Centralised so every usage row is UTC. A naive datetime in a metering record
    makes a period boundary ambiguous, and the period boundary is what decides
    which month a tenant's quota was consumed in.
    """
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SubscriptionSnapshot:
    """A tenant's commercial position, read once per enforcement decision.

    🔒 A snapshot rather than the ORM rows: the enforcement path must not be able
    to trigger a lazy load while deciding whether an action is permitted, and the
    limit read here is the one that gets snapshotted onto the counter.
    """

    subscription_id: uuid.UUID
    plan_definition_id: uuid.UUID
    plan_code: str
    status: str
    limits: dict[str, object]


def _coerce_limit(raw: object) -> tuple[Decimal | None, bool]:
    """Interpret one value from ``plan_definitions.limits``.

    Returns ``(limit, is_determinate)``.

    🔒 Three distinct outcomes, and collapsing any two is the bug FR-M0-046
    exists to prevent:

    * a number → that limit, determinate;
    * an explicit JSON ``null`` → unlimited, determinate. The plan says so.
    * anything else, including a missing key → **indeterminate**. A limits object
      that does not describe this resource is a configuration error, and the safe
      reading of "we cannot tell" is not "unlimited".

    ⚠️ ``bool`` is rejected before ``int``. In Python ``True`` is an ``int``, so a
    stray ``{"active_clients": true}`` would otherwise become a limit of 1 — a
    plan that silently permits exactly one client.
    """
    if raw is None:
        return None, True
    if isinstance(raw, bool):
        return None, False
    if isinstance(raw, int | float):
        value = Decimal(str(raw))
        return (value, True) if value >= 0 else (None, False)
    if isinstance(raw, str):
        try:
            value = Decimal(raw)
        except (InvalidOperation, ValueError):
            return None, False
        return (value, True) if value >= 0 else (None, False)
    return None, False


# ─── Reads ───────────────────────────────────────────────────────────────


async def load_subscription(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> SubscriptionSnapshot | None:
    """The tenant's subscription and the plan it points at.

    ``None`` when the tenant has no subscription row. 🔒 The caller must treat
    that as indeterminate rather than as free-tier access: a tenant with no
    commercial record is a provisioning failure, and defaulting it to the most
    generous free plan would hand out quota nobody granted.

    ⚠️ Reads under RLS. ``subscriptions`` is Pattern A, so this returns nothing
    at all unless a tenant scope is set on the transaction — which is the correct
    failure, not a silent cross-tenant read.

    ⚠️ 🔒 **No ``FOR UPDATE`` variant, and it is not an oversight.** Serialising
    metered actions by locking this row is the obvious design and PostgreSQL
    refuses it: row-level locking requires UPDATE privilege, and migration 0007
    revokes INSERT and UPDATE on ``subscriptions`` from ``app_user`` precisely so
    a tenant cannot provision or upgrade its own plan. ``SELECT ... FOR UPDATE``
    as ``app_user`` fails with "permission denied for table subscriptions".
    :class:`DatabaseEntitlementGuard` uses an advisory lock instead.
    """
    result = await session.execute(
        select(
            Subscription.id,
            Subscription.plan_definition_id,
            Subscription.status,
            PlanDefinition.code,
            PlanDefinition.limits,
        )
        .join(PlanDefinition, PlanDefinition.id == Subscription.plan_definition_id)
        .where(Subscription.tenant_id == tenant_id)
    )
    row = result.first()
    if row is None:
        logger.warning("entitlements.subscription_missing", extra={"tenant_id": str(tenant_id)})
        return None

    limits = row.limits if isinstance(row.limits, dict) else {}
    if not isinstance(row.limits, dict):
        # 🔒 The CHECK constraint makes this near-impossible, so reaching it means
        # something wrote past the constraint. Logged loudly; the empty dict then
        # makes every resource indeterminate, which fails safe.
        logger.error(
            "entitlements.limits_not_an_object",
            extra={"tenant_id": str(tenant_id), "plan_code": row.code},
        )

    return SubscriptionSnapshot(
        subscription_id=row.id,
        plan_definition_id=row.plan_definition_id,
        plan_code=row.code,
        status=row.status.value if hasattr(row.status, "value") else str(row.status),
        limits=dict(limits),
    )


async def load_allowance(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    resource: ResourceCode,
    subscription: SubscriptionSnapshot | None = None,
    live_used: Decimal | int | None = None,
    at: datetime | None = None,
) -> Allowance:
    """Assemble the current position for one resource — the enforcement read.

    🔒 DDR-14 — one indexed lookup, never a scan. The counter supplies ``used``;
    the plan supplies the limit.

    🔒 FR-M0-046 — every failure path here produces an *indeterminate* allowance
    rather than an optimistic one. A missing subscription, an unreadable limits
    object, a missing key, and a live-counted resource whose count was not
    supplied all land in the same place: new metered actions blocked, reads
    untouched.

    Args:
        subscription: Pass one already loaded to avoid a second query when
            several resources are checked in one request.
        live_used: 🔒 Required for ``active_clients``, which DB §14.4 counts from
            ``clients`` rather than from a counter (M1.5). Ignored otherwise.
        at: The moment defining "current period". Defaults to now.
    """
    snapshot = (
        subscription
        if subscription is not None
        else await load_subscription(session, tenant_id=tenant_id)
    )

    if snapshot is None:
        return Allowance(
            resource=resource,
            used=Decimal(0),
            limit=None,
            plan_code="unknown",
            is_determinate=False,
        )

    limit, determinate = _coerce_limit(snapshot.limits.get(resource.limit_key))
    if not determinate:
        logger.error(
            "entitlements.limit_unreadable",
            extra={
                "tenant_id": str(tenant_id),
                "resource": resource.value,
                "plan_code": snapshot.plan_code,
                "limit_key": resource.limit_key,
            },
        )

    if resource.is_counted_live:
        # 🔒 DB §14.4 / M1.5 — counted from source, never from a counter row.
        if live_used is None:
            logger.error(
                "entitlements.live_count_missing",
                extra={"tenant_id": str(tenant_id), "resource": resource.value},
            )
            return Allowance(
                resource=resource,
                used=Decimal(0),
                limit=limit,
                plan_code=snapshot.plan_code,
                is_determinate=False,
            )
        return Allowance(
            resource=resource,
            used=Decimal(live_used),
            limit=limit,
            plan_code=snapshot.plan_code,
            is_determinate=determinate,
        )

    counter = await _load_counter(session, tenant_id=tenant_id, resource=resource, at=at or now())
    used = counter.used if counter is not None else Decimal(0)
    already_warned = counter.warned if counter is not None else False

    return Allowance(
        resource=resource,
        used=used,
        limit=limit,
        plan_code=snapshot.plan_code,
        is_determinate=determinate,
        already_warned=already_warned,
    )


@dataclass(frozen=True, slots=True)
class _CounterRow:
    used: Decimal
    warned: bool


async def _load_counter(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    resource: ResourceCode,
    at: datetime,
) -> _CounterRow | None:
    """The counter covering ``at``, or ``None`` if none has been opened yet.

    ⚠️ Absence is *zero used*, not indeterminate. A tenant who has never consumed
    this resource in this period legitimately has no row, and treating that as an
    incident would block the first metered action of every month.
    """
    result = await session.execute(
        select(UsageCounter.used_amount, UsageCounter.warned_at_80pct).where(
            UsageCounter.tenant_id == tenant_id,
            UsageCounter.resource_code == resource.value,
            UsageCounter.period_start <= at,
            UsageCounter.period_end > at,
        )
    )
    row = result.first()
    if row is None:
        return None
    return _CounterRow(used=Decimal(row.used_amount), warned=row.warned_at_80pct is not None)


# ─── Periods ─────────────────────────────────────────────────────────────


def month_window(at: datetime) -> tuple[datetime, datetime]:
    """The calendar-month window containing ``at``, in UTC.

    🔒 Calendar months, not rolling 30-day windows, and not the subscription's
    own anniversary. The plan says "150 AI drafts per month" and the practitioner
    reads that as a calendar month; a rolling window would make the quota refill
    at a time nobody can predict, and the support conversation that follows costs
    more than the precision is worth.

    ⚠️ ``period_end`` is exclusive — the first instant of the next month. The
    counter lookup uses ``period_start <= at < period_end``, so an inclusive end
    would put the final microsecond of a month in two periods at once.
    """
    start = at.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


# ─── Writes ──────────────────────────────────────────────────────────────


async def record_usage(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    resource: ResourceCode,
    amount: Decimal | int,
    source_module: str,
    source_record_id: uuid.UUID | None = None,
    limit_amount: Decimal | None = None,
    at: datetime | None = None,
) -> None:
    """Record consumption: one event, one counter increment, one transaction.

    🔒 DDR-14 — both writes or neither. The event log is what a drifted counter
    is recovered from, so a counter incremented without its event is worse than
    no counter at all: it is a number nothing can explain. Both statements join
    the caller's transaction, so a failure after this point rolls back the
    consumption along with the action that caused it.

    🔒 The counter is an **upsert**, not a read-modify-write. Two concurrent
    metered actions in the same period would otherwise race on the read and one
    increment would be lost — the unique constraint on
    ``(tenant_id, resource_code, period_start)`` is what makes
    ``ON CONFLICT DO UPDATE`` the whole operation.

    ⚠️ Not called for ``active_clients``. That resource is counted live from
    ``clients`` (DB §14.4), and maintaining a counter alongside the live count
    would create exactly the drift M1.5 avoids by not having one.

    Args:
        amount: 🔒 Signed. Negative compensates a failed action — but prefer
            :func:`release_usage`, which names the intent.
        limit_amount: Snapshotted onto a newly opened counter so a mid-period
            plan change cannot retroactively alter what was permitted earlier in
            the period (DB §14.4). Ignored when the counter already exists.
    """
    if resource.is_counted_live:
        raise ValueError(
            f"{resource.value} is counted live from its source table (DB §14.4); "
            "recording a counter for it would create the drift M1.5 avoids."
        )

    quantity = Decimal(amount)
    if quantity == 0:
        # The CHECK constraint would reject it anyway; failing here names the
        # caller rather than surfacing as an integrity error three frames up.
        raise ValueError("A usage event of zero records nothing (ck_usage_events__amount_non_zero)")

    moment = at or now()
    period_start, period_end = month_window(moment)

    await session.execute(
        insert(_USAGE_EVENT_TABLE).values(
            tenant_id=tenant_id,
            resource_code=resource.value,
            amount=quantity,
            source_module=source_module,
            source_record_id=source_record_id,
            occurred_at=moment,
        )
    )

    # 🔒 Both halves clamp at zero, and both are required.
    #
    # ⚠️ PostgreSQL evaluates a CHECK constraint against the *proposed* row before
    # `ON CONFLICT` resolution, so the INSERT half is validated even when the row
    # already exists and the UPDATE is what will actually run. A compensating
    # negative event (EC-M10-04) therefore aborts on
    # `ck_usage_counters__used_non_negative` unless the inserted value is
    # clamped — the correction path would fail exactly when it is needed.
    #
    # The counter is a *cache* of the event log, and the log keeps the true
    # signed history: a reconciliation pass replays `usage_events` and can still
    # detect a double refund there. Clamping here loses nothing that is not
    # recoverable, whereas crashing loses the correction entirely.
    insert_amount = quantity if quantity > 0 else Decimal(0)
    statement = (
        pg_insert(_USAGE_COUNTER_TABLE)
        .values(
            tenant_id=tenant_id,
            resource_code=resource.value,
            period_start=period_start,
            period_end=period_end,
            used_amount=insert_amount,
            limit_amount=limit_amount,
            updated_at=moment,
        )
        .on_conflict_do_update(
            constraint="uq_usage_counters__tenant_resource_period",
            set_={
                # GREATEST rather than a bare sum: a compensating event for usage
                # recorded in a *previous* period would otherwise drive this
                # period's counter negative and hit the same constraint.
                "used_amount": func.greatest(_USAGE_COUNTER_TABLE.c.used_amount + quantity, 0),
                "updated_at": moment,
            },
        )
    )
    await session.execute(statement)


async def release_usage(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    resource: ResourceCode,
    amount: Decimal | int,
    source_module: str,
    source_record_id: uuid.UUID | None = None,
    at: datetime | None = None,
) -> None:
    """Give quota back after an action consumed it and then failed — EC-M10-04.

    🔒 A **compensating negative event**, never an edit to the event that
    consumed the quota and never a bare counter decrement. The trigger in
    migration 0007 refuses the first; the second would leave the counter and its
    log disagreeing, and the log is the thing the counter is rebuilt from.

    Args:
        amount: The positive quantity to return. Negated here so callers never
            have to reason about the sign at the call site — passing a negative
            is rejected rather than silently doubling the consumption.
    """
    quantity = Decimal(amount)
    if quantity <= 0:
        raise ValueError(
            "release_usage takes the positive amount to give back; it negates it itself."
        )

    await record_usage(
        session,
        tenant_id=tenant_id,
        resource=resource,
        amount=-quantity,
        source_module=source_module,
        source_record_id=source_record_id,
        at=at,
    )


async def mark_warned(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    resource: ResourceCode,
    at: datetime | None = None,
) -> None:
    """Stamp the 80% warning as sent for this period — FR-M10-005.

    🔒 Guarded on ``warned_at_80pct IS NULL`` in the statement itself rather than
    by reading first. Two concurrent metered actions can both observe the
    threshold crossing, and a check-then-write would send the warning twice.
    """
    moment = at or now()
    period_start, period_end = month_window(moment)

    await session.execute(
        update(_USAGE_COUNTER_TABLE)
        .where(
            _USAGE_COUNTER_TABLE.c.tenant_id == tenant_id,
            _USAGE_COUNTER_TABLE.c.resource_code == resource.value,
            _USAGE_COUNTER_TABLE.c.period_start == period_start,
            _USAGE_COUNTER_TABLE.c.period_end == period_end,
            _USAGE_COUNTER_TABLE.c.warned_at_80pct.is_(None),
        )
        .values(warned_at_80pct=moment)
    )


async def record_subscription_event(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    subscription_id: uuid.UUID,
    event_type: SubscriptionEventType,
    actor_type: str,
    actor_id: uuid.UUID | None = None,
    from_plan_id: uuid.UUID | None = None,
    to_plan_id: uuid.UUID | None = None,
    reason: str | None = None,
    at: datetime | None = None,
) -> None:
    """Append one entry to the subscription history — DB §14.3.

    🔒 The only write path for this table, and append-only in the same sense as
    the consent ledger: ``app_user`` holds INSERT and SELECT and nothing more
    (migration 0007). EC-M10-01 and EC-M10-05 both need to know *when* state
    changed, which a mutable current-state row cannot answer.
    """
    await session.execute(
        insert(_SUBSCRIPTION_EVENT_TABLE).values(
            subscription_id=subscription_id,
            tenant_id=tenant_id,
            event_type=event_type,
            from_plan_id=from_plan_id,
            to_plan_id=to_plan_id,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
            occurred_at=at or now(),
        )
    )


#: 🔒 Derives a 64-bit advisory-lock key from a text label, using only documented
#: SQL. ``md5`` → first 16 hex digits → ``bit(64)`` → ``bigint`` is the standard
#: idiom; ``hashtextextended`` would be shorter but it is an internal function
#: whose stability across major versions is not promised.
#:
#: ⚠️ The label is bound as a parameter, never interpolated. It is built from a
#: UUID and an enum member so it cannot contain anything hostile, but a lock key
#: assembled by string concatenation is one refactor away from being able to.
_ADVISORY_KEY_SQL: Final[str] = (
    "SELECT pg_advisory_xact_lock((('x' || substr(md5(:label), 1, 16))::bit(64))::bigint)"
)


async def serialise_tenant(
    session: AsyncSession, *, tenant_id: uuid.UUID, resource: ResourceCode
) -> None:
    """Hold a per-tenant, per-resource mutex until the transaction ends.

    🔒 The missing mutex for live-counted resources. See
    :class:`DatabaseEntitlementGuard` for why counting from source needs one and a
    row lock cannot supply it.

    ⚠️ **An advisory lock rather than ``SELECT ... FOR UPDATE`` on the subscription
    row**, and not by preference. PostgreSQL requires UPDATE privilege to take a
    row lock, and migration 0007 revokes UPDATE on ``subscriptions`` from
    ``app_user`` so a tenant cannot upgrade its own plan. The obvious design is
    refused by the grant model; this needs no table privileges at all.

    ⚠️ ``pg_advisory_xact_lock`` — the transaction-scoped variant. The session
    variant would leak the lock back into the connection pool, where the next
    request to borrow that connection would inherit a lock it never took and
    nothing would release it.

    ⚠️ Keyed per ``(resource, tenant)`` rather than per tenant. A tenant uploading
    a file and a tenant activating a client are unrelated decisions, and one
    key for both would serialise them against each other for no reason.
    """
    await session.execute(
        text(_ADVISORY_KEY_SQL),
        {"label": f"entitlement:{resource.value}:{tenant_id}"},
    )


# ─── The guard (kernel.entitlements.EntitlementGuard) ────────────────────


class DatabaseEntitlementGuard:
    """Satisfies ``kernel.entitlements.EntitlementGuard`` against the real tables.

    🔒 The seam that lets a *module* enforce a plan limit. Arch R5 forbids
    ``app.modules.*`` importing ``app.platform.*``, so the kernel declares the
    protocol, this satisfies it, and ``main`` wires the two — the same shape as
    ``ClientDirectory`` and ``StorageBackend``.

    🔒 **Live-counted resources are serialised per tenant, and they have to be.**
    ``active_clients`` is counted from ``clients`` rather than from a counter
    (DB §14.4), so two concurrent activations of *different* clients touch
    different rows and conflict on nothing: both count 29 against a limit of 30,
    both pass, both commit, and the tenant ends on 31. A row lock on the client
    being changed cannot prevent that — the two transactions never contend for a
    common row. :func:`serialise_tenant` supplies the missing mutex.

    ⚠️ Counter-backed resources are **not** serialised. Their counter upsert is
    already atomic (``ON CONFLICT DO UPDATE`` in :func:`record_usage`), so a lock
    would add contention without preventing anything. It would also serialise
    every AI draft in a clinic behind one lock, which is the busy path.
    """

    async def require(
        self,
        session: AsyncSession,
        /,
        *,
        tenant_id: uuid.UUID,
        resource: ResourceCode,
        amount: Decimal | int = 1,
        live_used: Decimal | int | None = None,
    ) -> None:
        """Permit this consumption, or raise — FR-M0-045.

        ⚠️ The lock is taken **before** the count is read, not after. A lock
        acquired after reading would serialise the writes while still letting both
        transactions decide on the same stale count — the lock would be present,
        the overshoot unchanged, and the test that caught it hard to write.

        Raises:
            EntitlementError: The plan does not permit it, the subscription is not
                in a state that permits new metered actions, or the allowance
                could not be determined (FR-M0-046).
        """
        if resource.is_counted_live:
            await serialise_tenant(session, tenant_id=tenant_id, resource=resource)

        subscription = await load_subscription(session, tenant_id=tenant_id)
        allowance = await load_allowance(
            session,
            tenant_id=tenant_id,
            resource=resource,
            subscription=subscription,
            live_used=live_used,
        )
        # 🔒 `status=None` when there is no subscription row at all. `check` treats
        # that as indeterminate and refuses, which is the fail-safe direction —
        # passing "active" here would invent a commercial state nobody granted.
        check(allowance, amount=amount, status=subscription.status if subscription else None)
