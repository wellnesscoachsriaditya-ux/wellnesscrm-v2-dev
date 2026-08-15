"""The client portal — API §12.

🔒 **The client realm's first data surface.** Everything here is reached with a
client-realm token obtained by redeeming a magic link (`/public/portal/access`),
and the acting client is the token's subject. 🔒 **No `client_id` appears in any
path** (API §12.1): a `client_id` parameter would be an authorization decision
waiting to be forgotten, and there is nothing here to forget because there is
nothing to pass.

🔒 **Isolation is Pattern C row-level security** (DB §17.1, migration
`0025_portal_client_realm_rls`), not the `WHERE` clauses below. Every table this
module reads carries a RESTRICTIVE policy that resolves to
`client_id = portal_client_id()` for a client-realm session, so a bug in this
file returns *no rows* rather than another client's. The explicit predicates are
kept for the same reason `modules.nutrition` keeps its tenant predicates: RLS is
the guarantee, the predicate is the part a reviewer can see.

⚠️ **`/portal/today` is a deliberate deviation from resource orientation**
(ADR-A09, API §12.2). M7.3's 60-second rule and NFR-002's 2.5 s on 4G cannot be
met by four sequential round trips on a high-latency mobile connection. It is a
**read-only projection** — every write still goes to its own resource endpoint,
so no write logic is duplicated here.

🔒 **Nutrition figures are omitted entirely unless the practitioner enabled them
for this client** (`clients.client_nutrition_visibility`, default off). Omitted,
not nulled: a client fixating on numbers is a clinical concern, and the decision
is the practitioner's. :class:`TodayItem` drops the key rather than serialising
`null`, so the guarantee is observable in the payload.

🔒 **Degradation never exposes the practitioner's account state** (EC-M7-08,
API §12.6). A suspended tenant produces a neutral service notice and a
read-only portal — never a billing reason, never a 402, never a different status
code that would let a client infer one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Request
from pydantic import (
    BaseModel,
    Field,
    PlainSerializer,
    SerializerFunctionWrapHandler,
    model_serializer,
)
from pydantic import (
    ValidationError as PayloadError,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.authz import DataScope, register_action
from app.kernel.clients import ClientStage
from app.kernel.clinical import MeasurementSource, ResponseStatus
from app.kernel.context import UserRole, get_context
from app.kernel.errors import AppError, AuthenticationError, ErrorType
from app.kernel.models import Tenant, TenantStatus
from app.kernel.nutrition import MealSlotType, format_measure
from app.modules.clients import Client, get_client
from app.modules.clinical import (
    AssessmentResponse,
    Measurement,
    list_responses,
    measurement_history,
    preferred_per_date,
    record_measurement,
)
from app.modules.nutrition import (
    IssuedPlan,
    PlanSlot,
    ResolvedPlanVersion,
    current_issued_plan_for_client,
    resolve_plan_version,
)
from app.modules.progress import (
    AdherenceValue,
    assert_within_window,
    log_adherence,
)
from app.platform.http.authz import requires
from app.platform.http.pipeline import get_session, realm_router, record_audit
from app.platform.logging import get_logger

logger = get_logger(__name__)

#: 🔒 API §4 — a ``Decimal`` leaves as a string, never as a JSON number. A
#: silently rounded clinical figure is a defect, not a rounding artefact.
Exact = Annotated[Decimal, PlainSerializer(str, return_type=str)]

router = realm_router("/api/v1/portal", tags=["portal"])


# ─── The action ──────────────────────────────────────────────────────────

#: 🔒 Declared here rather than in a module because no module owns this read.
#: The aggregate spans `nutrition`, `clinical` and `clients`, and R3 forbids any
#: of them importing another — the composition is an HTTP concern and lives with
#: the router that performs it, the same arrangement `session.read` uses.
#:
#: ``TENANT_PII``: the response carries a plan, a weight trend and a name. 🔒 No
#: `operator_access` — `register_action` would refuse it at import time, which is
#: the boundary working rather than a rule someone remembered.
#:
#: ``roles={CLIENT}`` alone. A practitioner token is already refused by the realm
#: check before this is consulted (`/portal` maps to `AuthRealm.CLIENT`); naming
#: only the client role means the two agree rather than the second one being
#: incidentally wider.
PORTAL_TODAY_READ = register_action(
    "portal.read_today",
    roles={UserRole.CLIENT},
    data_scope=DataScope.TENANT_PII,
    is_read=True,
)

#: 🔒 The offline queue drain (API §12.4, FR-M7-012).
#:
#: ⚠️ **One action for a batch that writes two different resources.** The
#: alternative — declaring `adherence.log` and `measurement.record` and checking
#: each per operation — would put an authorization decision inside a loop, which
#: is where they get forgotten. The authority being exercised is "this client may
#: sync their own queue"; *which* resources that covers is the operation
#: dispatch's business, and every row it writes is confined by Pattern C
#: regardless of what the loop does.
#:
#: Audited, unlike `portal.read_today`: this writes. The counts go in the audit
#: row so a support question ("did their Tuesday logs arrive?") is answerable
#: without reading the logs themselves.
PORTAL_SYNC = register_action(
    "portal.sync",
    roles={UserRole.CLIENT},
    data_scope=DataScope.TENANT_PII,
    audit_metadata_keys={"applied", "duplicate", "rejected"},
)


# ─── Tunable rules ───────────────────────────────────────────────────────

#: 🟡 PROPOSED — how stale the latest weight must be before the portal asks for
#: a new one. Weekly matches the check-in cadence S5 defaults to (FR-M8-023), so
#: the prompt and the message arrive together rather than nagging twice.
WEIGHT_PROMPT_AFTER_DAYS = 7

#: 🟡 PROPOSED — the window the landing-page trend is computed over. Long enough
#: that a day's water weight does not dominate it, short enough to feel current.
TEASER_WINDOW_DAYS = 14

#: 🔒 EC-M7-08 — what a client sees when their practitioner's account is
#: suspended. States that something is limited and nothing about why.
SUSPENDED_NOTICE = (
    "Some features are temporarily unavailable. Your plan is still here, "
    "and your practitioner will be in touch."
)

#: EC-M7-06 — a paused engagement. Read-only, and phrased as a pause rather than
#: a problem.
PAUSED_NOTICE = "Your programme is paused, so logging is turned off for now."

#: 🟡 **PROPOSED batch cap: 100 operations** (API §12.4). "A larger queue syncs
#: in pages", so exceeding it is a request the client must reshape — a 422 on the
#: envelope, not a per-operation rejection. That is the one validation failure
#: here that legitimately fails the whole batch, because there is no batch to
#: apply until the client splits it.
MAX_BATCH_OPERATIONS = 100

#: The operation types ``/portal/sync`` understands.
#:
#: 🔒 ``adherence.log`` is named in API §12.4's example. 🟡 ``measurement.log`` is
#: **PROPOSED**: §12.4 shows one type and does not enumerate the rest, but
#: migration ``0024`` exists solely to give measurements the idempotency key
#: "``/portal/sync`` replays against", so the queue is specified to carry them
#: and only the name was left open. Named by symmetry with the one type that is
#: written down.
OP_ADHERENCE = "adherence.log"
OP_MEASUREMENT = "measurement.log"

#: 🔒 The per-operation outcomes, API §12.4. ``duplicate`` is a **success**
#: state — replaying a queue is expected, not an error.
STATUS_APPLIED = "applied"
STATUS_DUPLICATE = "duplicate"
STATUS_REJECTED = "rejected"

#: 🔒 The unique indexes that mean "this operation has already been applied".
#:
#: ⚠️ Matched by name rather than by catching every ``IntegrityError``. A foreign
#: key violation is also an ``IntegrityError`` and is *our* defect, not a replay;
#: reporting it as ``duplicate`` would tell the client their data is safely
#: stored when it was discarded.
_REPLAY_CONSTRAINTS: frozenset[str] = frozenset(
    {
        "uq_adherence_logs__idempotency",
        "uq_measurements__client_idempotency",
    }
)

#: What a client is told when their portal is read-only (EC-M7-06, EC-M7-08).
#: 🔒 The same code for a paused client and a suspended tenant — a client must
#: not be able to tell their own pause from their practitioner's account state.
ERROR_READ_ONLY = "portal_read_only"

#: An operation naming a type this server does not implement.
ERROR_UNSUPPORTED_TYPE = "unsupported_operation_type"

#: 🔒 An operation that failed for a reason that is not the client's fault. The
#: client keeps it queued and retries; nothing is silently dropped (EC-M7-05).
ERROR_NOT_APPLIED = "not_applied"


# ─── Wire shapes ─────────────────────────────────────────────────────────


class MacroTotalsResponse(BaseModel):
    energy_kcal: Exact
    protein_g: Exact
    carbs_g: Exact
    fat_g: Exact
    fibre_g: Exact


class TodayItem(BaseModel):
    """One thing to eat, rendered for display.

    🔒 ``quantity_display`` is **server-rendered** (API §12.2, NFR-072). Household
    measure formatting lives in one place, so two clients cannot show the same
    plan two different ways.

    🔒 ``nutrition`` is **absent**, not null, when the practitioner has not
    enabled figures for this client. See :meth:`_omit_hidden_nutrition`.
    """

    display_name: str
    quantity_display: str
    note: str | None = None
    #: ⏳ Always empty. `plan_item_alternatives` has no reader yet (the plan
    #: authoring surface declares the same empty list); the shape is declared now
    #: so it does not change under a cached PWA later.
    alternatives: list[dict[str, str]] = Field(default_factory=list)
    nutrition: MacroTotalsResponse | None = None

    @model_serializer(mode="wrap")
    def _omit_hidden_nutrition(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """🔒 Drop the key entirely when figures are hidden.

        ⚠️ ``exclude_none`` on the route would do this — and would also delete
        ``plan: null`` and ``next_appointment: null``, both of which API §12.6
        requires to be present. The rule is about *this* field, so it is stated
        on this field.
        """
        data: dict[str, Any] = handler(self)
        if self.nutrition is None:
            data.pop("nutrition", None)
        return data


class TodayAdherence(BaseModel):
    """Whether this slot has been logged today.

    ⏳ **Always unlogged.** DB §12.1 specifies ``plan_slot_id`` and ``slot_type``
    on ``adherence_logs``; migration ``2eb56b8913d5`` built neither, so nothing
    in the schema can say *which* slot a log belongs to. Reporting a day-level
    score against every slot would be a fabricated per-meal answer, which is
    worse than an honest "not yet". The slice that adds the logging endpoint adds
    the columns, and this reads them without the shape changing.
    """

    logged: bool = False
    value: str | None = None


class TodaySlot(BaseModel):
    slot_id: uuid.UUID
    slot_type: str
    label: str
    target_time: str | None = None
    items: list[TodayItem]
    adherence: TodayAdherence


class TodayPlan(BaseModel):
    version_id: uuid.UUID
    #: 🔒 DDR-12 / FR-M7-011 — the service worker's cache key. ``None`` means the
    #: issued version has no snapshot and the PWA must not cache it.
    content_hash: str | None
    day_number: int
    slots: list[TodaySlot]


class TodayPrompts(BaseModel):
    weight_due: bool
    assessment_pending_id: uuid.UUID | None


class ProgressTeaser(BaseModel):
    """The one number worth putting on a landing page.

    ``weight_change_kg`` is negative for a loss. ⚠️ No band, no label and no
    judgement — the same restraint ``kernel.clinical`` applies to BMI, and for
    the same reason: OD-08 is unresolved.
    """

    weight_change_kg: Exact
    period_days: int


class NextAppointment(BaseModel):
    starts_at: datetime
    mode: str
    meeting_link: str | None = None


class PractitionerCard(BaseModel):
    """Who the client is working with.

    ⚠️ The **practice** name, not the practitioner's personal one. A client is
    engaged with the practice, and `clients.owner_user_id` can be reassigned
    (FR-M1-009) without the client's relationship changing.

    ⏳ ``branding`` is null: no branding table exists yet (API §12.2 marks it
    optional).
    """

    name: str
    branding: dict[str, str] | None = None


class Capabilities(BaseModel):
    """🔒 API §12.6 — what the client may do, stated rather than inferred.

    The UI must never have to work this out from a stage, a status or an error it
    happened to receive. Every portal response carries this object.
    """

    can_log_adherence: bool
    can_log_measurements: bool
    can_upload: bool
    can_view_plan: bool


class TodayResponse(BaseModel):
    """API §12.2 — the single most important endpoint in the portal."""

    date: date
    #: 🔒 EC-M7-02 — ``null``, with a 200, when there is no issued plan. Present
    #: in the payload rather than omitted, so the PWA renders an empty state
    #: instead of treating the field as missing data.
    plan: TodayPlan | None
    prompts: TodayPrompts
    progress_teaser: ProgressTeaser | None
    #: ⏳ Always ``null`` — appointments are S9. Declared now for the same reason
    #: as ``alternatives``.
    next_appointment: NextAppointment | None
    practitioner: PractitionerCard
    capabilities: Capabilities
    #: 🔒 EC-M7-06 / EC-M7-08 — a neutral sentence when something is limited, and
    #: ``null`` otherwise. Never names a billing state or an account status.
    notice: str | None


# ─── Sync wire shapes — API §12.4 ────────────────────────────────────────


class SyncOperation(BaseModel):
    """One queued action from the client's device.

    ⚠️ **``payload`` is an untyped object here, deliberately.** A discriminated
    union would make FastAPI reject the *whole request* with a 422 when one
    operation's payload is malformed — and API §12.4 guarantee 1 says "a single
    bad operation never fails the batch". The payload is parsed inside the
    per-operation savepoint instead, where a failure becomes that operation's
    ``rejected`` result and the rest of the week's queue still applies.
    """

    op_id: uuid.UUID
    type: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    #: 🔒 When the client acted, from their device clock. Kept alongside server
    #: time rather than instead of it (API §12.3).
    client_timestamp: datetime


class SyncRequest(BaseModel):
    """API §12.4's request body."""

    #: 🟡 Capped at 100 (see :data:`MAX_BATCH_OPERATIONS`). An empty list is
    #: **valid**: a PWA that comes online with nothing queued still needs to ask
    #: whether its cached plan is current, and refusing that would push clients
    #: into a second round trip on the connection §12.2 exists to protect.
    operations: list[SyncOperation] = Field(default_factory=list, max_length=MAX_BATCH_OPERATIONS)
    #: 🔒 DDR-12 / EC-M7-03 — what the device believes it has cached.
    known_plan_hash: str | None = Field(default=None, max_length=128)


