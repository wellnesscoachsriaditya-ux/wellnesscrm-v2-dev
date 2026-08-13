"""Tests for the nutrition kernel pure functions.

🔒 The locking guarantees (API §8.5) are proved here, without a database. They
are properties of a function, so they are tested as properties of a function —
an integration test can show they held once, this shows they hold by
construction.
"""

from decimal import Decimal
from uuid import uuid4

import pytest

from app.kernel.errors import ConflictError, DomainRuleError
from app.kernel.nutrition import (
    DEFAULT_TOLERANCE_PCT,
    FoodNutrientValue,
    FoodPortionValue,
    LockViolationError,
    MacroTotals,
    PlanItemNutrition,
    PlanState,
    PortionConversionError,
    _assert_outcome_preserves_locks,
    add_totals,
    assert_draft,
    assert_respects_locks,
    calculate_composition,
    calculate_meal_composition,
    compute_budget,
    convert_to_grams,
    format_measure,
    is_effectively_locked,
    recalculate_quantities,
    scale_totals,
    snapshot_content_hash,
    subtract_totals,
    sum_totals,
    zero_totals,
)


def _totals(
    energy: str = "0",
    protein: str = "0",
    carbs: str = "0",
    fat: str = "0",
    fibre: str = "0",
) -> MacroTotals:
    return {
        "energy_kcal": Decimal(energy),
        "protein_g": Decimal(protein),
        "carbs_g": Decimal(carbs),
        "fat_g": Decimal(fat),
        "fibre_g": Decimal(fibre),
    }


def _item(*, is_locked: bool, quantity: str, energy: str, protein: str = "0") -> PlanItemNutrition:
    return {
        "item_id": uuid4(),
        "is_locked": is_locked,
        "quantity": Decimal(quantity),
        "totals": _totals(energy=energy, protein=protein),
    }


# ─── Portion conversion ──────────────────────────────────────────────────


def test_convert_to_grams_with_gram_unit():
    """Fallback to grams works natively without needing a portion definition."""
    unit_id = uuid4()
    assert convert_to_grams(
        quantity=Decimal("150"),
        measure_unit_id=unit_id,
        measure_unit_name="g",
        available_portions=[],
    ) == Decimal("150")


@pytest.mark.parametrize("name", ["g", "G", "gram", "Grams", "GRAMS"])
def test_convert_to_grams_accepts_every_spelling_of_grams(name: str):
    """A gram is a gram however the seed data spells it."""
    assert convert_to_grams(
        quantity=Decimal("40"),
        measure_unit_id=uuid4(),
        measure_unit_name=name,
        available_portions=[],
    ) == Decimal("40")


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


def test_convert_to_grams_picks_the_matching_portion_from_several():
    """The unit id selects the portion, not the list order."""
    katori_id, spoon_id, piece_id = uuid4(), uuid4(), uuid4()
    portions: list[FoodPortionValue] = [
        {"measure_unit_id": spoon_id, "gram_weight": Decimal("15")},
        {"measure_unit_id": katori_id, "gram_weight": Decimal("150")},
        {"measure_unit_id": piece_id, "gram_weight": Decimal("30")},
    ]

    assert convert_to_grams(
        quantity=Decimal("1.5"),
        measure_unit_id=katori_id,
        measure_unit_name="katori",
        available_portions=portions,
    ) == Decimal("225.0")


def test_convert_to_grams_fractional_and_zero_quantities():
    """Edge quantities scale linearly rather than being special-cased."""
    katori_id = uuid4()
    portions: list[FoodPortionValue] = [
        {"measure_unit_id": katori_id, "gram_weight": Decimal("150")}
    ]

    assert convert_to_grams(
        quantity=Decimal("0.25"),
        measure_unit_id=katori_id,
        measure_unit_name="katori",
        available_portions=portions,
    ) == Decimal("37.50")
    assert convert_to_grams(
        quantity=Decimal("0"),
        measure_unit_id=katori_id,
        measure_unit_name="katori",
        available_portions=portions,
    ) == Decimal("0")


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


def test_convert_to_grams_food_with_no_portions_at_all():
    """🔒 DB §8.9 — a food with no portion rows is gram-only, not an error in grams."""
    with pytest.raises(PortionConversionError):
        convert_to_grams(
            quantity=Decimal("1"),
            measure_unit_id=uuid4(),
            measure_unit_name="katori",
            available_portions=[],
        )


