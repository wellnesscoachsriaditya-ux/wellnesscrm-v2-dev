"""ORM models for the nutrition catalogue — DB §8.

🔒 These live in the module, not the kernel. ``nutrition`` owns these tables.
Curated records (`tenant_id IS NULL`) are readable by all tenants but only
writable by the migrator.
Custom records (`tenant_id = ...`) are isolated.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.kernel import Base
from app.kernel.clients import DietaryClass
from app.kernel.db import pg_enum
from app.kernel.nutrition import PlanOrigin, PlanState, RenderStatus


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


class DietTemplate(Base):
    __tablename__ = "diet_templates"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    goal_type: Mapped[str | None] = mapped_column(Text)
    condition_tags: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    dietary_class: Mapped[DietaryClass | None] = mapped_column(
        pg_enum(DietaryClass, "dietary_classification")
    )
    target_energy_kcal: Mapped[Decimal | None] = mapped_column(Numeric)
    day_count: Mapped[int] = mapped_column(nullable=False)
    usage_count: Mapped[int] = mapped_column(server_default="0", nullable=False)
    created_by_user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TemplateDay(Base):
    __tablename__ = "template_days"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    template_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("diet_templates.id"), nullable=False
    )
    day_number: Mapped[int] = mapped_column(nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)


class TemplateSlot(Base):
    __tablename__ = "template_slots"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    template_day_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("template_days.id"), nullable=False
    )
    slot_type: Mapped[str] = mapped_column(Text, nullable=False)
    custom_label: Mapped[str | None] = mapped_column(Text)
    target_time: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(nullable=False)


class TemplateItem(Base):
    __tablename__ = "template_items"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    template_slot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("template_slots.id"), nullable=False
    )
    item_type: Mapped[str] = mapped_column(Text, nullable=False)
    food_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("foods.id"))
    recipe_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("recipes.id"))
    meal_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("meals.id"))
    quantity: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    measure_unit_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("measure_units.id"), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text)
    is_locked: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    sort_order: Mapped[int] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "template_slot_id", "sort_order", name="uq_template_items_order", deferrable=True
        ),
    )


class DietPlan(Base):
    __tablename__ = "diet_plans"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    client_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    goal_type: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    current_version_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("diet_plan_versions.id", use_alter=True, name="fk_diet_plans_current_version"),
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DietPlanVersion(Base):
    __tablename__ = "diet_plan_versions"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    plan_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("diet_plans.id"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(nullable=False)
    state: Mapped[PlanState] = mapped_column(pg_enum(PlanState, "plan_state"), nullable=False)
    origin: Mapped[PlanOrigin] = mapped_column(pg_enum(PlanOrigin, "plan_origin"), nullable=False)
    source_template_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("diet_templates.id")
    )
    source_version_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("diet_plan_versions.id")
    )
    ai_generation_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    target_energy_kcal: Mapped[Decimal | None] = mapped_column(Numeric)
    target_protein_g: Mapped[Decimal | None] = mapped_column(Numeric)
    target_carbs_g: Mapped[Decimal | None] = mapped_column(Numeric)
    target_fat_g: Mapped[Decimal | None] = mapped_column(Numeric)
    computed_totals: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issued_by_user_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    practitioner_notes: Mapped[str | None] = mapped_column(Text)
    row_version: Mapped[int] = mapped_column(server_default="1", nullable=False)

    __table_args__ = (
        UniqueConstraint("plan_id", "version_number", name="uq_diet_plan_versions__plan_version"),
        Index(
            "uq_diet_plan_versions__one_draft",
            "plan_id",
            unique=True,
            postgresql_where=text("state = 'draft'"),
        ),
    )


class PlanDay(Base):
    __tablename__ = "plan_days"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    plan_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("diet_plan_versions.id"), nullable=False
    )
    day_number: Mapped[int] = mapped_column(nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)


class PlanSlot(Base):
    __tablename__ = "plan_slots"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    plan_day_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("plan_days.id"), nullable=False
    )
    slot_type: Mapped[str] = mapped_column(Text, nullable=False)
    custom_label: Mapped[str | None] = mapped_column(Text)
    target_time: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(nullable=False)
    is_locked: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)


class PlanItem(Base):
    __tablename__ = "plan_items"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    plan_slot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("plan_slots.id"), nullable=False
    )
    item_type: Mapped[str] = mapped_column(Text, nullable=False)
    food_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("foods.id"))
    recipe_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("recipes.id"))
    meal_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("meals.id"))
    quantity: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    measure_unit_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("measure_units.id"), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text)
    client_note: Mapped[str | None] = mapped_column(Text)
    is_locked: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    resolved_grams: Mapped[Decimal | None] = mapped_column(Numeric)
    sort_order: Mapped[int] = mapped_column(nullable=False)


class PlanItemAlternative(Base):
    __tablename__ = "plan_item_alternatives"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    plan_item_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("plan_items.id"), nullable=False
    )
    item_type: Mapped[str] = mapped_column(Text, nullable=False)
    food_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("foods.id"))
    recipe_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("recipes.id"))
    meal_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("meals.id"))
    quantity: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    measure_unit_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("measure_units.id"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(nullable=False)


class PlanSnapshot(Base):
    __tablename__ = "plan_snapshots"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    plan_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("diet_plan_versions.id"), unique=True, nullable=False
    )
    document: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    document_schema_version: Mapped[int] = mapped_column(nullable=False)
    #: 🔒 DDR-12 — SHA-256 over the canonical document. The client portal compares
    #: it to decide whether its cached copy is still the plan that was issued
    #: (FR-M7-011, EC-M7-03). Computed by ``kernel.nutrition.snapshot_content_hash``,
    #: never by a caller, so two writers cannot disagree about what a plan hashes to.
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    pdf_file_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("files.id"))
    pdf_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pdf_status: Mapped[RenderStatus | None] = mapped_column(pg_enum(RenderStatus, "render_status"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class PlanSupplement(Base):
    __tablename__ = "plan_supplements"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    plan_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("diet_plan_versions.id"), nullable=False
    )
    supplement_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("supplements.id"), nullable=False
    )
    dosage: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False)
    frequency: Mapped[str] = mapped_column(Text, nullable=False)
    timing: Mapped[str | None] = mapped_column(Text)
    duration_days: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    #: 🔒 DB §8.10 — the practitioner has fixed this supplement. Recalculation and
    #: AI drafting exclude it from their mutable set, the same way they do a
    #: locked item or a locked slot.
    is_locked: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    sort_order: Mapped[int] = mapped_column(nullable=False)
