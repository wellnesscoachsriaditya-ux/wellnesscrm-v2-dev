"""Web process entry point.

🔒 ADR-01 — the web process serves HTTP and nothing else. Background work runs
in ``app.worker``, from the same codebase and the same modules, via a different
entry point. Two processes, one image.

Why this matters at S0 rather than later: NFR-095 requires background work to be
separable "without re-architecture". Separating it immediately makes that true
by construction, rather than a migration someone has to perform under pressure.

Startup order is deliberate. Configuration and logging come first so that any
subsequent failure is visible; the database verifications come next and are
allowed to abort startup, because running with silently disabled tenant
isolation is worse than not running.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.platform.config import Environment, get_settings
from app.platform.db import (
    dispose_engine,
    get_session_factory,
    verify_no_rls_bypass,
    verify_pooler_isolation,
)
from app.platform.http.errors import register_error_handlers
from app.platform.http.health import router as health_router
from app.platform.http.middleware import (
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.platform.logging import configure_logging, get_logger
from app.platform.observability import configure_observability, is_production_like

logger = get_logger(__name__)

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown.

    🔒 Two verifications run here and may abort startup. Both guard against
    failures that are invisible at runtime but catastrophic in effect — the kind
    that are discovered by an auditor rather than a test.
    """
    settings = get_settings()
    logger.info(
        "Starting web process",
        extra={"app_env": settings.app_env.value, "component": "web"},
    )

    if settings.app_env is not Environment.LOCAL:
        # 🔒 DB §2.4 — if the application role can bypass RLS, every isolation
        # policy is inert while appearing present. Refuse to start.
        # 🔒 DB §2.3 — if the pooler reuses sessions, tenant scope leaks between
        # requests. This is the highest-severity infrastructure assumption in
        # the design, so it is verified rather than trusted.
        #
        # Skipped locally only because a developer may not have a database up
        # yet; staging and production always verify.
        factory = get_session_factory(settings)
        async with factory() as session:
            await verify_no_rls_bypass(session)
        async with factory() as session:
            await verify_pooler_isolation(session)
    else:
        logger.warning(
            "Local environment: skipping RLS and pooler verification. "
            "Both are mandatory launch gates and run automatically in staging."
        )

    yield

    logger.info("Shutting down web process")
    await dispose_engine()


def create_app() -> FastAPI:
    """Build the application.

    A factory rather than a module-level instance so tests can construct an app
    with overridden settings, and so import order cannot trigger startup work.
    """
    settings = get_settings()
    production_like = is_production_like(settings)

    configure_logging(level=settings.log_level, json_output=production_like)
    configure_observability(settings, component="web")

    app = FastAPI(
        title="WellnessCRM V2 API",
        version="0.1.0",
        description=(
            "Practice management for Indian nutrition practitioners.\n\n"
            "🔒 Realm-segmented paths (ADR-A01): `/app` practitioner, "
            "`/portal` client, `/admin` operator, `/public` unauthenticated."
        ),
        lifespan=lifespan,
        # 🔒 Interactive docs are disabled outside local development. The schema
        # is generated for the TypeScript client (NFR-079); it does not need to
        # be publicly browsable in production.
        docs_url="/docs" if not production_like else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not production_like else None,
    )

    # Middleware executes in reverse registration order, so the context
    # middleware must be added last to run first — every other component,
    # including error handling, depends on the request id existing.
    app.add_middleware(SecurityHeadersMiddleware, is_production=production_like)
    app.add_middleware(RequestContextMiddleware)

    register_error_handlers(app)

    # 🔒 ADR-A01 — health lives under `/public`, the only unauthenticated surface.
    app.include_router(health_router, prefix=f"{API_PREFIX}/public")

    # Realm routers are registered as their modules land:
    #   S1  /public/auth        practitioner registration and sign-in
    #   S2  /app/clients        client records; /public/forms  enquiry form
    #   S3  /app/foods          nutrition catalogue
    #   S4  /app/plans          plan authoring
    #   S5  /public/webhooks    provider callbacks
    #   S6  /portal/*           client portal
    #   S12 /admin/*            operator console

    logger.info(
        "Application configured",
        extra={"component": "web", "docs_enabled": not production_like},
    )
    return app


app = create_app()
