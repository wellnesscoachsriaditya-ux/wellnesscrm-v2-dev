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
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Request
from pydantic import (
    BaseModel,
    Field,
    PlainSerializer,
    SerializerFunctionWrapHandler,
    model_serializer,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.authz import DataScope, register_action
from app.kernel.clients import ClientStage
from app.kernel.clinical import ResponseStatus
from app.kernel.context import UserRole, get_context
from app.kernel.errors import AuthenticationError
from app.kernel.models import Tenant, TenantStatus
from app.kernel.nutrition import format_measure
from app.modules.clients import Client, get_client
from app.modules.clinical import (
    AssessmentResponse,
    Measurement,
    list_responses,
    measurement_history,
    preferred_per_date,
)
from app.modules.nutrition import (
    IssuedPlan,
    ResolvedPlanVersion,
    current_issued_plan_for_client,
    resolve_plan_version,
)
from app.platform.http.authz import requires
from app.platform.http.pipeline import get_session, realm_router

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
