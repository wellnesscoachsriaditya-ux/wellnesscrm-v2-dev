"""Database engine, session management and the tenant isolation mechanism.

🔒 ADR-06 / DB §2.3 — tenant isolation is enforced by PostgreSQL Row Level
Security, keyed on a transaction-scoped session variable. This module is where
that variable is set, and therefore where isolation actually happens.

⚠️ **LAUNCH GATE (DB §2.3, Arch §23).** ``SET LOCAL`` is transaction-scoped, so
the transaction and every statement within it must share one connection. This
requires the pooler to run in **transaction mode**. Session-mode pooling with
connection reuse across requests would silently break isolation and make every
RLS policy decorative. :func:`verify_pooler_isolation` checks this; it is a
blocking launch gate, not an assumption.

🔒 The application connects as ``app_user``, which does **not** have
``BYPASSRLS`` and holds no DDL rights. Using the Supabase service-role key here
would disable every policy in the system — the exact V1 failure ADR-02 exists to
prevent. :func:`verify_no_rls_bypass` asserts this at startup.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.platform.config import Settings, get_settings
from app.platform.logging import get_logger

logger = get_logger(__name__)

# Session variables read by RLS policies (DB §2.3).
TENANT_SETTING = "app.tenant_id"
ACTOR_SETTING = "app.actor_id"
ROLE_SETTING = "app.actor_role"


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """Return the process-wide engine, creating it on first use."""
    global _engine
    if _engine is None:
        settings = settings or get_settings()
        _engine = create_async_engine(
            settings.database_url.get_secret_value(),
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_pre_ping=True,
            # 🔒 Never echo SQL: statements carry parameter values, i.e. clinical
            # data (NFR-033). Debugging uses explain plans, not statement logs.
            echo=False,
        )
    return _engine


def get_session_factory(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    """Return the session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(settings),
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def dispose_engine() -> None:
    """Dispose the engine on shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


async def set_tenant_scope(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    actor_id: uuid.UUID | None = None,
    actor_role: str | None = None,
) -> None:
    """Set the transaction-scoped variables that RLS policies read.

    🔒 Must be called inside an open transaction. ``SET LOCAL`` outside one is
    silently discarded — which would leave policies evaluating an empty tenant
    and quietly returning nothing, or worse.

    Args:
        tenant_id: The tenant to scope to. ``None`` for platform-level work
            (operator console, system jobs), where RLS is not the control.
        actor_id: The acting subject, available to policies if needed.
        actor_role: The actor's role.
    """
    # Parameterised: these values reach a SET statement, and string
    # interpolation there would be an injection vector (NFR-038).
    await session.execute(
        text(f"SELECT set_config('{TENANT_SETTING}', :tenant, true)"),
        {"tenant": str(tenant_id) if tenant_id else ""},
    )
    if actor_id is not None:
        await session.execute(
            text(f"SELECT set_config('{ACTOR_SETTING}', :actor, true)"),
            {"actor": str(actor_id)},
        )
    if actor_role is not None:
        await session.execute(
            text(f"SELECT set_config('{ROLE_SETTING}', :role, true)"),
            {"role": actor_role},
        )


@asynccontextmanager
async def transaction(
    *,
    tenant_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    actor_role: str | None = None,
) -> AsyncIterator[AsyncSession]:
    """Open a transaction with tenant scope applied.

    🔒 ADR-04 — one transaction per request, owned by the caller. Services never
    commit; the transaction commits on success and rolls back on any error.

    This is what makes multi-step operations atomic without services knowing
    about one another, and what makes job enqueue transactional: a job exists
    only if its transaction commits (Arch §11.1).
    """
    factory = get_session_factory()
    async with factory() as session, session.begin():
        await set_tenant_scope(
            session,
            tenant_id=tenant_id,
            actor_id=actor_id,
            actor_role=actor_role,
        )
        yield session


# ─── Startup verification ────────────────────────────────────────────────


async def verify_no_rls_bypass(session: AsyncSession) -> None:
    """🔒 Assert the application role cannot bypass RLS.

    If ``app_user`` has ``BYPASSRLS`` — or if the connection is using a
    superuser or the Supabase service role — every isolation policy in the
    system is inert while appearing to be present.

    Raises:
        RuntimeError: If the role can bypass RLS. Startup must fail: running
            with silently disabled isolation is worse than not running.
    """
    row = (
        await session.execute(
            text(
                "SELECT current_user AS role_name, "
                "       rolbypassrls, rolsuper "
                "FROM pg_roles WHERE rolname = current_user"
            )
        )
    ).one()

    role_name, bypass_rls, is_super = row.role_name, row.rolbypassrls, row.rolsuper

    if bypass_rls or is_super:
        raise RuntimeError(
            f"FATAL: the database role '{role_name}' can bypass Row Level Security "
            f"(bypassrls={bypass_rls}, superuser={is_super}).\n\n"
            "Every tenant isolation policy would be inert. The application must "
            "connect as 'app_user' — a role with neither BYPASSRLS nor SUPERUSER, "
            "and no DDL rights (DB §2.4).\n\n"
            "If this is a Supabase project, check that DATABASE_URL is not using "
            "the service-role credentials. The service key is for Auth and Storage "
            "administration only — never for data access (ADR-02)."
        )

    logger.info(
        "Database role verified: RLS cannot be bypassed",
        extra={"db_role": role_name},
    )


async def verify_pooler_isolation(session: AsyncSession) -> None:
    """🔒 Verify ``SET LOCAL`` is transaction-scoped on this connection.

    ⚠️ **The highest-severity infrastructure assumption in the whole design.**

    Sets a tenant value inside a transaction, confirms it is visible, and — on a
    fresh transaction — confirms it did not persist. If it persists, the pooler
    is reusing sessions in a way that leaks tenant scope between requests, and
    the correct response is to refuse to start.

    Raises:
        RuntimeError: If the setting survives its transaction.
    """
    probe = str(uuid.uuid4())

    async with session.begin():
        await session.execute(
            text(f"SELECT set_config('{TENANT_SETTING}', :v, true)"), {"v": probe}
        )
        seen = (
            await session.execute(text(f"SELECT current_setting('{TENANT_SETTING}', true)"))
        ).scalar()
        if seen != probe:
            raise RuntimeError(
                "FATAL: SET LOCAL did not take effect within a transaction. "
                "Tenant isolation cannot function on this connection."
            )

    async with session.begin():
        leaked = (
            await session.execute(text(f"SELECT current_setting('{TENANT_SETTING}', true)"))
        ).scalar()

    if leaked == probe:
        raise RuntimeError(
            "FATAL: the tenant setting survived its transaction.\n\n"
            "The connection pooler appears to be running in SESSION mode with "
            "connection reuse. Tenant scope would leak between requests, and every "
            "RLS policy in the system would be unreliable.\n\n"
            "Switch the pooler to TRANSACTION mode (DB §2.3). This is a blocking "
            "launch gate."
        )

    logger.info("Pooler verified: SET LOCAL is transaction-scoped")


async def check_database_health() -> bool:
    """Lightweight connectivity probe for the readiness endpoint (NFR-086)."""
    try:
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("Database health check failed")
        return False
