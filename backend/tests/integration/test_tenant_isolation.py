"""AC-M0-003 — tenant isolation, enforced by PostgreSQL rather than by us.

🔒 **This is the gate the entire tenancy model rests on.** ADR-06 chose a shared
schema with RLS over a database per tenant, and the argument for that choice is
precisely this: *a forgotten `WHERE tenant_id` returns nothing rather than
another tenant's data* (NFR-030, DB §65). V1's failure was trusting application
code with that filter. This file is where the replacement is proved.

⚠️ **Every query below deliberately omits the tenant filter.** That is not an
oversight to be tidied up — it is the experiment. A query with a `WHERE
tenant_id = :current` clause would pass on a database with RLS entirely
disabled, which is exactly the false pass these tests exist to make impossible.
If a future refactor "fixes" these queries by adding the filter back, the suite
still passes and stops testing anything. The comments say so at each site.

Structural counterparts live in ``tests/test_kernel_schema.py``: that policies
are declared, forced, and carry ``WITH CHECK``. Those run without a database and
cannot prove enforcement. These can, and need one.

Running them::

    # 1. Provision roles (once per cluster, as a superuser)
    psql -f ops/db/001_roles.sql
    psql -c "ALTER ROLE app_user LOGIN PASSWORD 'localdev'"
    psql -c "ALTER ROLE app_migrator LOGIN PASSWORD 'localdev'"

    # 2. Migrate
    cd backend
    $env:DATABASE_MIGRATION_URL = "postgresql+psycopg://app_migrator:localdev@localhost:5432/wellnesscrm"
    alembic upgrade head

    # 3. Run this file
    $env:TEST_DATABASE_URL = "postgresql+psycopg://app_user:localdev@localhost:5432/wellnesscrm"
    $env:TEST_DATABASE_MIGRATION_URL = $env:DATABASE_MIGRATION_URL
    pytest tests/integration -v

``ops/db/README.md`` carries the same sequence with the reasoning.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.platform.db import TENANT_SETTING

pytestmark = pytest.mark.isolation


async def _scope_to(connection: object, tenant_id: uuid.UUID | None) -> None:
    """Set the transaction-scoped tenant variable the policies read.

    Mirrors :func:`app.platform.db.set_tenant_scope` rather than calling it: this
    test must exercise the *database's* behaviour given a session variable, and
    routing through application code would make an application bug able to mask
    a database misconfiguration.
    """
    await connection.execute(  # type: ignore[attr-defined]
        text(f"SELECT set_config('{TENANT_SETTING}', :value, true)"),
        {"value": str(tenant_id) if tenant_id else ""},
    )


# ─── The gate ────────────────────────────────────────────────────────────


async def test_cross_tenant_read_returns_nothing_with_the_filter_removed(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 **AC-M0-003.** Scoped to tenant A, an unfiltered read sees only A.

    ⚠️ ``SELECT ... FROM users`` carries no ``WHERE tenant_id``. This simulates
    the defect the design assumes will eventually happen — a service method that
    forgets the filter. RLS must make that mistake harmless.

    Two tenants exist, each with one user. A correct database returns exactly the
    one row belonging to the scoped tenant. A database with RLS disabled,
    unforced, or bypassable returns two.
    """
    tenant_a, tenant_b = seeded_tenants

    async with app_engine.connect() as connection, connection.begin():
        await _scope_to(connection, tenant_a)

        # 🔒 No tenant filter. Deliberate. See the module docstring.
        rows = (await connection.execute(text("SELECT tenant_id FROM users"))).all()

    seen = {row.tenant_id for row in rows}

    assert seen == {tenant_a}, (
        f"An unfiltered read scoped to tenant {tenant_a} returned rows for {seen}. "
        f"Tenant isolation is NOT being enforced — AC-M0-003 fails. Check that "
        f"`users` has RLS enabled AND forced, that the policy exists, and that "
        f"the connecting role lacks BYPASSRLS."
    )
    assert tenant_b not in seen, "another tenant's data is visible; this is a data breach"
    assert len(rows) == 1, f"expected exactly one row for tenant {tenant_a}, got {len(rows)}"


