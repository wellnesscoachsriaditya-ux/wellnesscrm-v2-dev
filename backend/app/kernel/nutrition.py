"""Nutrition Kernel — pure functions for portion conversion and macro composition.

🔒 This is the single source of truth for all nutritional calculations (FR-M4-004, NFR-072).
Duplicating this logic in the application layer or frontend is an architectural violation.

Nothing here touches a database, a session or a request. Everything is a function of its
arguments, which is what makes the locking guarantees (API §8.5) provable without a
database — see ``tests/test_kernel_nutrition.py``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Any, Final, Literal, TypedDict
from uuid import UUID

from app.kernel.errors import ConflictError, DomainRuleError
from app.kernel.events import DomainEvent, register_event


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


class MealSlotType(StrEnum):
    """The meal slots a plan day is divided into — FR-M4-025.

    🟡 **The vocabulary is PROPOSED and unconfirmed.** FR-M4-025 marks these seven
    as a proposal, and Validation Gate G1 — the three practitioner sessions that
    would settle them — has not been run.

    ⚠️ **Deliberately not a PostgreSQL enum yet.** ``plan_slots.slot_type`` and
    ``template_slots.slot_type`` stay ``text`` until the vocabulary is confirmed,
    and the database type is created in the slice that also needs it for
    ``foods.meal_suitability`` (DB §8.3). The asymmetry is why: ``ALTER TYPE …
    ADD VALUE`` is cheap, but *removing* a value means rewriting the type and
    every column using it — the same argument migration 0010 makes for
    ``ClientStage.ARCHIVED``. Validating here costs nothing and commits nothing.

    🔒 **This is a structural type, not a display name.** Renaming a slot
    (FR-M4-025) sets ``plan_slots.custom_label``; it does not need a new member.
    :attr:`CUSTOM` covers a slot a practitioner adds that is none of the seven.
    """

    EARLY_MORNING = "early_morning"
    BREAKFAST = "breakfast"
    MID_MORNING = "mid_morning"
    LUNCH = "lunch"
    EVENING_SNACK = "evening_snack"
    DINNER = "dinner"
    BEDTIME = "bedtime"
    CUSTOM = "custom"


#: 🟡 The slots a new plan day is created with, in order — FR-M4-025's "default".
#:
#: ⚠️ ``CUSTOM`` is absent on purpose: it is what a practitioner *adds*, not
#: something to hand them seven times. A caller wanting a bare plan passes an
#: empty sequence rather than relying on this being short.
DEFAULT_SLOT_SEQUENCE: Final[tuple[MealSlotType, ...]] = (
    MealSlotType.EARLY_MORNING,
    MealSlotType.BREAKFAST,
    MealSlotType.MID_MORNING,
    MealSlotType.LUNCH,
    MealSlotType.EVENING_SNACK,
    MealSlotType.DINNER,
    MealSlotType.BEDTIME,
)


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
    """🔒 Raised when this module's own lock guarantee was broken.

    ⚠️ Deliberately **not** an :class:`~app.kernel.errors.AppError`. A caller
    *asking* to bypass locks is refused with :class:`DomainRuleError` (422) by
    :func:`assert_respects_locks`. This one means the redistribution returned a
    result that moved a locked item — that is our defect, not the caller's, and
    it must surface as a 500 rather than be presented to the practitioner as
    though they had done something wrong.
    """

    pass


class PlanItemNutrition(TypedDict):
    """One plan item, resolved: what it weighs, what it contains, whether it is fixed.

    ``is_locked`` is the **effective** lock — an item inside a locked slot is
    locked regardless of its own flag. Compute it with
    :func:`is_effectively_locked` so the budget and the redistribution can never
    disagree about what may move.
    """

    item_id: UUID
    is_locked: bool
    quantity: Decimal
    totals: MacroTotals


class NutritionBudget(TypedDict):
    """🔒 ADR-A07 / API §8.4 — locking expressed as a nutritional constraint.

    ``remaining_available = target − locked_consumed − unlocked_current``: the
    room left in the plan as it currently stands. ⚠️ It may be **negative** when
    the practitioner has deliberately fixed items that overshoot. That is a
    legitimate clinical state, reported as a soft warning and never blocked
    (EC-M4-05).
    """

    target: MacroTotals
    locked_consumed: MacroTotals
    unlocked_current: MacroTotals
    remaining_available: MacroTotals
    locked_item_count: int
    locked_slot_count: int
    is_within_tolerance: bool
    tolerance_pct: Decimal


#: 🔒 API §8.7 — severity decides whether a rule informs or blocks. Soft rules
#: inform and **never** block: practitioner judgement always prevails (EC-M4-05).
#: Hard rules return 422 and reject the write, and arrive with the
#: ``dietary_rules`` engine — DB §8.15's schema is a two-column stub today, so
#: nothing in this module can evaluate one yet.
WarningSeverity = Literal["soft", "hard"]


class WarningScope(TypedDict, total=False):
    """Where a warning applies, so the UI can point at it rather than at the plan."""

    type: Literal["plan", "day", "slot", "item"]
    day_number: int
    slot_id: str
    item_id: str


class PlanWarning(TypedDict):
    """One soft finding about a plan — API §8.7's shape exactly.

    ⚠️ ``message`` is written for a practitioner to read (NFR-063). ``rule_code``
    is the stable identifier the UI branches on; the message may be reworded
    freely, the code may not.
    """

    rule_code: str
    severity: WarningSeverity
    message: str
    scope: WarningScope


def budget_warnings(budget: NutritionBudget) -> list[PlanWarning]:
    """🔒 The soft warnings derivable from the budget alone — API §8.7.

    Three findings, all **soft**, all non-blocking:

    * ``energy_below_target`` / ``energy_above_target`` — the plan misses the
      energy target by more than the tolerance. Reported, never refused: a
      practitioner may have a clinical reason, and EC-M4-05 says their judgement
      prevails.
    * 🔒 ``locked_exceeds_target`` — the locked items *alone* overshoot the
      target, so ``remaining_available`` is negative before anything unlocked is
      counted. API §8.4 calls this out specifically as a legitimate state the API
      reports and never blocks.

    ⚠️ **Nothing here reads ``dietary_rules``.** Allergen and dietary-class
    enforcement (the *hard* half of API §8.7) needs DB §8.15's schema, which is
    currently a two-column stub. Returning an empty list for those is honest;
    inventing a check that only looks like enforcement would be worse.

    Args:
        budget: The computed budget for the scope being reported on.

    Returns:
        Zero to two warnings, ordered most-specific first. An empty list means
        the plan is inside tolerance and its locked items fit — not that no
        clinical rule applies.
    """
    warnings: list[PlanWarning] = []
    scope: WarningScope = {"type": "plan"}

    # 🔒 Checked before the tolerance test. A plan whose locked items alone blow
    # the target is a different conversation from one that is merely 200 kcal
    # light, and collapsing them would hide the constraint the practitioner set.
    if budget["locked_consumed"]["energy_kcal"] > budget["target"]["energy_kcal"] > 0:
        overshoot = budget["locked_consumed"]["energy_kcal"] - budget["target"]["energy_kcal"]
        warnings.append(
            {
                "rule_code": "locked_exceeds_target",
                "severity": "soft",
                "message": (
                    f"Locked items alone are {_trim(overshoot)} kcal over the target. "
                    "Unlock an item to give the recalculation room to work."
                ),
                "scope": scope,
            }
        )

    if budget["target"]["energy_kcal"] > 0 and not budget["is_within_tolerance"]:
        remaining = budget["remaining_available"]["energy_kcal"]
        if remaining > 0:
            warnings.append(
                {
                    "rule_code": "energy_below_target",
                    "severity": "soft",
                    "message": f"Daily energy is {_trim(remaining)} kcal below the target.",
                    "scope": scope,
                }
            )
        elif remaining < 0:
            warnings.append(
                {
                    "rule_code": "energy_above_target",
                    "severity": "soft",
                    "message": f"Daily energy is {_trim(-remaining)} kcal above the target.",
                    "scope": scope,
                }
            )

    return warnings


@dataclass(frozen=True, slots=True)
class QuantityChange:
    """One quantity a redistribution moved. ``changes[]`` in API §8.5."""

    item_id: UUID
    from_quantity: Decimal
    to_quantity: Decimal


@dataclass(frozen=True, slots=True)
class RecalculationOutcome:
    """The result of a redistribution, shaped so the UI can show what moved.

    🔒 ``quantities`` covers **every** input item, locked ones included and
    unchanged. The invariant that matters is that its key set equals the input's:
    a redistribution that dropped or invented an item would be the silent
    substitution API §8.5 forbids.
    """

    quantities: dict[UUID, Decimal]
    changes: tuple[QuantityChange, ...]
    unchanged_locked: tuple[UUID, ...]
    multiplier: Decimal


#: The IFCT-style nutrient codes the four-macro model recognises, mapped onto
#: :class:`MacroTotals` fields. Anything else on a food is carried in the
#: catalogue but does not participate in plan totalling (FR-M4-015 is Phase 2).
_ENERGY_CODE: Final = "ENERC_KCAL"
_PROTEIN_CODE: Final = "PROCNT"
_CARBS_CODE: Final = "CHOCDF"
_FAT_CODE: Final = "FAT"
_FIBRE_CODE: Final = "FIBTG"

_MACRO_CODES: Final[frozenset[str]] = frozenset(
    {_ENERGY_CODE, _PROTEIN_CODE, _CARBS_CODE, _FAT_CODE, _FIBRE_CODE}
)

#: 🔒 API §8.4 — how far from the target still counts as "on target". A single
#: default rather than a per-tenant setting: nobody has asked for one, and a
#: configurable clinical tolerance is a decision that needs a practitioner
#: conversation rather than a column.
DEFAULT_TOLERANCE_PCT: Final[Decimal] = Decimal("5.0")

#: Unit names that already *are* grams and therefore need no portion row.
_GRAM_UNIT_NAMES: Final[frozenset[str]] = frozenset({"g", "gram", "grams"})

# Kept for callers that read the canonical name of the gram unit.
GRAM_UNIT_NAME = "g"


# ─── MacroTotals arithmetic ──────────────────────────────────────────────
#
# ⚠️ Written out field by field rather than looped over a field-name tuple.
# A loop needs `totals[name]` on a TypedDict, which mypy --strict cannot check
# and which the previous version of this module silenced with `# type: ignore`.
# Five explicit lines are cheaper than an unchecked write into a clinical total.


def zero_totals() -> MacroTotals:
    """A totals record with every macro at zero."""
    return {
        "energy_kcal": Decimal("0"),
        "protein_g": Decimal("0"),
        "carbs_g": Decimal("0"),
        "fat_g": Decimal("0"),
        "fibre_g": Decimal("0"),
    }


def add_totals(left: MacroTotals, right: MacroTotals) -> MacroTotals:
    """Sum two totals records."""
    return {
        "energy_kcal": left["energy_kcal"] + right["energy_kcal"],
        "protein_g": left["protein_g"] + right["protein_g"],
        "carbs_g": left["carbs_g"] + right["carbs_g"],
        "fat_g": left["fat_g"] + right["fat_g"],
        "fibre_g": left["fibre_g"] + right["fibre_g"],
    }


def subtract_totals(left: MacroTotals, right: MacroTotals) -> MacroTotals:
    """``left − right``. 🔒 Never clamped at zero — a negative remainder is meaningful."""
    return {
        "energy_kcal": left["energy_kcal"] - right["energy_kcal"],
        "protein_g": left["protein_g"] - right["protein_g"],
        "carbs_g": left["carbs_g"] - right["carbs_g"],
        "fat_g": left["fat_g"] - right["fat_g"],
        "fibre_g": left["fibre_g"] - right["fibre_g"],
    }


def scale_totals(totals: MacroTotals, factor: Decimal) -> MacroTotals:
    """Multiply every macro by ``factor``."""
    return {
        "energy_kcal": totals["energy_kcal"] * factor,
        "protein_g": totals["protein_g"] * factor,
        "carbs_g": totals["carbs_g"] * factor,
        "fat_g": totals["fat_g"] * factor,
        "fibre_g": totals["fibre_g"] * factor,
    }


def sum_totals(records: Iterable[MacroTotals]) -> MacroTotals:
    """Sum any number of totals records."""
    running = zero_totals()
    for record in records:
        running = add_totals(running, record)
    return running


# ─── Portion conversion — 🔒 the single implementation (FR-M4-004) ────────


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
    if measure_unit_name.lower() in _GRAM_UNIT_NAMES:
        return quantity

    for portion in available_portions:
        if portion["measure_unit_id"] == measure_unit_id:
            return quantity * portion["gram_weight"]

    raise PortionConversionError(
        f"Cannot convert {quantity} of unit {measure_unit_name} to grams. "
        "No matching portion found for this food."
    )


def format_measure(quantity: Decimal, measure_unit_name: str) -> str:
    """🔒 Render a household measure for display — ``"1 katori"`` (API §8.3).

    Server-side so the formatting lives in one place. A client that rendered
    ``1.0 katori`` where another rendered ``1 katori`` would be showing the same
    plan two different ways.
    """
    return f"{_trim(quantity)} {measure_unit_name}"


def _trim(value: Decimal) -> str:
    """Render a Decimal without trailing zeros, and without exponent notation.

    ``Decimal("100").normalize()`` is ``1E+2``, which is why this is not simply
    ``normalize()``.
    """
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


# ─── Composition — 🔒 the single implementation (NFR-072) ────────────────


def calculate_composition(
    quantity_g: Decimal,
    nutrients: list[FoodNutrientValue],
) -> MacroTotals:
    """Calculate the macro composition for a given gram weight of a food.

    Args:
        quantity_g: The weight of the food in grams.
        nutrients: The list of nutrient values per 100g for the food.

    Returns:
        A dictionary containing the scaled macro values. Precision is preserved;
        🔒 rounding is a presentation concern and must not happen here.
    """
    multiplier = quantity_g / Decimal("100")

    scaled: dict[str, Decimal] = {}
    for nutrient in nutrients:
        code = nutrient["nutrient_code"].upper()
        if code in _MACRO_CODES:
            scaled[code] = scaled.get(code, Decimal("0")) + nutrient["amount_per_100g"] * multiplier

    return {
        "energy_kcal": scaled.get(_ENERGY_CODE, Decimal("0")),
        "protein_g": scaled.get(_PROTEIN_CODE, Decimal("0")),
        "carbs_g": scaled.get(_CARBS_CODE, Decimal("0")),
        "fat_g": scaled.get(_FAT_CODE, Decimal("0")),
        "fibre_g": scaled.get(_FIBRE_CODE, Decimal("0")),
    }


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
    return sum_totals(calculate_composition(grams, nutrients) for grams, nutrients in items)


# ─── Locking — 🔒 API §8.4, §8.5, DB §8.10 ───────────────────────────────


def is_effectively_locked(*, item_is_locked: bool, slot_is_locked: bool) -> bool:
    """🔒 Whether an item may be moved by automation.

    A locked slot fixes everything inside it (DB §8.10), so an unlocked item in a
    locked slot is still locked. Defined once because the budget and the
    redistribution both need the answer, and a disagreement between them would
    show the practitioner a budget computed over a different item set than the
    one recalculation actually moved.
    """
    return item_is_locked or slot_is_locked


def assert_respects_locks(respect_locks: bool) -> None:
    """🔒 API §8.5 guarantee 3 — ``respect_locks: false`` is refused, always.

    There is no API path to automatic modification of a locked item. The
    practitioner unlocks explicitly or edits the item directly.

    Raises:
        DomainRuleError: 422, whenever ``respect_locks`` is false.
    """
    if not respect_locks:
        raise DomainRuleError.for_rule(
            rule_code="locks_must_be_respected",
            message="Locked items cannot be changed automatically.",
            action="Unlock the items you want the recalculation to adjust, then try again.",
        )


def assert_draft(state: PlanState) -> None:
    """🔒 Only a draft is editable — DB §8.12, DDR-11.

    An issued version is a clinical record. Editing one would change what a
    client was told to eat after they were told it, which is the integrity
    property EC-M4-03 exists to protect.

    Raises:
        ConflictError: 409, when the version is not a draft.
    """
    if state is not PlanState.draft:
        raise ConflictError(
            message="This plan version can no longer be edited.",
            action="Revise the plan to make changes, then reload.",
            details={"current_state": state.value},
        )


def compute_budget(
    target: MacroTotals | None,
    items: Iterable[PlanItemNutrition],
    *,
    locked_slot_count: int = 0,
    tolerance_pct: Decimal = DEFAULT_TOLERANCE_PCT,
) -> NutritionBudget:
    """🔒 ADR-A07 — the nutrition budget, server-computed (API §8.4).

    Args:
        target: The client's targets, or ``None`` when none are set. A plan with
            no target still reports its consumption; ``remaining_available`` is
            then simply the negative of what the plan contains, and
            ``is_within_tolerance`` is ``True`` because there is nothing to miss.
        items: Every item in scope, carrying its **effective** lock state.
        locked_slot_count: How many slots are locked, for the response.
        tolerance_pct: Percentage of the energy target still counted as on target.

    Returns:
        The budget exactly as API §8.4 shapes it.
    """
    resolved_target = target if target is not None else zero_totals()

    locked_consumed = zero_totals()
    unlocked_current = zero_totals()
    locked_item_count = 0

    for item in items:
        if item["is_locked"]:
            locked_consumed = add_totals(locked_consumed, item["totals"])
            locked_item_count += 1
        else:
            unlocked_current = add_totals(unlocked_current, item["totals"])

    remaining_available = subtract_totals(
        subtract_totals(resolved_target, locked_consumed), unlocked_current
    )

    target_energy = resolved_target["energy_kcal"]
    if target_energy <= 0:
        within_tolerance = True
    else:
        allowance = target_energy * tolerance_pct / Decimal("100")
        within_tolerance = abs(remaining_available["energy_kcal"]) <= allowance

    return {
        "target": resolved_target,
        "locked_consumed": locked_consumed,
        "unlocked_current": unlocked_current,
        "remaining_available": remaining_available,
        "locked_item_count": locked_item_count,
        "locked_slot_count": locked_slot_count,
        "is_within_tolerance": within_tolerance,
        "tolerance_pct": tolerance_pct,
    }


def recalculate_quantities(
    target_kcal: Decimal, items: list[PlanItemNutrition]
) -> RecalculationOutcome:
    """🔒 Redistribute *quantities* of unlocked items — API §8.5.

    Locked energy is subtracted from the target first; only the remainder is
    distributed proportionally across the unlocked items.

    ⚠️ **Energy only.** The budget reports four macros, but proportional scaling
    cannot satisfy four targets at once — hitting a macro target by optimising
    portions is FR-M4-037, which is Phase 3. This function does the one thing it
    can do honestly, and says so.

    🔒 **It never adds, removes or substitutes a food.** Only quantities move;
    the returned key set is the input key set. Food *selection* is a clinical
    decision, and a silent substitution would violate the approved locking
    requirement even where the item is technically unlocked.

    Args:
        target_kcal: The energy budget for the scope being recalculated.
        items: Every item in scope, carrying its **effective** lock state.

    Returns:
        The new quantity for every item, plus the changes and the untouched
        locked items, for the response.

    Raises:
        LockViolationError: If the computed result would move a locked item.
            🔒 Our own defect, checked rather than assumed.
    """
    locked_consumed = sum(
        (item["totals"]["energy_kcal"] for item in items if item["is_locked"]), Decimal("0")
    )
    unlocked_budget = target_kcal - locked_consumed

    unlocked_items = [item for item in items if not item["is_locked"]]
    unlocked_current = sum((item["totals"]["energy_kcal"] for item in unlocked_items), Decimal("0"))

    # Nothing to scale, or nothing to scale *from*: a proportional
    # redistribution over zero energy is undefined, and inventing a quantity
    # would be exactly the silent change this function promises not to make.
    if not unlocked_items or unlocked_current <= 0:
        multiplier = Decimal("1")
    else:
        multiplier = unlocked_budget / unlocked_current

    quantities: dict[UUID, Decimal] = {}
    changes: list[QuantityChange] = []
    unchanged_locked: list[UUID] = []

    for item in items:
        if item["is_locked"]:
            quantities[item["item_id"]] = item["quantity"]
            unchanged_locked.append(item["item_id"])
            continue

        new_quantity = item["quantity"] * multiplier
        quantities[item["item_id"]] = new_quantity
        if new_quantity != item["quantity"]:
            changes.append(
                QuantityChange(
                    item_id=item["item_id"],
                    from_quantity=item["quantity"],
                    to_quantity=new_quantity,
                )
            )

    _assert_outcome_preserves_locks(items, quantities)

    return RecalculationOutcome(
        quantities=quantities,
        changes=tuple(changes),
        unchanged_locked=tuple(unchanged_locked),
        multiplier=multiplier,
    )


def _assert_outcome_preserves_locks(
    items: list[PlanItemNutrition], quantities: dict[UUID, Decimal]
) -> None:
    """🔒 The guarantee, asserted rather than trusted.

    Two properties, both cheap and both catastrophic to get wrong: no item was
    added or dropped, and no locked quantity moved.
    """
    if {item["item_id"] for item in items} != set(quantities):
        raise LockViolationError(
            "Recalculation changed the item set. Quantities may move; foods may not."
        )

    for item in items:
        if item["is_locked"] and quantities[item["item_id"]] != item["quantity"]:
            raise LockViolationError(
                f"Recalculation altered locked item {item['item_id']}: "
                f"{item['quantity']} -> {quantities[item['item_id']]}."
            )


# ─── Snapshots — 🔒 DDR-12 ───────────────────────────────────────────────


def snapshot_content_hash(document: Mapping[str, Any]) -> str:
    """🔒 The cache-validation key for an issued plan's snapshot (DB §8.13).

    Canonical JSON — sorted keys, no insignificant whitespace — so the same plan
    hashes identically however the document was assembled. The client portal
    compares hashes to detect that a plan it is displaying has been revised
    (FR-M7-011, EC-M7-03), which only works if the hash is a function of the
    content and nothing else.

    ``default=str`` renders ``Decimal`` and ``UUID`` losslessly; both are already
    exact in the document because nutrition values travel as strings (API §4).
    """
    canonical = json.dumps(
        document, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ─── Cross-module vocabulary (Arch §3.4a) ────────────────────────────────
#
# 🔒 Moved here in S5, from `modules/nutrition/events.py`, and the move is the
# point rather than a tidy-up. `messaging` must deliver a plan the moment it is
# issued (FR-M8-013, AC-M8-001), which means subscribing to this event — and R3
# forbids one module importing another *at all*. An event class that lives in the
# publisher's module is an event only that module can subscribe to.
#
# `modules.nutrition.events` re-exports it, so nothing that already imported it
# from there had to change.


@register_event("nutrition.plan_version_issued")
@dataclass(frozen=True, slots=True)
class PlanVersionIssued(DomainEvent):
    """A plan version was issued to a client — FR-M4-030, and M8's trigger.

    ⚠️ ``client_id`` is carried rather than looked up, and it has to be: a
    subscriber in `messaging` cannot read `diet_plans` (R6), so without it the
    event would name a plan nobody outside `nutrition` can resolve to a person.

    ⚠️ Identifiers only (NFR-033). There is no title, no macro total and no
    snapshot — the message says a plan is ready and links to it; the client opens
    the portal to see what is in it.
    """

    tenant_id: UUID
    plan_version_id: UUID
    client_id: UUID
    issued_at: datetime
