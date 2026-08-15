"""Pattern C, asserted against PostgreSQL itself — DB §17.1, migration 0025.

🔒 **These tests deliberately do not go through the application.** ``test_today``
proves the endpoint returns the right rows; that would still pass if the endpoint
were the *only* thing separating two clients. What is asserted here is the
stronger claim the security model actually rests on: with a client-realm scope
applied, a **bare ``SELECT *`` with no ``WHERE`` clause at all** returns only that
client's rows.

If Pattern C were ever dropped, weakened, or written against the wrong session
variable, every test in this file fails and the ones in ``test_today`` do not.

⚠️ Every connection here is ``app_engine`` — the ``app_user`` role, which
``tests/integration/conftest.py`` refuses to run against if it can bypass RLS.
Seeding goes through ``migrator_engine`` because a client-scoped connection
cannot, by construction, create another client's rows.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.platform.db import ACTOR_SETTING, ROLE_SETTING, TENANT_SETTING
from tests.integration.portal.conftest import PortalClient

pytestmark = pytest.mark.asyncio

#: Every table the portal can reach that Pattern C must confine, paired with the
#: column naming the row's owning client. The plan tree is checked separately
#: because it has no such column — that is the point of the EXISTS predicates.
_DIRECT_TABLES: tuple[tuple[str, str], ...] = (
    ("clients", "id"),
    ("diet_plans", "client_id"),
    ("measurements", "client_id"),
    ("assessment_responses", "client_id"),
    ("adherence_logs", "client_id"),
)

#: The plan tree, and how each row is traced back to a client for the assertion.
_PLAN_TREE: tuple[tuple[str, str], ...] = (
    ("diet_plan_versions", "SELECT id FROM diet_plan_versions"),
    ("plan_snapshots", "SELECT id FROM plan_snapshots"),
    ("plan_days", "SELECT id FROM plan_days"),
    ("plan_slots", "SELECT id FROM plan_slots"),
    ("plan_items", "SELECT id FROM plan_items"),
)


@asynccontextmanager
async def _as_client(engine: AsyncEngine, fixture: PortalClient) -> AsyncIterator[AsyncConnection]:
    """A connection scoped exactly as a ``/portal`` request scopes one.

    🔒 All three variables, because Pattern C reads two of them. Setting only the
    tenant would leave ``portal_client_id()`` NULL, the restrictive policies would
    short-circuit to true, and this file would assert nothing at all.
    """
    async with engine.begin() as connection:
        for setting, value in (
            (TENANT_SETTING, str(fixture.tenant_id)),
            (ACTOR_SETTING, str(fixture.client_id)),
            (ROLE_SETTING, "client"),
        ):
            await connection.execute(
                text(f"SELECT set_config('{setting}', :v, true)"), {"v": value}
            )
        yield connection


@asynccontextmanager
async def _as_practitioner(
    engine: AsyncEngine, fixture: PortalClient
) -> AsyncIterator[AsyncConnection]:
    """The same tenant, from the practitioner realm.

    🔒 The control case. Pattern C must **not** narrow this connection: a policy
    that compared ``client_id`` to ``app.actor_id`` unconditionally would hide
    every client from every practitioner, and the portal tests alone would never
    notice.
    """
    async with engine.begin() as connection:
        for setting, value in (
            (TENANT_SETTING, str(fixture.tenant_id)),
            (ACTOR_SETTING, str(fixture.owner_user_id)),
            (ROLE_SETTING, "owner"),
        ):
            await connection.execute(
                text(f"SELECT set_config('{setting}', :v, true)"), {"v": value}
            )
        yield connection


async def _ids(connection: AsyncConnection, statement: str) -> set[uuid.UUID]:
    return {row[0] for row in (await connection.execute(text(statement))).all()}


# ─── The client boundary ─────────────────────────────────────────────────


@pytest.mark.isolation
async def test_an_unfiltered_select_returns_only_the_acting_client(
    app_engine: AsyncEngine,
    client_a: PortalClient,
    client_a2: PortalClient,
) -> None:
    """🔒 The whole claim, in one assertion per table.

    No ``WHERE`` clause is supplied. Whatever comes back came back because the
    database decided it should, which is what "do not rely on application
    filtering" means in practice.
    """
    async with _as_client(app_engine, client_a) as connection:
        for table, column in _DIRECT_TABLES:
            visible = await _ids(connection, f"SELECT {column} FROM {table}")
            assert visible <= {client_a.client_id}, (
                f"{table} leaked rows across the client boundary: a client-realm "
                f"session saw {visible - {client_a.client_id}}"
            )
            assert client_a2.client_id not in visible


@pytest.mark.isolation
async def test_the_plan_tree_is_confined_to_the_acting_clients_plan(
    app_engine: AsyncEngine,
    client_a: PortalClient,
    client_a2: PortalClient,
) -> None:
    """🔒 The EXISTS half. These tables carry no ``client_id`` at all."""
    async with _as_client(app_engine, client_a) as connection:
        versions = await _ids(connection, "SELECT id FROM diet_plan_versions")
        assert versions == {client_a.version_id}
        assert client_a2.version_id not in versions

        slots = await _ids(connection, "SELECT id FROM plan_slots")
        assert slots == {client_a.slot_id}

        for table, statement in _PLAN_TREE:
            rows = await _ids(connection, statement)
            assert len(rows) <= 1, f"{table} returned another client's rows: {rows}"


@pytest.mark.isolation
async def test_a_client_cannot_read_across_the_tenant_boundary_either(
    app_engine: AsyncEngine, client_a: PortalClient, client_b: PortalClient
) -> None:
    """Pattern A still applies — the restrictive policy is *additional*, not a
    replacement. Both predicates must hold."""
    async with _as_client(app_engine, client_a) as connection:
        clients = await _ids(connection, "SELECT id FROM clients")
        assert client_b.client_id not in clients

        versions = await _ids(connection, "SELECT id FROM diet_plan_versions")
        assert client_b.version_id not in versions


@pytest.mark.isolation
async def test_a_client_cannot_write_a_row_attributed_to_someone_else(
    app_engine: AsyncEngine, client_a: PortalClient, client_a2: PortalClient
) -> None:
    """🔒 The ``WITH CHECK`` half.

    ⚠️ ``USING`` filters what is *read*; without ``WITH CHECK`` a client could
    insert a measurement against their neighbour — invisible to themselves
    afterwards, but present in the other client's clinical record. The portal's
    write endpoints land in later steps and inherit this rather than restating it.
    """
    insert = text(
        "INSERT INTO measurements (tenant_id, client_id, measured_on, weight_kg, source) "
        "VALUES (:t, :c, current_date, 70, 'client')"
    )

    async with _as_client(app_engine, client_a) as connection:
        with pytest.raises(Exception) as refused:
            await connection.execute(insert, {"t": client_a.tenant_id, "c": client_a2.client_id})
        assert "row-level security" in str(refused.value).lower()


# ─── The control case ────────────────────────────────────────────────────


@pytest.mark.isolation
async def test_a_practitioner_still_sees_every_client_in_their_tenant(
    app_engine: AsyncEngine, client_a: PortalClient, client_a2: PortalClient
) -> None:
    """🔒 Pattern C must be invisible outside the client realm.

    ⚠️ This is the test that fails if ``portal_client_id()`` ever stops keying on
    ``app.actor_role``. The failure mode it guards against — every practitioner
    losing sight of every client — is severe, and it would not show up in a
    portal test, which is exactly why it is asserted here.
    """
    async with _as_practitioner(app_engine, client_a) as connection:
        clients = await _ids(connection, "SELECT id FROM clients")
        assert {client_a.client_id, client_a2.client_id} <= clients

        versions = await _ids(connection, "SELECT id FROM diet_plan_versions")
        assert {client_a.version_id, client_a2.version_id} <= versions
