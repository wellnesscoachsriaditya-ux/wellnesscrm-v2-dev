"""Nutrition HTTP router — FR-M4-008..014.

🔒 Connects the frontend to the nutrition module under the platform realm.
All actions are authorized against the tenant boundaries automatically by
the core middleware, but we must use `authorized_client` when operating on client-specific objects.
Here we are dealing with tenant-wide or platform-wide catalogues, so we only need the tenant ID.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Body, Query, Request
from pydantic import BaseModel

from app.kernel.clients import DietaryClass
from app.kernel.context import get_context
from app.modules.nutrition import (
    NUTRITION_CATALOGUE_READ,
    NUTRITION_CATALOGUE_WRITE,
    create_custom_food as module_create_custom_food,
    search_foods as module_search_foods,
    record_search_miss as module_record_search_miss,
)
from app.platform.http.authz import requires
from app.platform.http.pipeline import get_session, realm_router

router = realm_router("/api/v1/app/nutrition", tags=["nutrition"])


class FoodItemResponse(BaseModel):
    id: uuid.UUID
    name: str
    category_id: uuid.UUID
    dietary_class: DietaryClass
    tenant_id: uuid.UUID | None

    model_config = {"from_attributes": True}


class CreateCustomFoodRequest(BaseModel):
    name: str
    category_id: uuid.UUID
    dietary_class: DietaryClass


@router.get("/foods", response_model=list[FoodItemResponse], operation_id="searchFoods")
@requires(NUTRITION_CATALOGUE_READ)
async def search_foods(
    request: Request,
    q: str = Query(..., min_length=1),
    dietary_class: DietaryClass | None = None,
) -> Any:
    """Search the food catalogue (FR-M4-008, AC-M4-003, AC-M4-009)."""
    session = get_session(request)
    tenant_id = get_context().actor.require_tenant()

    foods = await module_search_foods(
        session,
        tenant_id=tenant_id,
        query=q,
        dietary_class=dietary_class,
    )

    if not foods:
        await module_record_search_miss(session, tenant_id=tenant_id, query=q)

    return foods


@router.post("/foods", response_model=FoodItemResponse, operation_id="createCustomFood")
@requires(NUTRITION_CATALOGUE_WRITE)
async def create_custom_food(
    request: Request,
    payload: CreateCustomFoodRequest = Body(...),
) -> Any:
    """Create a new custom food for this tenant (FR-M4-012, FR-M4-013)."""
    session = get_session(request)
    tenant_id = get_context().actor.require_tenant()

    food = await module_create_custom_food(
        session,
        tenant_id=tenant_id,
        name=payload.name,
        category_id=payload.category_id,
        dietary_class=payload.dietary_class,
    )
    await session.commit()
    return food
