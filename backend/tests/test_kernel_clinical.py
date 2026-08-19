"""The rules of the clinical workspace — PRD M3, DB §7, DDR-07/08, M3.3.

🔒 No database. ``kernel.clinical`` is split from the persistence in
``app.modules.clinical`` so every rule below is a pure function over values — and
these are the rules that decide what ``nutrition`` and ``ai_drafting`` are later
allowed to calculate from, so a drift here is a drift in the meal plan.

Five groups, each pinning a different failure the module invites:

* **The definition engine** — FR-M3-002/003. The structure is *data*, so nothing
  here knows what the assessment asks. What matters is the rules that keep an
  already-captured response readable: a published definition is immutable, and
  ids are unique across the whole document because ``answers`` is one flat object.
* **The projection** — DDR-08. The seam that stops ``nutrition`` ever parsing an
  assessment. Its failure mode is silence: a binding that names nothing drops a
  clinical value without complaining, so the tests here are mostly about refusing
  at publish time.
* **Answer validation and completion** — FR-M3-005/006, EC-M3-01. The rules that
  let a coach skip every clinical section (AC-M3-002) and a client fill a form in
  over three sittings without losing a keystroke (AC-M3-001).
* **Anthropometry and plausibility** — FR-M3-012/015, EC-M3-02. Derived, never
  stored; two units in and one out; and the two-bound design that records a real
  180 kg client while still refusing a typo.
* **The event contract** — DDR-06, NFR-033. What the timeline is allowed to
  learn, and the one event that must reach no client-facing surface at all.
"""

from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.kernel.clients import DietaryClass, SexType

# ⚠️ ``_ENUM_TARGETS`` and ``_TARGET_TYPES`` are private, imported deliberately.
# Both are lookup tables keyed by `ProjectionTarget` and read with ``[]``, so a
# target added to the enum without an entry raises `KeyError` inside a publish
# rather than failing a check. The completeness guards below are the only thing
# that turns that into a test failure instead of a 500 on a valid definition.
from app.kernel.clinical import (
    _ENUM_TARGETS,
    _TARGET_TYPES,
    DOCUMENT_TYPES,
    MAX_DOCUMENT_TYPE_LENGTH,
    MAX_HEIGHT_CM,
    MAX_WEIGHT_KG,
    MIN_HEIGHT_CM,
    MIN_WEIGHT_KG,
    PLAUSIBLE_HEIGHT_CM,
    PLAUSIBLE_WEIGHT_KG,
    ActivityLevel,
    AssessmentCompleted,
    ConsultationNoteRecorded,
    DefinitionStatus,
    DocumentUploaded,
    FieldType,
    GoalType,
    MeasurementRecorded,
    MeasurementSource,
    NutritionProfile,
    NutritionProfileReader,
    ProjectionTarget,
    ResponseStatus,
    assert_publishable,
    assert_storable,
    body_mass_index,
    completion,
    display_precedence,
    height_from_feet_inches,
    is_implausible,
    parse_schema,
    project,
    validate_answers,
    validate_calculation_bindings,
    validate_document_type,
    waist_hip_ratio,
)
from app.kernel.errors import ConflictError, ValidationError
from app.kernel.events import to_payload

CLIENT = uuid.UUID("11111111-1111-4111-8111-111111111111")
RESPONSE = uuid.UUID("22222222-2222-4222-8222-222222222222")
MOMENT = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)


# ─── Fixtures: a schema shaped like the real seed ─────────────────────────


def _schema_document() -> dict[str, Any]:
    """A definition document with one non-clinical and one clinical section.

    ⚠️ Built as a raw ``dict`` rather than as ``AssessmentSchema`` objects. The
    parser is under test, and constructing typed values directly would skip it —
    the seed in migration 0016 arrives as ``jsonb``, which is exactly this shape.
    """
    return {
        "sections": [
            {
                "id": "basics",
                "title": "About you",
                "fields": [
                    {"id": "dob", "type": "date", "label": "Date of birth", "required": True},
                    {
                        "id": "sex",
                        "type": "choice",
                        "label": "Sex",
                        "required": True,
                        "options": [m.value for m in SexType],
                    },
                    {
                        "id": "height_cm",
                        "type": "number",
                        "label": "Height",
                        "min": 30,
                        "max": 275,
                    },
                    {"id": "current_weight_kg", "type": "number", "label": "Weight"},
                    {"id": "notes", "type": "long_text", "label": "Anything else"},
                ],
            },
            {
                "id": "diet",
                "title": "How you eat",
                "fields": [
                    {
                        "id": "dietary_class",
                        "type": "choice",
                        "label": "Diet",
                        "required": True,
                        "options": [m.value for m in DietaryClass],
                    },
                    {
                        "id": "fasting",
                        "type": "multi_choice",
                        "label": "Fasting",
                        "options": ["ekadashi", "navratri", "intermittent"],
                    },
                    {"id": "no_onion_garlic", "type": "boolean", "label": "No onion or garlic"},
                    {"id": "allergens", "type": "food_ref", "label": "Allergies"},
                    {"id": "staple", "type": "text", "label": "Staple grain"},
                ],
            },
            {
                "id": "history",
                "title": "Medical history",
                "is_clinical": True,
                "fields": [
                    {
                        "id": "conditions",
                        "type": "long_text",
                        "label": "Conditions",
                        "required": True,
                    },
                    {"id": "energy", "type": "scale", "label": "Energy", "min": 1, "max": 5},
                ],
            },
        ]
    }


def _bindings_document() -> dict[str, str]:
    return {
        "date_of_birth": "dob",
        "sex": "sex",
        "height_cm": "height_cm",
        "dietary_class": "dietary_class",
        "fasting_patterns": "fasting",
        "excludes_onion_garlic": "no_onion_garlic",
        "allergen_food_ids": "allergens",
        "staple_grain": "staple",
    }


@pytest.fixture
def schema() -> Any:
    return parse_schema(_schema_document())


# ─── The definition engine (FR-M3-002, FR-M3-003) ─────────────────────────


def test_a_definition_parses_into_typed_sections_and_fields(schema: Any) -> None:
    """FR-M3-002 — adding a question is an INSERT, so the parser is the contract."""
    assert [s.id for s in schema.sections] == ["basics", "diet", "history"]

    index = schema.field_index()
    assert index["dob"].type is FieldType.DATE
    assert index["dob"].required is True
    assert index["height_cm"].min_value == Decimal("30")
    assert index["height_cm"].max_value == Decimal("275")
    assert index["sex"].options == tuple(m.value for m in SexType)
    # ⚠️ Absent `required` defaults to False rather than raising. Most fields are
    # optional and a definition author should not have to say so nine times.
    assert index["notes"].required is False


