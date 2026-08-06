"""Live-database fixtures — AC-M0-003.

🔒 Every fixture here fails *closed*. If the database is unreachable, the schema
is not migrated, or the connecting role can bypass RLS, the tests skip or fail
with a message naming the fix. What must never happen is a pass that did not
actually test isolation: a green tick on an untested guarantee is worse than a
visible gap, because nobody looks again.

Connection URLs come from the environment, not from a fixture default:

* ``TEST_DATABASE_URL`` — connects as ``app_user``. 🔒 Must be a role *without*
  ``BYPASSRLS``; the whole point is that policies apply to it.
* ``TEST_DATABASE_MIGRATION_URL`` — connects as ``app_migrator``. Used only to
  seed rows the tests then try to read across the tenant boundary, because
  ``app_user`` cannot insert a row for a tenant it is not scoped to (that is
  itself asserted, in ``test_tenant_isolation``).

⚠️ Never point these at a database with real data. ``seeded_tenants`` deletes the
rows it creates, and a failure mid-test can leave them behind.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

#: Set by the operator running the suite. Absent on a machine without PostgreSQL,
#: which is the normal case for this project today (see ops/db/README.md).
APP_URL_VAR = "TEST_DATABASE_URL"
MIGRATION_URL_VAR = "TEST_DATABASE_MIGRATION_URL"

#: 🔒 Set to `1` in CI. Turns the skip below into a failure.
#:
#: ⚠️ Without this, the gate has a hole shaped exactly like its own purpose. A
#: skip is green. If the CI job's `services:` block were removed, its env vars
#: renamed, or the database made unreachable, these tests would report "skipped"
#: and the pipeline would pass — and the one guarantee the whole tenancy model
#: rests on would go unverified with no visible signal. Skipping is legitimate on
#: a developer machine with no PostgreSQL; it is never legitimate in CI, and the
#: difference has to be something the environment asserts rather than something a
#: reader of the log notices.
REQUIRE_VAR = "REQUIRE_LIVE_DATABASE"

_MISSING_DATABASE = (
    f"No live PostgreSQL: set {APP_URL_VAR} (as app_user) and {MIGRATION_URL_VAR} "
    f"(as app_migrator) to run the AC-M0-003 tenant-isolation gate. "
    f"See ops/db/README.md > 'Verifying tenant isolation (AC-M0-003)'."
)

_REQUIRED_BUT_MISSING = (
    f"{REQUIRE_VAR} is set, but {APP_URL_VAR} and/or {MIGRATION_URL_VAR} are not.\n\n"
    "This suite is mandatory here and must not be skipped. Either the database "
    "service failed to start or the connection variables were not passed to "
    "pytest. Fix the environment — do not unset "
    f"{REQUIRE_VAR}, which would turn the sprint gate back into a silent skip."
)

#: Tables the isolation gate exercises. `users` is the representative
#: tenant-scoped table: it has an RLS policy, a NOT NULL `tenant_id`, and no
#: dependency on a table that does not exist until S2.
ISOLATED_TABLE = "users"


def _url(variable: str) -> str | None:
    value = os.environ.get(variable, "").strip()
    return value or None


def require_database() -> tuple[str, str]:
    """Return both URLs, or skip the test — unless skipping is forbidden.

    Both are required. Running with only one would silently test something
    weaker than the gate: seeding as ``app_user`` cannot cross a tenant
    boundary, so the "other tenant's row" would never exist and the read would
    return zero rows for the wrong reason — the most dangerous kind of pass.

    🔒 When ``REQUIRE_LIVE_DATABASE`` is set, a missing URL is a *failure*, not a
    skip. See :data:`REQUIRE_VAR` for why the distinction is load-bearing.
    """
    app_url, migration_url = _url(APP_URL_VAR), _url(MIGRATION_URL_VAR)
    if not app_url or not migration_url:
        if _url(REQUIRE_VAR):
            pytest.fail(_REQUIRED_BUT_MISSING)
        pytest.skip(_MISSING_DATABASE)
    return app_url, migration_url


@pytest_asyncio.fixture
async def app_engine() -> AsyncIterator[AsyncEngine]:
    """Engine connecting as ``app_user`` — the role RLS must constrain.

    ⚠️ Function-scoped deliberately, despite costing a connection per test. A
    session-scoped async fixture is created on a session-scoped event loop, while
    the tests themselves run on function-scoped ones — so the engine would be
    bound to a loop nothing is awaiting it from, and every test would fail on
    first connect. That failure cannot appear locally, because with no database
    configured the tests skip before reaching it.
    """
    app_url, _ = require_database()
    engine = create_async_engine(app_url, poolclass=None, echo=False)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def migrator_engine() -> AsyncIterator[AsyncEngine]:
    """Engine connecting as ``app_migrator`` — used only to seed fixtures.

    Function-scoped for the same event-loop reason as :func:`app_engine`.
    """
    _, migration_url = require_database()
    engine = create_async_engine(migration_url, poolclass=None, echo=False)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def verify_test_preconditions(app_engine: AsyncEngine, migrator_engine: AsyncEngine) -> None:
    """🔒 Refuse to run the gate against a database that cannot prove anything.

    Three preconditions, each of which would otherwise produce a *false pass*:

    1. **The schema is migrated.** Without ``users``, every query errors and a
       carelessly written test could read that as "no rows returned".
    2. **``app_user`` cannot bypass RLS.** With ``BYPASSRLS`` the policies are
       inert and a cross-tenant read *succeeds* — but a test asserting on the
       row count would still be exercising the right code path, so the failure
       must be attributed correctly rather than reported as an isolation leak.
    3. **The policy exists and is forced.** ``pg_class.relforcerowsecurity`` is
       the half that ``ENABLE`` alone does not set, and ``app_migrator`` owns
       these tables — so an unforced table leaves the seeding connection able to
       see everything, which is fine, while hiding that ``app_user`` would too
       if it ever became the owner.
    """
    async with app_engine.connect() as connection:
        missing = not await _table_exists(connection, ISOLATED_TABLE)
        if missing:
            pytest.fail(
                f"Table `{ISOLATED_TABLE}` does not exist. Run the migrations "
                "first:\n  cd backend && alembic upgrade head\n"
                "See ops/db/README.md for the full provisioning order."
            )

        row = (
            await connection.execute(
                text(
                    "SELECT current_user AS role_name, rolbypassrls, rolsuper "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
        if row.rolbypassrls or row.rolsuper:
            pytest.fail(
                f"TEST_DATABASE_URL connects as `{row.role_name}`, which can bypass "
                f"RLS (bypassrls={row.rolbypassrls}, superuser={row.rolsuper}).\n\n"
                "This suite would pass without testing anything. Point it at "
                "`app_user`, created by ops/db/001_roles.sql."
            )

    async with migrator_engine.connect() as connection:
        forced = (
            await connection.execute(
                text("SELECT relforcerowsecurity FROM pg_class WHERE relname = :t"),
                {"t": ISOLATED_TABLE},
            )
        ).scalar()
        if not forced:
            pytest.fail(
                f"`{ISOLATED_TABLE}` does not have FORCE ROW LEVEL SECURITY. The "
                "table owner bypasses every policy without it, and migrations run "
                "as the owner. Re-run `alembic upgrade head` against a clean "
                "database — revision 0002_platform_kernel sets it."
            )


async def _table_exists(connection: AsyncConnection, table: str) -> bool:
    return bool(
        (
            await connection.execute(
                text("SELECT to_regclass(:qualified) IS NOT NULL"),
                {"qualified": f"public.{table}"},
            )
        ).scalar()
    )


@pytest_asyncio.fixture
async def seeded_tenants(migrator_engine: AsyncEngine) -> AsyncIterator[tuple[uuid.UUID, ...]]:
    """Create two tenants, each with one user, and clean up afterwards.

    Seeded as ``app_migrator`` deliberately: ``app_user`` cannot insert a row
    carrying a ``tenant_id`` it is not scoped to — the ``WITH CHECK`` half of the
    policy forbids it, and that is asserted separately. Building the fixture
    through the application role would therefore be impossible, not merely
    inconvenient.
    """
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()

    async with migrator_engine.begin() as connection:
        for index, tenant_id in enumerate((tenant_a, tenant_b)):
            await connection.execute(
                text(
                    "INSERT INTO tenants (id, name, slug, status) "
                    "VALUES (:id, :name, :slug, 'active')"
                ),
                {"id": tenant_id, "name": f"Tenant {index}", "slug": f"ac-m0-003-{tenant_id}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "  (tenant_id, auth_subject_id, email, full_name, role, status) "
                    "VALUES (:tenant, :subject, :email, :name, 'practitioner', 'active')"
                ),
                {
                    "tenant": tenant_id,
                    "subject": f"gotrue-{tenant_id}",
                    "email": f"user-{tenant_id}@example.test",
                    "name": f"Practitioner {index}",
                },
            )

    try:
        yield (tenant_a, tenant_b)
    finally:
        # 🔒 As the owner, and in FK order. Leaving rows behind would make a
        # re-run's uniqueness assertions fail for an unrelated reason.
        async with migrator_engine.begin() as connection:
            for tenant_id in (tenant_a, tenant_b):
                await connection.execute(
                    text("DELETE FROM users WHERE tenant_id = :id"), {"id": tenant_id}
                )
                await connection.execute(
                    text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
                )