async def test_unscoped_connection_sees_no_rows_at_all(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 No tenant set means no rows — fail closed, not open.

    A connection that never called ``set_tenant_scope`` is a bug: some path
    reached the database without establishing who it was acting for. The
    database's answer must be *nothing*, not *everything*.

    This is what ``current_tenant_id()`` returning NULL buys. ``tenant_id =
    NULL`` is NULL, never true, so the policy matches no row.
    """
    async with app_engine.connect() as connection, connection.begin():
        # No _scope_to() call at all — the variable is unset.
        count = (await connection.execute(text("SELECT count(*) FROM users"))).scalar()

    assert count == 0, (
        f"An unscoped connection saw {count} user row(s). A missing tenant scope "
        "must fail closed. Check that current_tenant_id() returns NULL rather "
        "than raising or defaulting."
    )


async def test_write_carrying_another_tenants_id_is_rejected(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 The ``WITH CHECK`` half — ``USING`` alone would permit this.

    Scoped to tenant A, inserting a row stamped for tenant B must fail. Without
    ``WITH CHECK`` the write *succeeds*: the row lands in tenant B's data,
    invisible to the writer afterwards (``USING`` hides it), and present in the
    victim's account. A read-only policy is half a policy, and the missing half
    is the one that corrupts someone else's data.
    """
    tenant_a, tenant_b = seeded_tenants

    async with app_engine.connect() as connection, connection.begin():
        await _scope_to(connection, tenant_a)

        with pytest.raises(DBAPIError) as exception_info:
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "  (tenant_id, auth_subject_id, email, full_name, role, status) "
                    "VALUES (:tenant, :subject, :email, 'Injected', "
                    "        'practitioner', 'active')"
                ),
                {
                    "tenant": tenant_b,
                    "subject": f"gotrue-injected-{uuid.uuid4()}",
                    "email": f"injected-{uuid.uuid4()}@example.test",
                },
            )

    message = str(exception_info.value).lower()
    assert "row-level security" in message or "row level security" in message, (
        "The insert failed, but not because of RLS. The policy's WITH CHECK "
        f"clause may be missing. Error was: {exception_info.value}"
    )


