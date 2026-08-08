"""The client spine, against a live PostgreSQL — DB §5.1–5.3.

🔒 **Everything asserted here is invisible to a unit test**, which is why the
file exists. Three different kinds of guarantee, none of them enforceable by
application code:

* **Grants.** ``client_stage_history`` is append-only and ``clients`` cannot be
  DELETEd. No amount of care in the ``clients`` module would stop a future caller
  issuing the statement — what must be proven is that the *privilege* is absent.
* **RLS.** Tenant isolation on both tables (AC-M0-003). A test with a fake
  session agrees with whatever the code believed, including when wrong.
* **Constraints and generated columns.** The contact rule, the E.164 shape, the
  activation invariant and the ``search_vector`` are the database's, and a client
  that vanishes from search because one column was NULL is a bug no mock reveals.

It also covers the service functions themselves — ``create_client`` writes two
rows or neither, and ``update_client``'s optimistic concurrency depends on the
``updated_at`` PostgreSQL actually stores. Both need a real session.

⚠️ Needs the same setup as the tenant-isolation gate — see
``tests/integration/test_tenant_isolation.py`` for the provisioning sequence.
These fail rather than skip in CI, where ``REQUIRE_LIVE_DATABASE`` is set.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, async_sessionmaker

from app.kernel.clients import ClientStage, DietaryClass
from app.kernel.errors import ConflictError, NotFoundError
from app.modules.clients import (
    UNSET,
    ClientCreate,
    ClientRepositoryDirectory,
    ClientUpdate,
    create_client,
    get_client,
    update_client,
)
from tests.integration.conftest import scope_to

pytestmark = pytest.mark.asyncio


async def _owner_of(connection: AsyncConnection, tenant_id: uuid.UUID) -> uuid.UUID:
    """The seeded practitioner for a tenant — clients need an owner (FR-M1-009)."""
    row = (
        await connection.execute(
            text("SELECT id FROM users WHERE tenant_id = :t LIMIT 1"), {"t": tenant_id}
        )
    ).first()
    assert row is not None, "seeded_tenants did not create a user for this tenant"
    return uuid.UUID(str(row.id))


async def _insert_client(
    connection: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    full_name: str = "Asha Menon",
    mobile: str | None = "+919876543210",
    email: str | None = None,
    stage: str = "lead",
    archived: bool = False,
) -> uuid.UUID:
    """Seed one client directly, bypassing the service.

    Used where the *database* is under test rather than the module: a row the
    service refused to create could never exercise the constraint that would
    have caught it.
    """
    owner = await _owner_of(connection, tenant_id)
    row = (
        await connection.execute(
            text(
                "INSERT INTO clients "
                "  (tenant_id, full_name, mobile, email, stage, owner_user_id, "
                "   activated_at, archived_at) "
                "VALUES (:t, :name, :mobile, :email, CAST(:stage AS client_stage), :owner, "
                "        CASE WHEN :stage = 'active' THEN now() ELSE NULL END, "
                "        CASE WHEN :archived THEN now() ELSE NULL END) "
                "RETURNING id"
            ),
            {
                "t": tenant_id,
                "name": full_name,
                "mobile": mobile,
                "email": email,
                "stage": stage,
                "owner": owner,
                "archived": archived,
            },
        )
    ).one()
    return uuid.UUID(str(row.id))


@pytest_asyncio.fixture
async def seeded_client(
    migrator_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """One client in tenant A, with its opening history row. Cleaned up after.

    Seeded as ``app_migrator`` so the test body's ``app_user`` connection is
    exercising reads and writes it did not itself create — a row inserted by the
    same connection under the same scope would prove less.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        client_id = await _insert_client(connection, tenant_a)
        await connection.execute(
            text(
                "INSERT INTO client_stage_history (tenant_id, client_id, to_stage) "
                "VALUES (:t, :c, 'lead')"
            ),
            {"t": tenant_a, "c": client_id},
        )

    try:
        yield tenant_a, client_id
    finally:
        # ⚠️ As the migrator, and under scope: an unscoped DELETE removes zero
        # rows and returns quietly, leaving the row to collide with a later run.
        async with migrator_engine.begin() as connection:
            await scope_to(connection, tenant_a)
            await connection.execute(
                text("DELETE FROM client_stage_history WHERE client_id = :c"), {"c": client_id}
            )
            await connection.execute(text("DELETE FROM clients WHERE id = :c"), {"c": client_id})


