"""Audit immutability — DDR-15 / FR-M0-031.

🔒 **The application cannot alter what it has written.** That is the entire
value of the audit log, and it is a property of the *grants*, not of the code:
code is what the log exists to audit, so code cannot be what protects it.

`ops/db/001_roles.sql` sets DEFAULT PRIVILEGES granting all four verbs on new
tables to `app_user` — right for every other table, wrong for this one. Revision
`0003_audit_infrastructure` revokes UPDATE and DELETE. This file proves the
revoke actually took, against a live database, as the role the application
actually uses.

⚠️ The structural counterpart in `tests/test_kernel_schema.py` can only show
that the migration *contains* a REVOKE. Only a real connection can show that
`app_user` is refused, which is the claim being made.

Running these requires the same setup as the tenant-isolation gate — see
`tests/integration/test_tenant_isolation.py` for the full sequence.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine

#: Written by the migrator, then attacked as `app_user`. Seeding through the
#: application role would work — INSERT is granted — but doing it as the owner
#: keeps the fixture independent of the privilege under test, so a broken INSERT
#: grant fails its own test rather than silently emptying every other one.
_SEED_ACTION = "test.audit_immutability"


@pytest_asyncio.fixture
async def seeded_entry(migrator_engine: AsyncEngine) -> AsyncIterator[int]:
    """One audit row, removed afterwards by the owner.

    🔒 Cleanup runs on `migrator_engine` because `app_user` has no DELETE — the
    very property under test. If this fixture could tidy up through the
    application role, there would be nothing to assert.
    """
    request_id = f"req_{uuid.uuid4().hex}"

    async with migrator_engine.begin() as connection:
        entry_id = (
            await connection.execute(
                text(
                    "INSERT INTO audit_log "
                    "  (actor_type, action, resource_type, outcome, request_id) "
                    "VALUES ('system', :action, 'test', 'allowed', :request_id) "
                    "RETURNING id"
                ),
                {"action": _SEED_ACTION, "request_id": request_id},
            )
        ).scalar_one()

    try:
        yield int(entry_id)
    finally:
        async with migrator_engine.begin() as connection:
            await connection.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": entry_id})


# ─── What the application may do ─────────────────────────────────────────


async def test_the_application_can_append(app_engine: AsyncEngine) -> None:
    """INSERT is granted — the log would be useless otherwise. Rolled back
    rather than committed, so the row never needs deleting by a role that
    deliberately cannot delete."""
    async with app_engine.connect() as connection:
        result = await connection.execute(
            text(
                "INSERT INTO audit_log (actor_type, action, resource_type, outcome) "
                "VALUES ('system', :action, 'test', 'allowed') RETURNING id"
            ),
            {"action": _SEED_ACTION},
        )
        assert result.scalar_one() is not None
        await connection.rollback()


async def test_the_application_can_read(app_engine: AsyncEngine, seeded_entry: int) -> None:
    """SELECT is granted: support and DPDP enquiries are answered from this
    table. 🔒 No RLS applies (Pattern D) — the table is never reached on a
    tenant-facing path, and a policy keyed on `tenant_id` would reject exactly
    the operator and system rows that most need recording."""
    async with app_engine.connect() as connection:
        found = (
            await connection.execute(
                text("SELECT action FROM audit_log WHERE id = :id"), {"id": seeded_entry}
            )
        ).scalar_one()
    assert found == _SEED_ACTION


# ─── What it may not ─────────────────────────────────────────────────────


async def test_the_application_cannot_rewrite_an_entry(
    app_engine: AsyncEngine, seeded_entry: int
) -> None:
    """🔒 DDR-15. A log the application can edit is evidence of nothing.

    The failure is a privilege error from PostgreSQL, not an application check —
    there is no code path to forget, and no code path to bypass.
    """
    async with app_engine.connect() as connection:
        with pytest.raises((ProgrammingError, DBAPIError)) as raised:
            await connection.execute(
                text("UPDATE audit_log SET outcome = 'allowed' WHERE id = :id"),
                {"id": seeded_entry},
            )
        assert "permission denied" in str(raised.value).lower()


async def test_the_application_cannot_delete_an_entry(
    app_engine: AsyncEngine, seeded_entry: int
) -> None:
    """Deleting the record of an action is the action an audit log exists to
    make visible."""
    async with app_engine.connect() as connection:
        with pytest.raises((ProgrammingError, DBAPIError)) as raised:
            await connection.execute(
                text("DELETE FROM audit_log WHERE id = :id"), {"id": seeded_entry}
            )
        assert "permission denied" in str(raised.value).lower()


async def test_the_application_cannot_truncate_the_table(app_engine: AsyncEngine) -> None:
    """⚠️ TRUNCATE is not covered by the DELETE privilege — it requires table
    ownership or an explicit grant. Asserted separately because "we revoked
    DELETE" is the kind of claim that quietly excludes the one verb that removes
    every row at once."""
    async with app_engine.connect() as connection:
        with pytest.raises((ProgrammingError, DBAPIError)) as raised:
            await connection.execute(text("TRUNCATE TABLE audit_log"))
        assert "permission denied" in str(raised.value).lower()


# ─── The grant itself ────────────────────────────────────────────────────


@pytest.mark.parametrize("privilege", ["UPDATE", "DELETE"])
async def test_the_privilege_is_absent_not_merely_unused(
    migrator_engine: AsyncEngine, privilege: str
) -> None:
    """Asks PostgreSQL directly, rather than inferring from a failed statement.

    A statement can fail for many reasons; this distinguishes "the grant is
    gone" from "that query happened to error", which is the difference between
    a guarantee and a coincidence.
    """
    async with migrator_engine.connect() as connection:
        held = (
            await connection.execute(
                text("SELECT has_table_privilege('app_user', 'audit_log', :privilege)"),
                {"privilege": privilege},
            )
        ).scalar()
    assert held is False, (
        f"app_user still holds {privilege} on audit_log. The DEFAULT PRIVILEGES in "
        "ops/db/001_roles.sql grant all four verbs on new tables; revision "
        "0003_audit_infrastructure must revoke this one. Re-run `alembic upgrade head` "
        "against a database where the roles already exist — the revoke is guarded on "
        "`app_user` existing, so it is skipped silently on a single-role database."
    )


@pytest.mark.parametrize("privilege", ["INSERT", "SELECT"])
async def test_the_privileges_the_log_needs_are_present(
    migrator_engine: AsyncEngine, privilege: str
) -> None:
    """The counterpart assertion: over-revoking would make the log unwritable,
    and a log nothing can write to fails silently at exactly the wrong moment."""
    async with migrator_engine.connect() as connection:
        held = (
            await connection.execute(
                text("SELECT has_table_privilege('app_user', 'audit_log', :privilege)"),
                {"privilege": privilege},
            )
        ).scalar()
    assert held is True
