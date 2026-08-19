"""Resolving a plan version — the one place a plan's nutrition is worked out.

🔒 **ADR-07 / DB §8.12.** A *draft* is computed on read, from a live join to
``food_portions`` and ``food_nutrients``, because FR-M4-027 wants running totals
that move as the practitioner edits. An *issued* version reads the grams frozen
at issue, because EC-M4-03 requires a plan to keep the values that were in force
when the client was given it.

🔒 **Nothing here does arithmetic.** Every gram, every macro, every budget figure
comes from ``kernel.nutrition``. This module's whole job is to get the right rows
out of the database in a bounded number of queries and hand them to the kernel —
NFR-072 has one implementation of the maths, and this is not it.

⚠️ **Query count is constant, not per item.** Three statements, whatever the plan
contains: the structure, the nutrients, the portions. DB §18 flags ``plan_items``
aggregation during authoring as a scaling concern (~2M rows, the largest domain
table), and the plan builder re-reads the whole aggregate on every edit. A
traversal that loaded a food's nutrients per item would be forty round trips per
keystroke-driven refresh.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.errors import NotFoundError
from app.kernel.nutrition import (
    FoodNutrientValue,
    FoodPortionValue,
    MacroTotals,
    NutritionBudget,
    PlanItemNutrition,
    PlanState,
    PlanWarning,
    PortionConversionError,
    WarningScope,
    add_totals,
    budget_warnings,
    calculate_composition,
    compute_budget,
    convert_to_grams,
    format_measure,
    is_effectively_locked,
    sum_totals,
    zero_totals,
)
from app.modules.nutrition.models import (
    DietPlanVersion,
    Food,
    FoodNutrient,
    FoodPortion,
    MealItem,
    MeasureUnit,
    Nutrient,
    PlanDay,
    PlanItem,
    PlanSlot,
)

#: The three things a ``plan_items`` row may point at (``ck_plan_items__one_reference``
#: guarantees exactly one is set).
ITEM_TYPE_FOOD = "food"
ITEM_TYPE_RECIPE = "recipe"
ITEM_TYPE_MEAL = "meal"


@dataclass(frozen=True, slots=True)
class ResolvedItem:
    """One plan item with everything the API and the PDF need, server-computed."""

    id: uuid.UUID
    item_type: str
    food_id: uuid.UUID | None
    recipe_id: uuid.UUID | None
    meal_id: uuid.UUID | None
    display_name: str
    quantity: Decimal
    measure_unit_id: uuid.UUID
    measure_unit_code: str
    #: 🔒 "1 katori" — rendered server-side (API §8.3) so the formatting lives in
    #: one place and two clients cannot disagree about the same plan.
    measure_display: str
    resolved_grams: Decimal | None
    nutrition: MacroTotals
    #: 🔒 The **effective** lock: an item in a locked slot is locked.
    is_locked: bool
    #: The item's own flag, so a round-trip through the API does not silently
    #: promote a slot lock into an item lock.
    item_is_locked: bool
    notes: str | None
    client_note: str | None
    sort_order: int


@dataclass(frozen=True, slots=True)
class ResolvedSlot:
    id: uuid.UUID
    slot_type: str
    custom_label: str | None
    target_time: str | None
    sort_order: int
    is_locked: bool
    items: list[ResolvedItem] = field(default_factory=list)
    slot_totals: MacroTotals = field(default_factory=zero_totals)


@dataclass(frozen=True, slots=True)
class ResolvedDay:
    id: uuid.UUID
    day_number: int
    label: str
    slots: list[ResolvedSlot] = field(default_factory=list)
    day_totals: MacroTotals = field(default_factory=zero_totals)


@dataclass(frozen=True, slots=True)
class ResolvedPlanVersion:
    """A whole plan version, ready to serialise — API §8.3's shape."""

    version: DietPlanVersion
    days: list[ResolvedDay]
    plan_totals: MacroTotals
    budget: NutritionBudget
    warnings: list[PlanWarning]


# ─── Loading ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Catalogue:
    """Everything the referenced foods contribute, keyed for O(1) lookup."""

    nutrients: dict[uuid.UUID, list[FoodNutrientValue]]
    portions: dict[uuid.UUID, list[FoodPortionValue]]
    food_names: dict[uuid.UUID, str]
    unit_names: dict[uuid.UUID, str]
    #: Expanded ``meal_items`` for any item pointing at a meal (FR-M4-018/019).
    meal_components: dict[uuid.UUID, list[tuple[uuid.UUID, Decimal, uuid.UUID]]]


