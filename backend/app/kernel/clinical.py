"""The rules of the clinical workspace — PRD M3, DB §7, DDR-07/08.

🔒 No database. ``kernel.clinical`` is split from the persistence in
``app.modules.clinical`` so every rule below is a pure function over values,
testable without a cluster — the same split ``kernel.leads`` makes.

Four groups, each pinning a different failure the module invites:

* **The definition engine** — FR-M3-002/003. Assessment structure is *data*, so
  the code here validates and walks a schema it did not author. The rules that
  matter are the ones that keep an old response readable: a published definition
  is immutable, and a response pins the version it started under.
* **The projection** — DDR-08. The handful of values nutrition calculates from
  are lifted out of ``jsonb`` into typed columns by ``calculation_bindings``.
  This is the seam that stops the nutrition module ever parsing an assessment.
* **Anthropometry** — FR-M3-012/015. BMI and waist-hip ratio are *derived*,
  never stored, and height arrives in two unit systems.
* **Plausibility** — EC-M3-02. A 400 kg weight is warned about and then
  accepted, because refusing a real outlier is worse than recording one.

⚠️ 🔒 **No clinical thresholds live here, deliberately.** BMI *bands* —
"overweight", "obese" — are OD-08 and unresolved: Indian cut-offs differ from
WHO, and the implementation plan's Definition of Done says "no clinical
threshold displayed without a citation". :func:`body_mass_index` returns a
number and nothing else. The band belongs in ``clinical_reference_ranges``
(DB §7.5) once sourced, which is a configuration row rather than a constant in
this file — so correcting it is not a release.
"""

from __future__ import annotations

import contextlib
import enum
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

from app.kernel.clients import DietaryClass, SexType
from app.kernel.errors import ConflictError, ValidationError
from app.kernel.events import DomainEvent

# ─── Vocabulary (DB §7.2, §7.3, §7.5) ────────────────────────────────────


class DefinitionStatus(str, enum.Enum):
    """Where a definition is in its life — DB §7.2 ``definition_status``.

    🔒 ``PUBLISHED`` is a one-way door. FR-M3-003 requires a completed response
    to stay readable under the structure in force when it was captured, and the
    only way to guarantee that is for the structure never to change once a
    response can point at it. Editing means a new version.
    """

    DRAFT = "draft"
    PUBLISHED = "published"
    RETIRED = "retired"


class ResponseStatus(str, enum.Enum):
    """Whether an administration is finished — DB §7.3 ``response_status``.

    ⚠️ Only two. "Abandoned" is not a state: EC-M3-01 says a partial response is
    retained and resumable, so an untouched ``in_progress`` row is exactly what
    an abandonment looks like, and the practitioner reads its completion
    percentage rather than a label somebody had to set.
    """

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class MeasurementSource(str, enum.Enum):
    """Who recorded a measurement — DB §7.5 ``measurement_source``.

    🔒 EC-M3-05 — a practitioner and a client may both record a weight on the
    same date, and both are kept. This column is what tells them apart, and what
    the display-precedence rule reads.
    """

    PRACTITIONER = "practitioner"
    CLIENT = "client"
    DEVICE = "device"


class ActivityLevel(str, enum.Enum):
    """Habitual activity, for requirement estimation — DB §7.4.

    ⚠️ 🟡 **PROPOSED.** DB §7.4 names the column and its type and stops there.
    These five are the standard Harris-Benedict / Mifflin-St Jeor activity
    multipliers' labels, which is what the column exists to feed — but the
    wording a practitioner sees is a §9 review question (Gate G4).
    """

    SEDENTARY = "sedentary"
    LIGHT = "light"
    MODERATE = "moderate"
    ACTIVE = "active"
    VERY_ACTIVE = "very_active"


class GoalType(str, enum.Enum):
    """What the client is trying to achieve — DB §7.4, PRD §9.5.

    ⚠️ 🟡 **PROPOSED**, and taken verbatim from PRD §9.5's own proposed list so
    that a Gate G4 revision has one place to land.
    """

    WEIGHT_LOSS = "weight_loss"
    WEIGHT_GAIN = "weight_gain"
    MUSCLE_GAIN = "muscle_gain"
    MANAGE_CONDITION = "manage_condition"
    IMPROVE_ENERGY = "improve_energy"
    IMPROVE_DIGESTION = "improve_digestion"
    SPORTS_PERFORMANCE = "sports_performance"
    GENERAL_WELLBEING = "general_wellbeing"
    OTHER = "other"


