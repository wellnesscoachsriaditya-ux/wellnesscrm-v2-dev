"""``/portal/sync`` is a client-facing **write** endpoint for health data.

🔒 ``/portal/today`` could only ever leak. This one can *forge*: an operation
that landed on the wrong client would put a weight in a stranger's clinical
record, and no later correction removes the fact that it was there. So the
boundary is asserted from both sides, in the style migration ``0025``'s suite
established:

* **through the application**, proving the endpoint attributes writes correctly;
* **against PostgreSQL directly**, proving that even if it did not, the row
  would be refused — application filtering is never the only mechanism.

⚠️ The Pattern C ``WITH CHECK`` half is what these tests are mostly about.
``USING`` decides what a client can *read*; ``WITH CHECK`` decides what they can
*write*, and only the second one stands between a malicious payload and someone
else's record.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.platform.db import ACTOR_SETTING, ROLE_SETTING, TENANT_SETTING
from tests.integration.portal.conftest import (
    SLOT_TYPE,
    PortalApi,
    PortalClient,
    adherence_rows,
    anonymous_actor,
    measurement_rows,
    operator_actor,
    practitioner_actor,
)
from tests.integration.portal.test_sync import (
    _server_today,
    _status,
    _sync,
    adherence_op,
    measurement_op,
)

pytestmark = pytest.mark.asyncio

SYNC = "/api/v1/portal/sync"


# ─── 1 & 2 — a client syncs only their own data ──────────────────────────


async def test_a_client_syncs_only_their_own_data(
    portal_api: PortalApi,
    client_a: PortalClient,
    client_a2: PortalClient,
    migrator_engine: AsyncEngine,
) -> None:
    """🔒 Two clients of one practitioner, syncing in turn.

    The tenant predicate is satisfied for both, so nothing Pattern A does keeps
    their rows apart. What keeps them apart is ``client_id = portal_client_id()``.
    """
    today = await _server_today(portal_api, client_a)

    await _sync(portal_api, client_a, [adherence_op(logged_for_date=today)])
    await _sync(portal_api, client_a2, [adherence_op(logged_for_date=today)])

    for fixture in (client_a, client_a2):
        rows = await adherence_rows(migrator_engine, fixture=fixture)
        assert len(rows) == 1
        assert rows[0]["client_id"] == fixture.client_id


async def test_a_clients_sync_never_touches_a_neighbours_record(
    portal_api: PortalApi,
    client_a: PortalClient,
    client_a2: PortalClient,
    migrator_engine: AsyncEngine,
) -> None:
    """🔒 Requirement 2 — client A cannot create data for client B."""
    today = await _server_today(portal_api, client_a)

    await _sync(
        portal_api,
        client_a,
        [adherence_op(logged_for_date=today), measurement_op(measured_on=today)],
    )

    assert await adherence_rows(migrator_engine, fixture=client_a2) == []
    assert await measurement_rows(migrator_engine, fixture=client_a2) == []


# ─── 3 — tenants ─────────────────────────────────────────────────────────


async def test_one_tenants_sync_cannot_reach_another(
    portal_api: PortalApi,
    client_a: PortalClient,
    client_b: PortalClient,
    migrator_engine: AsyncEngine,
) -> None:
    """🔒 Requirement 3 — Pattern A, re-asserted on the write path."""
    today = await _server_today(portal_api, client_a)

    await _sync(portal_api, client_a, [adherence_op(logged_for_date=today)])

    assert await adherence_rows(migrator_engine, fixture=client_b) == []


# ─── 4 — a payload cannot name its own authority ─────────────────────────


@pytest.mark.parametrize("field", ["client_id", "tenant_id"])
async def test_an_authority_field_in_the_payload_is_refused_not_ignored(
    portal_api: PortalApi,
    client_a: PortalClient,
    client_a2: PortalClient,
    field: str,
    migrator_engine: AsyncEngine,
) -> None:
    """🔒 Requirement 4 — and refused *loudly*.

    ⚠️ The operation is **rejected**, not silently applied to the authenticated
    client. Quietly ignoring an authority field teaches a client that sending one
    is harmless, and the next person to read this code has to re-prove that it
    still is. ``model_config = {"extra": "forbid"}`` is what makes the refusal
    structural rather than a check someone remembered to write.
    """
    today = await _server_today(portal_api, client_a)
    operation = adherence_op(logged_for_date=today)
    operation["payload"][field] = str(
        client_a2.client_id if field == "client_id" else client_a2.tenant_id
    )

    body = await _sync(portal_api, client_a, [operation])

    assert _status(body, operation["op_id"]) == "rejected"
    assert await adherence_rows(migrator_engine, fixture=client_a) == []
    assert await adherence_rows(migrator_engine, fixture=client_a2) == []


async def test_a_foreign_slot_id_does_not_attach_to_the_log(
    portal_api: PortalApi,
    client_a: PortalClient,
    client_a2: PortalClient,
    migrator_engine: AsyncEngine,
) -> None:
    """🔒 A slot id from a neighbour's plan.

    ⚠️ The log still **applies** — ``slot_type`` is what survives plan revision
    (API §12.3), and a client who ate breakfast has eaten breakfast whatever
    happened to the slot row. What must not happen is the foreign id being
    stored: ``plan_slots`` carries a client-realm policy, so the lookup finds
    nothing and the reference degrades to ``NULL``.
    """
    today = await _server_today(portal_api, client_a)
    operation = adherence_op(logged_for_date=today, slot_id=client_a2.slot_id)

    body = await _sync(portal_api, client_a, [operation])

    assert _status(body, operation["op_id"]) == "applied"
    row = (await adherence_rows(migrator_engine, fixture=client_a))[0]
    assert row["plan_slot_id"] is None


async def test_a_log_is_attributed_to_the_clients_own_plan_version(
    portal_api: PortalApi,
    client_a: PortalClient,
    client_a2: PortalClient,
    migrator_engine: AsyncEngine,
) -> None:
    """🔒 ``plan_version_id`` is resolved server-side and is never in the payload,
    so a client cannot attribute their log to a plan they were never issued."""
    today = await _server_today(portal_api, client_a)

    await _sync(portal_api, client_a, [adherence_op(logged_for_date=today)])

    row = (await adherence_rows(migrator_engine, fixture=client_a))[0]
    assert row["plan_version_id"] == client_a.version_id
    assert row["plan_version_id"] != client_a2.version_id


# ─── 5 — idempotency keys do not cross the client boundary ───────────────


async def test_the_same_op_id_from_two_clients_applies_to_both(
    portal_api: PortalApi,
    client_a: PortalClient,
    client_a2: PortalClient,
    migrator_engine: AsyncEngine,
) -> None:
    """🔒 Requirement 5, and the reason both uniqueness boundaries are scoped to
    the client.

    Two devices generate op_ids with no knowledge of each other, so a collision
    is expected rather than suspicious. A *global* unique key would make one
    client's sync silently suppress another's log — data loss wearing a
    deduplication feature's clothes, which is the argument migration ``0024``
    makes for measurements and DB §12.1 makes for adherence.
    """
    today = await _server_today(portal_api, client_a)
    shared = uuid.uuid4()

    first = await _sync(portal_api, client_a, [adherence_op(op_id=shared, logged_for_date=today)])
    second = await _sync(portal_api, client_a2, [adherence_op(op_id=shared, logged_for_date=today)])

    assert _status(first, str(shared)) == "applied"
    # 🔒 `applied`, not `duplicate` — the neighbour's key is not this client's.
    assert _status(second, str(shared)) == "applied"

    for fixture in (client_a, client_a2):
        rows = await adherence_rows(migrator_engine, fixture=fixture)
        assert len(rows) == 1
        assert rows[0]["idempotency_key"] == str(shared)


async def test_a_replay_cannot_suppress_another_clients_measurement(
    portal_api: PortalApi,
    client_a: PortalClient,
    client_a2: PortalClient,
    migrator_engine: AsyncEngine,
) -> None:
    """🔒 The same claim for the partial unique index added in ef3c636."""
    today = await _server_today(portal_api, client_a)
    shared = uuid.uuid4()

    await _sync(portal_api, client_a, [measurement_op(op_id=shared, measured_on=today)])
    body = await _sync(portal_api, client_a2, [measurement_op(op_id=shared, measured_on=today)])

    assert _status(body, str(shared)) == "applied"
    assert len(await measurement_rows(migrator_engine, fixture=client_a)) == 1
    assert len(await measurement_rows(migrator_engine, fixture=client_a2)) == 1


# ─── 6 & 7 — the database refuses, not just the application ──────────────


async def _as_client(connection: AsyncConnection, fixture: PortalClient) -> None:
    """Scope a raw connection exactly as a ``/portal`` request scopes one."""
    for setting, value in (
        (TENANT_SETTING, str(fixture.tenant_id)),
        (ACTOR_SETTING, str(fixture.client_id)),
        (ROLE_SETTING, "client"),
    ):
        await connection.execute(text(f"SELECT set_config('{setting}', :v, true)"), {"v": value})


@pytest.mark.isolation
@pytest.mark.parametrize(
    ("table", "columns", "values"),
    [
        (
            "adherence_logs",
            "tenant_id, client_id, logged_for_date, slot_type, adherence, "
            "idempotency_key, client_timestamp",
            f"(:t, :c, current_date, '{SLOT_TYPE}', 'followed', :k, now())",
        ),
        (
            "measurements",
            "tenant_id, client_id, measured_on, weight_kg, source, idempotency_key",
            "(:t, :c, current_date, 70, 'client', :k)",
        ),
    ],
)
async def test_the_database_refuses_a_write_for_another_client(
    app_engine: AsyncEngine,
    client_a: PortalClient,
    client_a2: PortalClient,
    table: str,
    columns: str,
    values: str,
) -> None:
    """🔒 Requirements 6 and 7 — **the application is not in this test.**

    A raw ``INSERT`` naming the neighbour's ``client_id``, issued on a connection
    scoped as client A, with no service and no router involved. The Pattern C
    ``WITH CHECK`` refuses it. This is what "do not rely on application
    filtering" means: the endpoint above could be rewritten badly tomorrow and
    this row would still not be storable.
    """
    async with app_engine.begin() as connection:
        await _as_client(connection, client_a)
        with pytest.raises(Exception) as refused:
            await connection.execute(
                text(f"INSERT INTO {table} ({columns}) VALUES {values}"),
                {"t": client_a.tenant_id, "c": client_a2.client_id, "k": str(uuid.uuid4())},
            )
    assert "row-level security" in str(refused.value).lower()


@pytest.mark.isolation
async def test_the_database_refuses_a_write_for_another_tenant(
    app_engine: AsyncEngine, client_a: PortalClient, client_b: PortalClient
) -> None:
    """🔒 Both predicates hold, and both are needed — the restrictive client
    policy is *additional* to Pattern A, never a replacement for it."""
    async with app_engine.begin() as connection:
        await _as_client(connection, client_a)
        with pytest.raises(Exception) as refused:
            await connection.execute(
                text(
                    "INSERT INTO adherence_logs "
                    "(tenant_id, client_id, logged_for_date, slot_type, adherence, "
                    " idempotency_key, client_timestamp) "
                    f"VALUES (:t, :c, current_date, '{SLOT_TYPE}', 'followed', :k, now())"
                ),
                {"t": client_b.tenant_id, "c": client_b.client_id, "k": str(uuid.uuid4())},
            )
    assert "row-level security" in str(refused.value).lower()


@pytest.mark.isolation
async def test_a_client_cannot_read_a_neighbours_synced_logs(
    portal_api: PortalApi,
    client_a: PortalClient,
    client_a2: PortalClient,
    app_engine: AsyncEngine,
) -> None:
    """🔒 The read half, on rows the endpoint itself created.

    An unfiltered ``SELECT`` — no ``WHERE`` clause is supplied, so whatever comes
    back came back because the database decided it should.
    """
    today = await _server_today(portal_api, client_a)
    await _sync(portal_api, client_a, [adherence_op(logged_for_date=today)])
    await _sync(portal_api, client_a2, [adherence_op(logged_for_date=today)])

    async with app_engine.begin() as connection:
        await _as_client(connection, client_a)
        visible = {
            row[0]
            for row in (
                await connection.execute(text("SELECT client_id FROM adherence_logs"))
            ).all()
        }

    assert visible == {client_a.client_id}


# ─── 8 & 9 — who may reach the endpoint at all ───────────────────────────


async def test_an_anonymous_caller_writes_nothing(
    portal_api: PortalApi, client_a: PortalClient, migrator_engine: AsyncEngine
) -> None:
    """🔒 Requirement 8 — deny by default, and no side effect on the way out."""
    today = await _server_today(portal_api, client_a)
    # ⚠️ Explicitly, because `_server_today` had to sign in as the client to ask.
    # Without this the "anonymous" request is the client's and the assertion
    # passes only by accident of ordering.
    portal_api.as_actor(anonymous_actor())

    response = await portal_api.http.post(
        SYNC, json={"operations": [adherence_op(logged_for_date=today)]}
    )

    assert response.status_code == 401
    assert await adherence_rows(migrator_engine, fixture=client_a) == []


async def test_a_practitioner_cannot_become_a_client_by_calling_the_portal(
    portal_api: PortalApi, client_a: PortalClient, migrator_engine: AsyncEngine
) -> None:
    """🔒 Requirement 9 — the realm check runs *before* the transaction opens.

    ⚠️ The practitioner here owns this very client, so an authorization model
    that asked only "may you touch this client?" would say yes. The realm rule is
    what says no: a practitioner-realm token on ``/portal`` is refused whatever
    it would otherwise have been allowed to do, and no row is written.
    """
    today = await _server_today(portal_api, client_a)
    portal_api.as_actor(practitioner_actor(client_a))

    response = await portal_api.http.post(
        SYNC, json={"operations": [adherence_op(logged_for_date=today)]}
    )

    assert response.status_code == 403
    assert await adherence_rows(migrator_engine, fixture=client_a) == []


async def test_an_operator_cannot_write_client_health_data(
    portal_api: PortalApi, client_a: PortalClient, migrator_engine: AsyncEngine
) -> None:
    """🔒 FR-M11-003 — running the platform is not a basis for writing a
    client's clinical record. ``portal.sync`` is ``TENANT_PII``, which
    ``register_action`` refuses to combine with operator access at import time;
    this proves the route refuses as well."""
    today = await _server_today(portal_api, client_a)
    portal_api.as_actor(operator_actor())

    response = await portal_api.http.post(
        SYNC, json={"operations": [adherence_op(logged_for_date=today)]}
    )

    assert response.status_code == 403
    assert await adherence_rows(migrator_engine, fixture=client_a) == []
