"""Tests for the nutrition kernel pure functions."""

from decimal import Decimal
from uuid import uuid4

import pytest

from app.kernel.nutrition import (
    FoodNutrientValue,
    FoodPortionValue,
    PortionConversionError,
    RecalculationItem,
    calculate_composition,
    calculate_meal_composition,
    convert_to_grams,
    recalculate_quantities,
)


def test_convert_to_grams_with_gram_unit():
    """Fallback to grams works natively without needing a portion definition."""
    unit_id = uuid4()
    assert convert_to_grams(
        quantity=Decimal("150"),
        measure_unit_id=unit_id,
        measure_unit_name="g",
        available_portions=[],
    ) == Decimal("150")


def test_convert_to_grams_with_household_measure():
    """Converts a standard katori to grams correctly."""
    katori_id = uuid4()
    portions: list[FoodPortionValue] = [
        {"measure_unit_id": katori_id, "gram_weight": Decimal("150.5")}
    ]

    # 2 katoris * 150.5g = 301g
    result = convert_to_grams(
        quantity=Decimal("2"),
        measure_unit_id=katori_id,
        measure_unit_name="Katori",
        available_portions=portions,
    )
    assert result == Decimal("301.0")


def test_convert_to_grams_missing_portion():
    """Raises PortionConversionError when the requested unit has no mapping."""
    cup_id = uuid4()
    spoon_id = uuid4()
    portions: list[FoodPortionValue] = [{"measure_unit_id": spoon_id, "gram_weight": Decimal("15")}]

    with pytest.raises(PortionConversionError, match="No matching portion found"):
        convert_to_grams(
            quantity=Decimal("1"),
            measure_unit_id=cup_id,
            measure_unit_name="Cup",
            available_portions=portions,
        )


def test_calculate_composition():
    """Validates macro calculation logic (AC-M4-006)."""
    nutrients: list[FoodNutrientValue] = [
        {"nutrient_code": "ENERC_KCAL", "amount_per_100g": Decimal("350")},
        {"nutrient_code": "PROCNT", "amount_per_100g": Decimal("10")},
        {"nutrient_code": "CHOCDF", "amount_per_100g": Decimal("75")},
        {"nutrient_code": "FAT", "amount_per_100g": Decimal("2.5")},
        {"nutrient_code": "FIBTG", "amount_per_100g": Decimal("4")},
        {"nutrient_code": "OTHER", "amount_per_100g": Decimal("99")},  # Ignored
    ]

    # 150g is a 1.5 multiplier
    result = calculate_composition(Decimal("150"), nutrients)

    assert result["energy_kcal"] == Decimal("525.0")
    assert result["protein_g"] == Decimal("15.0")
    assert result["carbs_g"] == Decimal("112.5")
    assert result["fat_g"] == Decimal("3.75")
    assert result["fibre_g"] == Decimal("6.0")


def test_calculate_meal_composition():
    """Validates summation across a multi-item meal."""
    wheat_nutrients: list[FoodNutrientValue] = [
        {"nutrient_code": "ENERC_KCAL", "amount_per_100g": Decimal("350")},
        {"nutrient_code": "PROCNT", "amount_per_100g": Decimal("10")},
    ]
    dal_nutrients: list[FoodNutrientValue] = [
        {"nutrient_code": "ENERC_KCAL", "amount_per_100g": Decimal("100")},
        {"nutrient_code": "PROCNT", "amount_per_100g": Decimal("5")},
    ]

    # 2 chapatis (70g) + 1 katori dal (150g)
    items = [
        (Decimal("70"), wheat_nutrients),
        (Decimal("150"), dal_nutrients),
    ]

    result = calculate_meal_composition(items)

    # Wheat: 350 * 0.7 = 245 kcal, 10 * 0.7 = 7g protein
    # Dal: 100 * 1.5 = 150 kcal, 5 * 1.5 = 7.5g protein
    # Total: 395 kcal, 14.5g protein
    assert result["energy_kcal"] == Decimal("395.0")
    assert result["protein_g"] == Decimal("14.5")
    assert result["carbs_g"] == Decimal("0")  # Default zero when omitted