async def _load_structure(
    session: AsyncSession, *, tenant_id: uuid.UUID, version_id: uuid.UUID
) -> tuple[list[PlanDay], list[PlanSlot], list[PlanItem]]:
    """Statement 1 — the day/slot/item graph, in render order.

    ⚠️ Three ``select()`` calls against one round trip each rather than a single
    outer-joined statement. The join would return one row per item with the day
    and slot columns repeated, and reassembling that is more code and more
    allocation than three flat reads of tables that are already indexed for
    exactly these predicates (``ix_plan_days__version``, ``ix_plan_slots__day``,
    ``ix_plan_items__slot``). Either way the count does not grow with the plan,
    which is the property that matters.
    """
    days = list(
        (
            await session.execute(
                select(PlanDay)
                .where(PlanDay.tenant_id == tenant_id, PlanDay.plan_version_id == version_id)
                .order_by(PlanDay.day_number)
            )
        )
        .scalars()
        .all()
    )
    if not days:
        return [], [], []

    day_ids = [day.id for day in days]
    slots = list(
        (
            await session.execute(
                select(PlanSlot)
                .where(PlanSlot.tenant_id == tenant_id, PlanSlot.plan_day_id.in_(day_ids))
                .order_by(PlanSlot.sort_order)
            )
        )
        .scalars()
        .all()
    )
    if not slots:
        return days, [], []

    slot_ids = [slot.id for slot in slots]
    items = list(
        (
            await session.execute(
                select(PlanItem)
                .where(PlanItem.tenant_id == tenant_id, PlanItem.plan_slot_id.in_(slot_ids))
                .order_by(PlanItem.sort_order)
            )
        )
        .scalars()
        .all()
    )
    return days, slots, items


async def _load_catalogue(session: AsyncSession, items: Sequence[PlanItem]) -> _Catalogue:
    """Statements 2 and 3 — everything the referenced foods contribute.

    🔒 **RLS does the isolation, and its answer is authoritative.** ``foods`` and
    its satellites are Pattern B (DB §17.1): curated rows (``tenant_id IS NULL``)
    are visible to everyone, another tenant's custom food is visible to nobody.
    A plan item pointing at a food this tenant cannot see therefore comes back
    with no name, no nutrients and no portions — which
    :func:`_resolve_item` turns into a warning rather than a crash.
    """
    meal_ids = {item.meal_id for item in items if item.meal_id is not None}

    meal_components: dict[uuid.UUID, list[tuple[uuid.UUID, Decimal, uuid.UUID]]] = {}
    if meal_ids:
        rows = await session.execute(
            select(MealItem.meal_id, MealItem.food_id, MealItem.quantity, MealItem.measure_unit_id)
            .where(MealItem.meal_id.in_(meal_ids))
            .order_by(MealItem.meal_id)
        )
        for meal_id, food_id, quantity, unit_id in rows:
            meal_components.setdefault(meal_id, []).append((food_id, quantity, unit_id))

    # Every food whose values are needed: the ones named directly by an item,
    # plus the ones inside a meal an item points at.
    food_ids = {item.food_id for item in items if item.food_id is not None}
    for components in meal_components.values():
        food_ids.update(food_id for food_id, _, _ in components)

    unit_ids = {item.measure_unit_id for item in items}
    for components in meal_components.values():
        unit_ids.update(unit_id for _, _, unit_id in components)

    nutrients: dict[uuid.UUID, list[FoodNutrientValue]] = {}
    portions: dict[uuid.UUID, list[FoodPortionValue]] = {}
    food_names: dict[uuid.UUID, str] = {}

    if food_ids:
        nutrient_rows = await session.execute(
            select(FoodNutrient.food_id, Nutrient.code, FoodNutrient.amount_per_100g)
            .join(Nutrient, Nutrient.id == FoodNutrient.nutrient_id)
            .where(FoodNutrient.food_id.in_(food_ids))
        )
        for food_id, code, amount in nutrient_rows:
            nutrients.setdefault(food_id, []).append(
                {"nutrient_code": code, "amount_per_100g": amount}
            )

        portion_rows = await session.execute(
            select(FoodPortion.food_id, FoodPortion.measure_unit_id, FoodPortion.gram_weight).where(
                FoodPortion.food_id.in_(food_ids)
            )
        )
        for food_id, unit_id, gram_weight in portion_rows:
            portions.setdefault(food_id, []).append(
                {"measure_unit_id": unit_id, "gram_weight": gram_weight}
            )

        name_rows = await session.execute(select(Food.id, Food.name).where(Food.id.in_(food_ids)))
        food_names = dict(name_rows.all())  # type: ignore[arg-type]

    unit_names: dict[uuid.UUID, str] = {}
    if unit_ids:
        unit_rows = await session.execute(
            select(MeasureUnit.id, MeasureUnit.name).where(MeasureUnit.id.in_(unit_ids))
        )
        unit_names = dict(unit_rows.all())  # type: ignore[arg-type]

    return _Catalogue(
        nutrients=nutrients,
        portions=portions,
        food_names=food_names,
        unit_names=unit_names,
        meal_components=meal_components,
    )