class SyncResult(BaseModel):
    """What became of one operation.

    🔒 ``error`` carries a stable machine-readable code (API §16.1), never
    prose: the PWA branches on it to decide whether to drop the operation from
    its queue or retry it, and a localisable sentence cannot be branched on.
    """

    op_id: uuid.UUID
    status: Literal["applied", "duplicate", "rejected"]
    error: str | None = None


class SyncResponse(BaseModel):
    """API §12.4's 200.

    🔒 ``plan_changed`` tells the PWA to refresh rather than silently swapping
    content while the client is reading it (EC-M7-03).
    """

    results: list[SyncResult]
    plan_changed: bool
    current_plan_hash: str | None


class AdherencePayload(BaseModel):
    """API §12.3's fields, as they arrive inside a sync operation.

    🔒 ``model_config`` forbids extra keys. A payload carrying ``client_id`` or
    ``tenant_id`` is refused outright rather than ignored — silently dropping an
    authority field teaches a client that sending it is harmless, and the next
    reader of this code has to prove it still is.
    """

    model_config = {"extra": "forbid"}

    logged_for_date: date
    slot_type: MealSlotType
    adherence: AdherenceValue
    slot_id: uuid.UUID | None = None


class MeasurementPayload(BaseModel):
    """What the portal's weight tile queues — API §12.1 ``/portal/measurements``.

    ⚠️ ``height_cm`` is absent. Height is captured once at assessment
    (FR-M3-012) and is not something the one-tap weight flow collects; accepting
    it here would let the offline queue rewrite a figure every BMI depends on.
    """

    model_config = {"extra": "forbid"}

    measured_on: date
    weight_kg: Decimal | None = None
    waist_cm: Decimal | None = None
    hip_cm: Decimal | None = None
    body_fat_pct: Decimal | None = None
    #: 🔒 EC-M3-02's second step. A value outside the plausible range is refused
    #: once and accepted when the client confirms it — a real 180 kg client
    #: exists, and a queue that cannot carry the confirmation would refuse them
    #: forever.
    confirmed_implausible: bool = False