def test_only_the_clinical_section_is_marked_clinical(schema: Any) -> None:
    """🔒 FR-M3-006 — the flag AC-M3-002's whole skippability rule turns on.

    If `is_clinical` defaulted to True, or the parser dropped it, a coach would be
    unable to submit; if it never parsed as True, a client's medical history would
    become mandatory. Both are silent until someone tries to submit.
    """
    assert [s.id for s in schema.sections if s.is_clinical] == ["history"]


def test_a_field_can_be_traced_back_to_its_section(schema: Any) -> None:
    """The lookup the renderer and the completion rule both need."""
    assert schema.section_of("conditions").id == "history"
    assert schema.section_of("dob").id == "basics"
    assert schema.section_of("nonexistent") is None


def test_duplicate_field_ids_are_refused_across_the_whole_document() -> None:
    """🔒 The subtlest way a definition can corrupt a response.

    ``answers`` is one flat object keyed by field id (DB §7.3), so two fields
    sharing an id in *different* sections silently overwrite each other's answer.
    Uniqueness therefore has to be document-wide, not per-section — a per-section
    check would let this through.
    """
    document = _schema_document()
    document["sections"][1]["fields"].append(
        {"id": "dob", "type": "text", "label": "Born on"},
    )

    with pytest.raises(ValidationError) as raised:
        parse_schema(document)
    assert "dob" in str(raised.value)


def test_duplicate_section_ids_are_refused() -> None:
    """``completed_sections`` is a list of section ids — FR-M3-006.

    Two sections sharing an id would make "section B is done" ambiguous, and the
    completion percentage would count one of them twice.

    ⚠️ The duplicate carries *distinct* field ids on purpose: reusing the
    original's fields would trip the field-id check first, and the test would
    pass while the section-id rule was gone.
    """
    document = _schema_document()
    document["sections"].append(
        {
            "id": "basics",
            "title": "About you, again",
            "fields": [{"id": "second_thoughts", "type": "text", "label": "Anything else"}],
        }
    )

    with pytest.raises(ValidationError) as raised:
        parse_schema(document)
    assert "basics" in str(raised.value)


def test_an_unknown_field_type_is_refused() -> None:
    """🔒 FieldType is closed, and that is what makes FR-M3-002 safe.

    The frontend renders a form it has never seen; it can only do that if the
    vocabulary is fixed. A definition naming a type the renderer cannot draw
    would produce a blank space where a question belongs.
    """
    document = _schema_document()
    document["sections"][0]["fields"][0]["type"] = "signature_pad"

    with pytest.raises(ValidationError) as raised:
        parse_schema(document)
    assert "type" in str(raised.value).lower()


def test_a_choice_without_options_is_refused() -> None:
    """A choice field with nothing to choose renders as an empty dropdown."""
    document = _schema_document()
    document["sections"][0]["fields"][1]["options"] = []

    with pytest.raises(ValidationError):
        parse_schema(document)

    # ⚠️ Only the choice types need options. A food reference draws its values
    # from the catalogue, and requiring an empty list here would be noise.
    food_ref_only = {
        "sections": [
            {
                "id": "s",
                "title": "T",
                "fields": [{"id": "a", "type": "food_ref", "label": "Allergies"}],
            }
        ]
    }
    assert parse_schema(food_ref_only).field_index()["a"].type is FieldType.FOOD_REF


@pytest.mark.parametrize(
    ("document", "why"),
    [
        ("not a dict", "a schema must be an object"),
        ({}, "no sections key"),
        ({"sections": []}, "an empty section list renders an empty form"),
        ({"sections": [{"id": "s", "title": "T"}]}, "a section with no fields key"),
        ({"sections": [{"id": "s", "title": "T", "fields": []}]}, "a section with no fields"),
        ({"sections": [{"title": "T", "fields": [{"id": "a"}]}]}, "a section with no id"),
        ({"sections": ["nope"]}, "a section that is not an object"),
        (
            {"sections": [{"id": "s", "title": "T", "fields": ["nope"]}]},
            "a field that is not an object",
        ),
        (
            {"sections": [{"id": " ", "title": "T", "fields": [{"id": "a"}]}]},
            "a whitespace-only id is not an id",
        ),
        (
            {
                "sections": [
                    {"id": "s", "title": "T", "fields": [{"id": "a", "type": "text", "label": ""}]}
                ]
            },
            "a field with no label is a question nobody can answer",
        ),
    ],
)
def test_a_malformed_definition_fails_loudly(document: Any, why: str) -> None:
    """🔒 Every failure here is *our* defect, not a user's input.

    Definitions are authored by us (FR-M3-009/010 are Phase 2/3), so a malformed
    one means a bad seed or a bad migration. It must fail at publish rather than
    render an empty form to a client who then has nothing to submit.
    """
    with pytest.raises(ValidationError):
        parse_schema(document)


def test_a_non_numeric_bound_is_refused() -> None:
    """`min` and `max` reach the number check as Decimals or not at all."""
    document = _schema_document()
    document["sections"][0]["fields"][2]["min"] = "waist-high"

    with pytest.raises(ValidationError):
        parse_schema(document)


def test_a_schema_error_tells_support_rather_than_the_practitioner() -> None:
    """🔒 NFR-063 — every error says what to do next, honestly.

    The honest next step for a misconfigured definition is not something a
    practitioner can do. An action inviting them to "check and try again" would
    send them round a loop they cannot exit.
    """
    with pytest.raises(ValidationError) as raised:
        parse_schema({"sections": []})

    assert raised.value.action
    assert "support" in raised.value.action.lower()


def test_publishing_is_a_one_way_door() -> None:
    """🔒 DB §7.2 / FR-M3-003 — the rule AC-M3-003 rests on.

    Editing a published structure silently changes what an already-captured
    response *means*: the answers stay put while the question they answered moves.
    """
    assert_publishable(DefinitionStatus.DRAFT)

    for closed in (DefinitionStatus.PUBLISHED, DefinitionStatus.RETIRED):
        with pytest.raises(ConflictError) as raised:
            assert_publishable(closed)
        # 409, not 422: the request was well-formed and refused on state.
        assert raised.value.status_code == 409
        assert "new version" in (raised.value.action or "").lower()


def test_a_response_has_no_abandoned_state() -> None:
    """⚠️ EC-M3-01 — a partial response is retained and resumable.

    An untouched `in_progress` row *is* what an abandonment looks like, and the
    practitioner reads its completion percentage rather than a label somebody had
    to remember to set. A third state would need a writer, and nothing would
    ever be that writer.
    """
    assert {m.value for m in ResponseStatus} == {"in_progress", "completed"}