class FieldType(str, enum.Enum):
    """The input types a definition may declare — DB §7.2 ``schema``.

    🔒 **A closed set, and that is the point of FR-M3-002 being safe.** The
    frontend renders a form from data it has never seen; it can only do that if
    the vocabulary of types is fixed. Adding a *field* needs no release (the
    requirement); adding a *type* needs one on both sides (acceptable, and rare).

    ⚠️ ``FOOD_REF`` exists but nothing can populate it until S3 seeds the food
    catalogue. It is declared now because DB §7.4 is explicit that allergens are
    captured as food ids and **never as free text** — a missed allergen is a
    clinical incident (FR-M5-006). Declaring the type now means the definition
    that needs it does not also need a code change.
    """

    TEXT = "text"
    LONG_TEXT = "long_text"
    NUMBER = "number"
    DATE = "date"
    BOOLEAN = "boolean"
    CHOICE = "choice"
    MULTI_CHOICE = "multi_choice"
    SCALE = "scale"
    #: 🔒 A reference to a food in the catalogue, by id. Allergens and exclusions.
    FOOD_REF = "food_ref"


# ─── The projection contract (DDR-08) ────────────────────────────────────


class ProjectionTarget(str, enum.Enum):
    """The typed columns an assessment field may project into — DB §7.4.

    🔒 **This enum *is* the contract DDR-08 describes.** The assessment schema
    may change freely; this may not, because `nutrition` and `ai_drafting` read
    these columns and must never learn an assessment field id.

    ⚠️ A binding naming a target absent here is refused at publish time rather
    than silently ignored — see :func:`validate_calculation_bindings`. A binding
    that quietly does nothing is the worst outcome available: the practitioner
    fills in an allergy, the projection drops it, and the AI candidate filter
    never sees it.
    """

    DATE_OF_BIRTH = "date_of_birth"
    SEX = "sex"
    HEIGHT_CM = "height_cm"
    ACTIVITY_LEVEL = "activity_level"
    PRIMARY_GOAL = "primary_goal"
    DIETARY_CLASS = "dietary_class"
    EXCLUDES_ONION_GARLIC = "excludes_onion_garlic"
    EXCLUDES_ROOT_VEGETABLES = "excludes_root_vegetables"
    ALLERGEN_FOOD_IDS = "allergen_food_ids"
    EXCLUDED_FOOD_IDS = "excluded_food_ids"
    FASTING_PATTERNS = "fasting_patterns"
    STAPLE_GRAIN = "staple_grain"
    REGION_CUISINE = "region_cuisine"


#: 🔒 What each target will accept, so a binding is checked against the *column*
#: rather than against whatever the first response happened to contain.
#:
#: ⚠️ The enum-valued targets are checked against the real Python enums
#: (``SexType``, ``DietaryClass``, …) rather than against a list of strings
#: repeated here. A second copy of the vocabulary is a second thing to update.
_TARGET_TYPES: dict[ProjectionTarget, frozenset[FieldType]] = {
    ProjectionTarget.DATE_OF_BIRTH: frozenset({FieldType.DATE}),
    ProjectionTarget.SEX: frozenset({FieldType.CHOICE}),
    ProjectionTarget.HEIGHT_CM: frozenset({FieldType.NUMBER}),
    ProjectionTarget.ACTIVITY_LEVEL: frozenset({FieldType.CHOICE}),
    ProjectionTarget.PRIMARY_GOAL: frozenset({FieldType.CHOICE}),
    ProjectionTarget.DIETARY_CLASS: frozenset({FieldType.CHOICE}),
    ProjectionTarget.EXCLUDES_ONION_GARLIC: frozenset({FieldType.BOOLEAN}),
    ProjectionTarget.EXCLUDES_ROOT_VEGETABLES: frozenset({FieldType.BOOLEAN}),
    ProjectionTarget.ALLERGEN_FOOD_IDS: frozenset({FieldType.FOOD_REF}),
    ProjectionTarget.EXCLUDED_FOOD_IDS: frozenset({FieldType.FOOD_REF}),
    ProjectionTarget.FASTING_PATTERNS: frozenset({FieldType.MULTI_CHOICE}),
    ProjectionTarget.STAPLE_GRAIN: frozenset({FieldType.TEXT, FieldType.CHOICE}),
    ProjectionTarget.REGION_CUISINE: frozenset({FieldType.TEXT, FieldType.CHOICE}),
}

#: The targets whose value is an enum, and the enum each one takes.
_ENUM_TARGETS: dict[ProjectionTarget, type[enum.Enum]] = {
    ProjectionTarget.SEX: SexType,
    ProjectionTarget.ACTIVITY_LEVEL: ActivityLevel,
    ProjectionTarget.PRIMARY_GOAL: GoalType,
    ProjectionTarget.DIETARY_CLASS: DietaryClass,
}