# ─── The endpoint ────────────────────────────────────────────────────────


@router.get(
    "/today",
    summary="Everything the portal's landing view needs",
    operation_id="portalToday",
)
@requires(PORTAL_TODAY_READ)
async def portal_today(request: Request) -> TodayResponse:
    """The aggregate — ADR-A09, API §12.2.

    🔒 The client is read from the verified token, never from a parameter, and
    every read below runs inside a transaction whose ``app.actor_id`` is that
    client. Pattern C makes the two agree at the database rather than here.
    """
    session = get_session(request)
    actor = get_context().actor
    tenant_id = actor.require_tenant()
    client_id = actor.require_subject()

    client = await get_client(session, tenant_id=tenant_id, client_id=client_id)

    # 🔒 Defence in depth, and cheap. RLS has already made any other client's row
    # invisible; this is what fails loudly if a future change ever opened a path
    # that loaded a client by something other than the token's subject.
    if client.id != client_id:  # pragma: no cover - unreachable while RLS holds
        raise AuthenticationError(
            message="Your session is no longer valid.",
            action="Request a new link and try again.",
        )

    # 🔒 A client whose record has been archived has no portal. Phrased as a
    # session failure with a self-service next step (EC-M7-01's shape) rather
    # than as a 403 — the client is not being refused a permission, their
    # engagement has ended, and the practitioner is the only route back.
    if client.archived_at is not None:
        raise AuthenticationError(
            message="This link is no longer active.",
            action="Contact your practitioner to regain access.",
        )

    tenant = await _load_tenant(session, tenant_id=tenant_id)
    today = _today_for(tenant)

    issued = await current_issued_plan_for_client(session, tenant_id=tenant_id, client_id=client_id)
    plan = None
    if issued is not None:
        resolved = await resolve_plan_version(
            session, tenant_id=tenant_id, version_id=issued.version.id
        )
        plan = _today_plan(
            issued,
            resolved,
            today=today,
            show_nutrition=client.client_nutrition_visibility,
        )

    measurements = preferred_per_date(
        await measurement_history(session, tenant_id=tenant_id, client_id=client_id)
    )
    responses = await list_responses(session, tenant_id=tenant_id, client_id=client_id)

    capabilities, notice = _capabilities_for(client, tenant)

    return TodayResponse(
        date=today,
        plan=plan,
        prompts=TodayPrompts(
            weight_due=_weight_is_due(measurements, today=today),
            assessment_pending_id=_pending_assessment_id(responses),
        ),
        progress_teaser=_teaser(measurements, today=today),
        next_appointment=None,
        practitioner=PractitionerCard(name=tenant.name),
        capabilities=capabilities,
        notice=notice,
    )