# ─── The projection contract (DDR-08) ─────────────────────────────────────


def test_valid_bindings_come_back_as_target_to_field(schema: Any) -> None:
    bindings = validate_calculation_bindings(_bindings_document(), schema)

    assert bindings[ProjectionTarget.DATE_OF_BIRTH] == "dob"
    assert bindings[ProjectionTarget.ALLERGEN_FOOD_IDS] == "allergens"
    assert len(bindings) == len(_bindings_document())


def test_a_binding_to_an_unknown_target_is_refused(schema: Any) -> None:
    """🔒 DDR-08 — `ProjectionTarget` *is* the contract, so it is closed.

    A binding naming a column that does not exist would be dropped on the floor;
    the practitioner fills in the value and nutrition never sees it.
    """
    with pytest.raises(ValidationError) as raised:
        validate_calculation_bindings({"favourite_colour": "notes"}, schema)
    assert "ProjectionTarget" in str(raised.value)


def test_a_binding_to_a_missing_field_is_refused(schema: Any) -> None:
    """🔒 The failure this whole function exists to prevent.

    A binding to a field that is not in the schema does nothing at all, and
    "nothing at all" is invisible: the form submits, the profile is short one
    value, and the AI candidate filter never learns about the allergy.
    """
    with pytest.raises(ValidationError) as raised:
        validate_calculation_bindings({"height_cm": "how_tall_are_you"}, schema)
    assert "silently" in str(raised.value)


def test_a_binding_whose_type_the_column_cannot_hold_is_refused(schema: Any) -> None:
    """🔒 Checked against the *column*, not against the first response.

    ``height_cm`` is numeric. Binding it to a free-text field would store "quite
    tall" in a Decimal column — or, worse, coerce silently to nothing at all.
    """
    with pytest.raises(ValidationError) as raised:
        validate_calculation_bindings({"height_cm": "notes"}, schema)
    assert "long_text" in str(raised.value)

    # ⚠️ Allergens are a safety case (FR-M5-006), so the type rule is what stops
    # them being captured as prose: DB §7.4 says food ids, never free text.
    with pytest.raises(ValidationError):
        validate_calculation_bindings({"allergen_food_ids": "notes"}, schema)


def test_bindings_must_be_an_object(schema: Any) -> None:
    for malformed in (["height_cm"], "height_cm", None, 7):
        with pytest.raises(ValidationError):
            validate_calculation_bindings(malformed, schema)


def test_no_bindings_at_all_is_legitimate(schema: Any) -> None:
    """⚠️ An empty mapping is valid, not suspicious.

    A definition may exist to gather narrative context and project nothing —
    FR-M3-010's custom forms are the obvious case. The publish check refuses
    *wrong* bindings, not the absence of any.
    """
    assert validate_calculation_bindings({}, schema) == {}


def test_every_projection_target_can_actually_be_bound() -> None:
    """🔒 A completeness guard on DDR-08's lookup table.

    `_TARGET_TYPES` is keyed by target and read with ``[]``. A target added to the
    enum without an entry raises `KeyError` inside a publish, which surfaces as a
    500 on a definition that looked fine — not as the validation error the caller
    would be able to act on.
    """
    missing = [t.value for t in ProjectionTarget if t not in _TARGET_TYPES]
    assert not missing, (
        f"ProjectionTarget {missing} has no entry in `_TARGET_TYPES`. Add one "
        "saying which FieldTypes may project into that column — an absent entry "
        "is a KeyError at publish time, not a validation error."
    )
    for target, permitted in _TARGET_TYPES.items():
        assert permitted, f"{target.value} permits no field type, so nothing can bind to it."


def test_every_projection_target_is_a_field_on_the_profile() -> None:
    """🔒 The contract `project()` depends on and cannot check for itself.

    ``project`` builds its result with ``NutritionProfile(**values)`` keyed by
    ``target.value``. A target whose name is not a field on the dataclass raises
    `TypeError` — on submit, after the practitioner has filled the form in, and
    only for the definitions that happen to bind that target.
    """
    profile_fields = {f.name for f in fields(NutritionProfile)}
    orphans = [t.value for t in ProjectionTarget if t.value not in profile_fields]
    assert not orphans, (
        f"ProjectionTarget {orphans} names no field on NutritionProfile. "
        "`project()` unpacks by target name, so this is a TypeError on submit."
    )


def test_every_enum_target_takes_a_choice_field() -> None:
    """⚠️ An enum-valued column bound to a free-text field cannot be coerced.

    `_ENUM_TARGETS` and `_TARGET_TYPES` are two halves of one statement, and
    nothing but this test connects them: a target whose value must be an enum
    member has to be fed by a field with a closed option list.
    """
    for target, enum_type in _ENUM_TARGETS.items():
        assert target in _TARGET_TYPES
        assert _TARGET_TYPES[target] <= {FieldType.CHOICE, FieldType.MULTI_CHOICE}, (
            f"{target.value} projects into {enum_type.__name__} but accepts a "
            "free-text field, whose value cannot be coerced to an enum member."
        )


def test_weight_is_deliberately_not_a_projection_target() -> None:
    """🔒 FR-M3-011 — weight is longitudinal, so it lives in `measurements`.

    A `weight_kg` on the profile would be a second source of truth beside the
    trend, and the two would disagree the first time a measurement was corrected.
    The assessment still *asks* for a weight; the answer becomes a measurement.
    """
    assert not [t for t in ProjectionTarget if "weight" in t.value]
    assert not [f for f in fields(NutritionProfile) if "weight" in f.name]


def test_the_nutrition_port_is_read_only() -> None:
    """🔒 R3 / DB §7 — `nutrition` and `ai_drafting` read the projection, never
    the tables, and never write it.

    The port is the seam that makes the dependency expressible without an import.
    A write method on it would let another module author a clinical value, which
    is the one thing DB §7's ownership rule exists to prevent.
    """
    surface = {name for name in vars(NutritionProfileReader) if not name.startswith("_")}
    assert surface == {"profile_for"}, (
        "NutritionProfileReader has gained a method. It is the whole of what "
        "`nutrition` and `ai_drafting` may do with clinical data — anything that "
        "writes belongs in the clinical module's own service."
    )


