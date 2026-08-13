"""The leaves of a plan — days, slots and items, at the paths API §8.1 gives them.

⚠️ **Separate from ``nutrition_plans.py`` on purpose, and separate from the
start.** API §8.1 addresses these by their own id (``/app/plan-items/{id}``)
rather than nesting them under the version, so they cannot share that router's
prefix; and by the time locking, alternatives and supplements land, one file
covering plans *and* their contents would be the six-hundred-line module that
gets skimmed rather than read. Same reasoning that splits ``clients.py`` from
``collaboration.py`` and ``discovery.py``.

🔒 **Every route here authorizes against the client**, by walking the row up to
``diet_plans.client_id`` — a leaf id is not a capability, and reaching an item by
guessing its uuid must not work.

🔒 **Every route requires ``If-Match`` on the parent version.** A slot and an item
have no version of their own; the plan is the unit of concurrency (ADR-14,
EC-M4-07), and every write here bumps it.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import Header, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.kernel.context import get_context
from app.kernel.errors import NotFoundError
from app.kernel.nutrition import MealSlotType
from app.modules.nutrition import (
    ITEM_TYPE_FOOD,
    NUTRITION_PLANS_WRITE,
    PlanDay,
    PlanItem,
    PlanSlot,
    add_item,
    remove_item,
    remove_slot,
    update_day,
    update_item,
    update_slot,
)
from app.platform.http.authz import requires
from app.platform.http.pipeline import get_session, realm_router, record_audit
from app.platform.http.preconditions import etag_for_row_version, parse_if_match_row_version
from app.platform.http.routers.nutrition_plans import (
    DayResponse,
    SlotResponse,
    authorized_plan_version,
)

IfMatch = Annotated[str | None, Header(alias="If-Match")]

_RESOURCE = "plan_version"

day_router = realm_router("/api/v1/app/plan-days", tags=["nutrition-plans"])
slot_router = realm_router("/api/v1/app/plan-slots", tags=["nutrition-plans"])
item_router = realm_router("/api/v1/app/plan-items", tags=["nutrition-plans"])


# ─── Wire shapes ─────────────────────────────────────────────────────────


class DayPatch(BaseModel):
    label: str = Field(min_length=1, max_length=120)


class SlotPatch(BaseModel):
    #: 🔒 Renaming a slot sets ``custom_label``. ``slot_type`` is structural — it
    #: is what the PDF and the food log group by — and changing it is a different
    #: intent from calling breakfast "Pre-gym".
    slot_type: MealSlotType | None = None
    custom_label: str | None = Field(default=None, max_length=120)
    target_time: str | None = Field(default=None, max_length=32)
    sort_order: int | None = Field(default=None, ge=1)


class ItemCreateRequest(BaseModel):
    slot_id: uuid.UUID
    #: ⏳ ``recipe`` and ``meal`` are accepted by the schema because
    #: ``plan_items`` supports all three, but recipe nutrition is not calculated
    #: until per-serving yield lands (FR-M4-022, Phase 2) and shows as a warning.
    item_type: str = ITEM_TYPE_FOOD
    food_id: uuid.UUID | None = None
    recipe_id: uuid.UUID | None = None
    meal_id: uuid.UUID | None = None
    quantity: Decimal = Field(gt=Decimal("0"))
    measure_unit_id: uuid.UUID
    notes: str | None = Field(default=None, max_length=2000)
    client_note: str | None = Field(default=None, max_length=2000)


class ItemPatch(BaseModel):
    """⚠️ No ``food_id``, and that is the point.

    Swapping the food under a fixed item id would be a substitution wearing an
    edit's clothes. The practitioner removes the item and adds the one they
    meant, which lands in the audit log as two decisions rather than one silent
    one — the same argument API §8.5 makes about recalculation never
    substituting a food.
    """

    quantity: Decimal | None = Field(default=None, gt=Decimal("0"))
    measure_unit_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=2000)
    client_note: str | None = Field(default=None, max_length=2000)
    sort_order: int | None = Field(default=None, ge=1)


class ItemResponse(BaseModel):
    id: uuid.UUID
    plan_slot_id: uuid.UUID
    item_type: str
    food_id: uuid.UUID | None
    recipe_id: uuid.UUID | None
    meal_id: uuid.UUID | None
    quantity: Decimal
    measure_unit_id: uuid.UUID
    notes: str | None
    client_note: str | None
    is_locked: bool
    sort_order: int


# ─── Resolving a leaf to the version that owns it ────────────────────────


async def _version_id_for_day(request: Request, day_id: uuid.UUID) -> uuid.UUID:
    actor = get_context().actor
    row = (
        await get_session(request).execute(
            select(PlanDay.plan_version_id).where(
                PlanDay.id == day_id, PlanDay.tenant_id == actor.require_tenant()
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(
            message="That day doesn't exist.", action="Reload the plan and try again."
        )
    return row


async def _version_id_for_slot(request: Request, slot_id: uuid.UUID) -> uuid.UUID:
    actor = get_context().actor
    row = (
        await get_session(request).execute(
            select(PlanDay.plan_version_id)
            .join(PlanSlot, PlanSlot.plan_day_id == PlanDay.id)
            .where(PlanSlot.id == slot_id, PlanSlot.tenant_id == actor.require_tenant())
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(
            message="That meal slot doesn't exist.", action="Reload the plan and try again."
        )
    return row


async def _version_id_for_item(request: Request, item_id: uuid.UUID) -> uuid.UUID:
    actor = get_context().actor
    row = (
        await get_session(request).execute(
            select(PlanDay.plan_version_id)
            .join(PlanSlot, PlanSlot.plan_day_id == PlanDay.id)
            .join(PlanItem, PlanItem.plan_slot_id == PlanSlot.id)
            .where(PlanItem.id == item_id, PlanItem.tenant_id == actor.require_tenant())
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(
            message="That item doesn't exist.", action="Reload the plan and try again."
        )
    return row


# ─── Days ────────────────────────────────────────────────────────────────


@day_router.patch("/{day_id}", summary="Rename a day", operation_id="planDayUpdate")
@requires(NUTRITION_PLANS_WRITE)
async def patch_day(
    request: Request,
    day_id: uuid.UUID,
    body: DayPatch,
    response: Response,
    if_match: IfMatch = None,
) -> DayResponse:
    expected = parse_if_match_row_version(if_match, _RESOURCE)
    version_id = await _version_id_for_day(request, day_id)
    await authorized_plan_version(request, version_id)
    actor = get_context().actor

    day = await update_day(
        get_session(request),
        tenant_id=actor.require_tenant(),
        day_id=day_id,
        expected_row_version=expected,
        label=body.label,
    )

    record_audit(request, resource_id=day.id, changed_fields=["label"])
    response.headers["ETag"] = etag_for_row_version(expected + 1)
    return DayResponse.model_validate(day, from_attributes=True)


# ─── Slots ───────────────────────────────────────────────────────────────


@slot_router.patch(
    "/{slot_id}", summary="Rename, retime or reorder a slot", operation_id="planSlotUpdate"
)
@requires(NUTRITION_PLANS_WRITE)
async def patch_slot(
    request: Request,
    slot_id: uuid.UUID,
    body: SlotPatch,
    response: Response,
    if_match: IfMatch = None,
) -> SlotResponse:
    """FR-M4-025 — add, rename, remove and reorder."""
    expected = parse_if_match_row_version(if_match, _RESOURCE)
    version_id = await _version_id_for_slot(request, slot_id)
    await authorized_plan_version(request, version_id)
    actor = get_context().actor

    supplied = body.model_fields_set
    slot = await update_slot(
        get_session(request),
        tenant_id=actor.require_tenant(),
        slot_id=slot_id,
        expected_row_version=expected,
        **{name: getattr(body, name) for name in supplied},
    )

    record_audit(request, resource_id=slot.id, changed_fields=sorted(supplied))
    response.headers["ETag"] = etag_for_row_version(expected + 1)
    return SlotResponse.model_validate(slot, from_attributes=True)


@slot_router.delete(
    "/{slot_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a slot and its items",
    operation_id="planSlotRemove",
)
@requires(NUTRITION_PLANS_WRITE)
async def delete_slot(request: Request, slot_id: uuid.UUID, if_match: IfMatch = None) -> Response:
    """🔒 Refused at the database for a non-draft version.

    ``plan_slots__delete_draft_only`` (migration 0020) is a ``RESTRICTIVE``
    policy, so the row is invisible to a ``DELETE`` unless its version is a
    draft — independently of the service's own ``assert_draft``.
    """
    expected = parse_if_match_row_version(if_match, _RESOURCE)
    version_id = await _version_id_for_slot(request, slot_id)
    await authorized_plan_version(request, version_id)
    actor = get_context().actor

    await remove_slot(
        get_session(request),
        tenant_id=actor.require_tenant(),
        slot_id=slot_id,
        expected_row_version=expected,
    )

    record_audit(request, resource_id=slot_id)
    # The plan moved even though the response carries no body, so the caller
    # still needs the new token to make its next edit.
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"ETag": etag_for_row_version(expected + 1)},
    )


# ─── Items ───────────────────────────────────────────────────────────────


@item_router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Add a food to a slot",
    operation_id="planItemAdd",
)
@requires(NUTRITION_PLANS_WRITE)
async def create_item(
    request: Request, body: ItemCreateRequest, response: Response, if_match: IfMatch = None
) -> ItemResponse:
    """FR-M4-024 — the plan's actual content."""
    expected = parse_if_match_row_version(if_match, _RESOURCE)
    version_id = await _version_id_for_slot(request, body.slot_id)
    await authorized_plan_version(request, version_id)
    actor = get_context().actor

    item = await add_item(
        get_session(request),
        tenant_id=actor.require_tenant(),
        slot_id=body.slot_id,
        expected_row_version=expected,
        item_type=body.item_type,
        food_id=body.food_id,
        recipe_id=body.recipe_id,
        meal_id=body.meal_id,
        quantity=body.quantity,
        measure_unit_id=body.measure_unit_id,
        notes=body.notes,
        client_note=body.client_note,
    )

    record_audit(request, resource_id=item.id, metadata={"item_type": body.item_type})
    response.headers["ETag"] = etag_for_row_version(expected + 1)
    return ItemResponse.model_validate(item, from_attributes=True)


