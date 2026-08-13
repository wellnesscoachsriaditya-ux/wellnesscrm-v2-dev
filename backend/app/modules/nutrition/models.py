"""ORM models for the nutrition catalogue — DB §8.

🔒 These live in the module, not the kernel. ``nutrition`` owns these tables.
Curated records (`tenant_id IS NULL`) are readable by all tenants but only
writable by the migrator.
Custom records (`tenant_id = ...`) are isolated.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.kernel import Base
from app.kernel.clients import DietaryClass
from app.kernel.db import pg_enum


class FoodCategory(Base):
    __tablename__ = "food_categories"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        comment="🔒 NULL = platform-authored (curated).",
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_food_categories_tenant_name"),)


class Food(Base):
    __tablename__ = "foods"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    category_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("food_categories.id"), nullable=False
    )
    dietary_class: Mapped[DietaryClass] = mapped_column(
        pg_enum(DietaryClass, "dietary_classification"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', name)", persisted=True),
        nullable=True,
    )

    __table_args__ = (Index("ix_foods_search_vector", "search_vector", postgresql_using="gin"),)


class FoodAlias(Base):
    __tablename__ = "food_aliases"

    food_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("foods.id"), primary_key=True
    )
    alias: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("tenants.id"))


class Nutrient(Base):
    """A nutrient definition (e.g. Energy, Protein). Strictly Pattern D (platform only)."""

    __tablename__ = "nutrients"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True, comment="e.g. ENERC_KCAL")
    name: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False, comment="e.g. kcal, g, mg")


class FoodNutrient(Base):
    __tablename__ = "food_nutrients"

    food_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("foods.id"), primary_key=True
    )
    nutrient_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("nutrients.id"), primary_key=True
    )
    tenant_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("tenants.id"))
    amount_per_100g: Mapped[Decimal] = mapped_column(Numeric, nullable=False)


class MeasureUnit(Base):
    """Household measures (e.g. katori, cup). Strictly Pattern D (platform only)."""

    __tablename__ = "measure_units"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)


class FoodPortion(Base):
    __tablename__ = "food_portions"

    food_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("foods.id"), primary_key=True
    )
    measure_unit_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("measure_units.id"), primary_key=True
    )
    tenant_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("tenants.id"))
    gram_weight: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)


class Recipe(Base):
    __tablename__ = "recipes"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    instructions: Mapped[str | None] = mapped_column(Text)
    yield_amount: Mapped[Decimal | None] = mapped_column(Numeric)
    yield_unit_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("measure_units.id")
    )


class RecipeItem(Base):
    __tablename__ = "recipe_items"

    recipe_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("recipes.id"), primary_key=True
    )
    food_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("foods.id"), primary_key=True
    )
    tenant_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("tenants.id"))
    quantity: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    measure_unit_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("measure_units.id"), nullable=False
    )


class Supplement(Base):
    __tablename__ = "supplements"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    instructions: Mapped[str | None] = mapped_column(Text)


class DietaryRule(Base):
    __tablename__ = "dietary_rules"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("tenants.id"))
    condition: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)


# ─── Pattern A (Tenant Scoped) ─────────────────────────────────────────────


class Meal(Base):
    __tablename__ = "meals"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)


class MealItem(Base):
    __tablename__ = "meal_items"

    meal_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("meals.id"), primary_key=True
    )
    food_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("foods.id"), primary_key=True
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    measure_unit_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("measure_units.id"), nullable=False
    )


class NutritionTarget(Base):
    __tablename__ = "nutrition_targets"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    client_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    energy_kcal: Mapped[Decimal | None] = mapped_column(Numeric)
    protein_g: Mapped[Decimal | None] = mapped_column(Numeric)
    carbs_g: Mapped[Decimal | None] = mapped_column(Numeric)
    fat_g: Mapped[Decimal | None] = mapped_column(Numeric)


class FoodSearchMiss(Base):
    __tablename__ = "food_search_misses"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    query: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