def test_the_profile_defaults_to_knowing_nothing() -> None:
    """⚠️ An unassessed client is a real state, and its defaults must be safe.

    Exclusions default to False and the id tuples to empty, so a profile built
    from nothing claims no restrictions — but it also claims no *permissions*:
    `dietary_class` is None rather than non-vegetarian, so nothing downstream can
    read an unassessed client as cleared to eat anything.
    """
    empty = NutritionProfile(client_id=CLIENT)
    assert empty.dietary_class is None
    assert empty.sex is None
    assert empty.allergen_food_ids == ()
    assert empty.excludes_onion_garlic is False


def test_the_projection_lifts_every_bound_answer(schema: Any) -> None:
    """DDR-08's happy path — the one run on submit."""
    peanut, shellfish = uuid.uuid4(), uuid.uuid4()
    profile = project(
        {
            "dob": "1990-06-15",
            "sex": "female",
            "height_cm": "162.4",
            "dietary_class": "vegetarian",
            "fasting": ["ekadashi", "navratri"],
            "no_onion_garlic": True,
            "allergens": [str(peanut), str(shellfish)],
            "staple": "wheat",
            # ⚠️ Answered but unbound: it must not appear anywhere below.
            "notes": "prefers evening consultations",
        },
        validate_calculation_bindings(_bindings_document(), schema),
        client_id=CLIENT,
        source_response_id=RESPONSE,
    )

    assert profile.client_id == CLIENT
    assert profile.source_response_id == RESPONSE
    assert profile.date_of_birth == date(1990, 6, 15)
    assert profile.sex is SexType.FEMALE
    assert profile.height_cm == Decimal("162.4")
    assert profile.dietary_class is DietaryClass.VEGETARIAN
    assert profile.fasting_patterns == ("ekadashi", "navratri")
    assert profile.excludes_onion_garlic is True
    assert profile.allergen_food_ids == (peanut, shellfish)
    assert profile.staple_grain == "wheat"


@pytest.mark.parametrize(
    ("field_id", "target", "answer", "why"),
    [
        ("height_cm", "height_cm", "quite tall", "an unparseable number"),
        ("sex", "sex", "unspecified", "a choice outside the enum"),
        ("dob", "date_of_birth", "15/06/1990", "a date in the wrong format"),
        ("dietary_class", "dietary_class", "flexitarian", "a diet we do not model"),
    ],
)
def test_an_uncoercible_answer_projects_as_absent(
    schema: Any, field_id: str, target: str, answer: Any, why: str
) -> None:
    """⚠️ DDR-08 — a gap, never a failure, and the asymmetry is deliberate.

    The assessment has already been accepted by the time `project` runs. Refusing
    the submission because one optional value cannot be coerced would lose the
    whole administration; a gap is visible to the practitioner and recoverable,
    while a lost submission is neither.
    """
    profile = project(
        {field_id: answer},
        validate_calculation_bindings(_bindings_document(), schema),
        client_id=CLIENT,
        source_response_id=RESPONSE,
    )

    assert getattr(profile, target) is None, f"{why} projected a value"


def test_a_food_reference_that_is_not_an_id_is_dropped_not_guessed(schema: Any) -> None:
    """🔒 FR-M5-006 / DB §7.4 — a missed allergen is a clinical incident.

    "nuts" typed into a box cannot be matched to a food reliably, so it is
    dropped rather than fuzzy-matched. That is the safe direction only because
    :func:`validate_answers` refuses the same value on submit — the practitioner
    is told, and the drop here is the second line rather than the first.
    """
    peanut = uuid.uuid4()
    profile = project(
        {"allergens": [str(peanut), "nuts", ""]},
        validate_calculation_bindings(_bindings_document(), schema),
        client_id=CLIENT,
        source_response_id=RESPONSE,
    )
    assert profile.allergen_food_ids == (peanut,)


def test_a_single_answer_projects_into_a_tuple_column(schema: Any) -> None:
    """⚠️ A client with one allergy answers with a value, not a list of one."""
    peanut = uuid.uuid4()
    bindings = validate_calculation_bindings(_bindings_document(), schema)

    profile = project(
        {"allergens": str(peanut), "fasting": "ekadashi"},
        bindings,
        client_id=CLIENT,
        source_response_id=RESPONSE,
    )
    assert profile.allergen_food_ids == (peanut,)
    assert profile.fasting_patterns == ("ekadashi",)


def test_a_blank_answer_leaves_the_column_at_its_default(schema: Any) -> None:
    """⚠️ Blank is absent, not False.

    A client who left "no onion or garlic" untouched has not said they eat it —
    but the column is a boolean, so the default has to be the safe reading, and
    the distinction only survives if blank never reaches the coercion.
    """
    profile = project(
        {"staple": "   ", "fasting": [], "no_onion_garlic": None, "allergens": []},
        validate_calculation_bindings(_bindings_document(), schema),
        client_id=CLIENT,
        source_response_id=RESPONSE,
    )
    assert profile.staple_grain is None
    assert profile.fasting_patterns == ()
    assert profile.excludes_onion_garlic is False
    assert profile.allergen_food_ids == ()


def test_the_projection_is_frozen() -> None:
    """🔒 Nothing downstream may adjust a clinical value it merely read."""
    profile = NutritionProfile(client_id=CLIENT)
    with pytest.raises(FrozenInstanceError):
        profile.height_cm = Decimal("170")  # type: ignore[misc]


# ─── Answer validation (FR-M3-005, FR-M3-006) ─────────────────────────────


def test_a_partial_save_never_complains_about_a_missing_answer(schema: Any) -> None:
    """🔒 FR-M3-005 / AC-M3-001 — the form saves continuously.

    A required answer that has not been typed yet is the *expected* state of a
    half-filled form. Treating it as an error on save is how a client loses a
    section by pausing mid-field.
    """
    assert validate_answers({}, schema, partial=True) == []
    assert validate_answers({"sex": "female"}, schema, partial=True) == []


def test_submitting_without_a_required_answer_is_refused(schema: Any) -> None:
    """FR-M3-004 — required-ness becomes real exactly once, on submit."""
    issues = validate_answers({"sex": "female"}, schema, partial=False)

    missing = {issue.field_id for issue in issues}
    assert "dob" in missing
    assert "dietary_class" in missing
    assert "sex" not in missing


def test_a_skipped_clinical_section_never_blocks_a_submission(schema: Any) -> None:
    """🔒 FR-M3-006 / AC-M3-002 — the coach persona's whole path.

    Rahul (P2) is not a clinician and will skip medical history every time. If
    `conditions` were enforced he could not submit at all, and the product would
    have shipped a clinical gate to a non-clinical user.
    """
    issues = validate_answers(
        {"dob": "1990-06-15", "sex": "female", "dietary_class": "vegan"},
        schema,
        partial=False,
    )
    assert issues == []