@router.post(
    "/sync",
    summary="Drain the client's offline queue",
    operation_id="portalSync",
)
@requires(PORTAL_SYNC)
async def portal_sync(payload: SyncRequest, request: Request) -> SyncResponse:
    """Apply a batch of queued operations — API §12.4, FR-M7-012, EC-M7-05.

    🔒 **Four guarantees, and each is a line of code below rather than a hope:**

    1. **A single bad operation never fails the batch.** Every operation runs
       inside its own ``SAVEPOINT``. Without one, a constraint violation aborts
       the request's transaction and PostgreSQL refuses every subsequent
       statement — so one replayed log would discard a week of queued data,
       which is the exact failure this endpoint exists to prevent.
    2. **``duplicate`` is a success state.** Decided by the unique index, not by
       a prior read: two replays of one queue arriving together would both pass
       a ``SELECT`` and both insert.
    3. **Client logs are never discarded.** An operation that cannot be applied
       comes back ``rejected`` with a code the PWA can branch on, so it stays
       queued rather than vanishing.
    4. **``plan_changed`` is answered from the snapshot hash** (DDR-12), so the
       PWA refreshes deliberately instead of swapping content under the reader.

    🔒 **The client is the token's subject.** Nothing in the request body can
    name a client, and ``AdherencePayload`` / ``MeasurementPayload`` refuse
    unknown keys, so a payload carrying ``client_id`` is rejected rather than
    ignored. Pattern C then confines every write at the database.
    """
    session = get_session(request)
    actor = get_context().actor
    tenant_id = actor.require_tenant()
    client_id = actor.require_subject()

    client = await get_client(session, tenant_id=tenant_id, client_id=client_id)
    if client.archived_at is not None:
        raise AuthenticationError(
            message="This link is no longer active.",
            action="Contact your practitioner to regain access.",
        )

    tenant = await _load_tenant(session, tenant_id=tenant_id)
    issued = await current_issued_plan_for_client(session, tenant_id=tenant_id, client_id=client_id)
    current_hash = issued.content_hash if issued is not None else None

    # 🔒 EC-M7-06 / EC-M7-08 — a read-only portal accepts no writes, and says so
    # per operation with a 200. A 403 here would let a client infer their
    # practitioner's account state from the status code alone.
    capabilities, _ = _capabilities_for(client, tenant)
    writable = capabilities.can_log_adherence

    results: list[SyncResult] = []
    for operation in payload.operations:
        if not writable:
            results.append(_rejected(operation, ERROR_READ_ONLY))
            continue
        results.append(
            await _apply_operation(
                session,
                operation,
                tenant_id=tenant_id,
                client_id=client_id,
                today=_today_for(tenant),
                plan_version_id=issued.version.id if issued is not None else None,
            )
        )

    tally = {status: 0 for status in (STATUS_APPLIED, STATUS_DUPLICATE, STATUS_REJECTED)}
    for result in results:
        tally[result.status] += 1
    record_audit(request, metadata=tally)

    return SyncResponse(
        results=results,
        # 🔒 A client that sent no hash is not told its plan changed — it has
        # nothing cached to invalidate, and a spurious `true` would make a fresh
        # install refetch a plan it is already about to fetch.
        plan_changed=(
            payload.known_plan_hash is not None and payload.known_plan_hash != current_hash
        ),
        current_plan_hash=current_hash,
    )


