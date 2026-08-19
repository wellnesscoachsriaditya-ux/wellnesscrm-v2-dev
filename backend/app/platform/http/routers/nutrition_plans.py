"""Diet plans over HTTP — API §8.1, §8.2, §8.3, §8.4, §8.6.

🔒 In ``platform/`` for the reason every router is: R5 forbids a module importing
``platform``, and this needs ``realm_router``, the session and the authorization
seam. The rules live in ``kernel.nutrition``, the persistence and the resolver in
``modules.nutrition``; this owns the HTTP shape and nothing else.

🔒 **Every route authorizes against the client**, not merely against the action —
through :func:`authorized_plan_version` and its siblings, which walk up to
``diet_plans.client_id`` and re-enter the same ``authorized_client`` the clinical
router uses. A practitioner who cannot open a client must not reach their plans;
that is AC-M1-006, through fifteen more doors than S2 had.

🔒 **Every mutation requires ``If-Match``** and answers 409 on a stale token
(ADR-14, EC-M4-07). ⚠️ **Child edits bump the parent version's ``row_version``**,
because the plan is the unit of concurrency: two practitioners editing different
slots of one plan are still editing one plan.

⚠️ **Numbers travel as strings** (API §4). Nutrition values are ``numeric`` in
PostgreSQL precisely to avoid IEEE-754 error, and JSON numbers are doubles —
``0.1 + 0.2 !== 0.3``. In a clinical product a silently rounded macro total is a
defect rather than a rounding artefact, so :data:`Exact` carries every
``Decimal`` out as text.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import Header, Request, Response, status
from pydantic import BaseModel, Field, PlainSerializer

from app.kernel.context import get_context
from app.kernel.nutrition import MealSlotType, PlanState
from app.modules.nutrition import (
    NUTRITION_PLANS_READ,
    NUTRITION_PLANS_WRITE,
    PLAN_SOURCE_BLANK,
    DietPlan,
    DietPlanVersion,
    ResolvedPlanVersion,
    create_plan,
    discard_plan_version,
    issue_plan_version,
    list_plan_versions,
    list_plans_for_client,
    load_plan,
    load_plan_version,
    resolve_plan_version,
    update_plan_version,
)
from app.modules.nutrition import (
    add_day as module_add_day,
)
from app.modules.nutrition import (
    add_slot as module_add_slot,
)
from app.platform.http.authz import requires
from app.platform.http.pipeline import get_session, realm_router, record_audit
from app.platform.http.preconditions import etag_for_row_version, parse_if_match_row_version
from app.platform.http.routers.clients import authorized_client

#: 🔒 API §4 — a ``Decimal`` leaves as a string, never as a JSON number.
Exact = Annotated[Decimal, PlainSerializer(str, return_type=str)]

#: The ``If-Match`` header, required on every mutation.
IfMatch = Annotated[str | None, Header(alias="If-Match")]

_RESOURCE = "plan_version"

plan_router = realm_router("/api/v1/app/clients", tags=["nutrition-plans"])
version_router = realm_router("/api/v1/app/plan-versions", tags=["nutrition-plans"])
plan_read_router = realm_router("/api/v1/app/plans", tags=["nutrition-plans"])


# ─── Wire shapes ─────────────────────────────────────────────────────────


class MacroTotalsResponse(BaseModel):
    energy_kcal: Exact
    protein_g: Exact
    carbs_g: Exact
    fat_g: Exact
    fibre_g: Exact


class NutritionBudgetResponse(BaseModel):
    """🔒 ADR-A07 / API §8.4 — locking made observable."""

    target: MacroTotalsResponse
    locked_consumed: MacroTotalsResponse
    unlocked_current: MacroTotalsResponse
    #: ⚠️ May be **negative** when locked items alone exceed the target. That is a
    #: legitimate clinical state (EC-M4-05), reported and never blocked.
    remaining_available: MacroTotalsResponse
    locked_item_count: int
    locked_slot_count: int
    is_within_tolerance: bool
    tolerance_pct: Exact


class PlanWarningResponse(BaseModel):
    rule_code: str
    severity: str
    message: str
    scope: dict[str, str | int]


class PlanItemResponse(BaseModel):
    id: uuid.UUID
    item_type: str
    food_id: uuid.UUID | None
    recipe_id: uuid.UUID | None
    meal_id: uuid.UUID | None
    display_name: str
    quantity: Exact
    measure_unit_id: uuid.UUID
    measure_unit_code: str
    #: 🔒 "1 katori" — server-rendered so the formatting lives in one place.
    measure_display: str
    resolved_grams: Exact | None
    nutrition: MacroTotalsResponse
    #: 🔒 The **effective** lock: an item inside a locked slot reads as locked.
    is_locked: bool
    item_is_locked: bool
    notes: str | None
    client_note: str | None
    sort_order: int
    #: ⏳ Always empty until the slice that adds alternatives. Present now so the
    #: shape is declared once and does not change under the client later.
    alternatives: list[dict[str, str]] = Field(default_factory=list)


class PlanSlotResponse(BaseModel):
    id: uuid.UUID
    slot_type: str
    custom_label: str | None
    target_time: str | None
    sort_order: int
    is_locked: bool
    items: list[PlanItemResponse]
    slot_totals: MacroTotalsResponse


class PlanDayResponse(BaseModel):
    id: uuid.UUID
    day_number: int
    label: str
    slots: list[PlanSlotResponse]
    day_totals: MacroTotalsResponse


class PlanVersionResponse(BaseModel):
    """API §8.3's contract, in full."""

    id: uuid.UUID
    plan_id: uuid.UUID
    version_number: int
    state: PlanState
    origin: str
    row_version: int
    valid_from: datetime | None
    valid_to: datetime | None
    targets: MacroTotalsResponse
    nutrition_budget: NutritionBudgetResponse
    days: list[PlanDayResponse]
    #: ⏳ Empty until the slice that adds supplements.
    supplements: list[dict[str, str]] = Field(default_factory=list)
    plan_totals: MacroTotalsResponse
    warnings: list[PlanWarningResponse]
    practitioner_notes: str | None