@dataclass(frozen=True, slots=True)
class NutritionProfile:
    """The projected, typed view of an assessment — DB §7.4.

    🔒 **The whole of what `nutrition` and `ai_drafting` may read**, and they
    read it through a kernel port (:class:`NutritionProfileReader`) rather than
    by importing `clinical`. That is R3 held in place by a type.
    """

    client_id: uuid.UUID
    source_response_id: uuid.UUID | None = None
    date_of_birth: date | None = None
    sex: SexType | None = None
    height_cm: Decimal | None = None
    activity_level: ActivityLevel | None = None
    primary_goal: GoalType | None = None
    dietary_class: DietaryClass | None = None
    excludes_onion_garlic: bool = False
    excludes_root_vegetables: bool = False
    #: 🔒 Safety-critical (FR-M5-006). Food ids, never parsed from prose.
    allergen_food_ids: tuple[uuid.UUID, ...] = ()
    excluded_food_ids: tuple[uuid.UUID, ...] = ()
    fasting_patterns: tuple[str, ...] = ()
    staple_grain: str | None = None
    region_cuisine: str | None = None


class NutritionProfileReader(Protocol):
    """The port `nutrition` and `ai_drafting` read the projection through.

    🔒 DB §7: "Readers: `nutrition` and `ai_drafting` need assessment +
    measurement data ... via kernel port". R3 forbids those modules importing
    `clinical`, and this is the seam that makes the dependency expressible
    without one. Same shape as ``ClientDirectory`` (S2-A) and ``CredentialStore``.
    """

    async def profile_for(
        self, *, tenant_id: uuid.UUID, client_id: uuid.UUID
    ) -> NutritionProfile | None:
        """The client's current profile, or ``None`` if no assessment is done."""
        ...


# ─── Definition schema validation (FR-M3-002) ────────────────────────────


@dataclass(frozen=True, slots=True)
class SchemaField:
    """One question, as declared by a definition's ``schema``."""

    id: str
    type: FieldType
    label: str
    required: bool = False
    #: For CHOICE / MULTI_CHOICE. Ignored by every other type.
    options: tuple[str, ...] = ()
    #: Inclusive bounds for NUMBER and SCALE.
    min_value: Decimal | None = None
    max_value: Decimal | None = None
    help_text: str | None = None


@dataclass(frozen=True, slots=True)
class SchemaSection:
    """One page of the form — PRD §9.3's A–L.

    ⚠️ ``is_clinical`` is what FR-M3-006 and AC-M3-002 turn on. A coach persona
    (P2, Rahul) must be able to skip every clinical section and still produce a
    usable assessment, and "usable" is decided by whether the projection got
    what it needed — never by how many sections were filled.
    """

    id: str
    title: str
    fields: tuple[SchemaField, ...]
    #: 🔒 FR-M3-006 — skippable individually and as a group.
    is_clinical: bool = False
    description: str | None = None


@dataclass(frozen=True, slots=True)
class AssessmentSchema:
    """A whole form structure, parsed from ``assessment_definitions.schema``."""

    sections: tuple[SchemaSection, ...]

    def field_index(self) -> dict[str, SchemaField]:
        return {f.id: f for section in self.sections for f in section.fields}

    def section_of(self, field_id: str) -> SchemaSection | None:
        for section in self.sections:
            if any(f.id == field_id for f in section.fields):
                return section
        return None


def _schema_error(message: str) -> ValidationError:
    """A malformed definition — always our defect, never a user's input.

    🔒 NFR-063 requires every error to say what to do next, and the honest
    "next" here is not something the caller can do: definitions are authored by
    us (FR-M3-009/010 are Phase 2/3), so a malformed one is a bad seed or a bad
    migration. The action says so rather than inventing a step for a
    practitioner who cannot act on it.
    """
    return ValidationError(
        message,
        action="This assessment form is misconfigured — please report it to support.",
    )


