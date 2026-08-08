"""Entitlements — what a tenant's plan permits, and whether this action fits.

🔒 DB §14, FR-M0-044/045/046, FR-M10-001..005, DDR-14. Enforcement is structural
in S1; collection is deferred to M10.3. Nothing in this module charges anyone.

Four properties, each answering a specific failure:

1. 🔒 **Enforced at the point of action** (FR-M0-045), not at login and not by a
   nightly sweep. :func:`check` is called immediately before the metered thing
   happens, and the error it raises names the limit and the upgrade path so the
   UI needs no second call.
2. 🔒 **Fail safe when state is unknown** (FR-M0-046, EC-M10-03). An
   :class:`Allowance` whose limit could not be determined denies *new* metered
   actions and says so. It has no opinion about reads — see the warning below.
3. 🔒 **O(1) on the hot path** (DDR-14). Every decision here is arithmetic over
   values the caller already holds. No function in this module counts anything,
   because a check that scans is a check someone eventually moves off the hot
   path and stops running.
4. 🔒 **Warned once, at 80%** (FR-M10-005). :func:`crosses_warning_threshold`
   answers whether *this* increment is the one that crosses, so the warning is a
   consequence of consumption rather than a scheduled job re-deciding it daily.

⚠️ 🔒 **This module cannot enforce "reads stay allowed".** FR-M0-046 requires
that when entitlement state is indeterminate, existing data remains readable and
only new metered actions are blocked. The second half is enforced here; the first
half is enforced by *not calling* :func:`check` on a read path. A caller that
guards a GET with an entitlement check has broken FR-M0-046, and no function in
this file can detect that. Reviewers should treat an entitlement check on a read
endpoint as a defect.

⚠️ **Active clients are counted live, not from a counter** (DB §14.4, M1.5). The
caller supplies ``used`` from ``clients WHERE stage='active'`` for that one
resource. It is a cheap indexed count and the product's most visible limit, where
a drifting counter would produce a bill the practitioner can disprove by eye.

This module holds rules only. Persistence lives in ``platform.entitlements`` —
the split is what lets every rule below be tested without a database.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal

from app.kernel.errors import EntitlementError

#: 🔒 FR-M10-005. Named rather than inlined: the threshold is a product decision
#: that will be tuned against real support load, and it appears in both the
#: crossing test and the warning copy.
WARNING_THRESHOLD: Decimal = Decimal("0.8")


class ResourceCode(str, enum.Enum):
    """The metered resources — FR-M0-044.

    🔒 This enum, not the database, is where a typo in a resource code is caught.
    ``usage_counters.resource_code`` and ``usage_events.resource_code`` are
    ``text`` because FR-M10-001 requires that adding a metered resource not
    require a migration; the cost of that is that the valid set lives here and
    every write must go through this type.
    """

    ACTIVE_CLIENTS = "active_clients"
    AI_GENERATIONS = "ai_generations"
    WHATSAPP_MESSAGES = "whatsapp_messages"
    STORAGE_MB = "storage_mb"
    PRACTITIONER_SEATS = "practitioner_seats"

    @property
    def limit_key(self) -> str:
        """The key this resource is stored under in ``plan_definitions.limits``.

        ⚠️ Not always the resource code. DB §14.1 fixes the limit keys as
        ``ai_generations_per_month`` and ``whatsapp_messages_per_month`` — the
        period is part of the plan's vocabulary but not of the resource's, since
        the counter already carries its own period. Mapping here rather than at
        each call site means the two namings cannot drift.
        """
        return _LIMIT_KEYS[self]

    @property
    def human_name(self) -> str:
        """Readable noun for the limit message (FR-M0-045)."""
        return _HUMAN_NAMES[self]

    @property
    def is_counted_live(self) -> bool:
        """Whether this resource is counted from source rather than a counter.

        🔒 DB §14.4 — active clients only. See the module warning.
        """
        return self is ResourceCode.ACTIVE_CLIENTS


_LIMIT_KEYS: dict[ResourceCode, str] = {
    ResourceCode.ACTIVE_CLIENTS: "active_clients",
    ResourceCode.AI_GENERATIONS: "ai_generations_per_month",
    ResourceCode.WHATSAPP_MESSAGES: "whatsapp_messages_per_month",
    ResourceCode.STORAGE_MB: "storage_mb",
    ResourceCode.PRACTITIONER_SEATS: "practitioner_seats",
}

_HUMAN_NAMES: dict[ResourceCode, str] = {
    ResourceCode.ACTIVE_CLIENTS: "active clients",
    ResourceCode.AI_GENERATIONS: "AI drafts this month",
    ResourceCode.WHATSAPP_MESSAGES: "WhatsApp messages this month",
    ResourceCode.STORAGE_MB: "MB of storage",
    ResourceCode.PRACTITIONER_SEATS: "practitioner seats",
}

#: 🔒 The upgrade ladder for FR-M0-045's "and the upgrade path". Ordered, and
#: deliberately not derived from ``sort_order`` in the database: a plan made
#: non-public would silently drop out of the ladder and the error message would
#: offer nothing.
_UPGRADE_LADDER: tuple[str, ...] = ("free", "starter", "growth", "clinic")

#: 🔒 An alternative to paying, offered before the upgrade because it is free.
#: Only for resources where an alternative genuinely exists — there is no way to
#: "free up" an AI draft already generated this month.
_REMEDIES: dict[ResourceCode, str] = {
    ResourceCode.ACTIVE_CLIENTS: "set a client to inactive if they have finished their programme",
    ResourceCode.STORAGE_MB: "delete files you no longer need",
}


def next_plan_after(plan_code: str) -> str | None:
    """The next tier up, or ``None`` at the top of the ladder.

    Returns ``None`` for an unrecognised plan rather than guessing: a custom or
    retired plan code has no defined successor, and inventing one would put a
    non-existent tier in a customer-facing error message.
    """
    try:
        position = _UPGRADE_LADDER.index(plan_code)
    except ValueError:
        return None
    if position + 1 >= len(_UPGRADE_LADDER):
        return None
    return _UPGRADE_LADDER[position + 1]


# ─── Subscription status (DB §14.2) ──────────────────────────────────────


#: 🔒 Which subscription states permit a *new* metered action.
#:
#: ``trialing`` and ``past_due`` both do, and ``past_due`` is the interesting one:
#: a failed payment is a billing problem, and cutting off a practitioner's ability
#: to serve their clients the day a card expires would damage the practice we are
#: selling to before anyone has read the dunning email. Suspension is the lever
#: for non-payment, and it is deliberate rather than automatic (FR-M10-008).
#:
#: ⚠️ ``cancelled`` and ``suspended`` block new metered actions and **must not**
#: block reads — FR-M0-046's "existing data remains readable" applies here too.
_ACTIVE_STATUSES: frozenset[str] = frozenset({"trialing", "active", "past_due"})


def status_permits_metered_action(status: str | None) -> bool:
    """Whether a subscription in this state may consume more quota.

    ``None`` — no subscription row at all — returns ``False``. 🔒 A tenant with no
    subscription is indeterminate, not unlimited, and the fail-safe direction for
    an unknown commercial state is to block new consumption (FR-M0-046).
    """
    return status is not None and status in _ACTIVE_STATUSES


# ─── The allowance ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Allowance:
    """What one resource permits right now, and how much is used.

    🔒 A snapshot, matching ``SessionSnapshot`` and ``LedgerEntry``: the fold and
    the decision are then testable with plain objects, and cannot accidentally
    trigger a lazy load while deciding whether an action is permitted.

    ⚠️ Three distinct states, and collapsing any two of them is a bug:

    * ``limit`` is a number — enforce it.
    * ``limit`` is ``None`` **and** ``is_determinate`` — genuinely unlimited.
    * ``is_determinate`` is ``False`` — unknown. Deny new metered actions
      (FR-M0-046). A missing plan, an unreadable ``limits`` object or a missing
      key all land here, because each one means we cannot say what is allowed.
    """

    resource: ResourceCode
    used: Decimal
    limit: Decimal | None
    plan_code: str
    is_determinate: bool = True
    #: 🔒 Set when the 80% warning has already been sent for this period, so
    #: :func:`crosses_warning_threshold` does not ask for it twice (FR-M10-005).
    already_warned: bool = False

    @property
    def is_unlimited(self) -> bool:
        """Whether this resource has no ceiling.

        ⚠️ Only true when the state is *known* to be unlimited. An indeterminate
        allowance also has ``limit is None``, and treating that as unlimited is
        precisely the fail-open FR-M0-046 forbids.
        """
        return self.is_determinate and self.limit is None

    @property
    def remaining(self) -> Decimal | None:
        """Headroom left, or ``None`` when unlimited or indeterminate.

        Never negative: a plan downgrade can leave a tenant over their new limit
        (EC-M10-01), and reporting "-7 remaining" in the UI reads as a bug rather
        than as the over-limit state it is. Use :func:`is_over_limit` for that.
        """
        if self.limit is None:
            return None
        return max(Decimal(0), self.limit - self.used)

    @property
    def usage_ratio(self) -> Decimal | None:
        """Consumption as a fraction of the limit, or ``None`` if there is none.

        A zero limit returns ``None`` rather than dividing: a resource the plan
        does not include at all is not "0% used", and the answer to a percentage
        question about it is that there is no percentage.
        """
        if self.limit is None or self.limit == 0:
            return None
        return self.used / self.limit


def is_over_limit(allowance: Allowance) -> bool:
    """Whether consumption already exceeds the limit.

    🔒 EC-M10-01 — a downgrade can put a tenant over a limit they were previously
    within, without them doing anything. Distinct from :func:`would_exceed`, which
    asks about a *prospective* action: the over-limit tenant keeps their data and
    loses only the ability to add more.
    """
    if allowance.limit is None:
        return False
    return allowance.used > allowance.limit


# ─── Enforcement (FR-M0-045, FR-M0-046) ──────────────────────────────────


def would_exceed(allowance: Allowance, *, amount: Decimal | int = 1) -> bool:
    """Whether consuming ``amount`` more would cross the limit.

    ⚠️ ``used + amount > limit``, not ``>=``. A tenant on the Starter plan with 29
    active clients may create their 30th: the limit is what they are entitled to,
    not what they must stay below. Off-by-one here is the difference between
    selling "30 clients" and delivering 29.

    Returns ``False`` for an unlimited allowance and ``True`` for an indeterminate
    one — the fail-safe direction (FR-M0-046).
    """
    if not allowance.is_determinate:
        return True
    if allowance.limit is None:
        return False
    return allowance.used + Decimal(amount) > allowance.limit


def check(
    allowance: Allowance,
    *,
    amount: Decimal | int = 1,
    status: str | None = "active",
) -> None:
    """Raise :class:`EntitlementError` unless this metered action fits.

    🔒 FR-M0-045 — called immediately before the metered thing happens, so the
    limit is enforced at the point of action rather than at login. The raised
    error carries the limit, the usage, the plan and the upgrade path, because
    the UI must be able to explain the refusal without a second request.

    🔒 FR-M0-046 — an indeterminate allowance raises rather than passing. The
    message says so plainly instead of quoting a limit we could not read, since
    "you have reached 0 of 0" would send the practitioner to a pricing page that
    cannot help them.

    ⚠️ **Never call this on a read path.** See the module docstring: the "existing
    data remains readable" half of FR-M0-046 is enforced by absence, and this
    function cannot tell a read from a write.

    Args:
        allowance: The resource's current position.
        amount: How much this action consumes. Fractional for storage.
        status: The subscription's state. ``None`` means no subscription, which
            is treated as indeterminate rather than as free-tier access.
    """
    if not status_permits_metered_action(status):
        raise EntitlementError(
            "Your subscription is not active, so new changes are paused.",
            action=(
                "Your existing data is safe and still readable. "
                "Contact support to reactivate your plan."
            ),
            details={
                "resource": allowance.resource.value,
                "subscription_status": status or "none",
                "plan_code": allowance.plan_code,
            },
        )

    if not allowance.is_determinate:
        # 🔒 EC-M10-03 — metering briefly unavailable. Blocked, logged by the
        # caller, and explicitly *not* an upgrade prompt: the tenant has done
        # nothing wrong and there is nothing for them to buy.
        raise EntitlementError(
            "We could not confirm your plan's limits just now, so new changes are paused.",
            action=(
                "Your existing data is safe and still readable. "
                "Please try again in a few minutes."
            ),
            details={
                "resource": allowance.resource.value,
                "reason": "indeterminate",
                "plan_code": allowance.plan_code,
            },
        )

    if not would_exceed(allowance, amount=amount):
        return

    # `would_exceed` returning True guarantees a determinate allowance with a
    # limit set, so this is narrowing rather than a real branch. Written as an
    # `if` rather than an `assert` because `python -O` strips asserts, and a
    # stripped narrowing check would turn a bad state into a TypeError inside
    # `int()` on the enforcement path.
    limit = allowance.limit
    if limit is None:  # pragma: no cover — unreachable, see above
        return

    raise EntitlementError.for_limit(
        resource=allowance.resource.value,
        limit=int(limit),
        used=int(allowance.used),
        plan_code=allowance.plan_code,
        human_resource=allowance.resource.human_name,
        upgrade_to=next_plan_after(allowance.plan_code),
        remedy=_REMEDIES.get(allowance.resource),
    )


# ─── The 80% warning (FR-M10-005) ────────────────────────────────────────


def crosses_warning_threshold(allowance: Allowance, *, amount: Decimal | int = 1) -> bool:
    """Whether this increment is the one that takes the tenant past 80%.

    🔒 FR-M10-005. Answers about the *transition*, not the state: it is true only
    when consumption was below the threshold before and is at or above it after.
    A state-based test would be true on every subsequent action too, and the
    practitioner would be warned about the same limit on every save.

    ``already_warned`` short-circuits it for the period, so a counter reset or a
    compensating negative event that dips usage back under 80% cannot make the
    same warning fire twice.

    Returns ``False`` for unlimited and indeterminate allowances alike — there is
    no meaningful percentage of an unknown limit, and warning about one would be
    noise attached to an incident the tenant cannot act on.
    """
    if allowance.already_warned or not allowance.is_determinate:
        return False
    if allowance.limit is None or allowance.limit == 0:
        return False

    before = allowance.used / allowance.limit
    after = (allowance.used + Decimal(amount)) / allowance.limit
    return before < WARNING_THRESHOLD <= after
