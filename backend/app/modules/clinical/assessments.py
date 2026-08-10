"""The assessment engine — FR-M3-001…008, DDR-07, DDR-08.

🔒 **The structure is data.** Nothing in this file knows what questions the
assessment asks; it reads a definition, validates answers against it, and
projects the bound ones into typed columns. That is what FR-M3-002 buys:
adding a question is an INSERT into ``assessment_definitions``, not a release.

🔒 **A response pins its definition at creation and never moves.** FR-M3-003 and
EC-M3-03 are both consequences of that one FK: a client mid-completion when v2
publishes finishes under v1, because nothing rewrites ``definition_id``.

⚠️ Functions take an ``AsyncSession`` rather than opening one (ADR-04). The
transaction belongs to the request pipeline, so an assessment, its projection,
its measurement and its timeline entry commit together or not at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.clinical import (
    AnswerIssue,
    AssessmentCompleted,
    AssessmentSchema,
    DefinitionStatus,
    MeasurementSource,
    ProjectionTarget,
    ResponseStatus,
    completion,
    parse_schema,
    project,
    validate_answers,
    validate_calculation_bindings,
)
from app.kernel.context import ActorType
from app.kernel.errors import ConflictError, NotFoundError, ValidationError
from app.kernel.events import publish
from app.modules.clinical.models import (
    AssessmentDefinition,
    AssessmentResponse,
    ClientNutritionProfile,
    Measurement,
)

#: The definition every client is assessed against at MVP — DB §7.2's seed.
#:
#: ⚠️ A constant rather than a lookup by "the newest published anything": a
#: tenant must not silently switch to a different *form* because one was added.
#: FR-M3-010 (custom forms) is Phase 3 and will need a real selection rule.
CORE_DEFINITION_CODE = "nutrition_core"


def now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class LoadedDefinition:
    """A definition with its schema and bindings already parsed."""

    id: uuid.UUID
    code: str
    version: int
    title: str
    schema: AssessmentSchema
    bindings: dict[ProjectionTarget, str]


@dataclass(frozen=True, slots=True)
class SaveOutcome:
    """What a partial save concluded.

    ⚠️ ``issues`` is advisory on a partial save — FR-M3-005 saves continuously,
    so a half-typed number must not block the write. The same list is fatal on
    submit, where :func:`submit_response` refuses.
    """

    response: AssessmentResponse
    issues: list[AnswerIssue]
    completion_percent: Decimal


# ─── Definitions (FR-M3-002, FR-M3-003) ──────────────────────────────────


async def load_definition(session: AsyncSession, *, definition_id: uuid.UUID) -> LoadedDefinition:
    """Read one definition and parse it.

    🔒 Parsed on every load rather than cached. A definition is a few kilobytes
    read once per assessment page, and a cache keyed by id would have to be
    invalidated on publish — an invalidation bug here means a client fills in a
    form that no longer exists.
    """
    row = await session.get(AssessmentDefinition, definition_id)
    if row is None:
        raise NotFoundError(
            "That assessment form could not be found.",
            action="Refresh and try again.",
        )
    return _load(row)


async def current_definition(
    session: AsyncSession, *, code: str = CORE_DEFINITION_CODE
) -> LoadedDefinition:
    """The highest published version of a definition — FR-M3-001.

    🔒 **Published only.** A draft is work in progress; serving one would let a
    client answer questions we have not committed to, and their answers would
    then be pinned to a version that may never publish.

    ⚠️ Platform definitions (``tenant_id IS NULL``) are visible to every tenant
    by the RLS policy in migration 0016. This query does not filter on tenant
    for exactly that reason — the policy decides, and a redundant WHERE here
    would silently exclude the platform rows that are the only ones at MVP.
    """
    statement = (
        select(AssessmentDefinition)
        .where(
            AssessmentDefinition.code == code,
            AssessmentDefinition.status == DefinitionStatus.PUBLISHED,
        )
        .order_by(AssessmentDefinition.version.desc())
        .limit(1)
    )
    row = (await session.execute(statement)).scalar_one_or_none()
    if row is None:
        raise NotFoundError(
            "No assessment form has been published yet.",
            action="Contact support — this is a configuration problem, not something you did.",
        )
    return _load(row)


def _load(row: AssessmentDefinition) -> LoadedDefinition:
    schema = parse_schema(row.schema)
    return LoadedDefinition(
        id=row.id,
        code=row.code,
        version=row.version,
        title=row.title,
        schema=schema,
        # 🔒 Validated on every load, not only at publish. A definition seeded by
        # a migration never passed through the publish path, and a binding that
        # names a missing field would silently drop a clinical value (DDR-08).
        bindings=validate_calculation_bindings(row.calculation_bindings, schema),
    )


# ─── Responses (FR-M3-004…007) ───────────────────────────────────────────


async def start_or_resume(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> tuple[AssessmentResponse, LoadedDefinition]:
    """The client's open assessment, or a new one — FR-M3-005, EC-M3-01.

    🔒 **Resume is the default, and that is the whole of FR-M3-005.** A client
    who leaves and comes back gets the same row with their answers in it; a new
    administration is a deliberate act (:func:`start_new`), because FR-M3-007
    keeps every administration separately for comparison and an accidental one
    would fragment the history.

    ⚠️ The resumed response keeps **its own** definition, not the current one
    (EC-M3-03). If v2 published while they were away, they finish under v1.
    """
    open_response = (
        await session.execute(
            select(AssessmentResponse)
            .where(
                AssessmentResponse.tenant_id == tenant_id,
                AssessmentResponse.client_id == client_id,
                AssessmentResponse.status == ResponseStatus.IN_PROGRESS,
            )
            .order_by(AssessmentResponse.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if open_response is not None:
        return open_response, await load_definition(
            session, definition_id=open_response.definition_id
        )

    return await start_new(session, tenant_id=tenant_id, client_id=client_id)


async def start_new(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> tuple[AssessmentResponse, LoadedDefinition]:
    """Begin a fresh administration — FR-M3-007.

    🔒 Pins the definition **now**. Everything EC-M3-03 promises follows from the
    version being decided at creation rather than at submit.
    """
    definition = await current_definition(session)
    response = AssessmentResponse(
        tenant_id=tenant_id,
        client_id=client_id,
        definition_id=definition.id,
        answers={},
        status=ResponseStatus.IN_PROGRESS,
        completed_sections=[],
    )
    session.add(response)
    await session.flush()
    return response, definition


async def load_response(
    session: AsyncSession, *, tenant_id: uuid.UUID, response_id: uuid.UUID
) -> tuple[AssessmentResponse, LoadedDefinition]:
    """One administration and the structure it was captured under — FR-M3-003.

    🔒 This pairing is what AC-M3-003 tests: a response rendered under its own
    version, not under whatever is current.
    """
    response = await session.get(AssessmentResponse, response_id)
    if response is None or response.tenant_id != tenant_id:
        raise NotFoundError(
            "That assessment could not be found.",
            action="Go back to the client and open their assessments again.",
        )
    return response, await load_definition(session, definition_id=response.definition_id)


async def save_progress(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    response_id: uuid.UUID,
    answers: dict[str, Any],
    completed_sections: list[str] | None = None,
) -> SaveOutcome:
    """Merge answers into an in-progress response — FR-M3-005, AC-M3-001.

    🔒 **Merged, not replaced.** The client PWA sends the section it just filled;
    replacing the document would delete every other section's answers the moment
    a phone posted a partial payload. AC-M3-001 ("multiple sessions, without data
    loss") is this line.

    ⚠️ **Validation issues do not block the save.** A half-typed number must
    persist — the alternative is a client losing a section because they paused
    mid-field. Issues are returned so the form can show them; submit is where
    they become fatal.
    """
    response, definition = await load_response(
        session, tenant_id=tenant_id, response_id=response_id
    )
    if response.status is ResponseStatus.COMPLETED:
        raise ConflictError(
            "This assessment has already been submitted.",
            action="Start a new assessment if you need to record something different.",
        )

    merged = {**response.answers, **answers}
    issues = validate_answers(merged, definition.schema, partial=True)

    response.answers = merged
    if completed_sections is not None:
        # 🔒 Union, never assignment. FR-M3-006 lets a section be marked done and
        # revisited; a client re-opening section B and saving must not un-complete
        # section A, which a bare assignment from a partial payload would do.
        known = {s.id for s in definition.schema.sections}
        response.completed_sections = sorted(
            (set(response.completed_sections) | set(completed_sections)) & known
        )
    response.updated_at = now()
    await session.flush()

    return SaveOutcome(
        response=response,
        issues=issues,
        completion_percent=completion(
            merged, definition.schema, frozenset(response.completed_sections)
        ),
    )


async def submit_response(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    response_id: uuid.UUID,
    completed_by: ActorType,
    actor_user_id: uuid.UUID | None,
) -> AssessmentResponse:
    """Finish an assessment and project it — FR-M3-004, DDR-08.

    Three things happen together, in one transaction (ADR-04):

    1. The response is marked complete.
    2. Its bound answers are projected into ``client_nutrition_profile``.
    3. A weight answered in the form becomes a ``measurements`` row.

    🔒 **Clinical sections are not required** (FR-M3-006, AC-M3-002). A coach who
    skipped every one of them submits successfully, and the projection takes
    whatever it got — "usable for planning" is decided by the profile, never by
    how many sections were filled.

    Raises:
        ValidationError: A required non-clinical answer is missing or malformed.
        ConflictError: Already submitted.
    """
    response, definition = await load_response(
        session, tenant_id=tenant_id, response_id=response_id
    )
    if response.status is ResponseStatus.COMPLETED:
        raise ConflictError(
            "This assessment has already been submitted.",
            action="Start a new assessment to record a fresh set of answers.",
        )

    issues = validate_answers(response.answers, definition.schema, partial=False)
    if issues:
        raise ValidationError.for_fields(
            [
                {"field": issue.field_id, "code": "invalid", "message": issue.message}
                for issue in issues
            ]
        )

    completed_at = now()
    response.status = ResponseStatus.COMPLETED
    response.completed_by = completed_by
    response.completed_at = completed_at
    response.updated_at = completed_at

    await _write_projection(
        session,
        tenant_id=tenant_id,
        response=response,
        definition=definition,
    )
    await _record_assessment_weight(
        session,
        tenant_id=tenant_id,
        response=response,
        definition=definition,
        completed_by=completed_by,
        actor_user_id=actor_user_id,
    )
    await session.flush()

    await publish(
        AssessmentCompleted(
            client_id=response.client_id,
            tenant_id=tenant_id,
            response_id=response.id,
            definition_code=definition.code,
            definition_version=definition.version,
            completed_by_client=completed_by is ActorType.CLIENT,
            occurred_at=completed_at,
            actor_user_id=actor_user_id,
        ),
        session,
    )
    return response


async def _write_projection(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    response: AssessmentResponse,
    definition: LoadedDefinition,
) -> ClientNutritionProfile:
    """Upsert the client's typed profile — DDR-08.

    🔒 **One row per client, rewritten on each completion.** The *history* is the
    responses, each of which keeps its own answers; this is the current picture
    nutrition calculates from. Keeping a row per assessment here would make
    "which one is current" a query every reader had to get right.
    """
    projected = project(
        response.answers,
        definition.bindings,
        client_id=response.client_id,
        source_response_id=response.id,
    )

    existing = (
        await session.execute(
            select(ClientNutritionProfile).where(
                ClientNutritionProfile.tenant_id == tenant_id,
                ClientNutritionProfile.client_id == response.client_id,
            )
        )
    ).scalar_one_or_none()

    profile = existing or ClientNutritionProfile(tenant_id=tenant_id, client_id=response.client_id)

    profile.source_response_id = projected.source_response_id
    profile.date_of_birth = projected.date_of_birth
    profile.sex = projected.sex
    profile.height_cm = projected.height_cm
    profile.activity_level = projected.activity_level
    profile.primary_goal = projected.primary_goal
    profile.dietary_class = projected.dietary_class
    profile.excludes_onion_garlic = projected.excludes_onion_garlic
    profile.excludes_root_vegetables = projected.excludes_root_vegetables
    profile.allergen_food_ids = list(projected.allergen_food_ids)
    profile.excluded_food_ids = list(projected.excluded_food_ids)
    profile.fasting_patterns = list(projected.fasting_patterns)
    profile.staple_grain = projected.staple_grain
    profile.region_cuisine = projected.region_cuisine
    profile.updated_at = now()

    if existing is None:
        session.add(profile)
    return profile


#: The field a v1 definition uses for current weight.
#:
#: ⚠️ 🔒 **Weight is not a projection target**, deliberately. It is longitudinal
#: (FR-M3-011) and belongs in `measurements`, so `client_nutrition_profile` has
#: no weight column and there is one source of truth. This constant is the seam
#: between the two: the assessment asks for a weight, and the answer becomes a
#: measurement rather than a profile field.
_WEIGHT_FIELD = "current_weight_kg"


async def _record_assessment_weight(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    response: AssessmentResponse,
    definition: LoadedDefinition,
    completed_by: ActorType,
    actor_user_id: uuid.UUID | None,
) -> None:
    """Turn the assessment's weight answer into a measurement — FR-M3-011.

    🔒 Without this the weight a client typed into the assessment would be
    invisible to the trend (FR-M3-014), and the practitioner would have to
    re-enter it. AC-M3-004's three-date trend starts here for most clients.

    ⚠️ Silently skipped when the definition has no weight field, which a future
    version may not. A missing field is a definition change, not an error.
    """
    if _WEIGHT_FIELD not in definition.schema.field_index():
        return
    raw = response.answers.get(_WEIGHT_FIELD)
    if raw is None:
        return
    try:
        weight = Decimal(str(raw))
    except Exception:
        return

    session.add(
        Measurement(
            tenant_id=tenant_id,
            client_id=response.client_id,
            measured_on=(response.completed_at or now()).date(),
            weight_kg=weight,
            # 🔒 EC-M3-05's attribution, decided by who filled the form in.
            source=(
                MeasurementSource.CLIENT
                if completed_by is ActorType.CLIENT
                else MeasurementSource.PRACTITIONER
            ),
            recorded_by_user_id=actor_user_id,
            notes="Recorded from the nutrition assessment.",
        )
    )


# ─── Reading (FR-M3-007, FR-M3-008) ──────────────────────────────────────


async def list_responses(
    session: AsyncSession, *, tenant_id: uuid.UUID, client_id: uuid.UUID
) -> list[AssessmentResponse]:
    """Every administration for a client, newest first — FR-M3-007, AC-M3-007.

    🔒 All of them, including in-progress ones: EC-M3-01 requires the
    practitioner to see completion status, which means seeing the unfinished row
    rather than inferring its absence.
    """
    statement = (
        select(AssessmentResponse)
        .where(
            AssessmentResponse.tenant_id == tenant_id,
            AssessmentResponse.client_id == client_id,
        )
        .order_by(AssessmentResponse.started_at.desc())
    )
    return list((await session.execute(statement)).scalars().all())


async def profile_for(
    session: AsyncSession, *, tenant_id: uuid.UUID, client_id: uuid.UUID
) -> ClientNutritionProfile | None:
    """The client's current typed profile — DDR-08's read side."""
    return (
        await session.execute(
            select(ClientNutritionProfile).where(
                ClientNutritionProfile.tenant_id == tenant_id,
                ClientNutritionProfile.client_id == client_id,
            )
        )
    ).scalar_one_or_none()