def parse_schema(raw: Any) -> AssessmentSchema:
    """Read a stored ``schema`` document into typed values.

    🔒 **Every failure here is a programming error, not user input.** A
    definition is authored by us (FR-M3-009/010 are Phase 2/3), so a malformed
    schema means a bad seed or a bad migration — it must fail loudly at publish
    time rather than render an empty form to a client.

    Raises:
        ValidationError: The document is not a well-formed schema.
    """
    if not isinstance(raw, dict):
        raise _schema_error("An assessment schema must be an object.")

    sections_raw = raw.get("sections")
    if not isinstance(sections_raw, list) or not sections_raw:
        raise _schema_error("An assessment schema must declare at least one section.")

    seen_field_ids: set[str] = set()
    seen_section_ids: set[str] = set()
    sections: list[SchemaSection] = []

    for entry in sections_raw:
        if not isinstance(entry, dict):
            raise _schema_error("Each assessment section must be an object.")

        section_id = _require_str(entry, "id", "section")
        if section_id in seen_section_ids:
            raise _schema_error(f"Duplicate section id {section_id!r} in the assessment schema.")
        seen_section_ids.add(section_id)

        fields_raw = entry.get("fields")
        if not isinstance(fields_raw, list) or not fields_raw:
            raise _schema_error(f"Section {section_id!r} declares no fields.")

        fields: list[SchemaField] = []
        for field_raw in fields_raw:
            if not isinstance(field_raw, dict):
                raise _schema_error(f"Each field in section {section_id!r} must be an object.")

            field_id = _require_str(field_raw, "id", "field")
            # 🔒 Ids are unique across the whole document, not per section:
            # `answers` is one flat object keyed by field id (DB §7.3), so two
            # sections sharing an id would overwrite each other's answer.
            if field_id in seen_field_ids:
                raise _schema_error(
                    f"Duplicate field id {field_id!r}. Answers are keyed by field id, "
                    "so two fields sharing one id would overwrite each other."
                )
            seen_field_ids.add(field_id)

            try:
                field_type = FieldType(_require_str(field_raw, "type", "field"))
            except ValueError as exc:
                raise _schema_error(
                    f"Field {field_id!r} declares an unknown type. The renderer can only "
                    f"draw the types in `FieldType`."
                ) from exc

            options = tuple(str(o) for o in field_raw.get("options", []))
            if field_type in (FieldType.CHOICE, FieldType.MULTI_CHOICE) and not options:
                raise _schema_error(f"Field {field_id!r} is a choice but declares no options.")

            fields.append(
                SchemaField(
                    id=field_id,
                    type=field_type,
                    label=_require_str(field_raw, "label", "field"),
                    required=bool(field_raw.get("required", False)),
                    options=options,
                    min_value=_optional_decimal(field_raw.get("min")),
                    max_value=_optional_decimal(field_raw.get("max")),
                    help_text=_optional_str(field_raw.get("help_text")),
                )
            )

        sections.append(
            SchemaSection(
                id=section_id,
                title=_require_str(entry, "title", "section"),
                fields=tuple(fields),
                is_clinical=bool(entry.get("is_clinical", False)),
                description=_optional_str(entry.get("description")),
            )
        )

    return AssessmentSchema(sections=tuple(sections))


def _require_str(source: dict[str, Any], key: str, what: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _schema_error(f"Every assessment {what} needs a non-empty {key!r}.")
    return value


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise _schema_error("A field's `min` and `max` must be numbers.") from exc


def validate_calculation_bindings(
    raw: Any, schema: AssessmentSchema
) -> dict[ProjectionTarget, str]:
    """Check the bindings against the schema they claim to project — DDR-08.

    🔒 **A binding that silently does nothing is the failure mode to prevent.**
    If a binding names a field that does not exist, or a target that is not a
    real column, the practitioner fills the form in, the projection drops the
    value, and nutrition calculates from a gap it cannot see. Refusing at
    publish time is the only moment this is cheap.

    Returns:
        The bindings as ``{target: field_id}``.

    Raises:
        ValidationError: A binding names an unknown target, an unknown field, or
            a field whose type the target cannot hold.
    """
    if not isinstance(raw, dict):
        raise _schema_error("`calculation_bindings` must be an object of target → field id.")

    fields = schema.field_index()
    bindings: dict[ProjectionTarget, str] = {}

    for target_name, field_id in raw.items():
        try:
            target = ProjectionTarget(target_name)
        except ValueError as exc:
            raise _schema_error(
                f"`calculation_bindings` names {target_name!r}, which is not a column on "
                "`client_nutrition_profile`. The projection contract is `ProjectionTarget`."
            ) from exc

        if not isinstance(field_id, str) or field_id not in fields:
            raise _schema_error(
                f"`calculation_bindings` maps {target_name!r} to {field_id!r}, which is not a "
                "field in this schema. A binding to a missing field would drop the value "
                "silently."
            )

        permitted = _TARGET_TYPES[target]
        actual = fields[field_id].type
        if actual not in permitted:
            raise _schema_error(
                f"Field {field_id!r} is a {actual.value}, which cannot be projected into "
                f"{target_name!r}. That column accepts: "
                f"{', '.join(sorted(t.value for t in permitted))}."
            )

        bindings[target] = field_id

    return bindings


def assert_publishable(status: DefinitionStatus) -> None:
    """🔒 A published definition is immutable — DB §7.2, FR-M3-003.

    Editing a published structure would silently change what an already-captured
    response *means*: the answers stay put while the question they answered
    moves. AC-M3-003 is the test, and this is the rule it rests on.
    """
    if status is not DefinitionStatus.DRAFT:
        raise ConflictError(
            "This assessment version has already been published and cannot be changed.",
            action="Create a new version instead — earlier responses stay readable under theirs.",
        )


# ─── Answer validation (FR-M3-005, EC-M3-02) ─────────────────────────────


@dataclass(frozen=True, slots=True)
class AnswerIssue:
    """One thing wrong with one answer."""

    field_id: str
    message: str


def validate_answers(
    answers: dict[str, Any], schema: AssessmentSchema, *, partial: bool
) -> list[AnswerIssue]:
    """Check answers against their schema.

    Args:
        partial: ``True`` for a save-as-you-go write (FR-M3-005), where a missing
            required answer is expected rather than wrong. ``False`` on submit.

    🔒 **Required-ness is only enforced on submit**, and only for non-clinical
    sections — FR-M3-006 makes clinical sections skippable, and AC-M3-002
    requires a coach who skipped all of them to still produce a usable result.

    ⚠️ Returns issues rather than raising, so a form can show every problem at
    once. A validator that raises on the first fault makes a twelve-field
    section a twelve-round-trip conversation.
    """
    issues: list[AnswerIssue] = []
    fields = schema.field_index()

    for field_id, value in answers.items():
        # ⚠️ An answer to a field the schema does not declare is dropped rather
        # than rejected: a client with a stale form open would otherwise be
        # unable to save anything at all (EC-M3-03).
        declared = fields.get(field_id)
        if declared is None or value is None:
            continue
        issues.extend(_check_value(declared, value))

    if not partial:
        for section in schema.sections:
            if section.is_clinical:
                continue
            for declared in section.fields:
                if declared.required and _is_blank(answers.get(declared.id)):
                    issues.append(
                        AnswerIssue(declared.id, f"{declared.label} is needed before submitting.")
                    )

    return issues


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list | tuple):
        return len(value) == 0
    return False


