"""Tests for the nutrition kernel pure functions."""

from decimal import Decimal
from uuid import uuid4

import pytest

from app.kernel.nutrition import (
    FoodNutrientValue,
    FoodPortionValue,
    PortionConversionError,
    calculate_composition,
    calculate_meal_composition,
    convert_to_grams,
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