@item_router.patch(
    "/{item_id}", summary="Change a quantity, measure or note", operation_id="planItemUpdate"
)
@requires(NUTRITION_PLANS_WRITE)
async def patch_item(
    request: Request,
    item_id: uuid.UUID,
    body: ItemPatch,
    response: Response,
    if_match: IfMatch = None,
) -> ItemResponse:
    expected = parse_if_match_row_version(if_match, _RESOURCE)
    version_id = await _version_id_for_item(request, item_id)
    await authorized_plan_version(request, version_id)
    actor = get_context().actor

    supplied = body.model_fields_set
    item = await update_item(
        get_session(request),
        tenant_id=actor.require_tenant(),
        item_id=item_id,
        expected_row_version=expected,
        **{name: getattr(body, name) for name in supplied},
    )

    record_audit(request, resource_id=item.id, changed_fields=sorted(supplied))
    response.headers["ETag"] = etag_for_row_version(expected + 1)
    return ItemResponse.model_validate(item, from_attributes=True)


@item_router.delete(
    "/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Take an item out of a slot",
    operation_id="planItemRemove",
)
@requires(NUTRITION_PLANS_WRITE)
async def delete_item(request: Request, item_id: uuid.UUID, if_match: IfMatch = None) -> Response:
    """🔒 Refused at the database for a non-draft version — see :func:`delete_slot`."""
    expected = parse_if_match_row_version(if_match, _RESOURCE)
    version_id = await _version_id_for_item(request, item_id)
    await authorized_plan_version(request, version_id)
    actor = get_context().actor

    await remove_item(
        get_session(request),
        tenant_id=actor.require_tenant(),
        item_id=item_id,
        expected_row_version=expected,
    )

    record_audit(request, resource_id=item_id)
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"ETag": etag_for_row_version(expected + 1)},
    )