def _check_value(declared: SchemaField, value: Any) -> list[AnswerIssue]:
    """Type- and range-check one answer."""
    issues: list[AnswerIssue] = []

    if declared.type in (FieldType.CHOICE,):
        if str(value) not in declared.options:
            issues.append(AnswerIssue(declared.id, f"Choose one of the offered {declared.label}."))

    elif declared.type is FieldType.MULTI_CHOICE:
        if not isinstance(value, list | tuple):
            issues.append(AnswerIssue(declared.id, f"{declared.label} takes a list of choices."))
        else:
            unknown = [str(v) for v in value if str(v) not in declared.options]
            if unknown:
                issues.append(
                    AnswerIssue(declared.id, f"{declared.label} has an unrecognised choice.")
                )

    elif declared.type in (FieldType.NUMBER, FieldType.SCALE):
        try:
            number = Decimal(str(value))
        except Exception:
            issues.append(AnswerIssue(declared.id, f"{declared.label} must be a number."))
        else:
            if declared.min_value is not None and number < declared.min_value:
                issues.append(
                    AnswerIssue(declared.id, f"{declared.label} looks too low — please check it.")
                )
            if declared.max_value is not None and number > declared.max_value:
                issues.append(
                    AnswerIssue(declared.id, f"{declared.label} looks too high — please check it.")
                )

    elif declared.type is FieldType.BOOLEAN:
        if not isinstance(value, bool):
            issues.append(AnswerIssue(declared.id, f"{declared.label} must be yes or no."))

    elif declared.type is FieldType.FOOD_REF:
        # 🔒 DB §7.4 — allergens are food ids, never free text. A string here is
        # exactly the "nuts typed into a box" case that cannot be matched to a
        # food reliably, and a missed allergen is a clinical incident.
        values = value if isinstance(value, list | tuple) else [value]
        for item in values:
            try:
                uuid.UUID(str(item))
            except ValueError:
                issues.append(
                    AnswerIssue(
                        declared.id,
                        f"{declared.label} must be chosen from the food list, not typed.",
                    )
                )
                break

    elif declared.type is FieldType.DATE and not _parses_as_date(value):
        issues.append(AnswerIssue(declared.id, f"{declared.label} must be a date."))

    return issues


def _parses_as_date(value: Any) -> bool:
    if isinstance(value, date):
        return True
    try:
        date.fromisoformat(str(value))
    except ValueError:
        return False
    return True


