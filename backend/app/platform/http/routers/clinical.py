"""The clinical workspace over HTTP — PRD M3, API §7.3.

🔒 In ``platform/`` for the reason every router is: R5 forbids a module importing
``platform``, and this needs ``realm_router``, the session and the authorization
seam. The rules live in ``kernel.clinical`` and the persistence in
``modules.clinical``; this owns the HTTP shape and nothing else.

🔒 **Every route authorizes against the client** through ``authorized_client``,
not merely against the action. A practitioner who cannot open a client must not
reach their assessment, measurements, notes or lab reports — the AC-M1-006 leak,
through four more doors than S2 had.

⚠️ **The composition seam lives here** (Arch §3.4c). Submitting an assessment
writes a measurement and a projection; issuing a download URL needs
``platform.storage``. Modules may not call each other or reach into ``platform``,
so the router is the layer permitted to see both — the same role it plays for
`enquiries.mark_responded`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.clients import DietaryClass, SexType
from app.kernel.clinical import (
    ActivityLevel,
    FieldType,
    GoalType,
    MeasurementSource,
    ResponseStatus,
    completion,
)
from app.kernel.context import ActorType, get_context
from app.modules.clinical import (
    ASSESSMENT_READ,
    ASSESSMENT_WRITE,
    CLIENT_DOCUMENT_ARCHIVE,
    CLIENT_DOCUMENT_DOWNLOAD,
    CLIENT_DOCUMENT_READ,
    CLIENT_DOCUMENT_UPLOAD,
    CONSULTATION_NOTE_ARCHIVE,
    CONSULTATION_NOTE_READ,
    CONSULTATION_NOTE_WRITE,
    MAX_NOTE_LENGTH,
    MEASUREMENT_READ,
    MEASUREMENT_RECORD,
    AssessmentResponse,
    ClientDocument,
    ClientNutritionProfile,
    ConsultationNote,
    LoadedDefinition,
    Measurement,
    NoteAuthor,
    add_consultation_note,
    archive_consultation_note,
    archive_document,
    attach_document,
    document_download_url,
    edit_consultation_note,
    height_for,
    latest_derived,
    list_consultation_notes,
    list_documents,
    list_responses,
    load_definition,
    load_response,
    measurement_history,
    preferred_per_date,
    profile_for,
    record_measurement,
    save_progress,
    start_new,
    start_or_resume,
    submit_response,
    with_derived,
)
from app.platform.http.authz import requires
from app.platform.http.pipeline import get_session, realm_router, record_audit
from app.platform.http.routers.clients import authorized_client
from app.platform.storage import authorize_download

router = realm_router("/api/v1/app/clients", tags=["clinical"])


def _author() -> NoteAuthor:
    """Who is writing, for the author-only rules — FR-M3-020.

    ⚠️ Read from the request context rather than taken as a parameter: the author
    of a note is whoever the token authenticated, never anything a body could
    name. A ``author_user_id`` field on the request would be forgeable.
    """
    actor = get_context().actor
    return NoteAuthor(user_id=actor.require_subject(), role=actor.role)


# ─── Assessment schemas (API §7.3) ───────────────────────────────────────


class FieldResponse(BaseModel):
    """One question, as the renderer needs it — FR-M3-002.

    🔒 **The frontend draws this without knowing what it asks.** That is the
    whole of FR-M3-002: adding a question to the definition changes what this
    endpoint returns, and no release is needed on either side.
    """

    id: str
    type: FieldType
    label: str
    required: bool
    options: list[str]
    min_value: Decimal | None
    max_value: Decimal | None
    help_text: str | None


class SectionResponse(BaseModel):
    """One page of the form — PRD §9.3.

    ⚠️ ``is_clinical`` is what FR-M3-006 turns on: the UI offers a skip for these
    and requires nothing in them. AC-M3-002 is a coach skipping every one.
    """

    id: str
    title: str
    description: str | None
    is_clinical: bool
    fields: list[FieldResponse]


class DefinitionResponse(BaseModel):
    """The structure an administration was captured under — FR-M3-003."""

    id: uuid.UUID
    code: str
    version: int
    title: str
    sections: list[SectionResponse]


class AssessmentResponseBody(BaseModel):
    """One administration — FR-M3-007.

    ⚠️ ``completion_percent`` counts **non-clinical sections only**. A coach who
    skipped every clinical section legitimately reads 100%, because "usable for
    planning" is decided by the projection, not by how many boxes were filled.
    """

    id: uuid.UUID
    client_id: uuid.UUID
    definition: DefinitionResponse
    answers: dict[str, Any]
    status: ResponseStatus
    completed_sections: list[str]
    completed_by: ActorType | None
    completion_percent: Decimal
    started_at: datetime
    completed_at: datetime | None
    updated_at: datetime


class AssessmentSummary(BaseModel):
    """An administration in a list — FR-M3-007, AC-M3-007.

    ⚠️ No answers. The list is for choosing which administration to open, and
    shipping every answer set would put a client's full history in a response
    that is rendered as five rows.
    """

    id: uuid.UUID
    definition_code: str
    definition_version: int
    status: ResponseStatus
    completed_by: ActorType | None
    started_at: datetime
    completed_at: datetime | None


class SaveAnswersRequest(BaseModel):
    """A partial save — FR-M3-005.

    🔒 **Merged, not replaced.** The client PWA posts the section it just filled;
    replacing the document would delete every other section (AC-M3-001).
    """

    answers: dict[str, Any] = Field(default_factory=dict)
    completed_sections: list[str] | None = None


class AnswerIssueResponse(BaseModel):
    field_id: str
    message: str


class SaveAnswersResponse(BaseModel):
    """What the save concluded.

    ⚠️ ``issues`` is advisory here and fatal on submit. A half-typed number must
    persist — losing a section because someone paused mid-field is the failure
    FR-M3-005 exists to prevent.
    """

    response: AssessmentResponseBody
    issues: list[AnswerIssueResponse]


class NutritionProfileResponse(BaseModel):
    """The typed projection — DDR-08.

    🔒 What `nutrition` and `ai_drafting` will read (through a kernel port, not
    this endpoint). Exposed to the practitioner UI so a coach can see what the
    assessment actually produced — AC-M3-002's "the result remains usable for
    planning" is a question about *this*, not about section counts.
    """

    date_of_birth: date | None
    sex: SexType | None
    height_cm: Decimal | None
    activity_level: ActivityLevel | None
    primary_goal: GoalType | None
    dietary_class: DietaryClass | None
    excludes_onion_garlic: bool
    excludes_root_vegetables: bool
    allergen_food_ids: list[uuid.UUID]
    excluded_food_ids: list[uuid.UUID]
    fasting_patterns: list[str]
    staple_grain: str | None
    region_cuisine: str | None
    source_response_id: uuid.UUID | None
    updated_at: datetime | None


class DerivedMetricsResponse(BaseModel):
    """Server-computed anthropometry — API §7.3.

    🔒 **Server-computed, never sent up.** A BMI calculated in the browser is a
    clinical figure the server cannot vouch for, and two clients would disagree
    the first time one rounded differently.

    ⚠️ 🔒 **``bmi_band``, ``bmi_band_source``, ``estimated_energy_kcal`` and
    ``calculation_method`` are always ``null``, and that is the contract.** OD-08
    (Indian BMI cut-offs differ from WHO) and OD-13 (which energy equation) are
    both unresolved, and the implementation plan's DoD forbids displaying a
    clinical threshold or an equation result without a citation. The fields exist
    so that resolving either needs no contract change; ``bmi_band_source`` is
    where the citation goes, which is why a band can never appear without one.
    """

    bmi: Decimal | None
    bmi_band: None = None
    bmi_band_source: None = None
    waist_hip_ratio: Decimal | None
    estimated_energy_kcal: None = None
    calculation_method: None = None


class AssessmentCompletionResponse(BaseModel):
    """What completing an assessment produced — API §7.3.

    🔒 Three things in one response because they are one transaction: the
    administration, the DDR-08 projection it wrote, and what is derived from the
    measurement it recorded. A practitioner who submits a form needs to see that
    the projection actually picked the answers up (AC-M3-002's "usable for
    planning"), and a second round trip to find out is a second chance to not
    bother.
    """

    assessment: AssessmentResponseBody
    nutrition_profile: NutritionProfileResponse | None
    derived: DerivedMetricsResponse


def _definition_response(definition: LoadedDefinition) -> DefinitionResponse:
    return DefinitionResponse(
        id=definition.id,
        code=definition.code,
        version=definition.version,
        title=definition.title,
        sections=[
            SectionResponse(
                id=section.id,
                title=section.title,
                description=section.description,
                is_clinical=section.is_clinical,
                fields=[
                    FieldResponse(
                        id=field.id,
                        type=field.type,
                        label=field.label,
                        required=field.required,
                        options=list(field.options),
                        min_value=field.min_value,
                        max_value=field.max_value,
                        help_text=field.help_text,
                    )
                    for field in section.fields
                ],
            )
            # ⚠️ ``definition.schema``, not ``definition``: `LoadedDefinition`
            # carries the parsed :class:`AssessmentSchema` rather than flattening
            # it, so that the bindings validated against it stay beside it.
            for section in definition.schema.sections
        ],
    )


def _assessment_body(
    response: AssessmentResponse, definition: LoadedDefinition
) -> AssessmentResponseBody:
    return AssessmentResponseBody(
        id=response.id,
        client_id=response.client_id,
        definition=_definition_response(definition),
        answers=response.answers,
        status=response.status,
        completed_sections=list(response.completed_sections),
        completed_by=response.completed_by,
        completion_percent=completion(
            response.answers, definition.schema, frozenset(response.completed_sections)
        ),
        started_at=response.started_at,
        completed_at=response.completed_at,
        updated_at=response.updated_at,
    )


# ─── Assessment endpoints ────────────────────────────────────────────────


@router.get(
    "/{client_id}/assessments",
    summary="Every assessment for a client",
    operation_id="assessmentList",
)
@requires(ASSESSMENT_READ)
async def assessment_list(request: Request, client_id: uuid.UUID) -> list[AssessmentSummary]:
    """Newest first — FR-M3-007, AC-M3-007.

    🔒 Includes in-progress administrations: EC-M3-01 requires the practitioner
    to see completion status, which means seeing the unfinished row rather than
    inferring it from an absence.
    """
    await authorized_client(request, client_id)
    session = get_session(request)
    tenant_id = get_context().actor.require_tenant()

    rows = await list_responses(session, tenant_id=tenant_id, client_id=client_id)
    summaries: list[AssessmentSummary] = []
    for row in rows:
        definition = await load_definition(session, definition_id=row.definition_id)
        summaries.append(
            AssessmentSummary(
                id=row.id,
                definition_code=definition.code,
                definition_version=definition.version,
                status=row.status,
                completed_by=row.completed_by,
                started_at=row.started_at,
                completed_at=row.completed_at,
            )
        )
    return summaries


@router.post(
    "/{client_id}/assessments",
    status_code=status.HTTP_201_CREATED,
    summary="Start or resume an assessment",
    operation_id="assessmentStartOrResume",
)
@requires(ASSESSMENT_WRITE)
async def assessment_start_or_resume(
    request: Request,
    client_id: uuid.UUID,
    restart: Annotated[
        bool, Query(description="Force a new administration rather than resuming (FR-M3-007).")
    ] = False,
) -> AssessmentResponseBody:
    """Resume the open assessment, or begin one — FR-M3-004/005/007.

    🔒 **Resume is the default.** A client who leaves and returns gets their
    answers back (AC-M3-001); a *new* administration is deliberate, because
    FR-M3-007 keeps each one separately and an accidental one fragments the
    history.

    ⚠️ ``restart=true`` is how a repeat assessment is taken. It leaves any open
    response untouched rather than closing it — an abandoned draft is evidence of
    an abandonment (EC-M3-01), not something to tidy away.
    """
    await authorized_client(request, client_id)
    session = get_session(request)
    tenant_id = get_context().actor.require_tenant()

    if restart:
        response, definition = await start_new(session, tenant_id=tenant_id, client_id=client_id)
    else:
        response, definition = await start_or_resume(
            session, tenant_id=tenant_id, client_id=client_id
        )

    record_audit(
        request,
        resource_id=response.id,
        metadata={"response_id": str(response.id), "definition_version": str(definition.version)},
    )
    return _assessment_body(response, definition)


@router.get(
    "/{client_id}/assessments/{response_id}",
    summary="One administration, under its own version",
    operation_id="assessmentRead",
)
@requires(ASSESSMENT_READ)
async def assessment_read(
    request: Request, client_id: uuid.UUID, response_id: uuid.UUID
) -> AssessmentResponseBody:
    """FR-M3-003, FR-M3-008, AC-M3-003.

    🔒 **Returns the definition the response was captured under**, not the
    current one. That pairing is the whole of AC-M3-003: publish v2, and a v1
    response still renders correctly because its structure travels with it.
    """
    await authorized_client(request, client_id)
    response, definition = await load_response(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        response_id=response_id,
    )
    return _assessment_body(response, definition)


@router.patch(
    "/{client_id}/assessments/{response_id}",
    summary="Save progress",
    operation_id="assessmentSaveProgress",
)
@requires(ASSESSMENT_WRITE)
async def assessment_save(
    request: Request,
    client_id: uuid.UUID,
    response_id: uuid.UUID,
    payload: SaveAnswersRequest,
) -> SaveAnswersResponse:
    """Continuous save — FR-M3-005, AC-M3-001.

    ⚠️ **Validation issues do not block the write.** They come back so the form
    can show them; only submit refuses. A save that rejected a half-typed number
    would lose the section around it.
    """
    await authorized_client(request, client_id)
    outcome = await save_progress(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        response_id=response_id,
        answers=payload.answers,
        completed_sections=payload.completed_sections,
    )
    _, definition = await load_response(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        response_id=response_id,
    )
    record_audit(request, resource_id=response_id, metadata={"response_id": str(response_id)})
    return SaveAnswersResponse(
        response=_assessment_body(outcome.response, definition),
        issues=[
            AnswerIssueResponse(field_id=issue.field_id, message=issue.message)
            for issue in outcome.issues
        ],
    )


def _nutrition_profile_response(profile: ClientNutritionProfile) -> NutritionProfileResponse:
    return NutritionProfileResponse(
        date_of_birth=profile.date_of_birth,
        sex=profile.sex,
        height_cm=profile.height_cm,
        activity_level=profile.activity_level,
        primary_goal=profile.primary_goal,
        dietary_class=profile.dietary_class,
        excludes_onion_garlic=profile.excludes_onion_garlic,
        excludes_root_vegetables=profile.excludes_root_vegetables,
        allergen_food_ids=list(profile.allergen_food_ids),
        excluded_food_ids=list(profile.excluded_food_ids),
        fasting_patterns=list(profile.fasting_patterns),
        staple_grain=profile.staple_grain,
        region_cuisine=profile.region_cuisine,
        source_response_id=profile.source_response_id,
        updated_at=profile.updated_at,
    )


@router.post(
    "/{client_id}/assessments/{response_id}/complete",
    summary="Complete an assessment",
    operation_id="assessmentComplete",
)
@requires(ASSESSMENT_WRITE)
async def assessment_complete(
    request: Request, client_id: uuid.UUID, response_id: uuid.UUID
) -> AssessmentCompletionResponse:
    """Finish, project and record — FR-M3-004, DDR-08, API §7.3.

    🔒 Three writes in one transaction: the completion, the DDR-08 projection
    into ``client_nutrition_profile``, and a ``measurements`` row for the weight
    the form captured. The timeline entry follows from the published event.

    🔒 **Clinical sections are not required** (FR-M3-006, AC-M3-002). A refusal
    here names the *non-clinical* fields that are missing, and nothing else.

    ⚠️ ``derived`` is read back **after** the submit rather than computed from the
    request: the weight became a `measurements` row inside
    ``submit_response``, and deriving from the answer document instead would
    produce a BMI that disagreed with the trend chart's first point.
    """
    await authorized_client(request, client_id)
    session = get_session(request)
    actor = get_context().actor
    tenant_id = actor.require_tenant()

    response = await submit_response(
        session,
        tenant_id=tenant_id,
        response_id=response_id,
        # 🔒 A practitioner-realm token is submitting, so this administration is
        # practitioner-completed by definition. The client's own path is the
        # portal realm, which carries `ActorType.CLIENT`.
        completed_by=ActorType.PRACTITIONER,
        actor_user_id=actor.require_subject(),
    )
    _, definition = await load_response(session, tenant_id=tenant_id, response_id=response_id)
    profile = await profile_for(session, tenant_id=tenant_id, client_id=client_id)

    derived = await latest_derived(session, tenant_id=tenant_id, client_id=client_id)
    record_audit(
        request,
        resource_id=response_id,
        metadata={"response_id": str(response_id), "definition_version": str(definition.version)},
    )
    return AssessmentCompletionResponse(
        assessment=_assessment_body(response, definition),
        nutrition_profile=None if profile is None else _nutrition_profile_response(profile),
        derived=DerivedMetricsResponse(bmi=derived.bmi, waist_hip_ratio=derived.waist_hip_ratio),
    )


@router.get(
    "/{client_id}/nutrition-profile",
    summary="The typed projection of the latest assessment",
    operation_id="nutritionProfileRead",
)
@requires(ASSESSMENT_READ)
async def nutrition_profile_read(
    request: Request, client_id: uuid.UUID
) -> NutritionProfileResponse | None:
    """DDR-08's read side — what planning will actually use.

    ⚠️ ``null`` when no assessment has been completed. That is a real state, not
    an error: a client added by hand has no profile until somebody fills the form
    in, and a 404 would make "not yet assessed" indistinguishable from "no such
    client".
    """
    await authorized_client(request, client_id)
    profile = await profile_for(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        client_id=client_id,
    )
    if profile is None:
        return None
    return _nutrition_profile_response(profile)


# ─── Measurements (FR-M3-011…015) ────────────────────────────────────────


class MeasurementResponse(BaseModel):
    """One dated measurement, with what is derived from it — FR-M3-012.

    🔒 ``bmi`` and ``waist_hip_ratio`` are **computed on read and never stored**.
    A corrected weight corrects them everywhere at once.

    ⚠️ 🔒 **No band, no label, no colour.** OD-08 is unresolved — Indian BMI
    cut-offs differ from WHO — and the implementation plan's DoD forbids
    displaying a clinical threshold without a citation. A number is a fact;
    "overweight" is a judgement we are not yet entitled to publish.
    """

    id: uuid.UUID
    measured_on: date
    weight_kg: Decimal | None
    height_cm: Decimal | None
    waist_cm: Decimal | None
    hip_cm: Decimal | None
    body_fat_pct: Decimal | None
    bmi: Decimal | None
    waist_hip_ratio: Decimal | None
    source: MeasurementSource
    is_flagged_implausible: bool
    notes: str | None
    created_at: datetime


class RecordMeasurementRequest(BaseModel):
    """FR-M3-011, FR-M3-015, EC-M3-02.

    ⚠️ ``height_cm`` only. FR-M3-015 accepts feet/inches *at the boundary* and
    normalises on entry — the conversion is `kernel.clinical.
    height_from_feet_inches`, applied by the client before it posts, so no unit
    column exists and a 5 cannot mean five feet in one row and five centimetres
    in another.
    """

    measured_on: date
    weight_kg: Decimal | None = None
    height_cm: Decimal | None = None
    waist_cm: Decimal | None = None
    hip_cm: Decimal | None = None
    body_fat_pct: Decimal | None = None
    notes: str | None = None
    #: 🔒 EC-M3-02 — the second step. An implausible value is refused once with a
    #: warning and accepted when resent with this set.
    confirm_implausible: bool = False


def _measurement_response(row: Measurement, *, height_cm: Decimal | None) -> MeasurementResponse:
    """Shape one row for the wire.

    ⚠️ The derivation is ``measurements.with_derived``, not arithmetic repeated
    here. It already decides that the row's own height beats the profile's, and a
    second copy of that precedence would disagree with the trend chart the first
    time one of them changed.
    """
    derived = with_derived(row, fallback_height_cm=height_cm)
    return MeasurementResponse(
        id=row.id,
        measured_on=row.measured_on,
        weight_kg=row.weight_kg,
        height_cm=row.height_cm,
        waist_cm=row.waist_cm,
        hip_cm=row.hip_cm,
        body_fat_pct=row.body_fat_pct,
        bmi=derived.bmi,
        waist_hip_ratio=derived.waist_hip_ratio,
        source=row.source,
        is_flagged_implausible=row.is_flagged_implausible,
        notes=row.notes,
        created_at=row.created_at,
    )


@router.get(
    "/{client_id}/measurements",
    summary="A client's measurement trend",
    operation_id="measurementList",
)
@requires(MEASUREMENT_READ)
async def measurement_list(
    request: Request,
    client_id: uuid.UUID,
    preferred_only: Annotated[
        bool,
        Query(description="One row per date, practitioner value winning — EC-M3-05."),
    ] = False,
) -> list[MeasurementResponse]:
    """Newest first — FR-M3-014, AC-M3-004.

    ⚠️ **Every row by default, including same-date duplicates.** EC-M3-05 keeps
    both a practitioner's and a client's value for one date; ``preferred_only``
    applies the display rule without deleting anything, which is what a chart
    wants and an audit does not.
    """
    await authorized_client(request, client_id)
    session = get_session(request)
    tenant_id = get_context().actor.require_tenant()

    rows = await measurement_history(session, tenant_id=tenant_id, client_id=client_id)
    if preferred_only:
        rows = preferred_per_date(rows)
    height = await height_for(session, tenant_id=tenant_id, client_id=client_id)
    return [_measurement_response(row, height_cm=height) for row in rows]


@router.post(
    "/{client_id}/measurements",
    status_code=status.HTTP_201_CREATED,
    summary="Record a measurement",
    operation_id="measurementRecord",
)
@requires(MEASUREMENT_RECORD)
async def measurement_record(
    request: Request, client_id: uuid.UUID, payload: RecordMeasurementRequest
) -> MeasurementResponse:
    """FR-M3-011, FR-M3-013, EC-M3-02.

    🔒 An implausible value comes back as a 422 carrying
    ``details.requires_confirmation``; the UI confirms and resends with
    ``confirm_implausible``. The row is then stored **and flagged**, because a
    real 180 kg client exists and refusing to record them is useless exactly when
    the record matters most.
    """
    await authorized_client(request, client_id)
    session = get_session(request)
    actor = get_context().actor

    row = await record_measurement(
        session,
        tenant_id=actor.require_tenant(),
        client_id=client_id,
        measured_on=payload.measured_on,
        # 🔒 A practitioner-realm token is recording. The client's own path is
        # the portal realm and carries `MeasurementSource.CLIENT` — which is what
        # makes EC-M3-05's attribution real rather than declared.
        source=MeasurementSource.PRACTITIONER,
        recorded_by_user_id=actor.require_subject(),
        weight_kg=payload.weight_kg,
        height_cm=payload.height_cm,
        waist_cm=payload.waist_cm,
        hip_cm=payload.hip_cm,
        body_fat_pct=payload.body_fat_pct,
        notes=payload.notes,
        confirmed_implausible=payload.confirm_implausible,
    )
    record_audit(
        request,
        resource_id=row.id,
        metadata={"measurement_id": str(row.id), "measured_on": row.measured_on.isoformat()},
    )
    height = await height_for(session, tenant_id=actor.require_tenant(), client_id=client_id)
    return _measurement_response(row, height_cm=height)


# ─── Consultation notes (FR-M3-018…021) ──────────────────────────────────


class ConsultationNoteResponse(BaseModel):
    """One consultation note.

    🔒 **Never reaches a client-facing surface.** There is no portal endpoint
    that returns this model, `consultation_notes` has no client-realm RLS policy,
    and no timeline row is written for one. Three mechanisms, none of which is a
    condition somebody could get wrong (FR-M3-021, AC-M3-006).
    """

    id: uuid.UUID
    client_id: uuid.UUID
    note_date: date
    body: str
    author_user_id: uuid.UUID
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class WriteNoteRequest(BaseModel):
    note_date: date
    body: str = Field(min_length=1, max_length=MAX_NOTE_LENGTH)


class EditNoteRequest(BaseModel):
    body: str = Field(min_length=1, max_length=MAX_NOTE_LENGTH)


def _note_response(row: ConsultationNote) -> ConsultationNoteResponse:
    """One conversion, named, so the four write paths cannot drift apart.

    🔒 The parameter is typed against the ORM model rather than left to
    ``model_validate``'s ``Any``: a column renamed in ``models.py`` should fail
    type checking here, not silently start returning ``null`` to the UI.
    """
    return ConsultationNoteResponse.model_validate(row, from_attributes=True)


@router.get(
    "/{client_id}/consultation-notes",
    summary="A client's consultation notes",
    operation_id="consultationNoteList",
)
@requires(CONSULTATION_NOTE_READ)
async def consultation_note_list(
    request: Request, client_id: uuid.UUID
) -> list[ConsultationNoteResponse]:
    """Newest first — FR-M3-018."""
    await authorized_client(request, client_id)
    rows = await list_consultation_notes(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        client_id=client_id,
    )
    return [_note_response(row) for row in rows]


@router.post(
    "/{client_id}/consultation-notes",
    status_code=status.HTTP_201_CREATED,
    summary="Record a consultation note",
    operation_id="consultationNoteCreate",
)
@requires(CONSULTATION_NOTE_WRITE)
async def consultation_note_create(
    request: Request, client_id: uuid.UUID, payload: WriteNoteRequest
) -> ConsultationNoteResponse:
    """FR-M3-018."""
    await authorized_client(request, client_id)
    row = await add_consultation_note(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        client_id=client_id,
        note_date=payload.note_date,
        body=payload.body,
        author=_author(),
    )
    record_audit(request, resource_id=row.id, metadata={"note_id": str(row.id)})
    return _note_response(row)


@router.patch(
    "/{client_id}/consultation-notes/{note_id}",
    summary="Edit a consultation note",
    operation_id="consultationNoteEdit",
)
@requires(CONSULTATION_NOTE_WRITE)
async def consultation_note_edit(
    request: Request, client_id: uuid.UUID, note_id: uuid.UUID, payload: EditNoteRequest
) -> ConsultationNoteResponse:
    """🔒 Author-only, including against the owner — FR-M3-020.

    The edit is recorded in the audit log by the pipeline, which is the other
    half of FR-M3-020.
    """
    await authorized_client(request, client_id)
    row = await edit_consultation_note(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        note_id=note_id,
        body=payload.body,
        author=_author(),
    )
    record_audit(request, resource_id=row.id, metadata={"note_id": str(row.id)})
    return _note_response(row)


@router.post(
    "/{client_id}/consultation-notes/{note_id}/archive",
    summary="Archive a consultation note",
    operation_id="consultationNoteArchive",
)
@requires(CONSULTATION_NOTE_ARCHIVE)
async def consultation_note_archive(
    request: Request, client_id: uuid.UUID, note_id: uuid.UUID
) -> ConsultationNoteResponse:
    """Author, or the owner on their behalf. Never deleted — the grant revokes it."""
    await authorized_client(request, client_id)
    row = await archive_consultation_note(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        note_id=note_id,
        author=_author(),
    )
    record_audit(request, resource_id=row.id, metadata={"note_id": str(row.id)})
    return _note_response(row)


# ─── Documents (FR-M3-024…027) ───────────────────────────────────────────


class ClientDocumentResponse(BaseModel):
    """A document's metadata. The bytes are fetched separately (FR-M0-038)."""

    id: uuid.UUID
    client_id: uuid.UUID
    file_id: uuid.UUID
    document_type: str
    document_date: date | None
    uploaded_by: ActorType
    description: str | None
    archived_at: datetime | None
    created_at: datetime


class AttachDocumentRequest(BaseModel):
    """Label an already-confirmed upload — FR-M3-024.

    🔒 The upload itself went through ``platform.storage``: type and size
    allowlists (EC-M3-04) and the tenant quota (EC-M3-07) were applied *before*
    any bytes moved, which is the only moment refusing is free.
    """

    file_id: uuid.UUID
    document_type: str
    document_date: date | None = None
    description: str | None = None


class DocumentUrlResponse(BaseModel):
    """A short-lived URL — FR-M0-038, NFR-035.

    ⚠️ Delivery, not authorization. It must not be stored, logged or embedded
    anywhere that outlives it.
    """

    url: str


def _document_response(row: ClientDocument) -> ClientDocumentResponse:
    """One conversion, named — the same reason as :func:`_note_response`."""
    return ClientDocumentResponse.model_validate(row, from_attributes=True)


class _StorageAuthorizer:
    """Adapts ``platform.storage.authorize_download`` to the kernel port.

    🔒 The indirection is R5: ``modules.clinical.documents`` may not import
    ``platform``, so it declares what it needs as
    ``kernel.storage.DownloadAuthorizer`` and this router — an entry point —
    supplies the implementation. Same shape as ``DatabaseEntitlementGuard``.

    ⚠️ ``session`` is typed as the port declares it (``object``) rather than as
    ``AsyncSession``: narrowing a parameter would make this stop satisfying
    :class:`~app.kernel.storage.DownloadAuthorizer`, and the narrowing would buy
    nothing — the module hands back the session it was given.
    """

    async def authorize_download(
        self, session: object, *, tenant_id: uuid.UUID, file_id: uuid.UUID
    ) -> str:
        if not isinstance(session, AsyncSession):  # pragma: no cover - programming error
            raise TypeError("authorize_download needs the request's AsyncSession")
        return await authorize_download(session, tenant_id=tenant_id, file_id=file_id)


@router.get(
    "/{client_id}/documents",
    summary="A client's documents",
    operation_id="clientDocumentList",
)
@requires(CLIENT_DOCUMENT_READ)
async def client_document_list(
    request: Request,
    client_id: uuid.UUID,
    include_archived: Annotated[bool, Query()] = False,
) -> list[ClientDocumentResponse]:
    """Newest report first — FR-M3-024."""
    await authorized_client(request, client_id)
    rows = await list_documents(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        client_id=client_id,
        include_archived=include_archived,
    )
    return [_document_response(row) for row in rows]


@router.post(
    "/{client_id}/documents",
    status_code=status.HTTP_201_CREATED,
    summary="Attach an uploaded document",
    operation_id="clientDocumentAttach",
)
@requires(CLIENT_DOCUMENT_UPLOAD)
async def client_document_attach(
    request: Request, client_id: uuid.UUID, payload: AttachDocumentRequest
) -> ClientDocumentResponse:
    """FR-M3-024."""
    await authorized_client(request, client_id)
    actor = get_context().actor
    row = await attach_document(
        get_session(request),
        tenant_id=actor.require_tenant(),
        client_id=client_id,
        file_id=payload.file_id,
        document_type=payload.document_type,
        document_date=payload.document_date,
        uploaded_by=ActorType.PRACTITIONER,
        uploaded_by_user_id=actor.require_subject(),
        description=payload.description,
    )
    record_audit(
        request,
        resource_id=row.id,
        metadata={"document_id": str(row.id), "document_type": row.document_type},
    )
    return _document_response(row)


@router.get(
    "/{client_id}/documents/{document_id}/url",
    summary="A short-lived URL for a document",
    operation_id="clientDocumentUrl",
)
@requires(CLIENT_DOCUMENT_DOWNLOAD)
async def client_document_url(
    request: Request, client_id: uuid.UUID, document_id: uuid.UUID
) -> DocumentUrlResponse:
    """FR-M3-026, FR-M0-038, NFR-035.

    🔒 **Its own audited action**, not part of `read`. A signed URL outlives the
    request and leaks through logs, history and screenshots, so "who obtained the
    bytes of this lab report" has to be answerable separately from "who listed
    the documents".

    ⚠️ ``authorize_download`` is passed in rather than imported by the module
    (R5) — the port is ``kernel.storage.DownloadAuthorizer`` and this router is
    the entry point permitted to supply it.
    """
    await authorized_client(request, client_id)
    url = await document_download_url(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        document_id=document_id,
        authorizer=_StorageAuthorizer(),
    )
    record_audit(request, resource_id=document_id, metadata={"document_id": str(document_id)})
    return DocumentUrlResponse(url=url)


@router.post(
    "/{client_id}/documents/{document_id}/archive",
    summary="Archive a document",
    operation_id="clientDocumentArchive",
)
@requires(CLIENT_DOCUMENT_ARCHIVE)
async def client_document_archive(
    request: Request, client_id: uuid.UUID, document_id: uuid.UUID
) -> ClientDocumentResponse:
    """Detach from the active list. The bytes go with erasure, not with this."""
    await authorized_client(request, client_id)
    row = await archive_document(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        document_id=document_id,
    )
    record_audit(request, resource_id=row.id, metadata={"document_id": str(row.id)})
    return _document_response(row)
