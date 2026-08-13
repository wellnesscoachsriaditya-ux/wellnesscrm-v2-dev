"""Nutrition Plans HTTP router."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Path, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.context import get_context
from app.modules.nutrition import (
    NUTRITION_PLANS_WRITE,
    issue_plan_version,
)
from app.platform.http.authz import requires
from app.platform.http.pipeline import get_session, realm_router

router = realm_router("/api/v1/app/nutrition/plans", tags=["nutrition_plans"])


@router.post("/{plan_id}/versions/{version_id}/issue", operation_id="issueDietPlan")
@requires(NUTRITION_PLANS_WRITE)
async def issue_plan(
    request: Request,
    plan_id: uuid.UUID = Path(...),
    version_id: uuid.UUID = Path(...),
) -> Any:
    """Issue a draft plan, generating a snapshot and PDF."""
    session: AsyncSession = get_session(request)
    actor = get_context().actor
    tenant_id = actor.require_tenant()

    # In a full implementation, optimistic concurrency checks would go here.

    await issue_plan_version(
        session=session,
        tenant_id=tenant_id,
        plan_id=plan_id,
        version_id=version_id,
        issued_by_user_id=actor.require_subject(),
    )

    await session.commit()
    return {"status": "ok"}