def completion(
    answers: dict[str, Any], schema: AssessmentSchema, completed_sections: frozenset[str]
) -> Decimal:
    """How far through the form the respondent is, as a percentage — EC-M3-01.

    🔒 Counted over **non-clinical** sections only. A coach who legitimately
    skipped every clinical section (AC-M3-002) must not see 40% and conclude
    they did it wrong, and a practitioner reading completion status needs the
    number to mean "is this usable", not "how many boxes are filled".

    ⚠️ A section the respondent explicitly marked done counts fully, even if its
    optional fields are blank — that is what "done" means to the person who said
    it (FR-M3-006).
    """
    relevant = [s for s in schema.sections if not s.is_clinical]
    if not relevant:
        return Decimal("100.0")

    done = 0
    for section in relevant:
        if section.id in completed_sections:
            done += 1
            continue
        # Otherwise a section counts once its required fields are answered and
        # something has actually been entered — a wholly untouched section is
        # not progress, however few required fields it happens to declare.
        if all(not _is_blank(answers.get(f.id)) for f in section.fields if f.required) and any(
            not _is_blank(answers.get(f.id)) for f in section.fields
        ):
            done += 1

    return (Decimal(done) / Decimal(len(relevant)) * 100).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )


# ─── The projection (DDR-08) ─────────────────────────────────────────────


def project(
    answers: dict[str, Any],
    bindings: dict[ProjectionTarget, str],
    *,
    client_id: uuid.UUID,
    source_response_id: uuid.UUID,
) -> NutritionProfile:
    """Lift the calculation-critical answers into typed values — DDR-08.

    🔒 **This function is the reason `nutrition` never parses `jsonb`.** It runs
    once, on submit, and everything downstream reads typed columns.

    ⚠️ **An unparseable answer projects as absent, not as a failure.** The
    assessment has already been accepted by this point; refusing to submit it
    because one optional value cannot be coerced would lose the whole
    administration. A gap is visible to the practitioner and recoverable; a lost
    submission is neither.
    """
    values: dict[str, Any] = {}

    for target, field_id in bindings.items():
        raw = answers.get(field_id)
        if raw is None or _is_blank(raw):
            continue

        if target in _ENUM_TARGETS:
            member = _coerce_enum(_ENUM_TARGETS[target], raw)
            if member is not None:
                values[target.value] = member

        elif target is ProjectionTarget.DATE_OF_BIRTH:
            parsed = _coerce_date(raw)
            if parsed is not None:
                values[target.value] = parsed

        elif target is ProjectionTarget.HEIGHT_CM:
            # ⚠️ Suppressed, not handled: an unparseable height projects as
            # absent (see the docstring). Any coercion failure is the same
            # outcome, so naming the exception types would only be a list to
            # keep in step with `Decimal`.
            with contextlib.suppress(Exception):
                values[target.value] = Decimal(str(raw)).quantize(Decimal("0.1"))

        elif target in (
            ProjectionTarget.EXCLUDES_ONION_GARLIC,
            ProjectionTarget.EXCLUDES_ROOT_VEGETABLES,
        ):
            values[target.value] = bool(raw)

        elif target in (ProjectionTarget.ALLERGEN_FOOD_IDS, ProjectionTarget.EXCLUDED_FOOD_IDS):
            values[target.value] = _coerce_uuids(raw)

        elif target is ProjectionTarget.FASTING_PATTERNS:
            items = raw if isinstance(raw, list | tuple) else [raw]
            values[target.value] = tuple(str(i) for i in items)

        else:
            values[target.value] = str(raw)

    return NutritionProfile(client_id=client_id, source_response_id=source_response_id, **values)


def _coerce_enum(enum_type: type[enum.Enum], raw: Any) -> enum.Enum | None:
    try:
        return enum_type(str(raw))
    except ValueError:
        return None


def _coerce_date(raw: Any) -> date | None:
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def _coerce_uuids(raw: Any) -> tuple[uuid.UUID, ...]:
    """🔒 Food ids only. A value that is not a uuid is dropped, never guessed at."""
    items = raw if isinstance(raw, list | tuple) else [raw]
    parsed: list[uuid.UUID] = []
    for item in items:
        try:
            parsed.append(uuid.UUID(str(item)))
        except ValueError:
            continue
    return tuple(parsed)


# ─── Documents (FR-M3-024…027) ───────────────────────────────────────────

#: 🔒 The document labels the UI offers — DB §7.6's ``document_type``.
#:
#: ⚠️ A **free-text column with a validated vocabulary**, not a database enum,
#: and the difference is deliberate. FR-M3-024 lists the kinds of document a
#: practitioner uploads; that list is a UI affordance, not a clinical invariant.
#: A practice that needs "insurance letter" should not wait for a migration, and
#: nothing in the product branches on the value — it is a filter and a label.
DOCUMENT_TYPES: frozenset[str] = frozenset(
    {
        "lab_report",
        "prescription",
        "imaging",
        "referral",
        "progress_photo",
        "consent_form",
        "other",
    }
)

#: A label a practitioner typed, rather than chose. Bounded so it stays a label.
MAX_DOCUMENT_TYPE_LENGTH = 40