# ─── Measure display ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("quantity", "unit", "expected"),
    [
        (Decimal("1"), "katori", "1 katori"),
        (Decimal("1.0"), "katori", "1 katori"),
        (Decimal("1.50"), "katori", "1.5 katori"),
        (Decimal("0.25"), "cup", "0.25 cup"),
        (Decimal("100"), "g", "100 g"),
        (Decimal("0"), "g", "0 g"),
    ],
)
def test_format_measure(quantity: Decimal, unit: str, expected: str):
    """🔒 One rendering of a household measure, server-side (API §8.3)."""
    assert format_measure(quantity, unit) == expected


# ─── Totals arithmetic ───────────────────────────────────────────────────


def test_totals_arithmetic():
    left = _totals(energy="100", protein="10", carbs="20", fat="5", fibre="2")
    right = _totals(energy="50", protein="5", carbs="10", fat="1", fibre="1")

    assert add_totals(left, right)["energy_kcal"] == Decimal("150")
    assert subtract_totals(left, right)["protein_g"] == Decimal("5")
    assert scale_totals(left, Decimal("2"))["carbs_g"] == Decimal("40")
    assert sum_totals([left, right, zero_totals()])["fat_g"] == Decimal("6")


def test_subtract_totals_is_never_clamped():
    """🔒 A negative remainder is a real clinical state, not an error to hide."""
    result = subtract_totals(_totals(energy="100"), _totals(energy="400"))
    assert result["energy_kcal"] == Decimal("-300")


# ─── Composition ─────────────────────────────────────────────────────────


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


def test_calculate_composition_is_case_insensitive_on_codes():
    """Seed data casing must not silently zero a macro."""
    nutrients: list[FoodNutrientValue] = [
        {"nutrient_code": "enerc_kcal", "amount_per_100g": Decimal("200")},
    ]
    assert calculate_composition(Decimal("100"), nutrients)["energy_kcal"] == Decimal("200")


def test_calculate_composition_keeps_full_precision():
    """🔒 Rounding is a presentation concern (API §4); the kernel must not round."""
    nutrients: list[FoodNutrientValue] = [
        {"nutrient_code": "ENERC_KCAL", "amount_per_100g": Decimal("333.333")},
    ]
    assert calculate_composition(Decimal("30"), nutrients)["energy_kcal"] == Decimal("99.99990")


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


# ─── Lock semantics ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("item_locked", "slot_locked", "expected"),
    [(False, False, False), (True, False, True), (False, True, True), (True, True, True)],
)
def test_is_effectively_locked(item_locked: bool, slot_locked: bool, expected: bool):
    """🔒 DB §8.10 — a locked slot fixes an unlocked item inside it."""
    assert is_effectively_locked(item_is_locked=item_locked, slot_is_locked=slot_locked) is expected


def test_assert_respects_locks_allows_true():
    assert_respects_locks(True)  # does not raise


def test_assert_respects_locks_refuses_false():
    """🔒 API §8.5 guarantee 3 — always 422, no exceptions, no flag, no override."""
    with pytest.raises(DomainRuleError) as excinfo:
        assert_respects_locks(False)

    assert excinfo.value.status_code == 422
    assert excinfo.value.details["rule_code"] == "locks_must_be_respected"


@pytest.mark.parametrize("state", [PlanState.issued, PlanState.superseded, PlanState.discarded])
def test_assert_draft_refuses_non_draft(state: PlanState):
    """🔒 EC-M4-03 — an issued version is a clinical record, not an editable row."""
    with pytest.raises(ConflictError) as excinfo:
        assert_draft(state)

    assert excinfo.value.status_code == 409
    assert excinfo.value.details["current_state"] == state.value


def test_assert_draft_allows_draft():
    assert_draft(PlanState.draft)  # does not raise


# ─── The nutrition budget — ADR-A07 / API §8.4 ───────────────────────────


def test_compute_budget_matches_the_api_worked_example():
    """The exact numbers in API §8.4, so a drift in the contract fails here."""
    items = [
        _item(is_locked=True, quantity="1", energy="420", protein="22"),
        _item(is_locked=False, quantity="1", energy="760", protein="31"),
    ]

    budget = compute_budget(_totals(energy="1400", protein="70"), items, locked_slot_count=1)

    assert budget["target"]["energy_kcal"] == Decimal("1400")
    assert budget["locked_consumed"]["energy_kcal"] == Decimal("420")
    assert budget["unlocked_current"]["energy_kcal"] == Decimal("760")
    assert budget["remaining_available"]["energy_kcal"] == Decimal("220")
    assert budget["remaining_available"]["protein_g"] == Decimal("17")
    assert budget["locked_item_count"] == 1
    assert budget["locked_slot_count"] == 1
    assert budget["tolerance_pct"] == DEFAULT_TOLERANCE_PCT
    assert budget["is_within_tolerance"] is False