class PlanResponse(BaseModel):
    id: uuid.UUID
    client_id: uuid.UUID
    title: str
    goal_type: str | None
    created_by_user_id: uuid.UUID
    current_version_id: uuid.UUID | None
    archived_at: datetime | None


class PlanVersionSummary(BaseModel):
    id: uuid.UUID
    version_number: int
    state: PlanState
    origin: str
    row_version: int
    issued_at: datetime | None
    valid_from: datetime | None
    valid_to: datetime | None


class PlanSummaryResponse(BaseModel):
    """A plan in a list, with the version a practitioner means by "the plan"."""

    plan: PlanResponse
    version: PlanVersionSummary | None


class PlanDetailResponse(BaseModel):
    """🔒 AC-M4-008 — prior versions remain retrievable and clearly dated."""

    plan: PlanResponse
    versions: list[PlanVersionSummary]


class PlanCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    #: 🔒 FR-M4-032. ⏳ ``template`` and ``ai_draft`` are refused by the service
    #: with a message naming what is supported — the template tables have no
    #: reader yet and AI drafting is M5.
    source: str = PLAN_SOURCE_BLANK
    source_plan_version_id: uuid.UUID | None = None
    goal_type: str | None = None
    #: FR-M4-026. Constrained to the two structures the PRD names.
    day_count: int = Field(default=1, ge=1, le=7)
    #: 🟡 Omit for FR-M4-025's seven defaults; pass ``[]`` for a bare plan.
    slot_types: list[MealSlotType] | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    target_energy_kcal: Decimal | None = None
    target_protein_g: Decimal | None = None
    target_carbs_g: Decimal | None = None
    target_fat_g: Decimal | None = None


class PlanCreateResponse(BaseModel):
    """🔒 API §8.2 — a plan and its first draft, never one without the other."""

    plan: PlanResponse
    draft_version: PlanVersionSummary


class PlanVersionPatch(BaseModel):
    title: str | None = None
    goal_type: str | None = None
    practitioner_notes: str | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    target_energy_kcal: Decimal | None = None
    target_protein_g: Decimal | None = None
    target_carbs_g: Decimal | None = None
    target_fat_g: Decimal | None = None


