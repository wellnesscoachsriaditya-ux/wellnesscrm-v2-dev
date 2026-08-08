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

from app.kernel.clients import configure_client_directory
from app.kernel.events import configure_deferred_enqueuer, deferred_job_types
from app.kernel.jobs import verify_handlers_exist
from app.modules.clients import ClientRepositoryDirectory
from app.platform.audit import (
    LoggingAuditSink,
    SqlAlchemyAuditSink,
    configure_audit_sink,
)
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
from app.platform.http.pipeline import configure_actor_resolver, verify_route_authorization
from app.platform.http.routers.auth import app_router as auth_app_router
from app.platform.http.routers.auth import portal_router as auth_portal_router
from app.platform.http.routers.auth import public_router as auth_public_router
from app.platform.http.routers.clients import router as clients_router
from app.platform.identity.authentication import resolve_actor as authenticate
from app.platform.identity.credentials import raise_if_credentials_are_local
from app.platform.jobs import enqueue_for_event
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

        # 🔒 FR-M0-031 — audit rows go to the append-only table. Installed here
        # rather than at import so the default remains the in-memory sink for
        # tests, which must never reach a database.
        configure_audit_sink(SqlAlchemyAuditSink())

        # 🔒 NFR-029 / D1 — credentials belong to the identity provider. The
        # local store keeps them in process memory: fine for a developer, an
        # outage and a security incident anywhere else. Refuse to start rather
        # than run on it, because the failure is otherwise invisible until
        # someone restarts the process and every password is gone.
        raise_if_credentials_are_local(settings)
    else:
        logger.warning(
            "Local environment: skipping RLS and pooler verification. "
            "Both are mandatory launch gates and run automatically in staging."
        )
        # ⚠️ Logs are not an audit trail — they rotate, they are mutable, and
        # they are not retained for the statutory period. Acceptable only
        # because a developer may have no database, and said loudly rather than
        # degraded quietly.
        configure_audit_sink(LoggingAuditSink())
        logger.warning(
            "Local environment: audit entries are logged, not persisted. "
            "This is NOT an audit trail (FR-M0-031)."
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

    # 🔒 Arch §5.1 step 2 — replace Slice A's anonymous placeholder with real
    # bearer-token verification. Installed here rather than imported by the
    # middleware so the seam stays a single, greppable line: what authenticates
    # this process is decided in one place.
    configure_actor_resolver(authenticate)

    # 🔒 Arch §3.4 / §11.1 — deferred event subscribers enqueue through this.
    # Installed at the entry point because the kernel must not import platform
    # (R5): `kernel.events` names the capability, this decides it is the
    # PostgreSQL queue. Wired in the web process because that is where events
    # are published; the worker wires it too, since a job handler may publish.
    configure_deferred_enqueuer(enqueue_for_event)

    # 🔒 DB §5 — the seam five modules read client identity and stage through.
    # Installed here for the same reason as the enqueuer: R1 forbids the kernel
    # importing the `clients` module that satisfies its port, so the entry point
    # is the one place allowed to know about both.
    configure_client_directory(ClientRepositoryDirectory())

    # 🔒 Fail startup if a deferred subscriber names a job type nothing can run.
    # Those rows would enqueue, fail on every attempt and dead-letter — found in
    # production, at the moment the work was actually needed.
    verify_handlers_exist(deferred_job_types())

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

    # 🔒 Authentication. These routers carry their own full prefixes because the
    # exemption list names absolute paths, and a prefix applied here would make
    # the two disagree silently.
    app.include_router(auth_public_router)
    app.include_router(auth_portal_router)
    app.include_router(auth_app_router)

    # Realm routers are registered as their modules land:
    #   S2  /app/clients        client records; /public/forms  enquiry form
    #   S3  /app/foods          nutrition catalogue
    #   S4  /app/plans          plan authoring
    #   S5  /public/webhooks    provider callbacks
    #   S6  /portal/*           client portal
    #   S12 /admin/*            operator console
    app.include_router(clients_router)

    # 🔒 ADR-05 — last, after every router is registered, so it sees the whole
    # route table. A route that declares no authorization action, declares one
    # nobody registered, or declares one correctly while bypassing the pipeline
    # aborts startup here. Deliberately at import time rather than in `lifespan`:
    # the check needs no I/O, and a failure should be visible to whoever runs
    # the process rather than to whoever first calls the endpoint.
    verify_route_authorization(app)

    logger.info(
        "Application configured",
        extra={"component": "web", "docs_enabled": not production_like},
    )
    return app


app = create_app()
