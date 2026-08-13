"""Tests for the nutrition catalogue and search API (M4 Slice 1.2)."""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.kernel.clients import DietaryClass
from app.modules.nutrition.catalogue import (
    create_custom_food,
    record_search_miss,
    search_foods,
)
from app.modules.nutrition.models import Food, FoodCategory, FoodSearchMiss
from tests.integration.conftest import scope_to

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def food_category_id(migrator_engine: AsyncEngine) -> uuid.UUID:
    """A test category seeded as the owner (tenant_id = NULL allowed)."""
    category_id = uuid.uuid4()
    cat = FoodCategory(
        id=category_id,
        tenant_id=None,
        name="Test Category",
    )
    async with async_sessionmaker(migrator_engine)() as session:
        session.add(cat)
        await session.commit()
    return category_id


@pytest_asyncio.fixture
async def mock_foods(migrator_engine: AsyncEngine, food_category_id: uuid.UUID) -> list[Food]:
    """Some sample foods for search, seeded globally."""
    foods = [
        Food(
            tenant_id=None,
            name="Apple",
            category_id=food_category_id,
            dietary_class=DietaryClass.VEGAN,
        ),
        Food(
            tenant_id=None,
            name="Chicken Breast",
            category_id=food_category_id,
            dietary_class=DietaryClass.NON_VEGETARIAN,
        ),
        Food(
            tenant_id=None,
            name="Paneer Butter Masala",
            category_id=food_category_id,
            dietary_class=DietaryClass.VEGETARIAN,
        ),
    ]
    async with async_sessionmaker(migrator_engine)() as session:
        session.add_all(foods)
        await session.commit()
    return foods


async def test_search_foods_filters_dietary_class(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    mock_foods: list[Food],
) -> None:
    """AC-M4-009: Vegetarian search filters out non-vegetarian food."""
    tenant_id = seeded_tenants[0]

    async with async_sessionmaker(app_engine)() as session:
        await scope_to(await session.connection(), tenant_id)

        # Search for everything (using 'e' which matches Apple, Paneer, Chicken)
        foods = await search_foods(
            session,
            tenant_id=tenant_id,
            query="e",
            dietary_class=DietaryClass.VEGETARIAN,
        )

        names = {f.name for f in foods}

        # Should find Paneer and Apple (since vegan is a subset of vegetarian),
        # but strictly NOT Chicken.
        assert "Paneer Butter Masala" in names or "Apple" in names
        assert "Chicken Breast" not in names


async def test_search_foods_miss_recording(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
) -> None:
    """FR-M4-014: Failed searches are recorded for telemetry."""
    tenant_id = seeded_tenants[0]

    async with async_sessionmaker(app_engine)() as session:
        await scope_to(await session.connection(), tenant_id)

        await record_search_miss(
            session,
            tenant_id=tenant_id,
            query="Supercalifragilisticexpialidocious",
        )

        misses = (
            (
                await session.execute(
                    select(FoodSearchMiss).where(
                        FoodSearchMiss.query == "Supercalifragilisticexpialidocious"
                    )
                )
            )
            .scalars()
            .all()
        )

        assert len(misses) == 1
        assert misses[0].query == "Supercalifragilisticexpialidocious"


async def test_create_custom_food(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    food_category_id: uuid.UUID,
) -> None:
    """AC-M4-004: Inline custom food creation."""
    tenant_id = seeded_tenants[0]

    async with async_sessionmaker(app_engine)() as session:
        await scope_to(await session.connection(), tenant_id)

        food = await create_custom_food(
            session,
            tenant_id=tenant_id,
            name="My Custom Protein Bar",
            category_id=food_category_id,
            dietary_class=DietaryClass.VEGETARIAN,
        )

        assert food.name == "My Custom Protein Bar"
        assert food.tenant_id == tenant_id
        assert food.dietary_class == DietaryClass.VEGETARIAN

        # Verify it can be searched
        foods = await search_foods(
            session,
            tenant_id=tenant_id,
            query="Protein Bar",
        )
        assert any(f.id == food.id for f in foods)