# ─── Operation dispatch ──────────────────────────────────────────────────


async def _apply_operation(
    session: AsyncSession,
    operation: SyncOperation,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    today: date,
    plan_version_id: uuid.UUID | None,
) -> SyncResult:
    """Apply one operation inside its own savepoint — guarantee 1.

    ⚠️ **The savepoint is what makes the guarantee true**, not the ``try``. A
    failed ``INSERT`` puts PostgreSQL's transaction into an aborted state, and
    catching the Python exception does not clear it — every later statement then
    fails with "current transaction is aborted", so operation 3 would poison
    operations 4 through 100. ``begin_nested()`` issues a real ``SAVEPOINT`` and
    rolls back to it, leaving the outer transaction usable.

    ⚠️ Rolling back also expunges the pending ORM object the failed operation
    added, so it cannot be re-flushed at commit and resurrect the failure after
    every result has been decided.
    """
    try:
        async with session.begin_nested():
            await _dispatch(
                session,
                operation,
                tenant_id=tenant_id,
                client_id=client_id,
                today=today,
                plan_version_id=plan_version_id,
            )
    except IntegrityError as exc:
        if _is_replay(exc):
            # 🔒 Guarantee 2 — the queue was replayed, which is expected.
            return SyncResult(op_id=operation.op_id, status=STATUS_DUPLICATE)
        # Not a replay: our defect, not the client's. Reported as rejected so the
        # client keeps the operation queued (guarantee 3), and logged so it is
        # visible to us rather than only to them.
        logger.warning(
            "Sync operation failed on an unexpected constraint",
            extra={"operation_type": operation.type},
        )
        return _rejected(operation, ERROR_NOT_APPLIED)
    except PayloadError:
        # 🔒 A malformed or unknown-field payload. Rejected as *this* operation,
        # never as the batch — which is why `SyncOperation.payload` is untyped.
        return _rejected(operation, ErrorType.VALIDATION_FAILED.value)
    except AppError as exc:
        # A domain rule refused it — a date outside the backdating window, an
        # implausible weight awaiting confirmation, a slot that is not theirs.
        return _rejected(operation, exc.error_type.value)

    return SyncResult(op_id=operation.op_id, status=STATUS_APPLIED)