def test_compute_budget_allows_negative_remaining():
    """🔒 EC-M4-05 — locked items may deliberately overshoot. Report, never block."""
    items = [_item(is_locked=True, quantity="1", energy="2000")]

    budget = compute_budget(_totals(energy="1400"), items)

    assert budget["remaining_available"]["energy_kcal"] == Decimal("-600")
    assert budget["is_within_tolerance"] is False


def test_compute_budget_within_tolerance():
    """A plan 40 kcal under a 1400 kcal target is inside the 5% allowance."""
    items = [_item(is_locked=False, quantity="1", energy="1360")]

    budget = compute_budget(_totals(energy="1400"), items)

    assert budget["remaining_available"]["energy_kcal"] == Decimal("40")
    assert budget["is_within_tolerance"] is True


def test_compute_budget_without_a_target():
    """No target set is not an error — the plan still reports what it contains."""
    items = [_item(is_locked=False, quantity="1", energy="500")]

    budget = compute_budget(None, items)

    assert budget["target"] == zero_totals()
    assert budget["unlocked_current"]["energy_kcal"] == Decimal("500")
    assert budget["remaining_available"]["energy_kcal"] == Decimal("-500")
    assert budget["is_within_tolerance"] is True


def test_compute_budget_on_an_empty_plan():
    budget = compute_budget(_totals(energy="1400"), [])

    assert budget["locked_item_count"] == 0
    assert budget["remaining_available"]["energy_kcal"] == Decimal("1400")


# ─── Recalculation — the DoD guarantees ──────────────────────────────────


def test_recalculate_quantities_basic():
    """Validates proportional redistribution with locked and unlocked items."""
    locked = _item(is_locked=True, quantity="100", energy="500")
    first = _item(is_locked=False, quantity="100", energy="250")
    second = _item(is_locked=False, quantity="50", energy="250")

    # Target is 1500 kcal. Locked consumes 500. Remaining is 1000.
    # Unlocked current total is 500 kcal. Multiplier should be 2.0.
    outcome = recalculate_quantities(Decimal("1500"), [locked, first, second])

    assert outcome.quantities[locked["item_id"]] == Decimal("100")  # locked, byte-identical
    assert outcome.quantities[first["item_id"]] == Decimal("200")
    assert outcome.quantities[second["item_id"]] == Decimal("100")
    assert outcome.multiplier == Decimal("2")
    assert outcome.unchanged_locked == (locked["item_id"],)
    assert {change.item_id for change in outcome.changes} == {
        first["item_id"],
        second["item_id"],
    }


def test_recalculate_never_alters_a_locked_quantity():
    """🔒 The DoD's first locking guarantee, asserted directly."""
    locked_a = _item(is_locked=True, quantity="123.456", energy="700")
    locked_b = _item(is_locked=True, quantity="2", energy="300")
    unlocked = _item(is_locked=False, quantity="10", energy="400")

    outcome = recalculate_quantities(Decimal("3000"), [locked_a, locked_b, unlocked])

    assert outcome.quantities[locked_a["item_id"]] == Decimal("123.456")
    assert outcome.quantities[locked_b["item_id"]] == Decimal("2")
    assert set(outcome.unchanged_locked) == {locked_a["item_id"], locked_b["item_id"]}
    assert all(change.item_id == unlocked["item_id"] for change in outcome.changes)


def test_recalculate_never_adds_or_removes_an_item():
    """🔒 API §8.5 — quantities may move; the set of foods may not."""
    items = [
        _item(is_locked=True, quantity="1", energy="100"),
        _item(is_locked=False, quantity="1", energy="100"),
        _item(is_locked=False, quantity="1", energy="100"),
    ]

    outcome = recalculate_quantities(Decimal("900"), items)

    assert set(outcome.quantities) == {item["item_id"] for item in items}
    assert len(outcome.quantities) == 3