def validate_document_type(document_type: str) -> str:
    """Normalise and check a document label — FR-M3-024.

    ⚠️ Unknown values are **accepted**, not refused, provided they are short and
    slug-shaped. Refusing would make the vocabulary a release-gated list, which
    is what :data:`DOCUMENT_TYPES` exists to avoid; the check that matters is that
    the value is a label rather than a paragraph of free text.
    """
    normalised = document_type.strip().lower().replace(" ", "_").replace("-", "_")
    if not normalised:
        raise ValidationError(
            "Choose what kind of document this is.",
            action="Pick a document type, such as a lab report.",
        )
    if len(normalised) > MAX_DOCUMENT_TYPE_LENGTH:
        raise ValidationError(
            "That document type is too long.",
            action="Use a short label, such as 'lab report'.",
        )
    if not all(part.isalnum() for part in normalised.split("_") if part):
        raise ValidationError(
            "A document type may only contain letters, numbers and spaces.",
            action="Use a short label, such as 'lab report'.",
        )
    return normalised


# ─── Anthropometry (FR-M3-012, FR-M3-015) ────────────────────────────────

#: 🔒 DB §7.5's CHECK bounds, mirrored so the API can refuse before the database
#: does and name the field while doing it.
MIN_WEIGHT_KG = Decimal("2")
MAX_WEIGHT_KG = Decimal("500")
MIN_HEIGHT_CM = Decimal("30")
MAX_HEIGHT_CM = Decimal("275")

#: 🔒 EC-M3-02 — the range outside which a value is *warned about* but still
#: accepted. Narrower than the CHECK bounds above by design: the CHECK is what
#: the database will not store at all, this is what a human should look at twice.
#:
#: ⚠️ 🟡 PROPOSED. These are plausibility rails, not clinical thresholds — they
#: say "please confirm", never "this is unhealthy". Nothing here is a diagnosis,
#: and OD-08's BMI bands are deliberately absent.
PLAUSIBLE_WEIGHT_KG = (Decimal("25"), Decimal("250"))
PLAUSIBLE_HEIGHT_CM = (Decimal("100"), Decimal("230"))

_CM_PER_INCH = Decimal("2.54")
_INCHES_PER_FOOT = 12


