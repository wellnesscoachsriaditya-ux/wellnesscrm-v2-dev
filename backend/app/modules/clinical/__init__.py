"""The clinical workspace — PRD M3, DB §7.

Assessments, measurements, consultation notes and documents: everything the
practitioner knows about a client's health, as opposed to their commercial
relationship (which is `clients`).

🔒 **Owner of the clinical tables** (DB §7). `nutrition` and `ai_drafting` need
what this module holds, and they get it through
``kernel.clinical.NutritionProfileReader`` — a port, not an import (R3).
"""

from __future__ import annotations

from app.modules.clinical.models import (
    AssessmentDefinition,
    AssessmentResponse,
    ClientDocument,
    ClientNutritionProfile,
    ConsultationNote,
    Measurement,
)

__all__ = [
    "AssessmentDefinition",
    "AssessmentResponse",
    "ClientDocument",
    "ClientNutritionProfile",
    "ConsultationNote",
    "Measurement",
]