# ─── Resolving one item ──────────────────────────────────────────────────


def _resolve_item(
    item: PlanItem,
    *,
    catalogue: _Catalogue,
    slot_is_locked: bool,
    freeze_grams: bool,
    warnings: list[PlanWarning],
) -> ResolvedItem:
    """Turn one row into what the API returns.

    ``freeze_grams`` is DB §8.12's split: an issued version uses the grams
    written at issue and never re-derives them, so a later correction to a
    curated food's portion weight cannot change what the client was told
    (EC-M4-03).
    """
    unit_name = catalogue.unit_names.get(item.measure_unit_id, "")
    effective_lock = is_effectively_locked(
        item_is_locked=item.is_locked, slot_is_locked=slot_is_locked
    )

    grams, nutrition, display_name = _resolve_reference(
        item, catalogue=catalogue, freeze_grams=freeze_grams, unit_name=unit_name, warnings=warnings
    )

    return ResolvedItem(
        id=item.id,
        item_type=item.item_type,
        food_id=item.food_id,
        recipe_id=item.recipe_id,
        meal_id=item.meal_id,
        display_name=display_name,
        quantity=item.quantity,
        measure_unit_id=item.measure_unit_id,
        measure_unit_code=unit_name,
        measure_display=format_measure(item.quantity, unit_name) if unit_name else "",
        resolved_grams=grams,
        nutrition=nutrition,
        is_locked=effective_lock,
        item_is_locked=item.is_locked,
        notes=item.notes,
        client_note=item.client_note,
        sort_order=item.sort_order,
    )


def _resolve_reference(
    item: PlanItem,
    *,
    catalogue: _Catalogue,
    freeze_grams: bool,
    unit_name: str,
    warnings: list[PlanWarning],
) -> tuple[Decimal | None, MacroTotals, str]:
    """Grams, macros and a display name for whichever of the three an item names."""
    scope: WarningScope = {"type": "item", "item_id": str(item.id)}

    if item.food_id is not None:
        name = catalogue.food_names.get(item.food_id)
        if name is None:
            # 🔒 Invisible by RLS — another tenant's custom food, or one retired
            # from this tenant's view. A plan that will not open is worse than a
            # plan with one line the practitioner is told about.
            warnings.append(
                {
                    "rule_code": "item_food_unavailable",
                    "severity": "soft",
                    "message": "A food in this plan is no longer available and shows no nutrition.",
                    "scope": scope,
                }
            )
            return None, zero_totals(), "Unavailable food"

        grams = _grams_for(
            item.quantity,
            item.measure_unit_id,
            unit_name,
            catalogue.portions.get(item.food_id, []),
            frozen=item.resolved_grams if freeze_grams else None,
            warnings=warnings,
            scope=scope,
            label=name,
        )
        if grams is None:
            return None, zero_totals(), name
        return grams, calculate_composition(grams, catalogue.nutrients.get(item.food_id, [])), name

    if item.meal_id is not None:
        return _resolve_meal(item, catalogue=catalogue, warnings=warnings, scope=scope)

    # ⏳ A recipe's per-serving derivation depends on `recipes.yield_amount` and
    # `yield_unit_id`, and FR-M4-022 puts practitioner-authored recipes with a
    # yield in **Phase 2**. Resolving one now would mean inventing the conversion
    # from "1 katori of this recipe" to grams, and a guessed clinical figure is
    # worse than an absent one.
    warnings.append(
        {
            "rule_code": "recipe_nutrition_unavailable",
            "severity": "soft",
            "message": "Recipe nutrition is not calculated yet; this item counts as zero.",
            "scope": scope,
        }
    )
    return None, zero_totals(), "Recipe"