def height_from_feet_inches(feet: int, inches: Decimal | int) -> Decimal:
    """Normalise ft/in to centimetres on entry — FR-M3-015.

    🔒 The system stores metric only. Accepting both units and normalising *at
    the boundary* is what stops a unit column existing, and a unit column is how
    a 5 ends up meaning five feet in one row and five centimetres in another.
    """
    total_inches = Decimal(feet * _INCHES_PER_FOOT) + Decimal(str(inches))
    if total_inches <= 0:
        raise ValidationError(
            "A height must be greater than zero.",
            action="Enter the height in feet and inches.",
        )
    return (total_inches * _CM_PER_INCH).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def body_mass_index(*, weight_kg: Decimal, height_cm: Decimal) -> Decimal | None:
    """BMI, derived — FR-M3-012. Never stored.

    🔒 Storing it would create a second source of truth that drifts the moment a
    weight is corrected. AC-M3-004 checks this against an independent
    calculation.

    ⚠️ 🔒 **Returns a number, never a band.** "Overweight" and "obese" require
    thresholds, Indian cut-offs differ from WHO, and OD-08 is unresolved — the
    implementation plan's DoD forbids displaying a threshold without a citation.
    Bands belong in `clinical_reference_ranges` once sourced.
    """
    if height_cm <= 0 or weight_kg <= 0:
        return None
    metres = height_cm / Decimal(100)
    return (weight_kg / (metres * metres)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def waist_hip_ratio(*, waist_cm: Decimal, hip_cm: Decimal) -> Decimal | None:
    """WHR, derived — FR-M3-012. Never stored. No band, for the same reason."""
    if hip_cm <= 0 or waist_cm <= 0:
        return None
    return (waist_cm / hip_cm).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def is_implausible(*, weight_kg: Decimal | None, height_cm: Decimal | None) -> bool:
    """Whether a measurement deserves a second look — EC-M3-02.

    🔒 **A warning, not a refusal.** The edge case is explicit: "soft warning
    with confirmation; value accepted if confirmed; flagged for practitioner
    review". A real 180 kg client exists, and a system that refuses to record
    them is useless precisely when the record matters most.
    """
    if weight_kg is not None:
        low, high = PLAUSIBLE_WEIGHT_KG
        if weight_kg < low or weight_kg > high:
            return True
    if height_cm is not None:
        low, high = PLAUSIBLE_HEIGHT_CM
        if height_cm < low or height_cm > high:
            return True
    return False


def assert_storable(*, weight_kg: Decimal | None, height_cm: Decimal | None) -> None:
    """Refuse what the database's CHECK would refuse — DB §7.5.

    ⚠️ Distinct from :func:`is_implausible`. This is the outer bound where a
    value is not a measurement at all; that one is the inner bound where it is
    surprising. Conflating them would either reject real outliers or store
    typos.
    """
    if weight_kg is not None and not (MIN_WEIGHT_KG <= weight_kg <= MAX_WEIGHT_KG):
        raise ValidationError(
            f"A weight must be between {MIN_WEIGHT_KG} kg and {MAX_WEIGHT_KG} kg.",
            action="Check the number and the units.",
        )
    if height_cm is not None and not (MIN_HEIGHT_CM <= height_cm <= MAX_HEIGHT_CM):
        raise ValidationError(
            f"A height must be between {MIN_HEIGHT_CM} cm and {MAX_HEIGHT_CM} cm.",
            action="Check the number and the units.",
        )


#: 🔒 EC-M3-05 — when a practitioner and a client both record on one date, the
#: practitioner's value is displayed. Both rows are kept, and neither is edited.
_SOURCE_PRECEDENCE: dict[MeasurementSource, int] = {
    MeasurementSource.PRACTITIONER: 0,
    MeasurementSource.DEVICE: 1,
    MeasurementSource.CLIENT: 2,
}


def display_precedence(source: MeasurementSource) -> int:
    """Sort key deciding which same-date measurement is shown — EC-M3-05.

    Lower wins. A device reading outranks a client's self-report because it is
    an instrument rather than a recollection, and both yield to the practitioner
    who was in the room.
    """
    return _SOURCE_PRECEDENCE[source]


# ─── Events (DDR-06) ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AssessmentCompleted(DomainEvent):
    """A client or practitioner submitted an assessment — FR-M3-004, AC-M1-004.

    🔒 Carries no answer content. The timeline summary is built from enums only
    (NFR-033), and an assessment's answers are the most sensitive text in the
    product — a summary quoting one would leak clinical detail into a surface
    the client's own portal reads a filtered view of.
    """

    client_id: uuid.UUID
    tenant_id: uuid.UUID
    response_id: uuid.UUID
    definition_code: str
    definition_version: int
    completed_by_client: bool
    occurred_at: datetime
    actor_user_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class MeasurementRecorded(DomainEvent):
    """A dated measurement was recorded — FR-M3-011, FR-M1-018."""

    client_id: uuid.UUID
    tenant_id: uuid.UUID
    measurement_id: uuid.UUID
    source: MeasurementSource
    occurred_at: datetime
    actor_user_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class DocumentUploaded(DomainEvent):
    """A document was attached to a client — FR-M3-024/025.

    ⚠️ ``document_type`` is a label, not a filename. A filename is user-supplied
    text that routinely contains a person's name and condition
    ("priya-thyroid-2026.pdf"), and the timeline is read by more surfaces than
    the uploader expects.
    """

    client_id: uuid.UUID
    tenant_id: uuid.UUID
    document_id: uuid.UUID
    document_type: str
    uploaded_by_client: bool
    occurred_at: datetime
    actor_user_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class ConsultationNoteRecorded(DomainEvent):
    """A consultation note was written — FR-M3-018.

    ⚠️ 🔒 **This event must never reach a client-facing surface.** FR-M3-021 and
    AC-M3-006 make notes invisible to the client, and DB §5.6's note on the
    portal projection is explicit that even the *existence* of a note is a leak.
    The timeline subscriber writes a practitioner-only row; S6's portal
    projection must exclude the type outright.
    """

    client_id: uuid.UUID
    tenant_id: uuid.UUID
    note_id: uuid.UUID
    occurred_at: datetime
    actor_user_id: uuid.UUID | None = None


__all__ = [
    "DOCUMENT_TYPES",
    "MAX_HEIGHT_CM",
    "MAX_WEIGHT_KG",
    "MIN_HEIGHT_CM",
    "MIN_WEIGHT_KG",
    "PLAUSIBLE_HEIGHT_CM",
    "PLAUSIBLE_WEIGHT_KG",
    "ActivityLevel",
    "AnswerIssue",
    "AssessmentCompleted",
    "AssessmentSchema",
    "ConsultationNoteRecorded",
    "DefinitionStatus",
    "DocumentUploaded",
    "FieldType",
    "GoalType",
    "MeasurementRecorded",
    "MeasurementSource",
    "NutritionProfile",
    "NutritionProfileReader",
    "ProjectionTarget",
    "ResponseStatus",
    "SchemaField",
    "SchemaSection",
    "assert_publishable",
    "assert_storable",
    "body_mass_index",
    "completion",
    "display_precedence",
    "height_from_feet_inches",
    "is_implausible",
    "parse_schema",
    "project",
    "validate_answers",
    "validate_calculation_bindings",
    "validate_document_type",
    "waist_hip_ratio",
]
