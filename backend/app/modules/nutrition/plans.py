"""Nutrition Plans Service."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.errors import ConflictError, NotFoundError
from app.kernel.events import publish
from app.kernel.nutrition import (
    PlanState,
    RenderStatus,
)
from app.modules.nutrition.events import PlanVersionIssued
from app.modules.nutrition.models import DietPlan, DietPlanVersion, PlanSnapshot


# -- PDF Service Abstraction --
async def enqueue_pdf_generation(
    session: AsyncSession, tenant_id: UUID, plan_version_id: UUID
) -> None:
    """Queue a background job to generate the PDF for this issued plan version."""
    await publish(PlanVersionIssued(tenant_id=tenant_id, plan_version_id=plan_version_id), session)


# -- State Machine Transitions --


async def issue_plan_version(
    session: AsyncSession,
    tenant_id: UUID,
    plan_id: UUID,
    version_id: UUID,
    issued_by_user_id: UUID,
) -> None:
    """Issue a draft plan version."""
    stmt = select(DietPlanVersion).where(
        DietPlanVersion.id == version_id,
        DietPlanVersion.tenant_id == tenant_id,
        DietPlanVersion.plan_id == plan_id,
    )
    result = await session.execute(stmt)
    version = result.scalar_one_or_none()

    if not version:
        raise NotFoundError(
            message="The requested diet plan version could not be found.",
            action="Check the version ID and try again.",
        )

    if version.state != PlanState.draft:
        raise ConflictError(
            message="This plan version has already been issued or discarded.",
            action="Reload the plan to see the latest version.",
        )

    # In a full implementation, we would query the resolved portions here
    # and compute real totals using `calculate_composition`.
    computed_totals = {"energy_kcal": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0, "fibre_g": 0}

    now = datetime.now(UTC)

    # Generate snapshot (denormalized JSON representing the plan in full)
    snapshot_doc = {"title": "Diet Plan", "version_number": version.version_number, "days": []}

    snapshot = PlanSnapshot(
        tenant_id=tenant_id,
        plan_version_id=version_id,
        document=snapshot_doc,
        document_schema_version=1,
        pdf_status=RenderStatus.pending,
    )
    session.add(snapshot)

    # Update version
    version.state = PlanState.issued
    version.issued_at = now
    version.issued_by_user_id = issued_by_user_id
    version.computed_totals = computed_totals

    # Supersede previous issued version
    stmt_supersede = (
        update(DietPlanVersion)
        .where(
            DietPlanVersion.plan_id == plan_id,
            DietPlanVersion.tenant_id == tenant_id,
            DietPlanVersion.state == PlanState.issued,
            DietPlanVersion.id != version_id,
        )
        .values(state=PlanState.superseded)
    )
    await session.execute(stmt_supersede)

    # Update current_version_id on DietPlan
    stmt_plan = (
        update(DietPlan)
        .where(DietPlan.id == plan_id, DietPlan.tenant_id == tenant_id)
        .values(current_version_id=version_id)
    )
    await session.execute(stmt_plan)

    # Enqueue PDF generation job
    await enqueue_pdf_generation(session, tenant_id, version_id)