def test_an_answer_to_a_field_the_schema_forgot_is_dropped_not_refused(schema: Any) -> None:
    """⚠️ EC-M3-03 — a client holding a stale form must still be able to save.

    Their browser has v1 open, v2 published, and their next save carries a field
    id v2 removed. Rejecting the payload would leave them unable to save anything
    at all; the unknown answer is ignored and the rest persists.
    """
    assert validate_answers({"gone_in_v2": "value"}, schema, partial=True) == []
    assert (
        validate_answers(
            {"dob": "1990-06-15", "sex": "male", "dietary_class": "vegan", "gone_in_v2": 3},
            schema,
            partial=False,
        )
        == []
    )


def test_a_null_answer_is_not_type_checked(schema: Any) -> None:
    """⚠️ Clearing a field posts null, which is a value the form is allowed to send."""
    assert validate_answers({"height_cm": None, "sex": None}, schema, partial=True) == []


@pytest.mark.parametrize(
    ("answers", "expect_field"),
    [
        ({"sex": "yes"}, "sex"),
        ({"fasting": "ekadashi"}, "fasting"),
        ({"fasting": ["ekadashi", "shivratri"]}, "fasting"),
        ({"height_cm": "tall"}, "height_cm"),
        ({"height_cm": 12}, "height_cm"),
        ({"height_cm": 400}, "height_cm"),
        ({"energy": 9}, "energy"),
        ({"no_onion_garlic": "yes"}, "no_onion_garlic"),
        ({"allergens": "nuts"}, "allergens"),
        ({"allergens": ["nuts"]}, "allergens"),
        ({"dob": "15/06/1990"}, "dob"),
    ],
)
def test_a_malformed_answer_is_reported_against_its_field(
    schema: Any, answers: dict[str, Any], expect_field: str
) -> None:
    """Each type's check, and the field id the form needs to highlight."""
    issues = validate_answers(answers, schema, partial=True)
    assert [issue.field_id for issue in issues] == [expect_field]
    assert issues[0].message


@pytest.mark.parametrize(
    "answers",
    [
        {"sex": "female"},
        {"fasting": ["ekadashi", "navratri"]},
        {"fasting": []},
        {"height_cm": "162.4"},
        {"height_cm": 162},
        {"energy": 3},
        {"no_onion_garlic": False},
        {"allergens": [str(uuid.uuid4())]},
        {"allergens": str(uuid.uuid4())},
        {"dob": "1990-06-15"},
        {"dob": date(1990, 6, 15)},
        {"notes": "any prose at all"},
    ],
)
def test_a_well_formed_answer_passes(schema: Any, answers: dict[str, Any]) -> None:
    assert validate_answers(answers, schema, partial=True) == []


def test_a_boolean_is_checked_strictly(schema: Any) -> None:
    """⚠️ `1` is not True here, deliberately.

    Python would coerce it, and a clinical exclusion that turns on truthiness
    accepts the string "false" as a yes. An exclusion is a safety value; the
    payload has to say so in the type.
    """
    assert validate_answers({"no_onion_garlic": True}, schema, partial=True) == []
    assert validate_answers({"no_onion_garlic": 1}, schema, partial=True) != []
    assert validate_answers({"no_onion_garlic": "false"}, schema, partial=True) != []


def test_a_scale_is_bounded_by_its_own_declaration(schema: Any) -> None:
    """Bounds come from the definition, not from a constant in the code."""
    assert validate_answers({"energy": 1}, schema, partial=True) == []
    assert validate_answers({"energy": 5}, schema, partial=True) == []
    assert validate_answers({"energy": 0}, schema, partial=True) != []
    assert validate_answers({"energy": 6}, schema, partial=True) != []


def test_every_problem_is_reported_at_once(schema: Any) -> None:
    """⚠️ Issues are returned rather than raised, so a form shows them together.

    A validator that raised on the first fault would make a twelve-field section
    a twelve-round-trip conversation — the exact V1 failure that made the
    assessment feel like an interrogation.
    """
    issues = validate_answers(
        {"sex": "yes", "height_cm": "tall", "no_onion_garlic": "maybe"},
        schema,
        partial=True,
    )
    assert {issue.field_id for issue in issues} == {"sex", "height_cm", "no_onion_garlic"}


def test_whitespace_does_not_answer_a_required_question(schema: Any) -> None:
    """A space bar is not an answer — the required check strips before deciding."""
    issues = validate_answers(
        {"dob": "1990-06-15", "sex": "male", "dietary_class": "   "},
        schema,
        partial=False,
    )
    assert [issue.field_id for issue in issues] == ["dietary_class"]


# ─── Completion (EC-M3-01) ────────────────────────────────────────────────


def test_completion_counts_only_the_sections_that_decide_usability(schema: Any) -> None:
    """🔒 EC-M3-01 / AC-M3-002 — clinical sections are out of the denominator.

    A coach who legitimately skipped every clinical section must not read 40% and
    conclude they did it wrong. The number answers "is this usable for planning",
    not "how many boxes are filled".
    """
    both_non_clinical_done = completion(
        {
            "dob": "1990-06-15",
            "sex": "female",
            "dietary_class": "vegan",
        },
        schema,
        frozenset(),
    )
    assert both_non_clinical_done == Decimal("100.0")


def test_a_section_the_respondent_marked_done_counts_fully(schema: Any) -> None:
    """⚠️ FR-M3-006 — "done" means what the person who said it meant.

    A section whose optional fields are blank is finished if they say it is; the
    alternative is a form that argues with the client about whether they have
    answered enough.
    """
    assert completion({}, schema, frozenset({"basics"})) == Decimal("50.0")
    assert completion({}, schema, frozenset({"basics", "diet"})) == Decimal("100.0")


def test_an_untouched_section_is_not_progress() -> None:
    """⚠️ The case that makes the `any()` clause load-bearing.

    A section declaring no required fields would otherwise satisfy `all()`
    vacuously and count as complete before anyone had typed into it — a wholly
    untouched form would read as 100%.
    """
    optional_only = parse_schema(
        {
            "sections": [
                {
                    "id": "a",
                    "title": "A",
                    "fields": [{"id": "a1", "type": "text", "label": "Anything"}],
                },
                {
                    "id": "b",
                    "title": "B",
                    "fields": [{"id": "b1", "type": "text", "label": "Anything"}],
                },
            ]
        }
    )

    assert completion({}, optional_only, frozenset()) == Decimal("0.0")
    assert completion({"a1": "something"}, optional_only, frozenset()) == Decimal("50.0")