async def _dispatch(
    session: AsyncSession,
    operation: SyncOperation,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    today: date,
    plan_version_id: uuid.UUID | None,
) -> None:
    """Route one operation to the module that owns its table.

    🔒 ``op_id`` **is** the idempotency key. API §13.1 says ``/portal/sync`` is
    keyed "per-operation ``op_id``", and both tables already carry a uniqueness
    boundary scoped to the client — so a replayed queue collides on exactly the
    row it would have duplicated, and two clients sending the same op_id do not
    collide at all.
    """
    key = str(operation.op_id)

    if operation.type == OP_ADHERENCE:
        logged = AdherencePayload.model_validate(operation.payload)
        assert_within_window(logged.logged_for_date, today=today)
        await log_adherence(
            session,
            tenant_id=tenant_id,
            client_id=client_id,
            logged_for_date=logged.logged_for_date,
            slot_type=logged.slot_type.value,
            adherence=logged.adherence,
            idempotency_key=key,
            client_timestamp=operation.client_timestamp,
            plan_slot_id=await _own_slot_id(session, tenant_id=tenant_id, slot_id=logged.slot_id),
            # 🔒 Server-resolved. A client-supplied version id would let them
            # attribute a log to a plan they were never issued.
            plan_version_id=plan_version_id,
        )
        return

    if operation.type == OP_MEASUREMENT:
        fields = MeasurementPayload.model_validate(operation.payload)
        await record_measurement(
            session,
            tenant_id=tenant_id,
            client_id=client_id,
            measured_on=fields.measured_on,
            # 🔒 `CLIENT`, always. EC-M3-05 keeps a practitioner's and a client's
            # value for one date apart, and the display-precedence rule reads
            # this column — a client's queue must never claim to be the
            # practitioner's reading.
            source=MeasurementSource.CLIENT,
            recorded_by_user_id=None,
            weight_kg=fields.weight_kg,
            waist_cm=fields.waist_cm,
            hip_cm=fields.hip_cm,
            body_fat_pct=fields.body_fat_pct,
            confirmed_implausible=fields.confirmed_implausible,
            idempotency_key=key,
        )
        return

    raise UnsupportedOperationError(operation.type)


class UnsupportedOperationError(AppError):
    """🔒 An operation type this server does not implement.

    ⚠️ An :class:`AppError` rather than a bare exception, so it flows through the
    same ``rejected`` path as every other refusal. A newer PWA queuing a type an
    older deployment has never heard of must get a per-operation answer, not a
    500 that discards the batch around it.
    """

    error_type = ErrorType.VALIDATION_FAILED
    status_code = 422

    def __init__(self, operation_type: str) -> None:
        super().__init__(
            message="This app version queued something the server does not understand.",
            action="Update the app and try again.",
            details={"operation_type": operation_type},
        )