def _resolve_meal(
    item: PlanItem,
    *,
    catalogue: _Catalogue,
    warnings: list[PlanWarning],
    scope: WarningScope,
) -> tuple[Decimal | None, MacroTotals, str]:
    """A meal is its constituents — FR-M4-019, nutrition derived, never stored.

    ⚠️ ``plan_items.quantity`` is treated as a **multiplier of one serving of the
    meal**. ``meals`` carries no yield column (DB §8.8 specifies more than
    migration 0017 built), so there is nothing else it could mean, and saying so
    here is cheaper than leaving the next reader to infer it.
    """
    assert item.meal_id is not None
    components = catalogue.meal_components.get(item.meal_id, [])
    if not components:
        warnings.append(
            {
                "rule_code": "meal_empty",
                "severity": "soft",
                "message": "A meal in this plan has no foods in it and counts as zero.",
                "scope": scope,
            }
        )
        return None, zero_totals(), "Meal"

    total_grams = Decimal("0")
    totals = zero_totals()
    for food_id, component_quantity, unit_id in components:
        unit_name = catalogue.unit_names.get(unit_id, "")
        grams = _grams_for(
            component_quantity * item.quantity,
            unit_id,
            unit_name,
            catalogue.portions.get(food_id, []),
            frozen=None,
            warnings=warnings,
            scope=scope,
            label=catalogue.food_names.get(food_id, "a food"),
        )
        if grams is None:
            continue
        total_grams += grams
        totals = add_totals(
            totals, calculate_composition(grams, catalogue.nutrients.get(food_id, []))
        )

    return total_grams, totals, "Meal"


def _grams_for(
    quantity: Decimal,
    measure_unit_id: uuid.UUID,
    unit_name: str,
    portions: list[FoodPortionValue],
    *,
    frozen: Decimal | None,
    warnings: list[PlanWarning],
    scope: WarningScope,
    label: str,
) -> Decimal | None:
    """Grams for one quantity, or ``None`` with a warning if it cannot be resolved.

    🔒 DB §8.9 — a food with no portion row is expressible in grams and nothing
    else. That is a curation gap, not a client error, so it degrades to a warning
    instead of a 500 and the rest of the plan still renders.
    """
    if frozen is not None:
        return frozen
    try:
        return convert_to_grams(quantity, measure_unit_id, unit_name, portions)
    except PortionConversionError:
        warnings.append(
            {
                "rule_code": "unresolvable_portion",
                "severity": "soft",
                "message": (
                    f"{label} has no gram weight for “{unit_name or 'this measure'}”, "
                    "so it is not counted in the totals."
                ),
                "scope": scope,
            }
        )
        return None


# ─── The entry point ─────────────────────────────────────────────────────