def test_a_partly_filled_section_does_not_count(schema: Any) -> None:
    """A section counts once its *required* fields are answered, not before."""
    assert completion({"height_cm": "162"}, schema, frozenset()) == Decimal("0.0")
    assert completion({"dob": "1990-06-15", "sex": "male"}, schema, frozenset()) == Decimal("50.0")


def test_an_all_clinical_form_is_complete_by_definition() -> None:
    """⚠️ Division by zero, in the shape it would actually arrive in.

    A definition whose every section is clinical has an empty denominator.
    Returning 100 rather than raising is the honest answer: there is nothing left
    that the respondent is obliged to do.
    """
    clinical_only = parse_schema(
        {
            "sections": [
                {
                    "id": "h",
                    "title": "History",
                    "is_clinical": True,
                    "fields": [{"id": "c", "type": "text", "label": "Conditions"}],
                }
            ]
        }
    )
    assert completion({}, clinical_only, frozenset()) == Decimal("100.0")


def test_completion_is_a_one_decimal_percentage() -> None:
    """A third of the way through is 33.3, and stays 33.3 — the API returns it."""
    thirds = parse_schema(
        {
            "sections": [
                {
                    "id": s,
                    "title": s,
                    "fields": [{"id": f"{s}1", "type": "text", "label": "L", "required": True}],
                }
                for s in ("a", "b", "c")
            ]
        }
    )
    assert completion({"a1": "x"}, thirds, frozenset()) == Decimal("33.3")
    assert completion({"a1": "x", "b1": "y"}, thirds, frozenset()) == Decimal("66.7")


def test_an_unknown_completed_section_is_ignored(schema: Any) -> None:
    """⚠️ EC-M3-03 again — a stale form may name a section that no longer exists."""
    assert completion({}, schema, frozenset({"section_from_v1"})) == Decimal("0.0")


# ─── Anthropometry (FR-M3-012, FR-M3-015) ─────────────────────────────────


@pytest.mark.parametrize(
    ("weight", "height", "expected"),
    [
        ("70", "170", "24.2"),
        ("55.5", "158", "22.2"),
        ("95", "180", "29.3"),
        ("48", "150.5", "21.2"),
    ],
)
def test_bmi_matches_an_independent_calculation(weight: str, height: str, expected: str) -> None:
    """🔒 AC-M3-004 — checked against the arithmetic, not against ourselves.

    kg / m². Each expectation here was computed by hand rather than by calling
    the function, which is the only way this test can catch the function being
    wrong rather than merely being consistent.
    """
    assert body_mass_index(weight_kg=Decimal(weight), height_cm=Decimal(height)) == Decimal(
        expected
    )


def test_bmi_and_whr_are_never_stored_only_returned() -> None:
    """🔒 FR-M3-012 — derived on read, so a corrected weight corrects everything.

    Asserted on the *shape* of what the kernel offers: there is no BMI setter, no
    profile column, and nothing that takes a precomputed value. A stored BMI is a
    second source of truth that drifts the moment a weight is corrected.
    """
    assert not [f for f in fields(NutritionProfile) if "bmi" in f.name or "ratio" in f.name]


def test_a_derived_value_is_none_rather_than_a_division_error() -> None:
    """A zero or negative input is a data problem, not an exception."""
    assert body_mass_index(weight_kg=Decimal("70"), height_cm=Decimal("0")) is None
    assert body_mass_index(weight_kg=Decimal("0"), height_cm=Decimal("170")) is None
    assert body_mass_index(weight_kg=Decimal("-70"), height_cm=Decimal("170")) is None
    assert waist_hip_ratio(waist_cm=Decimal("80"), hip_cm=Decimal("0")) is None
    assert waist_hip_ratio(waist_cm=Decimal("0"), hip_cm=Decimal("100")) is None


def test_whr_keeps_two_decimals() -> None:
    """A ratio near 0.85 needs both, or the number cannot move meaningfully."""
    assert waist_hip_ratio(waist_cm=Decimal("80"), hip_cm=Decimal("100")) == Decimal("0.80")
    assert waist_hip_ratio(waist_cm=Decimal("86"), hip_cm=Decimal("101")) == Decimal("0.85")


def test_no_derived_value_carries_a_band_or_a_label() -> None:
    """⚠️ 🔒 OD-08 is unresolved, and the DoD forbids an uncited threshold.

    Indian BMI cut-offs differ from WHO's. Until a sourced set lands in
    `clinical_reference_ranges` (DB §7.5), a number is a fact we can publish and
    "overweight" is a judgement we cannot. This guards the whole kernel surface,
    because the tempting place to add a band is next to the calculation.
    """
    import app.kernel.clinical as module

    forbidden = ("band", "category", "categor", "classify", "classification", "interpret")
    offenders = [
        name
        for name in dir(module)
        if not name.startswith("_") and any(word in name.lower() for word in forbidden)
    ]
    assert not offenders, (
        f"{offenders} looks like a clinical band. OD-08 is unresolved and the "
        "implementation plan's DoD forbids displaying a threshold without a "
        "citation — bands belong in `clinical_reference_ranges`, sourced."
    )


@pytest.mark.parametrize(
    ("feet", "inches", "expected"),
    [
        (5, 7, "170.2"),
        (5, 0, "152.4"),
        (6, 1, "185.4"),
        (0, 30, "76.2"),
        (5, Decimal("6.5"), "168.9"),
    ],
)
def test_feet_and_inches_normalise_to_centimetres(feet: int, inches: Any, expected: str) -> None:
    """🔒 FR-M3-015 — normalised at the boundary, because storage is metric only.

    Accepting both units and converting on entry is what stops a `unit` column
    existing, and a unit column is how a 5 comes to mean five feet in one row and
    five centimetres in the next.
    """
    assert height_from_feet_inches(feet, inches) == Decimal(expected)


def test_a_height_of_zero_is_refused() -> None:
    """An empty ft/in form posts zeroes, which is not a height."""
    with pytest.raises(ValidationError):
        height_from_feet_inches(0, 0)
    with pytest.raises(ValidationError):
        height_from_feet_inches(-1, 0)


def test_a_normalised_height_is_storable() -> None:
    """🔒 The two rules meet here: FR-M3-015's conversion must satisfy DB §7.5.

    A conversion that produced a value the CHECK refuses would turn a legitimate
    ft/in entry into a database error the practitioner cannot act on.
    """
    for feet, inches in ((3, 0), (5, 7), (7, 6)):
        assert_storable(weight_kg=None, height_cm=height_from_feet_inches(feet, inches))


