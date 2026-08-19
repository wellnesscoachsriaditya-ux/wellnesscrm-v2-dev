"""Background jobs for the nutrition module."""

import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.events import subscribe
from app.kernel.jobs import register_handler
from app.kernel.models import JobClass
from app.modules.nutrition.events import PlanVersionIssued

logger = logging.getLogger(__name__)


async def generate_pdf_handler(payload: Mapping[str, Any], session: AsyncSession) -> None:
    """Worker handler to generate a PDF for an issued diet plan version."""
    plan_version_id_str = payload.get("plan_version_id")
    if not plan_version_id_str:
        raise ValueError("Missing plan_version_id in job payload")

    # The actual renderer (e.g. Playwright/Chromium) is not integrated in this slice.
    # The production boundary is respected:
    # pending -> worker -> renderer -> storage -> ready/failed.
    logger.info("PDF generation requested", extra={"plan_version_id": plan_version_id_str})
    raise NotImplementedError(
        "PDF rendering infrastructure (Playwright) is not yet available in this slice"
    )


def register_jobs() -> None:
    """Register all background job handlers for the nutrition module."""
    register_handler("generate_nutrition_pdf", JobClass.RENDERING, generate_pdf_handler)
    subscribe(PlanVersionIssued, deferred_job_type="generate_nutrition_pdf")