async def _own_slot_id(
    session: AsyncSession, *, tenant_id: uuid.UUID, slot_id: uuid.UUID | None
) -> uuid.UUID | None:
    """Confirm a slot belongs to this client's plan, or drop it.

    🔒 **Confirmed by Pattern C, not by a join written here.** ``plan_slots``
    carries a client-realm policy (migration ``0025``), so this ``SELECT``
    returns nothing for a slot on someone else's plan — the same query, a
    different answer, decided by the database.

    ⚠️ An unrecognised slot **degrades to ``None``** rather than rejecting the
    operation. ``slot_type`` is the field that survives plan revision (API
    §12.3), and a client logging breakfast against a slot the practitioner
    deleted this morning has still eaten breakfast. Losing the slot reference is
    the correct outcome; losing the log is not.
    """
    if slot_id is None:
        return None
    found = await session.scalar(
        select(PlanSlot.id).where(PlanSlot.tenant_id == tenant_id, PlanSlot.id == slot_id)
    )
    return found


def _is_replay(exc: IntegrityError) -> bool:
    """Whether this violation is a replayed operation rather than a defect.

    ⚠️ Matched on the constraint name PostgreSQL reports. The alternative —
    treating every ``IntegrityError`` as a duplicate — would tell a client their
    data was stored whenever any constraint failed, which is the one lie this
    endpoint must not tell.
    """
    diagnostic = getattr(getattr(exc, "orig", None), "diag", None)
    name = getattr(diagnostic, "constraint_name", None)
    return name in _REPLAY_CONSTRAINTS


def _rejected(operation: SyncOperation, error: str) -> SyncResult:
    return SyncResult(op_id=operation.op_id, status=STATUS_REJECTED, error=error)


# ─── Assembly ────────────────────────────────────────────────────────────


async def _load_tenant(session: AsyncSession, *, tenant_id: uuid.UUID) -> Tenant:
    """The practice, for its name, timezone and status.

    ⚠️ ``tenants`` carries **no RLS** — it is a platform table that *defines*
    ``app.tenant_id`` and so cannot filter on it (DB §4.1, Pattern D). The
    boundary here is that ``tenant_id`` comes from the verified token via
    ``kernel.tenancy.resolve_scope`` and can arrive no other way; there is no
    request-supplied value on this path.
    """
    tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
    if tenant is None:  # pragma: no cover - a token names a tenant that must exist
        raise AuthenticationError(
            message="Your session is no longer valid.",
            action="Request a new link and try again.",
        )
    return tenant


def _today_for(tenant: Tenant) -> date:
    """Today in the practice's own timezone — NFR-099.

    ⚠️ Not UTC. A client in Kolkata logging breakfast at 07:00 IST is on 01:30
    UTC of the same day, but one logging dinner at 22:00 IST is already on the
    *next* UTC day — and their plan would silently roll over mid-evening.
    """
    try:
        zone = ZoneInfo(tenant.timezone)
    except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - column is seeded
        zone = ZoneInfo("UTC")
    return datetime.now(UTC).astimezone(zone).date()


def _today_plan(
    issued: IssuedPlan,
    resolved: ResolvedPlanVersion,
    *,
    today: date,
    show_nutrition: bool,
) -> TodayPlan | None:
    """Project one day of the plan into the landing view."""
    if not resolved.days:
        return None

    day = resolved.days[_day_index(issued, today=today, day_count=len(resolved.days))]

    return TodayPlan(
        version_id=issued.version.id,
        content_hash=issued.content_hash,
        day_number=day.day_number,
        slots=[
            TodaySlot(
                slot_id=slot.id,
                slot_type=slot.slot_type,
                label=_slot_label(slot.custom_label, slot.slot_type),
                target_time=slot.target_time,
                items=[
                    TodayItem(
                        display_name=item.display_name,
                        quantity_display=_quantity_display(
                            item.measure_display, item.resolved_grams
                        ),
                        # 🔒 `client_note`, never `notes`. `notes` is the
                        # practitioner's own working annotation (API §8.3) and
                        # has no business on a client's screen.
                        note=item.client_note,
                        nutrition=(
                            MacroTotalsResponse.model_validate(item.nutrition)
                            if show_nutrition
                            else None
                        ),
                    )
                    for item in slot.items
                ],
                adherence=TodayAdherence(),
            )
            for slot in day.slots
        ],
    )