# ─── Plausibility (EC-M3-02) ──────────────────────────────────────────────


def test_the_plausible_range_sits_strictly_inside_the_storable_one() -> None:
    """🔒 EC-M3-02's entire design, expressed as an inequality.

    Two bounds do different jobs: the CHECK is what the database will not hold at
    all, and the plausibility rail is what a human should look at twice. If they
    ever met, either a real outlier would be unrecordable or a typo would be
    stored silently — and the confirmation step would have nothing to mean.
    """
    assert MIN_WEIGHT_KG < PLAUSIBLE_WEIGHT_KG[0] < PLAUSIBLE_WEIGHT_KG[1] < MAX_WEIGHT_KG
    assert MIN_HEIGHT_CM < PLAUSIBLE_HEIGHT_CM[0] < PLAUSIBLE_HEIGHT_CM[1] < MAX_HEIGHT_CM


def test_a_real_outlier_is_warned_about_and_then_recordable() -> None:
    """🔒 EC-M3-02 — a 180 kg client exists, and their record matters most.

    Implausible *and* storable is the combination the two-step depends on: the
    first attempt is refused with a warning, and the confirmed resend succeeds.
    A system that refuses is useless exactly when the reading is significant.
    """
    outlier = Decimal("260")
    assert is_implausible(weight_kg=outlier, height_cm=None) is True
    assert_storable(weight_kg=outlier, height_cm=None)


def test_confirming_a_value_cannot_make_it_storable() -> None:
    """🔒 The order of the two checks in `measurements.record`, pinned here.

    A 900 kg weight is a typo whatever the caller confirms, and the CHECK would
    refuse it anyway — as an unhandled database error rather than a message. The
    outer bound is not a confirmable warning.
    """
    with pytest.raises(ValidationError):
        assert_storable(weight_kg=Decimal("900"), height_cm=None)
    with pytest.raises(ValidationError):
        assert_storable(weight_kg=Decimal("1"), height_cm=None)
    with pytest.raises(ValidationError):
        assert_storable(weight_kg=None, height_cm=Decimal("300"))
    with pytest.raises(ValidationError):
        assert_storable(weight_kg=None, height_cm=Decimal("10"))


def test_an_unstorable_value_names_the_units() -> None:
    """NFR-063 — the likeliest cause of a rejected weight is pounds in a kg box."""
    with pytest.raises(ValidationError) as raised:
        assert_storable(weight_kg=Decimal("600"), height_cm=None)
    assert "units" in (raised.value.action or "").lower()


@pytest.mark.parametrize(
    ("weight", "height", "flagged"),
    [
        ("70", "170", False),
        ("25", "100", False),
        ("250", "230", False),
        ("24.9", None, True),
        ("250.1", None, True),
        (None, "99", True),
        (None, "231", True),
        ("70", "99", True),
        (None, None, False),
    ],
)
def test_implausibility_is_inclusive_at_the_rails(
    weight: str | None, height: str | None, flagged: bool
) -> None:
    """⚠️ The rails themselves are plausible; only outside them is a warning.

    A 25 kg client is a child, and asking a paediatric practitioner to confirm
    every reading would train them to click through the warning — which is how a
    real confirmation step stops working.
    """
    assert (
        is_implausible(
            weight_kg=Decimal(weight) if weight else None,
            height_cm=Decimal(height) if height else None,
        )
        is flagged
    )


def test_a_measurement_with_neither_weight_nor_height_is_not_flagged() -> None:
    """⚠️ A waist-only entry has nothing to be implausible about.

    The rails cover weight and height; a client measuring only their waist should
    not be asked to confirm a value nobody checked.
    """
    assert is_implausible(weight_kg=None, height_cm=None) is False
    assert_storable(weight_kg=None, height_cm=None)


# ─── Display precedence (EC-M3-05) ────────────────────────────────────────


def test_the_practitioner_value_wins_on_a_shared_date() -> None:
    """🔒 EC-M3-05 — "both retained; practitioner value takes display precedence".

    A device outranks a self-report because it is an instrument rather than a
    recollection, and both yield to the practitioner who was in the room.
    """
    assert display_precedence(MeasurementSource.PRACTITIONER) < display_precedence(
        MeasurementSource.DEVICE
    )
    assert display_precedence(MeasurementSource.DEVICE) < display_precedence(
        MeasurementSource.CLIENT
    )


def test_every_source_has_a_distinct_precedence() -> None:
    """🔒 A completeness guard: the map is read with ``[]``.

    A source added to the enum without a precedence raises `KeyError` while
    rendering a trend — a 500 on a page that was working, for the one client who
    happens to have that row. Ties are excluded too: a tie would make the
    displayed value depend on iteration order, which is not a rule.
    """
    ranks = [display_precedence(source) for source in MeasurementSource]
    assert len(set(ranks)) == len(list(MeasurementSource))


# ─── Documents (FR-M3-024) ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "normalised"),
    [
        ("lab_report", "lab_report"),
        ("Lab Report", "lab_report"),
        ("  LAB-REPORT  ", "lab_report"),
        ("Progress Photo", "progress_photo"),
        ("insurance letter", "insurance_letter"),
        ("Ayurvedic Prakriti Assessment", "ayurvedic_prakriti_assessment"),
        ("bloods2026", "bloods2026"),
    ],
)
def test_a_document_label_is_normalised_to_a_slug(raw: str, normalised: str) -> None:
    """FR-M3-024 — case and spacing must not fragment the filter.

    ⚠️ Unknown labels are accepted. Refusing them would make the vocabulary
    release-gated, which is exactly what a free-text column with a validated
    shape exists to avoid — a practice needing "insurance letter" should not wait
    for a migration.
    """
    assert validate_document_type(raw) == normalised


def test_every_offered_document_type_survives_its_own_validator() -> None:
    """🔒 The UI offers `DOCUMENT_TYPES`; the validator must accept all of them.

    A member that normalised to something else would store a different value than
    the filter searches for, and the document would vanish from the list it was
    uploaded into.
    """
    for offered in DOCUMENT_TYPES:
        assert validate_document_type(offered) == offered


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ("", "nothing chosen"),
        ("   ", "whitespace is not a label"),
        ("___", "no alphanumeric content"),
        ("lab/report", "punctuation is not a slug"),
        ("report:2026", "punctuation is not a slug"),
        ("x" * (MAX_DOCUMENT_TYPE_LENGTH + 1), "a label, not a description"),
        (
            "Lab report from the endocrinologist about the thyroid panel",
            "a sentence in a label column is a clinical detail in a filter",
        ),
    ],
)
def test_a_label_that_is_not_a_label_is_refused(raw: str, why: str) -> None:
    with pytest.raises(ValidationError):
        validate_document_type(raw)


