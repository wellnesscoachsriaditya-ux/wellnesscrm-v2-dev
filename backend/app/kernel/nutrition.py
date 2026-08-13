"""Nutrition Kernel — pure functions for portion conversion and macro composition.

🔒 This is the single source of truth for all nutritional calculations (FR-M4-004, NFR-072).
Duplicating this logic in the application layer or frontend is an architectural violation.
"""

from decimal import Decimal
from enum import Enum
from typing import TypedDict
from uuid import UUID


class PlanState(str, Enum):
    draft = "draft"
    issued = "issued"
    superseded = "superseded"
    discarded = "discarded"


class PlanOrigin(str, Enum):
    manual = "manual"
    template = "template"
    ai_draft = "ai_draft"
    revision = "revision"


class RenderStatus(str, Enum):
    pending = "pending"
    ready = "ready"
    failed = "failed"


class MacroTotals(TypedDict):
    """The standard four macros calculated across the system."""

    energy_kcal: Decimal
    protein_g: Decimal
    carbs_g: Decimal
    fat_g: Decimal
    fibre_g: Decimal


class FoodNutrientValue(TypedDict):
    nutrient_code: str
    amount_per_100g: Decimal


class FoodPortionValue(TypedDict):
    measure_unit_id: UUID
    gram_weight: Decimal


class PortionConversionError(ValueError):
    """Raised when a portion cannot be converted to grams."""

    pass


class LockViolationError(ValueError):
    """Raised when an operation attempts to alter a locked item or breaks lock constraints."""

    pass


class RecalculationItem(TypedDict):
    item_id: UUID
    is_locked: bool
    quantity: Decimal
    energy_kcal: Decimal


# A sentinel UUID for "grams" to allow fallback.
# In a real system, this might be queried from the DB, but for pure math
# it's common to treat grams intrinsically or pass its UUID.
# We will assume callers can identify if the unit is grams.
GRAM_UNIT_NAME = "g"


def convert_to_grams(
    quantity: Decimal,
    measure_unit_id: UUID,
    measure_unit_name: str,
    available_portions: list[FoodPortionValue],
) -> Decimal:
    """Convert a quantity of a specific household measure into grams.

    Args:
        quantity: The amount of the measure (e.g., 1.5).
        measure_unit_id: The UUID of the measure unit.
        measure_unit_name: The name of the measure unit (e.g. 'katori', 'g').
        available_portions: The list of portions defined for the specific food.

    Returns:
        The equivalent weight in grams.

    Raises:
        PortionConversionError: If the unit is not grams and not in available_portions.
    """
    if measure_unit_name.lower() in ("g", "gram", "grams"):
        return quantity

    for portion in available_portions:
        if portion["measure_unit_id"] == measure_unit_id:
            return quantity * portion["gram_weight"]

    raise PortionConversionError(
        f"Cannot convert {quantity} of unit {measure_unit_name} to grams. "
        "No matching portion found for this food."
    )


def calculate_composition(
    quantity_g: Decimal,
    nutrients: list[FoodNutrientValue],
) -> MacroTotals:
    """Calculate the macro composition for a given gram weight of a food.

    Args:
        quantity_g: The weight of the food in grams.
        nutrients: The list of nutrient values per 100g for the food.

    Returns:
        A dictionary containing the scaled macro values.
    """
    multiplier = quantity_g / Decimal("100")

    totals: MacroTotals = {
        "energy_kcal": Decimal("0"),
        "protein_g": Decimal("0"),
        "carbs_g": Decimal("0"),
        "fat_g": Decimal("0"),
        "fibre_g": Decimal("0"),
    }

    # IFCT 2017 nutrient codes (hypothetical standard we map to)
    code_map = {
        "ENERC_KCAL": "energy_kcal",
        "PROCNT": "protein_g",
        "CHOCDF": "carbs_g",
        "FAT": "fat_g",
        "FIBTG": "fibre_g",
    }

    for nutrient in nutrients:
        code = nutrient["nutrient_code"].upper()
        if code in code_map:
            field_name = code_map[code]
            value = nutrient["amount_per_100g"] * multiplier
            # We keep precision here; rounding is a presentation concern.
            totals[field_name] += value  # type: ignore

    return totals


def calculate_meal_composition(
    items: list[tuple[Decimal, list[FoodNutrientValue]]],
) -> MacroTotals:
    """Calculate the macro composition for a meal consisting of multiple items.

    Args:
        items: A list of tuples, each containing the gram weight of a food
               and its associated list of FoodNutrientValues.

    Returns:
        The summed MacroTotals for the entire meal.
    """
    totals: MacroTotals = {
        "energy_kcal": Decimal("0"),
        "protein_g": Decimal("0"),
        "carbs_g": Decimal("0"),
        "fat_g": Decimal("0"),
        "fibre_g": Decimal("0"),
    }

    for quantity_g, nutrients in items:
        item_totals = calculate_composition(quantity_g, nutrients)
        totals["energy_kcal"] += item_totals["energy_kcal"]
        totals["protein_g"] += item_totals["protein_g"]
        totals["carbs_g"] += item_totals["carbs_g"]
        totals["fat_g"] += item_totals["fat_g"]
        totals["fibre_g"] += item_totals["fibre_g"]

    return totals


def recalculate_quantities(
    target_kcal: Decimal, items: list[RecalculationItem]
) -> dict[UUID, Decimal]:
    """Recalculate quantities for unlocked items to hit the target energy budget.

    Args:
        target_kcal: The target energy budget in kcal.
        items: A list of items in the plan, containing their current quantities and energy_kcal.

    Returns:
        A dictionary mapping item_id to their newly computed quantity.
    """
    locked_consumed = sum(
        (item["energy_kcal"] for item in items if item["is_locked"]), Decimal("0")
    )
    remaining_available = target_kcal - locked_consumed

    unlocked_items = [item for item in items if not item["is_locked"]]

    if not unlocked_items:
        return {item["item_id"]: item["quantity"] for item in items}

    unlocked_current = sum((item["energy_kcal"] for item in unlocked_items), Decimal("0"))

    if unlocked_current <= 0:
        # Cannot scale proportionally if current sum is zero (or negative).
        return {item["item_id"]: item["quantity"] for item in items}

    multiplier = remaining_available / unlocked_current

    result = {}
    for item in items:
        if item["is_locked"]:
            result[item["item_id"]] = item["quantity"]
        else:
            result[item["item_id"]] = item["quantity"] * multiplier

    return result