def test_recalculate_quantities_basic():
    """Validates proportional redistribution with locked and unlocked items."""
    item1 = uuid4()
    item2 = uuid4()
    item3 = uuid4()

    items: list[RecalculationItem] = [
        {
            "item_id": item1,
            "is_locked": True,
            "quantity": Decimal("100"),
            "energy_kcal": Decimal("500"),
        },
        {
            "item_id": item2,
            "is_locked": False,
            "quantity": Decimal("100"),
            "energy_kcal": Decimal("250"),
        },
        {
            "item_id": item3,
            "is_locked": False,
            "quantity": Decimal("50"),
            "energy_kcal": Decimal("250"),
        },
    ]

    # Target is 1500 kcal. Locked consumes 500. Remaining is 1000.
    # Unlocked current total is 500 kcal. Multiplier should be 2.0.
    # Item 2 quantity -> 200, Item 3 quantity -> 100.

    result = recalculate_quantities(Decimal("1500"), items)

    assert result[item1] == Decimal("100")  # locked, byte-identical
    assert result[item2] == Decimal("200")
    assert result[item3] == Decimal("100")


def test_recalculate_quantities_zero_unlocked():
    """Handles case where there are no unlocked items."""
    item1 = uuid4()
    items: list[RecalculationItem] = [
        {
            "item_id": item1,
            "is_locked": True,
            "quantity": Decimal("100"),
            "energy_kcal": Decimal("500"),
        },
    ]

    result = recalculate_quantities(Decimal("1500"), items)
    assert result[item1] == Decimal("100")


def test_recalculate_quantities_negative_budget():
    """Handles negative remaining budget (locked exceeds target) by scaling unlocked downwards."""
    item1 = uuid4()
    item2 = uuid4()

    items: list[RecalculationItem] = [
        {
            "item_id": item1,
            "is_locked": True,
            "quantity": Decimal("100"),
            "energy_kcal": Decimal("2000"),
        },
        {
            "item_id": item2,
            "is_locked": False,
            "quantity": Decimal("100"),
            "energy_kcal": Decimal("500"),
        },
    ]

    # Target 1500. Locked 2000. Remaining -500.
    # Unlocked current 500. Multiplier -1.
    # Note: Mathematically correct, but practically might need bounds in actual system.
    # Assuming pure math function scales exactly.
    result = recalculate_quantities(Decimal("1500"), items)
    assert result[item1] == Decimal("100")
    assert result[item2] == Decimal("-100")


def test_recalculate_quantities_exact_target():
    """Scales exactly to target if already equal."""
    item1 = uuid4()
    item2 = uuid4()

    items: list[RecalculationItem] = [
        {
            "item_id": item1,
            "is_locked": True,
            "quantity": Decimal("100"),
            "energy_kcal": Decimal("500"),
        },
        {
            "item_id": item2,
            "is_locked": False,
            "quantity": Decimal("100"),
            "energy_kcal": Decimal("500"),
        },
    ]

    # Target 1000. Locked 500. Remaining 500. Unlocked 500. Multiplier 1.0.
    result = recalculate_quantities(Decimal("1000"), items)
    assert result[item1] == Decimal("100")
    assert result[item2] == Decimal("100")


def test_recalculate_quantities_fractional():
    """Handles fractional targets and items deterministically."""
    item1 = uuid4()
    item2 = uuid4()

    items: list[RecalculationItem] = [
        {
            "item_id": item1,
            "is_locked": True,
            "quantity": Decimal("100.5"),
            "energy_kcal": Decimal("500.25"),
        },
        {
            "item_id": item2,
            "is_locked": False,
            "quantity": Decimal("25.0"),
            "energy_kcal": Decimal("100.5"),
        },
    ]

    # Target 1500. Locked 500.25. Remaining 999.75.
    # Unlocked 100.5. Multiplier = 999.75 / 100.5 = 9.947761...
    # Quantity = 25 * 9.947761... = 248.694029...
    result = recalculate_quantities(Decimal("1500"), items)
    assert result[item1] == Decimal("100.5")
    assert abs(result[item2] - Decimal("248.6940298507462686567164179")) < Decimal("0.0001")