def _day_index(issued: IssuedPlan, *, today: date, day_count: int) -> int:
    """Which day of a multi-day plan today is.

    🟡 **PROPOSED — the cycling rule is not specified in an approved document.**
    A plan holds 1–7 days (FR-M4-026) and is followed for longer than that, so
    the days repeat from the day the plan took effect. The anchor is
    ``valid_from`` where the practitioner set one and the issue date otherwise.

    ⚠️ A plan whose ``valid_from`` is in the future shows day 1 rather than a
    negative index — the client opening it early should see the start.
    """
    anchor = issued.version.valid_from or issued.version.issued_at
    if anchor is None:  # pragma: no cover - an issued version always has one
        return 0
    elapsed = (today - anchor.astimezone(UTC).date()).days
    if elapsed <= 0:
        return 0
    return elapsed % day_count


def _slot_label(custom_label: str | None, slot_type: str) -> str:
    """🔒 Rendered server-side, like every other display string here (NFR-072)."""
    return custom_label or slot_type.replace("_", " ").capitalize()


def _quantity_display(measure_display: str, grams: Decimal | None) -> str:
    """``"2 katori (70 g)"`` — the measure, and what it weighs.

    🔒 API §12.2 puts this on the server. The gram weight is what makes a
    household measure actionable, and a client that computed it would be
    computing a clinical figure.
    """
    if not measure_display:
        return ""
    if grams is None:
        return measure_display
    return f"{measure_display} ({format_measure(grams, 'g')})"


def _weight_is_due(measurements: list[Measurement], *, today: date) -> bool:
    """Whether to ask for a weight — FR-M7-006.

    A client who has never recorded one is always due; after that it is the age
    of the most recent reading.
    """
    latest = next((row for row in measurements if row.weight_kg is not None), None)
    if latest is None:
        return True
    return (today - latest.measured_on).days >= WEIGHT_PROMPT_AFTER_DAYS


def _pending_assessment_id(responses: list[AssessmentResponse]) -> uuid.UUID | None:
    """The assessment waiting to be finished, if any — FR-M3-005.

    ⚠️ ``list_responses`` returns newest first, so the first in-progress row is
    the one to resume. A client with two open responses is not a state the
    assessment service produces (`start_or_resume` reuses the open one), but
    picking the newest is the right answer if it ever occurs.
    """
    return next(
        (row.id for row in responses if row.status is ResponseStatus.IN_PROGRESS),
        None,
    )


def _teaser(measurements: list[Measurement], *, today: date) -> ProgressTeaser | None:
    """Weight change over the recent window — API §12.2.

    Returns ``None`` rather than a zero when there is nothing to compare: a
    client with one weight has not "maintained", they have one weight, and
    showing ``0.0`` would claim a result nobody measured.
    """
    cutoff = today.toordinal() - TEASER_WINDOW_DAYS
    weighed = [
        row
        for row in measurements
        if row.weight_kg is not None and row.measured_on.toordinal() >= cutoff
    ]
    if len(weighed) < 2:
        return None

    # `preferred_per_date` returns newest first.
    latest, earliest = weighed[0], weighed[-1]
    assert latest.weight_kg is not None and earliest.weight_kg is not None
    return ProgressTeaser(
        weight_change_kg=latest.weight_kg - earliest.weight_kg,
        period_days=(latest.measured_on - earliest.measured_on).days,
    )


def _capabilities_for(client: Client, tenant: Tenant) -> tuple[Capabilities, str | None]:
    """🔒 API §12.6 — the four degradation states, in one place.

    ⚠️ Order matters. A suspended tenant outranks a paused client, because the
    tenant-level notice is the neutral one and a client of a suspended practice
    must not be told their own engagement was paused when it was not.

    🔒 Both states answer **200**. A 402, a 403 or a different shape would let a
    client infer their practitioner's account state (EC-M7-08), which is exactly
    what the neutral message exists to prevent.
    """
    if tenant.status is TenantStatus.SUSPENDED:
        return (
            Capabilities(
                can_log_adherence=False,
                can_log_measurements=False,
                can_upload=False,
                # The plan they were already given stays readable. Withdrawing it
                # would turn a billing event into a clinical one.
                can_view_plan=True,
            ),
            SUSPENDED_NOTICE,
        )

    if client.stage is ClientStage.PAUSED:
        return (
            Capabilities(
                can_log_adherence=False,
                can_log_measurements=False,
                can_upload=False,
                can_view_plan=True,
            ),
            PAUSED_NOTICE,
        )

    return (
        Capabilities(
            can_log_adherence=True,
            can_log_measurements=True,
            can_upload=True,
            can_view_plan=True,
        ),
        None,
    )