class DayCreateRequest(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    slot_types: list[MealSlotType] | None = None


class SlotCreateRequest(BaseModel):
    day_id: uuid.UUID
    slot_type: MealSlotType
    custom_label: str | None = Field(default=None, max_length=120)
    target_time: str | None = Field(default=None, max_length=32)


class DayResponse(BaseModel):
    id: uuid.UUID
    plan_version_id: uuid.UUID
    day_number: int
    label: str


class SlotResponse(BaseModel):
    id: uuid.UUID
    plan_day_id: uuid.UUID
    slot_type: str
    custom_label: str | None
    target_time: str | None
    sort_order: int
    is_locked: bool


# ─── The authorization seams ─────────────────────────────────────────────


async def authorized_plan(request: Request, plan_id: uuid.UUID) -> DietPlan:
    """Load a plan and authorize *this* action against its client — AC-M1-006.

    🔒 The plan is not the subject of the decision; the **client** is. A
    practitioner who is not the owner and holds no grant gets **404, not 403**,
    which ``pipeline._error_for`` produces from the ``not_assigned_to_actor``
    reason. A 403 would confirm the plan exists and let a colleague's caseload be
    enumerated one request at a time.
    """
    actor = get_context().actor
    plan = await load_plan(get_session(request), tenant_id=actor.require_tenant(), plan_id=plan_id)
    await authorized_client(request, plan.client_id)
    return plan


async def authorized_plan_version(request: Request, version_id: uuid.UUID) -> DietPlanVersion:
    """Load a version and authorize against the client it belongs to."""
    actor = get_context().actor
    version = await load_plan_version(
        get_session(request), tenant_id=actor.require_tenant(), version_id=version_id
    )
    await authorized_plan(request, version.plan_id)
    return version


def _totals(source: object) -> MacroTotalsResponse:
    return MacroTotalsResponse.model_validate(source)


def _plan_response(plan: DietPlan) -> PlanResponse:
    return PlanResponse.model_validate(plan, from_attributes=True)


def _version_summary(version: DietPlanVersion) -> PlanVersionSummary:
    return PlanVersionSummary(
        id=version.id,
        version_number=version.version_number,
        state=version.state,
        origin=version.origin.value,
        row_version=version.row_version,
        issued_at=version.issued_at,
        valid_from=version.valid_from,
        valid_to=version.valid_to,
    )


def _version_response(resolved: ResolvedPlanVersion) -> PlanVersionResponse:
    version = resolved.version
    return PlanVersionResponse(
        id=version.id,
        plan_id=version.plan_id,
        version_number=version.version_number,
        state=version.state,
        origin=version.origin.value,
        row_version=version.row_version,
        valid_from=version.valid_from,
        valid_to=version.valid_to,
        targets=_totals(resolved.budget["target"]),
        nutrition_budget=NutritionBudgetResponse.model_validate(resolved.budget),
        days=[
            PlanDayResponse(
                id=day.id,
                day_number=day.day_number,
                label=day.label,
                slots=[
                    PlanSlotResponse(
                        id=slot.id,
                        slot_type=slot.slot_type,
                        custom_label=slot.custom_label,
                        target_time=slot.target_time,
                        sort_order=slot.sort_order,
                        is_locked=slot.is_locked,
                        items=[
                            PlanItemResponse.model_validate(item, from_attributes=True)
                            for item in slot.items
                        ],
                        slot_totals=_totals(slot.slot_totals),
                    )
                    for slot in day.slots
                ],
                day_totals=_totals(day.day_totals),
            )
            for day in resolved.days
        ],
        plan_totals=_totals(resolved.plan_totals),
        warnings=[PlanWarningResponse.model_validate(w) for w in resolved.warnings],
        practitioner_notes=version.practitioner_notes,
    )


async def _respond_with_version(
    request: Request, response: Response, version_id: uuid.UUID
) -> PlanVersionResponse:
    """Re-resolve and return the aggregate, with the new ETag.

    Every mutation answers with the whole plan rather than the row it changed.
    ⚠️ That is deliberate: a nutrition total is a property of the *plan*, so an
    edit that returned only the edited item would leave the practitioner's
    running totals to be recomputed client-side — which NFR-072 forbids.
    """
    actor = get_context().actor
    resolved = await resolve_plan_version(
        get_session(request), tenant_id=actor.require_tenant(), version_id=version_id
    )
    response.headers["ETag"] = etag_for_row_version(resolved.version.row_version)
    return _version_response(resolved)


# ─── Plans, under the client ─────────────────────────────────────────────


@plan_router.post(
    "/{client_id}/plans",
    status_code=status.HTTP_201_CREATED,
    summary="Create a diet plan",
    operation_id="planCreate",
)
@requires(NUTRITION_PLANS_WRITE)
async def create(
    request: Request, client_id: uuid.UUID, body: PlanCreateRequest
) -> PlanCreateResponse:
    """Create a plan and its first draft version — API §8.2, FR-M4-032.

    🔒 Both, atomically. A plan with no version is not a state a practitioner can
    use, so the API never produces one. A second draft on the same plan is
    refused by ``uq_diet_plan_versions__one_draft`` with a 409.
    """
    await authorized_client(request, client_id)
    actor = get_context().actor

    plan, version = await create_plan(
        get_session(request),
        tenant_id=actor.require_tenant(),
        client_id=client_id,
        created_by_user_id=actor.require_subject(),
        title=body.title,
        source=body.source,
        source_plan_version_id=body.source_plan_version_id,
        goal_type=body.goal_type,
        day_count=body.day_count,
        slot_types=body.slot_types,
        valid_from=body.valid_from,
        valid_to=body.valid_to,
        target_energy_kcal=body.target_energy_kcal,
        target_protein_g=body.target_protein_g,
        target_carbs_g=body.target_carbs_g,
        target_fat_g=body.target_fat_g,
    )

    record_audit(request, resource_id=plan.id, metadata={"source": body.source})
    return PlanCreateResponse(plan=_plan_response(plan), draft_version=_version_summary(version))


@plan_router.get(
    "/{client_id}/plans",
    summary="List a client's diet plans",
    operation_id="planList",
)
@requires(NUTRITION_PLANS_READ)
async def list_for_client(request: Request, client_id: uuid.UUID) -> list[PlanSummaryResponse]:
    """Every plan for one client — API §8.1."""
    await authorized_client(request, client_id)
    actor = get_context().actor

    rows = await list_plans_for_client(
        get_session(request), tenant_id=actor.require_tenant(), client_id=client_id
    )
    return [
        PlanSummaryResponse(
            plan=_plan_response(plan),
            version=_version_summary(version) if version else None,
        )
        for plan, version in rows
    ]


@plan_read_router.get(
    "/{plan_id}",
    summary="Read a plan and its version history",
    operation_id="planRead",
)
@requires(NUTRITION_PLANS_READ)
async def read_plan(request: Request, plan_id: uuid.UUID) -> PlanDetailResponse:
    """🔒 AC-M4-008 — a revision is issued while the prior version stays retrievable."""
    plan = await authorized_plan(request, plan_id)
    actor = get_context().actor

    versions = await list_plan_versions(
        get_session(request), tenant_id=actor.require_tenant(), plan_id=plan_id
    )

    record_audit(request, resource_id=plan.id)
    return PlanDetailResponse(
        plan=_plan_response(plan),
        versions=[_version_summary(version) for version in versions],
    )


# ─── One version ─────────────────────────────────────────────────────────


@version_router.get(
    "/{version_id}",
    summary="Read a plan version with its nutrition and budget",
    operation_id="planVersionRead",
)
@requires(NUTRITION_PLANS_READ)
async def read_version(
    request: Request, version_id: uuid.UUID, response: Response
) -> PlanVersionResponse:
    """🔒 API §8.3 — every nutrition figure and ``resolved_grams`` server-computed.

    The client never converts a portion or sums a nutrient. A mismatch between
    what is displayed and what is stored would be a clinical defect, not a
    presentation bug (NFR-072).
    """
    await authorized_plan_version(request, version_id)
    record_audit(request, resource_id=version_id)
    return await _respond_with_version(request, response, version_id)


@version_router.patch(
    "/{version_id}",
    summary="Edit a draft's metadata and targets",
    operation_id="planVersionUpdate",
)
@requires(NUTRITION_PLANS_WRITE)
async def update_version(
    request: Request,
    version_id: uuid.UUID,
    body: PlanVersionPatch,
    response: Response,
    if_match: IfMatch = None,
) -> PlanVersionResponse:
    """Title, goal, validity, notes and targets — API §8.3."""
    expected = parse_if_match_row_version(if_match, _RESOURCE)
    await authorized_plan_version(request, version_id)
    actor = get_context().actor

    supplied = body.model_fields_set
    await update_plan_version(
        get_session(request),
        tenant_id=actor.require_tenant(),
        version_id=version_id,
        expected_row_version=expected,
        **{name: getattr(body, name) for name in supplied},
    )

    record_audit(request, resource_id=version_id, changed_fields=sorted(supplied))
    return await _respond_with_version(request, response, version_id)


@version_router.post(
    "/{version_id}/discard",
    summary="Discard a draft",
    operation_id="planVersionDiscard",
)
@requires(NUTRITION_PLANS_WRITE)
async def discard_version(
    request: Request, version_id: uuid.UUID, response: Response, if_match: IfMatch = None
) -> PlanVersionResponse:
    """🔒 A state, not a delete. DDR-11 keeps the record of what was tried."""
    expected = parse_if_match_row_version(if_match, _RESOURCE)
    await authorized_plan_version(request, version_id)
    actor = get_context().actor

    await discard_plan_version(
        get_session(request),
        tenant_id=actor.require_tenant(),
        version_id=version_id,
        expected_row_version=expected,
    )

    record_audit(request, resource_id=version_id, metadata={"state": "discarded"})
    return await _respond_with_version(request, response, version_id)


@version_router.post(
    "/{version_id}/days",
    status_code=status.HTTP_201_CREATED,
    summary="Add a day to a draft",
    operation_id="planDayAdd",
)
@requires(NUTRITION_PLANS_WRITE)
async def add_day(
    request: Request,
    version_id: uuid.UUID,
    body: DayCreateRequest,
    response: Response,
    if_match: IfMatch = None,
) -> DayResponse:
    """FR-M4-026 — a plan grows a day at a time."""
    expected = parse_if_match_row_version(if_match, _RESOURCE)
    await authorized_plan_version(request, version_id)
    actor = get_context().actor

    day = await module_add_day(
        get_session(request),
        tenant_id=actor.require_tenant(),
        version_id=version_id,
        expected_row_version=expected,
        label=body.label,
        slot_types=body.slot_types,
    )

    record_audit(request, resource_id=day.id)
    response.headers["ETag"] = etag_for_row_version(expected + 1)
    return DayResponse.model_validate(day, from_attributes=True)


@version_router.post(
    "/{version_id}/slots",
    status_code=status.HTTP_201_CREATED,
    summary="Add a meal slot to a day",
    operation_id="planSlotAdd",
)
@requires(NUTRITION_PLANS_WRITE)
async def add_slot(
    request: Request,
    version_id: uuid.UUID,
    body: SlotCreateRequest,
    response: Response,
    if_match: IfMatch = None,
) -> SlotResponse:
    """FR-M4-025 — practitioners add, rename, remove and reorder slots."""
    expected = parse_if_match_row_version(if_match, _RESOURCE)
    await authorized_plan_version(request, version_id)
    actor = get_context().actor

    slot = await module_add_slot(
        get_session(request),
        tenant_id=actor.require_tenant(),
        day_id=body.day_id,
        expected_row_version=expected,
        slot_type=body.slot_type,
        custom_label=body.custom_label,
        target_time=body.target_time,
    )

    record_audit(request, resource_id=slot.id, metadata={"slot_type": body.slot_type.value})
    response.headers["ETag"] = etag_for_row_version(expected + 1)
    return SlotResponse.model_validate(slot, from_attributes=True)


# ─── Issue (Slice 1.3; the snapshot it writes is still a stub) ───────────


@version_router.post(
    "/{version_id}/issue",
    summary="Issue a draft plan version",
    operation_id="planVersionIssue",
)
@requires(NUTRITION_PLANS_WRITE)
async def issue(
    request: Request, version_id: uuid.UUID, if_match: IfMatch = None
) -> dict[str, str]:
    """🔒 The only path from ``draft`` to ``issued`` — API §8.6.

    ⚠️ The snapshot this freezes is still Slice 1.3's placeholder rather than the
    resolved plan; rewriting it against the resolver is its own slice. The route
    is moved here from the old ``/app/nutrition/plans/...`` prefix so that every
    plan operation sits under the paths API §8.1 specifies.
    """
    expected = parse_if_match_row_version(if_match, _RESOURCE)
    version = await authorized_plan_version(request, version_id)
    actor = get_context().actor

    if version.row_version != expected:
        from app.kernel.errors import ConflictError

        raise ConflictError(
            message="Someone else changed this plan while you were editing it.",
            action="Reload the plan to see their changes, then issue it again.",
            details={"current_row_version": version.row_version},
        )

    await issue_plan_version(
        get_session(request),
        actor.require_tenant(),
        version.plan_id,
        version_id,
        actor.require_subject(),
    )

    record_audit(request, resource_id=version_id, metadata={"state": "issued"})
    return {"status": "ok"}