# ─── The event contract (DDR-06, NFR-033) ─────────────────────────────────


def test_the_clinical_events_are_registered_under_stable_wire_names() -> None:
    """🔒 DDR-06 — the name is the job payload's discriminator.

    A rename would make every queued job referencing the old one undeliverable,
    so the names are pinned here rather than derived from the class.
    """
    assert AssessmentCompleted.event_name == "assessment.completed"
    assert MeasurementRecorded.event_name == "measurement.recorded"
    assert DocumentUploaded.event_name == "document.uploaded"
    assert ConsultationNoteRecorded.event_name == "consultation_note.recorded"


def test_an_assessment_event_carries_no_answer_content() -> None:
    """🔒 NFR-033 — the timeline summary is built from enums only.

    An assessment's answers are the most sensitive text in the product. The
    guarantee is structural: the content is absent from the event, so no future
    subscriber can put a medical history into a timeline row either.
    """
    names = {f.name for f in fields(AssessmentCompleted)}
    assert "answers" not in names
    assert "response_id" in names
    # ⚠️ The version travels because a subscriber may legitimately need to know
    # which structure was answered; the answers themselves never do.
    assert {"definition_code", "definition_version"} <= names


def test_a_document_event_carries_no_filename() -> None:
    """🔒 A filename is user-supplied text that routinely names a condition.

    "priya-thyroid-2026.pdf" in a timeline row leaks a diagnosis into the surface
    most likely to be read in bulk — and, in S6, projected to the client.
    """
    names = {f.name for f in fields(DocumentUploaded)}
    assert "filename" not in names
    assert "storage_key" not in names
    assert "document_id" in names


def test_a_note_event_carries_no_body() -> None:
    """🔒 FR-M3-021 — a consultation note is never client-visible.

    Even reaching the event bus, it carries only an id: the note's text cannot be
    put anywhere by anything downstream.
    """
    names = {f.name for f in fields(ConsultationNoteRecorded)}
    assert "body" not in names
    assert "note_id" in names


def test_the_note_event_has_no_timeline_subscriber() -> None:
    """🔒 AC-M3-006 / DB §5.6 — even the *existence* of a note is a leak.

    The other three clinical events write a timeline row. This one must not: the
    client portal reads a projection of `timeline_events` (S6), and a row saying
    "consultation note recorded" tells the client a note about them exists —
    which FR-M3-021 says they may not learn.

    ⚠️ Asserted against the live subscriber registry rather than by reading the
    registration block, because the failure mode is somebody adding a fourth
    `subscribe` call for symmetry.
    """
    from app.kernel.events import _TRANSACTIONAL
    from app.modules.clients.timeline import register_subscribers

    register_subscribers()

    assert _TRANSACTIONAL.get(ConsultationNoteRecorded, []) == [], (
        "ConsultationNoteRecorded has gained a subscriber. If it writes a "
        "timeline row, the client portal's projection (S6) will disclose that a "
        "consultation note exists — FR-M3-021, AC-M3-006, DB §5.6."
    )
    # The other three do write, and their absence would be just as wrong.
    for writes in (AssessmentCompleted, MeasurementRecorded, DocumentUploaded):
        assert _TRANSACTIONAL.get(writes), f"{writes.__name__} writes no timeline row (FR-M1-018)."


def test_every_clinical_event_survives_payload_encoding() -> None:
    """🔒 `to_payload` refuses anything that is not an identifier, enum or stamp.

    A failure would abort the publish *inside* the publisher's transaction,
    rolling back the practitioner's action — so the contract is asserted here
    rather than discovered when a measurement refuses to save.
    """
    tenant = uuid.uuid4()
    events = [
        AssessmentCompleted(
            client_id=CLIENT,
            tenant_id=tenant,
            response_id=RESPONSE,
            definition_code="nutrition_core",
            definition_version=1,
            completed_by_client=True,
            occurred_at=MOMENT,
        ),
        MeasurementRecorded(
            client_id=CLIENT,
            tenant_id=tenant,
            measurement_id=uuid.uuid4(),
            source=MeasurementSource.PRACTITIONER,
            occurred_at=MOMENT,
            actor_user_id=uuid.uuid4(),
        ),
        DocumentUploaded(
            client_id=CLIENT,
            tenant_id=tenant,
            document_id=uuid.uuid4(),
            document_type="lab_report",
            uploaded_by_client=False,
            occurred_at=MOMENT,
            actor_user_id=uuid.uuid4(),
        ),
        ConsultationNoteRecorded(
            client_id=CLIENT,
            tenant_id=tenant,
            note_id=uuid.uuid4(),
            occurred_at=MOMENT,
            actor_user_id=uuid.uuid4(),
        ),
    ]

    for event in events:
        payload = to_payload(event)
        assert payload["_event"] == type(event).event_name
        assert payload["tenant_id"] == str(tenant)
        assert payload["client_id"] == str(CLIENT)


def test_a_client_completed_assessment_is_distinguishable() -> None:
    """🔒 FR-M3-004 — who filled it in changes what the row *means*.

    "The client completed their assessment" is a prompt to review it; "you
    completed it for them" is a record of work already done. The timeline actor
    and EC-M3-05's measurement attribution both read this flag.
    """
    by_client = AssessmentCompleted(
        client_id=CLIENT,
        tenant_id=uuid.uuid4(),
        response_id=RESPONSE,
        definition_code="nutrition_core",
        definition_version=1,
        completed_by_client=True,
        occurred_at=MOMENT,
    )
    assert by_client.completed_by_client is True
    # ⚠️ No practitioner actor on a client-completed assessment: the client's own
    # id is not a `users.id`, so the column would be wrong rather than merely empty.
    assert by_client.actor_user_id is None


def test_the_vocabularies_the_projection_depends_on_are_closed() -> None:
    """⚠️ 🟡 PROPOSED (Gate G4) — pinned so a revision is a deliberate edit.

    `ActivityLevel` feeds the requirement estimate and `GoalType` comes verbatim
    from PRD §9.5. Both are wording questions rather than settled clinical facts,
    and both are already bound by the seed — changing a *value* silently
    invalidates every profile that projected the old one.
    """
    assert [m.value for m in ActivityLevel] == [
        "sedentary",
        "light",
        "moderate",
        "active",
        "very_active",
    ]
    assert "weight_loss" in {m.value for m in GoalType}
    assert "other" in {m.value for m in GoalType}