# ─── Append-only history (FR-M1-015) ─────────────────────────────────────


async def test_the_application_cannot_update_stage_history(
    app_engine: AsyncEngine, seeded_client: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """🔒 FR-M1-015 — every transition recorded, and none revised.

    The failure is a privilege error from PostgreSQL, not an application check:
    there is no code path to forget and none to bypass. A rewritable history
    makes the conversion metrics (FR-M9-006) unfalsifiable.
    """
    tenant_id, client_id = seeded_client
    async with app_engine.connect() as connection:
        await scope_to(connection, tenant_id)
        with pytest.raises((ProgrammingError, DBAPIError)) as raised:
            await connection.execute(
                text("UPDATE client_stage_history SET to_stage = 'active' WHERE client_id = :c"),
                {"c": client_id},
            )
        assert "permission denied" in str(raised.value).lower()


async def test_the_application_cannot_delete_stage_history(
    app_engine: AsyncEngine, seeded_client: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Deleting a transition is the edit the append-only rule exists to stop."""
    tenant_id, client_id = seeded_client
    async with app_engine.connect() as connection:
        await scope_to(connection, tenant_id)
        with pytest.raises((ProgrammingError, DBAPIError)) as raised:
            await connection.execute(
                text("DELETE FROM client_stage_history WHERE client_id = :c"), {"c": client_id}
            )
        assert "permission denied" in str(raised.value).lower()


async def test_the_application_cannot_truncate_stage_history(app_engine: AsyncEngine) -> None:
    """⚠️ TRUNCATE is not covered by the DELETE privilege — it needs ownership or
    an explicit grant. Asserted separately because "we revoked DELETE" is the
    kind of claim that quietly excludes the one verb removing every row at once.
    """
    async with app_engine.connect() as connection:
        with pytest.raises((ProgrammingError, DBAPIError)) as raised:
            await connection.execute(text("TRUNCATE TABLE client_stage_history"))
        assert "permission denied" in str(raised.value).lower()


async def test_the_application_cannot_delete_a_client(
    app_engine: AsyncEngine, seeded_client: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """🔒 FR-M1-010 — soft delete only. Hard deletion is the DPDP erasure
    pathway (FR-M1-011), which runs as the migrator role."""
    tenant_id, client_id = seeded_client
    async with app_engine.connect() as connection:
        await scope_to(connection, tenant_id)
        with pytest.raises((ProgrammingError, DBAPIError)) as raised:
            await connection.execute(text("DELETE FROM clients WHERE id = :c"), {"c": client_id})
        assert "permission denied" in str(raised.value).lower()


@pytest.mark.parametrize("privilege", ["UPDATE", "DELETE"])
async def test_history_privilege_is_absent_not_merely_unused(
    migrator_engine: AsyncEngine, privilege: str
) -> None:
    """The catalogue's own answer, independent of any statement succeeding.

    A test that only asserts an error could pass for the wrong reason — a
    constraint, a missing row, a typo in the SQL. This asks PostgreSQL directly.
    """
    async with migrator_engine.connect() as connection:
        held = (
            await connection.execute(
                text("SELECT has_table_privilege('app_user', 'client_stage_history', :p)"),
                {"p": privilege},
            )
        ).scalar()
        assert held is False, (
            f"app_user holds {privilege} on client_stage_history. FR-M1-015 "
            "requires the transition log to be append-only."
        )


async def test_clients_keep_update_so_archiving_still_works(
    migrator_engine: AsyncEngine,
) -> None:
    """🔒 The other half. Archiving *is* an UPDATE — a revoke that took it would
    make FR-M1-010's soft delete impossible, which no test of DELETE would show.
    """
    async with migrator_engine.connect() as connection:
        assert (
            await connection.execute(
                text("SELECT has_table_privilege('app_user', 'clients', 'UPDATE')")
            )
        ).scalar() is True
        assert (
            await connection.execute(
                text("SELECT has_table_privilege('app_user', 'clients', 'DELETE')")
            )
        ).scalar() is False


# ─── Tenant isolation (AC-M0-003) ────────────────────────────────────────


async def test_a_client_is_invisible_to_another_tenant(
    app_engine: AsyncEngine, seeded_client: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """🔒 AC-M0-003 on the table that holds the practice's whole client base.

    The RLS policy, not a WHERE clause — the query below has no tenant filter of
    its own, which is exactly the query a future careless caller writes.
    """
    tenant_a, client_id = seeded_client
    async with app_engine.connect() as connection:
        await scope_to(connection, uuid.uuid4())  # some other tenant
        found = (
            await connection.execute(
                text("SELECT count(*) FROM clients WHERE id = :c"), {"c": client_id}
            )
        ).scalar()
        assert found == 0, "a client leaked across the tenant boundary"

        await scope_to(connection, tenant_a)
        assert (
            await connection.execute(
                text("SELECT count(*) FROM clients WHERE id = :c"), {"c": client_id}
            )
        ).scalar() == 1, "the client is invisible to its own tenant — the policy is too strict"


async def test_writing_a_client_for_another_tenant_is_rejected(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
) -> None:
    """🔒 The ``WITH CHECK`` half. A policy with only ``USING`` lets a caller
    insert across the boundary while being unable to read it back — a silent,
    one-way leak that looks like nothing at all from the writing side.

    ⚠️ Tenant B's owner id is fetched as the migrator, under B's own scope. Doing
    it on the scoped ``app_user`` connection would return nothing — RLS hides the
    user row — and the test would then fail while *reading*, never reaching the
    INSERT it exists to assert on.
    """
    tenant_a, tenant_b = seeded_tenants[0], seeded_tenants[1]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_b)
        foreign_owner = await _owner_of(connection, tenant_b)

    async with app_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        with pytest.raises((ProgrammingError, DBAPIError)) as raised:
            await connection.execute(
                text(
                    "INSERT INTO clients (tenant_id, full_name, mobile, owner_user_id) "
                    "VALUES (:t, 'Cross Tenant', '+919876543210', :o)"
                ),
                {"t": tenant_b, "o": foreign_owner},
            )
        assert "row-level security" in str(raised.value).lower(), (
            "the insert failed for some reason other than the RLS policy; the "
            "WITH CHECK half is unproven."
        )


async def test_stage_history_is_isolated_too(
    app_engine: AsyncEngine, seeded_client: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The history is as sensitive as the client: it names when somebody became
    a paying client of a named practice."""
    _, client_id = seeded_client
    async with app_engine.connect() as connection:
        await scope_to(connection, uuid.uuid4())
        assert (
            await connection.execute(
                text("SELECT count(*) FROM client_stage_history WHERE client_id = :c"),
                {"c": client_id},
            )
        ).scalar() == 0


# ─── Constraints the database owns ───────────────────────────────────────


async def test_a_client_with_no_contact_method_is_refused(
    migrator_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 ``ck_clients__contact_present`` — FR-M1-004 / EC-M1-08.

    ``kernel.clients`` checks this early enough to name the field; the constraint
    catches anything arriving by another path, which is what a future import job
    (FR-M1-013) will be.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        with pytest.raises((IntegrityError, DBAPIError)) as raised:
            await _insert_client(connection, tenant_a, mobile=None, email=None)
        assert "ck_clients__contact_present" in str(raised.value)


async def test_a_non_e164_mobile_is_refused(
    migrator_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 ``ck_clients__mobile_e164`` — NFR-100. Shape only; the Indian numbering
    rule lives in ``normalise_mobile`` so widening the market is not a migration.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        with pytest.raises((IntegrityError, DBAPIError)) as raised:
            await _insert_client(connection, tenant_a, mobile="9876543210")
        assert "ck_clients__mobile_e164" in str(raised.value)


async def test_an_active_client_must_have_an_activation_time(
    migrator_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 ``ck_clients__active_has_activated_at`` — FR-M8-023's check-in anchor.

    Without it the scheduler has no day to count from and silently skips the
    client, which presents as "check-ins are not going out for some people".
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        owner = await _owner_of(connection, tenant_a)
        with pytest.raises((IntegrityError, DBAPIError)) as raised:
            await connection.execute(
                text(
                    "INSERT INTO clients (tenant_id, full_name, mobile, stage, owner_user_id) "
                    "VALUES (:t, 'No Anchor', '+919876543210', 'active', :o)"
                ),
                {"t": tenant_a, "o": owner},
            )
        assert "ck_clients__active_has_activated_at" in str(raised.value)


async def test_a_transition_to_the_same_stage_is_refused(
    migrator_engine: AsyncEngine, seeded_client: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """``ck_client_stage_history__actual_transition`` — a transition that goes
    nowhere is not a transition, and would double-count in FR-M9-006."""
    tenant_id, client_id = seeded_client
    async with migrator_engine.begin() as connection:
        await scope_to(connection, tenant_id)
        with pytest.raises((IntegrityError, DBAPIError)) as raised:
            await connection.execute(
                text(
                    "INSERT INTO client_stage_history "
                    "  (tenant_id, client_id, from_stage, to_stage) "
                    "VALUES (:t, :c, 'lead', 'lead')"
                ),
                {"t": tenant_id, "c": client_id},
            )
        assert "ck_client_stage_history__actual_transition" in str(raised.value)


async def test_two_clients_may_share_a_mobile(
    migrator_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 EC-M1-01 — a mother and daughter on one handset are two real clients.

    Proven by inserting, not by reading the schema: a unique index added later
    would fail here and nowhere else in the suite.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        first = await _insert_client(connection, tenant_a, full_name="Asha Menon")
        second = await _insert_client(connection, tenant_a, full_name="Priya Menon")
        assert first != second
        await connection.execute(
            text("DELETE FROM clients WHERE id = ANY(:ids)"), {"ids": [first, second]}
        )


# ─── The generated search vector (NFR-005, FR-M1-021) ────────────────────


async def test_search_vector_is_populated_and_survives_a_null_email(
    migrator_engine: AsyncEngine, seeded_client: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """⚠️ The ``coalesce`` bug, made visible.

    ``to_tsvector`` of NULL is NULL and a NULL tsvector matches nothing — so
    without coalescing, this client (no email) would silently vanish from search
    entirely. That reads as "search is flaky" for months rather than as a bug.
    """
    tenant_id, client_id = seeded_client
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_id)
        vector = (
            await connection.execute(
                text("SELECT search_vector::text FROM clients WHERE id = :c"), {"c": client_id}
            )
        ).scalar()
        assert vector, "search_vector is NULL for a client with no email"
        assert "asha" in str(vector).lower()

        matched = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM clients "
                    "WHERE id = :c AND search_vector @@ to_tsquery('simple', 'asha')"
                ),
                {"c": client_id},
            )
        ).scalar()
        assert matched == 1, "the client is not findable by name through the GIN index"


async def test_search_vector_tracks_an_update(
    migrator_engine: AsyncEngine, seeded_client: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """``GENERATED ALWAYS`` rather than a trigger — there is no way to write a
    row that bypasses it, so the projection cannot drift from its source."""
    tenant_id, client_id = seeded_client
    async with migrator_engine.begin() as connection:
        await scope_to(connection, tenant_id)
        await connection.execute(
            text("UPDATE clients SET full_name = 'Renamed Person' WHERE id = :c"),
            {"c": client_id},
        )
        vector = (
            await connection.execute(
                text("SELECT search_vector::text FROM clients WHERE id = :c"), {"c": client_id}
            )
        ).scalar()
        assert "renamed" in str(vector).lower()


# ─── The service (FR-M1-004..010) ────────────────────────────────────────


async def test_create_writes_the_client_and_its_opening_history(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], migrator_engine: AsyncEngine
) -> None:
    """🔒 Both rows, or neither — FR-M1-015.

    A client with no opening history row has a lifecycle that appears to start
    from nothing, and the conversion metrics would silently undercount.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner = await _owner_of(connection, tenant_a)

    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)
    async with sessions() as session:
        await scope_to(await session.connection(), tenant_a)
        created = await create_client(
            session,
            tenant_id=tenant_a,
            payload=ClientCreate(
                full_name="  Asha Menon  ",
                owner_user_id=owner,
                mobile="98765 43210",  # ⚠️ typed form — must be normalised
                dietary_class=DietaryClass.JAIN,
            ),
            actor_user_id=owner,
        )
        assert created.full_name == "Asha Menon", "the name was not trimmed"
        assert created.mobile == "+919876543210", "the mobile was stored unnormalised"
        assert created.stage is ClientStage.LEAD
        assert created.activated_at is None, "a lead must not carry an activation date"

        history = (
            await (await session.connection()).execute(
                text(
                    "SELECT from_stage, to_stage FROM client_stage_history " "WHERE client_id = :c"
                ),
                {"c": created.id},
            )
        ).all()
        assert len(history) == 1, "creation did not write exactly one history row"
        assert history[0].from_stage is None, "the opening row must have no from_stage"
        assert history[0].to_stage == "lead"

        await session.rollback()


async def test_create_at_active_sets_the_activation_anchor(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], migrator_engine: AsyncEngine
) -> None:
    """FR-M8-023 — an active client has a day the check-in schedule counts from."""
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner = await _owner_of(connection, tenant_a)

    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)
    async with sessions() as session:
        await scope_to(await session.connection(), tenant_a)
        created = await create_client(
            session,
            tenant_id=tenant_a,
            payload=ClientCreate(
                full_name="Active From The Start",
                owner_user_id=owner,
                email="active@example.test",
                stage=ClientStage.ACTIVE,
            ),
            actor_user_id=owner,
        )
        assert created.activated_at is not None
        await session.rollback()


async def test_reading_another_tenants_client_is_a_not_found(
    app_engine: AsyncEngine, seeded_client: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """🔒 API §5.4 — indistinguishable from absent.

    RLS makes it so at the database, and the service must not undo that by
    reporting a different error for "exists but not yours".
    """
    _, client_id = seeded_client
    other_tenant = uuid.uuid4()
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)
    async with sessions() as session:
        await scope_to(await session.connection(), other_tenant)
        with pytest.raises(NotFoundError):
            await get_client(session, tenant_id=other_tenant, client_id=client_id)


async def test_update_refuses_a_stale_write(
    app_engine: AsyncEngine, seeded_client: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """🔒 ADR-14 / API §4.4 — two practitioners editing one client is routine.

    Without the precondition the second save silently discards the first one's
    work, with no error and no trace that it happened.
    """
    tenant_id, client_id = seeded_client
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)
    async with sessions() as session:
        await scope_to(await session.connection(), tenant_id)
        stale = datetime.now(UTC) - timedelta(days=1)
        with pytest.raises(ConflictError):
            await update_client(
                session,
                tenant_id=tenant_id,
                client_id=client_id,
                payload=ClientUpdate(city="Pune"),
                expected_updated_at=stale,
            )
        await session.rollback()


async def test_update_distinguishes_absent_from_null(
    app_engine: AsyncEngine, seeded_client: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """🔒 The reason :data:`UNSET` exists.

    Clearing an email is a real edit. If ``None`` meant "unchanged", every
    nullable field would be write-once and the clear button would do nothing —
    while a field left out of the PATCH must not be wiped.
    """
    tenant_id, client_id = seeded_client
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)
    async with sessions() as session:
        await scope_to(await session.connection(), tenant_id)

        # Give the client an email, then clear it explicitly.
        with_email = await update_client(
            session,
            tenant_id=tenant_id,
            client_id=client_id,
            payload=ClientUpdate(email="asha@example.test", city="Pune"),
        )
        assert with_email.email == "asha@example.test"

        cleared = await update_client(
            session,
            tenant_id=tenant_id,
            client_id=client_id,
            payload=ClientUpdate(email=None),
        )
        assert cleared.email is None, "explicit null did not clear the column"
        assert cleared.city == "Pune", "an absent field was wiped; UNSET is not honoured"
        assert cleared.mobile == "+919876543210", "the untouched contact method was lost"

        await session.rollback()


async def test_update_cannot_remove_the_last_contact_method(
    app_engine: AsyncEngine, seeded_client: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """🔒 FR-M1-004 — validated as a *pair*, not field by field.

    Clearing a mobile is legitimate; clearing the last way to reach somebody is
    not, and only the combination can tell the two apart.
    """
    tenant_id, client_id = seeded_client
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)
    async with sessions() as session:
        await scope_to(await session.connection(), tenant_id)
        from app.kernel.errors import ValidationError

        with pytest.raises(ValidationError):
            await update_client(
                session,
                tenant_id=tenant_id,
                client_id=client_id,
                payload=ClientUpdate(mobile=None),  # the client has no email
            )
        await session.rollback()


async def test_update_leaves_stage_alone(
    app_engine: AsyncEngine, seeded_client: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """🔒 ADR-A06 — :class:`ClientUpdate` has no stage field at all.

    Asserted rather than assumed: a generic update that accepted ``stage=``
    would be the PATCH-on-status-field ADR-A06 exists to forbid, and would skip
    the entitlement check and the history row that a transition owes.
    """
    assert not hasattr(ClientUpdate(), "stage"), (
        "ClientUpdate has gained a stage field. Transitions are named actions "
        "(ADR-A06) with entitlement checks and history — not a PATCH."
    )

    tenant_id, client_id = seeded_client
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)
    async with sessions() as session:
        await scope_to(await session.connection(), tenant_id)
        updated = await update_client(
            session,
            tenant_id=tenant_id,
            client_id=client_id,
            payload=ClientUpdate(full_name="Asha M Menon"),
        )
        assert updated.stage is ClientStage.LEAD
        assert UNSET is not None  # the sentinel is importable from the public surface
        await session.rollback()


# ─── The kernel port, against real rows (DB §5) ──────────────────────────


async def test_directory_finds_within_the_tenant_and_not_across_it(
    app_engine: AsyncEngine, seeded_client: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The seam five modules read clients through — proven against RLS."""
    tenant_id, client_id = seeded_client
    directory = ClientRepositoryDirectory()
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as session:
        await scope_to(await session.connection(), tenant_id)
        found = await directory.find(session, tenant_id=tenant_id, client_id=client_id)
        assert found is not None
        assert found.full_name == "Asha Menon"
        assert found.is_archived is False

    async with sessions() as session:
        other = uuid.uuid4()
        await scope_to(await session.connection(), other)
        assert await directory.find(session, tenant_id=other, client_id=client_id) is None


async def test_directory_returns_every_client_on_a_shared_number(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 EC-M1-01 / EC-M2-02 — a list, because a handset can belong to a family.

    Returning one record would force an arbitrary choice between two real
    people, and the enquiry path would silently attach a submission to whichever
    one happened to sort first.
    """
    tenant_a = seeded_tenants[0]
    shared = "+919812345678"
    async with migrator_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        mother = await _insert_client(connection, tenant_a, full_name="Asha", mobile=shared)
        daughter = await _insert_client(connection, tenant_a, full_name="Priya", mobile=shared)
        archived = await _insert_client(
            connection, tenant_a, full_name="Old Record", mobile=shared, archived=True
        )

    try:
        directory = ClientRepositoryDirectory()
        sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)
        async with sessions() as session:
            await scope_to(await session.connection(), tenant_a)
            matches = await directory.find_by_mobile(session, tenant_id=tenant_a, mobile=shared)

        ids = {match.id for match in matches}
        assert {mother, daughter} <= ids, "a client on a shared number was dropped"
        assert archived not in ids, (
            "an archived client matched. A resubmitted enquiry would silently "
            "attach to a record the practitioner archived; restoring it is a "
            "decision, not a side effect."
        )
    finally:
        async with migrator_engine.begin() as connection:
            await scope_to(connection, tenant_a)
            await connection.execute(
                text("DELETE FROM clients WHERE id = ANY(:ids)"),
                {"ids": [mother, daughter, archived]},
            )


async def test_count_active_is_the_entitlement_predicate(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 M1.5 / AC-M1-005 — ``stage = 'active' AND archived_at IS NULL``.

    Counted live rather than from a counter: it is the product's most visible
    limit, and a drifting counter produces a bill the practitioner can disprove
    by eye (DB §14.4).
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        active = await _insert_client(connection, tenant_a, full_name="A", stage="active")
        lead = await _insert_client(connection, tenant_a, full_name="L", stage="lead")
        paused = await _insert_client(connection, tenant_a, full_name="P", stage="paused")
        archived_active = await _insert_client(
            connection, tenant_a, full_name="X", stage="active", archived=True
        )

    try:
        directory = ClientRepositoryDirectory()
        sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)
        async with sessions() as session:
            await scope_to(await session.connection(), tenant_a)
            count = await directory.count_active(session, tenant_id=tenant_a)

        assert count == 1, (
            f"count_active returned {count}, expected 1. Only the `active`, "
            "non-archived client consumes the entitlement (M1.5) — a lead, a "
            "paused client and an archived one must all be excluded."
        )
    finally:
        async with migrator_engine.begin() as connection:
            await scope_to(connection, tenant_a)
            await connection.execute(
                text("DELETE FROM clients WHERE id = ANY(:ids)"),
                {"ids": [active, lead, paused, archived_active]},
            )


async def test_minor_status_is_derived_and_not_a_column(
    migrator_engine: AsyncEngine, seeded_client: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """🔒 FR-M0-028 — the column does not exist, and must not come back.

    A stored flag written while the client was 17 keeps saying so the day they
    turn 18 — the day guardian consent stops being required. Asserted against
    the live catalogue because a migration is what would reintroduce it.
    """
    tenant_id, client_id = seeded_client
    async with migrator_engine.begin() as connection:
        await scope_to(connection, tenant_id)
        columns = {
            row.column_name
            for row in (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'clients'"
                    )
                )
            ).all()
        }
        assert "is_minor" not in columns, (
            "clients.is_minor exists. It goes stale on a birthday and gates "
            "guardian consent (FR-M0-028) — derive it at read time."
        )
        assert "date_of_birth" in columns, "the column it is derived from is missing"

        # And the derivation runs off what the database actually stores.
        await connection.execute(
            text("UPDATE clients SET date_of_birth = :dob WHERE id = :c"),
            {"dob": date(2015, 1, 1), "c": client_id},
        )
        stored = (
            await connection.execute(
                text("SELECT date_of_birth FROM clients WHERE id = :c"), {"c": client_id}
            )
        ).scalar()
        assert stored == date(2015, 1, 1)
