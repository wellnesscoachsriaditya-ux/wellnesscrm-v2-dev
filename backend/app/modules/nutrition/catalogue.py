"""Nutrition catalogue management — FR-M4-008..014, AC-M4-003, AC-M4-004.

🔒 Uses the Pattern B RLS enabled on `foods` to guarantee isolation of custom
foods while maintaining a unified search interface.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.clients import DietaryClass
from app.kernel.errors import ValidationError
from app.modules.nutrition.models import (
    Food,
    FoodAlias,
    FoodPortion,
    FoodSearchMiss,
    MeasureUnit,
)


async def list_food_portions(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    food_id: uuid.UUID,
) -> Sequence[tuple[uuid.UUID, str, Decimal, bool]]:
    """The household measures a food can be entered in — FR-M4-011.

    🔒 Read under the same Pattern B RLS as :func:`search_foods`, so a tenant's
    custom food's portions are visible to that tenant and the curated ones to
    everyone; ``measure_units`` is platform-only (Pattern D) and joins freely.
    The default measure sorts first, which is the one the builder pre-selects.
    """
    stmt = (
        select(
            FoodPortion.measure_unit_id,
            MeasureUnit.name,
            FoodPortion.gram_weight,
            FoodPortion.is_default,
        )
        .join(MeasureUnit, MeasureUnit.id == FoodPortion.measure_unit_id)
        .where(FoodPortion.food_id == food_id)
        .order_by(FoodPortion.is_default.desc(), MeasureUnit.name)
    )
    result = await session.execute(stmt)
    return [tuple(row) for row in result.all()]


async def search_foods(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    query: str,
    dietary_class: DietaryClass | None = None,
) -> Sequence[Food]:
    """Search for foods matching the query.

    Matches against both the food name and its regional aliases (FR-M4-009).
    Respects dietary constraints (e.g. vegeterian sees no non-veg) (FR-M4-035, AC-M4-009).
    """
    q = query.strip()
    if not q:
        return []

    stmt = select(Food).outerjoin(FoodAlias, FoodAlias.food_id == Food.id)

    # Full text search on name or ILIKE on name/alias
    # We use both to capture typos and partial words
    fts_condition = Food.search_vector.op("@@")(func.websearch_to_tsquery("english", q))
    ilike_condition = Food.name.ilike(f"%{q}%")
    alias_condition = FoodAlias.alias.ilike(f"%{q}%")

    stmt = stmt.where(or_(fts_condition, ilike_condition, alias_condition))

    if dietary_class:
        # DB §8.5, FR-M4-035: Filter by dietary class
        if dietary_class == DietaryClass.VEGETARIAN:
            stmt = stmt.where(Food.dietary_class != DietaryClass.NON_VEGETARIAN)
        elif dietary_class == DietaryClass.VEGAN:
            stmt = stmt.where(Food.dietary_class == DietaryClass.VEGAN)
        elif dietary_class == DietaryClass.JAIN:
            # Jain excludes root veg, which ideally is tagged, but minimally must be Jain.
            stmt = stmt.where(Food.dietary_class == DietaryClass.JAIN)
        elif dietary_class == DietaryClass.EGGETARIAN:
            stmt = stmt.where(
                Food.dietary_class != DietaryClass.NON_VEGETARIAN
            )  # Eggetarian can eat veg + egg

    # RLS guarantees tenant isolation automatically.
    # Group by id to avoid duplicates from alias joins
    stmt = stmt.group_by(Food.id).order_by(Food.name).limit(50)

    result = await session.execute(stmt)
    return result.scalars().all()


async def record_search_miss(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    query: str,
) -> None:
    """Record a failed search for telemetry (FR-M4-014)."""
    q = query.strip()
    if not q:
        return

    miss = FoodSearchMiss(tenant_id=tenant_id, query=q)
    session.add(miss)
    await session.flush()


async def create_custom_food(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    category_id: uuid.UUID,
    dietary_class: DietaryClass,
) -> Food:
    """Create a tenant-private custom food (FR-M4-012, FR-M4-013)."""
    clean_name = name.strip()
    if not clean_name:
        raise ValidationError(
            "Food name cannot be empty.",
            action="Provide a name for the custom food.",
        )

    # AC-M4-004: Created inline and immediately usable.
    food = Food(
        tenant_id=tenant_id,
        name=clean_name,
        category_id=category_id,
        dietary_class=dietary_class,
    )
    session.add(food)
    await session.flush()
    return food