async def test_update_cannot_move_a_row_to_another_tenant(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 ``WITH CHECK`` also constrains UPDATE, not only INSERT.

    Re-stamping an owned row with another tenant's id is a subtler version of the
    same attack: the row already passes ``USING``, so only ``WITH CHECK`` stops
    it leaving. Success here would mean a tenant can *push* data into another
    tenant's account.
    """
    tenant_a, tenant_b = seeded_tenants

    async with app_engine.connect() as connection, connection.begin():
        await _scope_to(connection, tenant_a)

        with pytest.raises(DBAPIError) as exception_info:
            await connection.execute(
                text("UPDATE users SET tenant_id = :other"), {"other": tenant_b}
            )

    message = str(exception_info.value).lower()
    assert (
        "row-level security" in message or "row level security" in message
    ), f"UPDATE was not blocked by RLS. Error was: {exception_info.value}"


async def test_delete_cannot_reach_another_tenants_row(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 A destructive unfiltered statement must be scoped too.

    ``DELETE FROM users`` with no ``WHERE`` is the worst-case forgotten filter.
    Under RLS it can only reach rows ``USING`` admits, so it deletes the scoped
    tenant's row and leaves the other tenant untouched.

    ⚠️ Asserted here only by the deleted row *count*. That tenant B's row is
    still present is the next test's job, on a separate connection — the
    deleting transaction is the last thing that should be asked whether the
    delete stayed in bounds.
    """
    tenant_a, tenant_b = seeded_tenants

    async with app_engine.connect() as connection, connection.begin():
        await _scope_to(connection, tenant_a)
        # 🔒 No WHERE clause. Deliberate.
        result = await connection.execute(text("DELETE FROM users"))
        deleted = result.rowcount

    assert deleted == 1, f"expected to delete only tenant A's single row, deleted {deleted}"


async def test_tenant_b_row_survives_tenant_a_deleting_everything(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> None:
    """The other half of the previous test: B's row is still there afterwards.

    Read on a second connection so the answer cannot come from the transaction
    that did the deleting.

    ⚠️ The owner connection is scoped to tenant B first. ``users`` is FORCE ROW
    LEVEL SECURITY, so ``app_migrator`` is subject to the policy despite owning
    the table — an unscoped read here returns 0 and this test would report "an
    unfiltered DELETE crossed the tenant boundary" when nothing of the sort
    happened. A false alarm on this particular assertion is expensive: it points
    the reader at the tenancy model when the bug is in the fixture.
    """
    tenant_a, tenant_b = seeded_tenants

    async with app_engine.connect() as connection, connection.begin():
        await _scope_to(connection, tenant_a)
        await connection.execute(text("DELETE FROM users"))

    async with migrator_engine.connect() as connection, connection.begin():
        await _scope_to(connection, tenant_b)
        surviving = (
            await connection.execute(
                text("SELECT count(*) FROM users WHERE tenant_id = :id"), {"id": tenant_b}
            )
        ).scalar()

    assert surviving == 1, (
        f"Tenant B had {surviving} rows after tenant A deleted everything it could "
        "see. An unfiltered DELETE crossed the tenant boundary."
    )


async def test_force_rls_binds_the_table_owner_too(
    migrator_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 ``FORCE`` is enforced, not merely declared.

    ``app_migrator`` owns every table here. Without ``FORCE ROW LEVEL SECURITY``
    an owner bypasses all policies, which would make a fix-up script or a
    migration the one context where tenant isolation silently does not apply —
    the context where a mistake reaches every tenant at once.

    ⚠️ The structural counterpart reads ``pg_class.relforcerowsecurity``, and the
    ``conftest`` precondition does the same. Both check the flag is *set*. This
    checks it *bites*, which is a different claim: an unscoped owner sees zero
    of the two rows it just inserted.

    This is also the property that broke this suite's own fixture — seeding
    assumed owner bypass, and the INSERT was rejected by the policy's
    ``WITH CHECK``. Asserted explicitly so the next person meets it as a stated
    rule rather than as a confusing failure.
    """
    async with migrator_engine.connect() as connection, connection.begin():
        # 🔒 No _scope_to() call. Deliberate — that is the experiment.
        visible = (await connection.execute(text("SELECT count(*) FROM users"))).scalar()

    assert visible == 0, (
        f"The table owner saw {visible} user row(s) without a tenant scope. "
        "`users` is missing FORCE ROW LEVEL SECURITY, so migrations and any "
        "script run as `app_migrator` bypass tenant isolation entirely. "
        "Revision 0002_platform_kernel sets it."
    )


async def test_scope_does_not_survive_its_transaction(app_engine: AsyncEngine) -> None:
    """🔒 DB §2.3 — the highest-severity infrastructure assumption in the design.

    ``set_config(..., true)`` is transaction-scoped. If the value survives into
    the next transaction on the same connection, the pooler is reusing sessions
    in a way that leaks tenant scope between requests, and every policy in the
    system becomes unreliable in a way no policy audit would reveal.

    This is the same property :func:`app.platform.db.verify_pooler_isolation`
    checks at startup. It is asserted here as well because startup verification
    only runs where something is deployed, and nothing is yet.
    """
    probe = uuid.uuid4()

    # ⚠️ The connection is opened *outside* both transactions on purpose. Scoping
    # it to either one would mean the second `begin()` ran on a different
    # connection, where the setting is absent for a trivial reason and the test
    # passes without testing anything. The whole point is to ask the same
    # connection twice, across a transaction boundary.
    async with app_engine.connect() as connection:
        async with connection.begin():
            await _scope_to(connection, probe)
            inside = (
                await connection.execute(text(f"SELECT current_setting('{TENANT_SETTING}', true)"))
            ).scalar()
            assert inside == str(probe), "set_config did not take effect inside its transaction"

        async with connection.begin():
            after = (
                await connection.execute(text(f"SELECT current_setting('{TENANT_SETTING}', true)"))
            ).scalar()

    assert after != str(probe), (
        "The tenant setting survived its transaction. The pooler is in SESSION "
        "mode with connection reuse, tenant scope leaks between requests, and "
        "RLS cannot be relied on (DB §2.3 — blocking launch gate)."
    )


# ─── Platform tables are deliberately not isolated ───────────────────────


async def test_app_user_cannot_perform_ddl(app_engine: AsyncEngine) -> None:
    """🔒 DB §2.4 — ``app_user`` holds no DDL rights.

    A runtime role that can ``CREATE TABLE`` can also ``ALTER TABLE ... DISABLE
    ROW LEVEL SECURITY``, which makes every other assertion in this file
    contingent on the application never doing so. Withholding CREATE on the
    schema makes it a database-level fact instead.
    """
    async with app_engine.connect() as connection, connection.begin():
        with pytest.raises(DBAPIError) as exception_info:
            await connection.execute(text("CREATE TABLE rls_probe__delete_me (id int)"))

    assert "permission denied" in str(exception_info.value).lower(), (
        f"app_user was not denied DDL. Re-run ops/db/001_roles.sql. "
        f"Error was: {exception_info.value}"
    )


async def test_app_user_cannot_write_the_version_table(app_engine: AsyncEngine) -> None:
    """🔒 An application that can UPDATE ``alembic_version`` can convince the
    next deploy that a migration already ran.

    ``001_roles.sql`` grants CRUD on future tables by default privilege, and
    ``alembic_version`` is created like any other table — so the baseline
    revision revokes it explicitly. This is that revocation, verified.
    """
    async with app_engine.connect() as connection, connection.begin():
        with pytest.raises(DBAPIError) as exception_info:
            await connection.execute(text("UPDATE alembic_version SET version_num = 'nope'"))

    assert "permission denied" in str(exception_info.value).lower(), (
        f"app_user can write alembic_version. Revision 0001_baseline should have "
        f"revoked it. Error was: {exception_info.value}"
    )