async def resolve_plan_version(
    session: AsyncSession, *, tenant_id: uuid.UUID, version_id: uuid.UUID
) -> ResolvedPlanVersion:
    """Load a plan version and compute everything the API §8.3 response carries.

    Args:
        session: The request's transaction. Never committed here (ADR-04).
        tenant_id: The caller's tenant. 🔒 Also a predicate on every query —
            RLS is the guarantee, this is the part a reviewer can see.
        version_id: The version to resolve.

    Returns:
        The version, its day/slot/item tree with server-computed nutrition, the
        plan totals, the ADR-A07 budget and any soft warnings.

    Raises:
        NotFoundError: If the version does not exist, or belongs to another
            tenant. 🔒 The two are indistinguishable on purpose — API §5.4.
    """
    version = (
        await session.execute(
            select(DietPlanVersion).where(
                DietPlanVersion.id == version_id, DietPlanVersion.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()

    if version is None:
        raise NotFoundError(
            message="That plan version doesn't exist.",
            action="Go back to the plan and try again.",
        )

    # 🔒 DB §8.12 — an issued version keeps the grams it was issued with.
    freeze_grams = version.state is not PlanState.draft

    warnings: list[PlanWarning] = []
    days_rows, slot_rows, item_rows = await _load_structure(
        session, tenant_id=tenant_id, version_id=version_id
    )
    catalogue = await _load_catalogue(session, item_rows)

    items_by_slot: dict[uuid.UUID, list[PlanItem]] = {}
    for item in item_rows:
        items_by_slot.setdefault(item.plan_slot_id, []).append(item)

    slots_by_day: dict[uuid.UUID, list[PlanSlot]] = {}
    for slot in slot_rows:
        slots_by_day.setdefault(slot.plan_day_id, []).append(slot)

    days: list[ResolvedDay] = []
    budget_items: list[PlanItemNutrition] = []
    locked_slot_count = 0

    for day in days_rows:
        resolved_slots: list[ResolvedSlot] = []
        for slot in slots_by_day.get(day.id, []):
            if slot.is_locked:
                locked_slot_count += 1
            resolved_items = [
                _resolve_item(
                    item,
                    catalogue=catalogue,
                    slot_is_locked=slot.is_locked,
                    freeze_grams=freeze_grams,
                    warnings=warnings,
                )
                for item in items_by_slot.get(slot.id, [])
            ]
            budget_items.extend(
                {
                    "item_id": resolved.id,
                    "is_locked": resolved.is_locked,
                    "quantity": resolved.quantity,
                    "totals": resolved.nutrition,
                }
                for resolved in resolved_items
            )
            resolved_slots.append(
                ResolvedSlot(
                    id=slot.id,
                    slot_type=slot.slot_type,
                    custom_label=slot.custom_label,
                    target_time=slot.target_time,
                    sort_order=slot.sort_order,
                    is_locked=slot.is_locked,
                    items=resolved_items,
                    slot_totals=sum_totals(resolved.nutrition for resolved in resolved_items),
                )
            )
        days.append(
            ResolvedDay(
                id=day.id,
                day_number=day.day_number,
                label=day.label,
                slots=resolved_slots,
                day_totals=sum_totals(slot.slot_totals for slot in resolved_slots),
            )
        )

    plan_totals = sum_totals(day.day_totals for day in days)
    budget = compute_budget(_target_of(version), budget_items, locked_slot_count=locked_slot_count)
    warnings.extend(budget_warnings(budget))

    if freeze_grams:
        _warn_on_issued_drift(version, plan_totals, warnings)

    return ResolvedPlanVersion(
        version=version,
        days=days,
        plan_totals=plan_totals,
        budget=budget,
        warnings=warnings,
    )


def _target_of(version: DietPlanVersion) -> MacroTotals | None:
    """The version's own targets, or ``None`` when none were set.

    ⏳ Targets live on ``diet_plan_versions`` rather than being read from
    ``nutrition_targets``: that table is missing seven of the columns DB §8.16
    specifies, and completing it belongs with the catalogue work.
    """
    if version.target_energy_kcal is None and version.target_protein_g is None:
        return None
    return {
        "energy_kcal": version.target_energy_kcal or Decimal("0"),
        "protein_g": version.target_protein_g or Decimal("0"),
        "carbs_g": version.target_carbs_g or Decimal("0"),
        "fat_g": version.target_fat_g or Decimal("0"),
        "fibre_g": Decimal("0"),
    }


def _warn_on_issued_drift(
    version: DietPlanVersion, plan_totals: MacroTotals, warnings: list[PlanWarning]
) -> None:
    """🔒 EC-M4-03's tripwire, until the snapshot read lands.

    An issued version froze ``computed_totals`` and ``resolved_grams``. The grams
    are honoured above, but the per-item *macros* are still multiplied by
    whatever ``food_nutrients`` holds today, because the only place the macros
    were frozen is ``plan_snapshots.document`` — and that document is still the
    stub revision 1.3 shipped.

    So rather than quietly present a recomputed figure as the issued one, the
    two are compared and a disagreement is reported. A drift here means a
    curated food was corrected after this plan was issued (EC-M11-03), which is
    exactly the event EC-M4-03 exists to keep out of a client's record.

    ⏳ The slice that rewrites ``issue_plan_version`` replaces this with a read
    of the snapshot, and this function goes away with it.
    """
    frozen = version.computed_totals or {}
    frozen_energy = frozen.get("energy_kcal")
    if frozen_energy is None:
        return

    if Decimal(str(frozen_energy)) != plan_totals["energy_kcal"]:
        warnings.append(
            {
                "rule_code": "issued_totals_drifted",
                "severity": "soft",
                "message": (
                    "A food in this issued plan has been corrected since it was sent. "
                    "The client's copy is unchanged."
                ),
                "scope": {"type": "plan"},
            }
        )
