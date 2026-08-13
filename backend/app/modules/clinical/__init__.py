"""The clinical workspace — PRD M3, DB §7.

Assessments, measurements, consultation notes and documents: everything the
practitioner knows about a client's health, as opposed to their commercial
relationship (which is `clients`).

🔒 **Owner of the clinical tables** (DB §7). `nutrition` and `ai_drafting` need
what this module holds, and they get it through
``kernel.clinical.NutritionProfileReader`` — a port, not an import (R3).

⚠️ This package exposes its whole public surface here, in ``__init__.py``,
because R2 forbids importing a module's internals: ``from app.modules.clinical
import assessments`` is a boundary violation and the checker reports it as one.
Callers import the names below, never the submodules.

⚠️ **Four of these are re-exported under a longer name than they carry inside
their own file.** ``notes.archive`` and ``documents.archive`` are both
unambiguous where they live and collide the moment they share one namespace, so
the front door disambiguates: ``archive_consultation_note`` and
``archive_document``. The alternative — renaming at source, as `clients` does
with ``add_note`` — reads worse inside a file whose every function is about
notes. The aliases are the only place the two vocabularies meet, which is
exactly what this file is for.
"""

from __future__ import annotations

from app.modules.clinical.actions import (
    ASSESSMENT_READ,
    ASSESSMENT_WRITE,
    CLIENT_DOCUMENT_ARCHIVE,
    CLIENT_DOCUMENT_DOWNLOAD,
    CLIENT_DOCUMENT_READ,
    CLIENT_DOCUMENT_UPLOAD,
    CONSULTATION_NOTE_ARCHIVE,
    CONSULTATION_NOTE_READ,
    CONSULTATION_NOTE_WRITE,
    MEASUREMENT_READ,
    MEASUREMENT_RECORD,
)
from app.modules.clinical.assessments import (
    CORE_DEFINITION_CODE,
    LoadedDefinition,
    SaveOutcome,
    current_definition,
    list_responses,
    load_definition,
    load_response,
    profile_for,
    save_progress,
    start_new,
    start_or_resume,
    submit_response,
)
from app.modules.clinical.documents import archive as archive_document
from app.modules.clinical.documents import attach as attach_document
from app.modules.clinical.documents import download_url as document_download_url
from app.modules.clinical.documents import list_for_client as list_documents
from app.modules.clinical.measurements import (
    MAX_TREND_ROWS,
    DerivedMetrics,
    height_for,
    latest_derived,
    preferred_per_date,
    with_derived,
)
from app.modules.clinical.measurements import history as measurement_history
from app.modules.clinical.measurements import record as record_measurement
from app.modules.clinical.models import (
    AssessmentDefinition,
    AssessmentResponse,
    ClientDocument,
    ClientNutritionProfile,
    ConsultationNote,
    Measurement,
)
from app.modules.clinical.notes import MAX_NOTE_LENGTH, NoteAuthor, validate_body
from app.modules.clinical.notes import add as add_consultation_note
from app.modules.clinical.notes import archive as archive_consultation_note
from app.modules.clinical.notes import edit as edit_consultation_note
from app.modules.clinical.notes import list_for_client as list_consultation_notes

__all__ = [
    "ASSESSMENT_READ",
    "ASSESSMENT_WRITE",
    "CLIENT_DOCUMENT_ARCHIVE",
    "CLIENT_DOCUMENT_DOWNLOAD",
    "CLIENT_DOCUMENT_READ",
    "CLIENT_DOCUMENT_UPLOAD",
    "CONSULTATION_NOTE_ARCHIVE",
    "CONSULTATION_NOTE_READ",
    "CONSULTATION_NOTE_WRITE",
    "CORE_DEFINITION_CODE",
    "MAX_NOTE_LENGTH",
    "MAX_TREND_ROWS",
    "MEASUREMENT_READ",
    "MEASUREMENT_RECORD",
    "AssessmentDefinition",
    "AssessmentResponse",
    "ClientDocument",
    "ClientNutritionProfile",
    "ConsultationNote",
    "DerivedMetrics",
    "LoadedDefinition",
    "Measurement",
    "NoteAuthor",
    "SaveOutcome",
    "add_consultation_note",
    "archive_consultation_note",
    "archive_document",
    "attach_document",
    "current_definition",
    "document_download_url",
    "edit_consultation_note",
    "height_for",
    "latest_derived",
    "list_consultation_notes",
    "list_documents",
    "list_responses",
    "load_definition",
    "load_response",
    "measurement_history",
    "preferred_per_date",
    "profile_for",
    "record_measurement",
    "save_progress",
    "start_new",
    "start_or_resume",
    "submit_response",
    "validate_body",
    "with_derived",
]
