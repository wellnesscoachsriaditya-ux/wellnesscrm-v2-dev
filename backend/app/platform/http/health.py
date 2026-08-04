"""Health endpoints.

🔒 NFR-086 — health checks cover the database, storage and each external
dependency.

Three distinct questions, deliberately separated:

* **Liveness** — is the process running? Restart it if not.
* **Readiness** — can it serve traffic? Withhold traffic if not.
* **Dependencies** — are external services reachable? ⚠️ Reported, never
  failing the check: a WhatsApp outage must not take the application down
  (Arch §18.4). Degrade, never block.

🔒 These are the only unauthenticated endpoints outside ``/public/*`` and they
disclose nothing about tenants, clients or configuration.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field

from app.platform.db import check_database_health
from app.platform.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["health"])


class LivenessResponse(BaseModel):
    """Process is alive."""

    status: Literal["ok"] = "ok"


class DependencyStatus(BaseModel):
    """State of one dependency."""

    name: str
    status: Literal["ok", "degraded", "unavailable", "not_configured"]
    detail: str | None = None


class ReadinessResponse(BaseModel):
    """Readiness plus dependency detail.

    ``ready`` reflects only what is *required* to serve traffic. Optional
    dependencies appear in ``dependencies`` and are surfaced to the operator
    console (FR-M11-009) without gating the check.
    """

    ready: bool
    dependencies: list[DependencyStatus] = Field(default_factory=list)


@router.get(
    "/health",
    response_model=LivenessResponse,
    summary="Liveness probe",
    operation_id="healthLiveness",
)
async def liveness() -> LivenessResponse:
    """Return 200 if the process is running.

    🔒 Deliberately does nothing else. A liveness probe that touches the
    database will fail during a transient database blip and cause the platform
    to restart a perfectly healthy process — turning a brief outage into a
    longer one.
    """
    return LivenessResponse()


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    operation_id="healthReadiness",
)
async def readiness(response: Response) -> ReadinessResponse:
    """Report whether the application can serve traffic.

    Returns 503 when a *required* dependency is unavailable, so the platform
    withholds traffic rather than serving errors.
    """
    dependencies: list[DependencyStatus] = []

    db_ok = await check_database_health()
    dependencies.append(
        DependencyStatus(
            name="database",
            status="ok" if db_ok else "unavailable",
            detail=None if db_ok else "Connection failed",
        )
    )

    # Storage and third-party adapters register here as they arrive (S1, S5).
    # ⚠️ Only the database is required for readiness. An external provider being
    # down must never make the whole application unready — practitioner work
    # continues and the affected work queues (Arch §17.3).

    ready = db_ok
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(ready=ready, dependencies=dependencies)