def test_recalculate_quantities_zero_unlocked():
    """Handles case where there are no unlocked items."""
    locked = _item(is_locked=True, quantity="100", energy="500")

    outcome = recalculate_quantities(Decimal("1500"), [locked])

    assert outcome.quantities[locked["item_id"]] == Decimal("100")
    assert outcome.changes == ()
    assert outcome.multiplier == Decimal("1")


def test_recalculate_quantities_zero_energy_unlocked_is_a_no_op():
    """No division by zero, and no invented quantity, when unlocked energy is zero."""
    unlocked = _item(is_locked=False, quantity="3", energy="0")

    outcome = recalculate_quantities(Decimal("1500"), [unlocked])

    assert outcome.quantities[unlocked["item_id"]] == Decimal("3")
    assert outcome.changes == ()


def test_recalculate_quantities_empty_plan():
    outcome = recalculate_quantities(Decimal("1500"), [])

    assert outcome.quantities == {}
    assert outcome.changes == ()


def test_recalculate_quantities_negative_budget():
    """Locked exceeds target: unlocked items scale below zero rather than being clamped.

    ⚠️ The service is expected to surface this as a warning; the kernel's job is
    to report the arithmetic honestly rather than to invent a floor.
    """
    locked = _item(is_locked=True, quantity="100", energy="2000")
    unlocked = _item(is_locked=False, quantity="100", energy="500")

    outcome = recalculate_quantities(Decimal("1500"), [locked, unlocked])

    assert outcome.quantities[locked["item_id"]] == Decimal("100")
    assert outcome.quantities[unlocked["item_id"]] == Decimal("-100")


def test_recalculate_quantities_exact_target():
    """Scales exactly to target if already equal."""
    locked = _item(is_locked=True, quantity="100", energy="500")
    unlocked = _item(is_locked=False, quantity="100", energy="500")

    outcome = recalculate_quantities(Decimal("1000"), [locked, unlocked])

    assert outcome.quantities[locked["item_id"]] == Decimal("100")
    assert outcome.quantities[unlocked["item_id"]] == Decimal("100")
    assert outcome.changes == ()


def test_recalculate_quantities_fractional():
    """Handles fractional targets and items deterministically."""
    locked = _item(is_locked=True, quantity="100.5", energy="500.25")
    unlocked = _item(is_locked=False, quantity="25.0", energy="100.5")

    # Target 1500. Locked 500.25. Remaining 999.75.
    # Unlocked 100.5. Multiplier = 999.75 / 100.5 = 9.947761...
    outcome = recalculate_quantities(Decimal("1500"), [locked, unlocked])

    assert outcome.quantities[locked["item_id"]] == Decimal("100.5")
    assert abs(
        outcome.quantities[unlocked["item_id"]] - Decimal("248.6940298507462686567164179")
    ) < Decimal("0.0001")


def test_lock_guarantee_check_catches_a_moved_locked_item():
    """🔒 The invariant check is real: feed it a bad result and it must object."""
    locked = _item(is_locked=True, quantity="100", energy="500")

    with pytest.raises(LockViolationError, match="altered locked item"):
        _assert_outcome_preserves_locks([locked], {locked["item_id"]: Decimal("999")})


def test_lock_guarantee_check_catches_a_dropped_item():
    locked = _item(is_locked=True, quantity="100", energy="500")

    with pytest.raises(LockViolationError, match="changed the item set"):
        _assert_outcome_preserves_locks([locked], {})


# ─── Snapshot hashing — DDR-12 ───────────────────────────────────────────


def test_snapshot_hash_is_deterministic():
    document = {"title": "Plan", "days": [{"day_number": 1}]}

    assert snapshot_content_hash(document) == snapshot_content_hash(dict(document))


def test_snapshot_hash_ignores_key_order():
    """Canonical JSON — assembly order must not change the cache key."""
    first = {"title": "Plan", "version": 2}
    second = {"version": 2, "title": "Plan"}

    assert snapshot_content_hash(first) == snapshot_content_hash(second)


def test_snapshot_hash_changes_with_content():
    """🔒 A single changed gram must invalidate the portal's cached copy."""
    base = {"items": [{"grams": "150.000"}]}
    changed = {"items": [{"grams": "150.001"}]}

    assert snapshot_content_hash(base) != snapshot_content_hash(changed)


def test_snapshot_hash_handles_decimals_and_non_ascii():
    """Indic script and Decimal values must not raise on serialisation."""
    document = {"title": "आहार योजना", "total": Decimal("1234.5678")}

    assert len(snapshot_content_hash(document)) == 64
