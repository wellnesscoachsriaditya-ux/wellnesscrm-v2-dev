"""Client lifecycle against a live PostgreSQL — S2 Slice B.

🔒 **Everything asserted here is invisible to a unit test**, which is why the file
exists. The transition rules themselves are pure and tested in
``tests/test_kernel_clients.py``; what needs a real cluster is everything the
rules sit on top of:

* 🔒 **The entitlement gate actually binds** (FR-M1-002). The count is a live
  query against ``clients`` (DB §14.4) joined to a plan limit in ``jsonb``, and
  a fake session agrees with whatever the code believed — including when wrong.
* 🔒 **The 402 carries what the UI needs** (FR-M0-045) — limit, usage, plan and
  upgrade path, read out of a real plan row rather than a fixture's guess.
* 🔒 **Concurrency**. Two simultaneous activations against the last free slot
  must not both succeed. That is the failure a single-threaded test cannot see,
  and the advisory lock that prevents it needs a real connection each.
* 🔒 **The archive encoding is single-valued** — migration 0010's CHECK is the
  database's, and a service that stopped enforcing it would still pass every
  unit test.
* 🔒 **History is appended, never revised** (FR-M1-015), by *grant*.

⚠️ Needs the same setup as the tenant-isolation gate — see
``tests/integration/test_tenant_isolation.py`` for the provisioning sequence.
These fail rather than skip in CI, where ``REQUIRE_LIVE_DATABASE`` is set.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, async_sessionmaker

from app.kernel.clients import ClientStage
from app.kernel.entitlements import ResourceCode
from app.kernel.errors import EntitlementError, NotFoundError, ValidationError
from app.modules.clients import (
    ClientCreate,
    archive,
    change_stage,
    count_active_clients,
    create_client,
    restore,
)
from app.platform.entitlements import serialise_tenant
from tests.integration.conftest import scope_to

pytestmark = pytest.mark.asyncio


async def _owner_of(connection: AsyncConnection, tenant_id: uuid.UUID) -> uuid.UUID:
    row = (
        await connection.execute(
            text("SELECT id FROM users WHERE tenant_id = :t LIMIT 1"), {"t": tenant_id}
        )
    ).first()
    assert row is not None, "seeded_tenants did not create a user for this tenant"
    return uuid.UUID(str(row.id))


async def _set_active_client_limit(
    migrator_engine: AsyncEngine, *, tenant_id: uuid.UUID, limit: int
) -> None:
    """Rewrite the tenant's plan limit for ``active_clients``.

    ⚠️ Edits the *plan*, which is shared by every tenant on that tier — so the
    fixture below restores it. Done as ``app_migrator`` because ``app_user``
    cannot write ``plan_definitions``: a tenant that could would raise its own
    limit (FR-M10-001), and ``test_entitlements`` asserts that refusal.

    ⚠️ ``CAST(:limit AS int)`` rather than ``:limit::int``. SQLAlchemy's
    ``text()`` reads ``:name`` as a bind parameter and PostgreSQL's ``::`` cast
    collides with it — the parameter silently fails to bind and the statement
    dies on a syntax error.

    ⚠️ **Scoped, even though it writes ``plan_definitions``.** The subquery reads
    ``subscriptions``, which is Pattern A, and ``app_migrator`` is not BYPASSRLS.
    Unscoped, the subquery returns nothing, the UPDATE matches zero rows and
    reports success — leaving the plan at its seeded limit so every entitlement
    assertion below passes for the wrong reason.
    """
    async with migrator_engine.begin() as connection:
        await scope_to(connection, tenant_id)
        await connection.execute(
            text(
                "UPDATE plan_definitions SET limits = jsonb_set(limits, '{active_clients}', "
                "to_jsonb(CAST(:limit AS int))) WHERE id = ("
                "  SELECT plan_definition_id FROM subscriptions WHERE tenant_id = :t)"
            ),
            {"limit": limit, "t": tenant_id},
        )


@pytest_asyncio.fixture
async def restore_plan_limits(migrator_engine: AsyncEngine) -> AsyncIterator[None]:
    """Put every plan's ``active_clients`` limit back after a test moves it.

    🔒 Plans are global catalogue rows (Pattern D), shared by every tenant on a
    tier. A test that lowered Starter to 1 and did not restore it would make every
    later test in the session fail on an entitlement error attributed to whatever
    they were actually testing.

    ⚠️ Re-serialised with ``json.dumps`` rather than ``str()``. psycopg hands back
    a Python ``int`` for a JSON number, and ``str(None)`` for a JSON null would
    write the text ``"None"`` — an unreadable limit, which fails safe and would
    therefore look like a working entitlement check while the plan was corrupt.
    """
    async with migrator_engine.begin() as connection:
        original = (
            await connection.execute(
                text("SELECT code, limits -> 'active_clients' AS limit_value FROM plan_definitions")
            )
        ).all()

    try:
        yield
    finally:
        async with migrator_engine.begin() as connection:
            for row in original:
                await connection.execute(
                    text(
                        "UPDATE plan_definitions SET limits = "
                        "jsonb_set(limits, '{active_clients}', CAST(:limit AS jsonb)) "
                        "WHERE code = :code"
                    ),
                    {"limit": json.dumps(row.limit_value), "code": row.code},
                )


@pytest_asyncio.fixture
async def clean_clients(
    migrator_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> AsyncIterator[None]:
    """Remove every client these tests created, in FK order.

    ⚠️ Per tenant and under that tenant's scope. ``clients`` is Pattern A with
    FORCE, so an unscoped DELETE removes *zero* rows and returns quietly —
    leaving clients that inflate the next test's active count, which surfaces as
    an entitlement failure in a test that never mentions billing.

    ⚠️ ``SET LOCAL row_security = off`` would be shorter and is not available: it
    requires BYPASSRLS or superuser, and ``app_migrator`` deliberately holds
    neither (the precondition fixture asserts as much).

    Depends on ``seeded_tenants`` so it tears down *first* — ``clients``
    references ``users``, and the reverse order would fail on the foreign key.
    """
    yield
    async with migrator_engine.begin() as connection:
        for tenant_id in seeded_tenants:
            await scope_to(connection, tenant_id)
            await connection.execute(
                text("DELETE FROM client_stage_history WHERE tenant_id = :t"), {"t": tenant_id}
            )
            await connection.execute(
                text("DELETE FROM clients WHERE tenant_id = :t"), {"t": tenant_id}
            )


async def _make_client(
    engine: AsyncEngine,
    *,
    tenant_id: uuid.UUID,
    owner: uuid.UUID,
    stage: ClientStage = ClientStage.LEAD,
    name: str = "Asha Menon",
    email: str = "asha@example.test",
) -> uuid.UUID:
    """Create one client through the service and commit it."""
    sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_id)
        created = await create_client(
            session,
            tenant_id=tenant_id,
            payload=ClientCreate(full_name=name, owner_user_id=owner, email=email, stage=stage),
            actor_user_id=owner,
        )
        return created.id


# ─── The entitlement gate (FR-M1-002, FR-M1-003) ─────────────────────────


async def test_converting_a_lead_to_active_is_refused_at_the_limit(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    subscribed_tenants: tuple[uuid.UUID, ...],
    restore_plan_limits: None,
    clean_clients: None,
) -> None:
    """🔒 FR-M1-002 — blocked, naming the limit and the upgrade path.

    The error is the whole product surface of a plan limit: the practitioner sees
    it mid-workflow and must be able to act on it without a second request
    (FR-M0-045).
    """
    tenant_a, _ = subscribed_tenants
    await _set_active_client_limit(migrator_engine, tenant_id=tenant_a, limit=1)

    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner = await _owner_of(connection, tenant_a)

    # One active client fills the plan.
    await _make_client(
        app_engine,
        tenant_id=tenant_a,
        owner=owner,
        stage=ClientStage.ACTIVE,
        name="Already Active",
        email="active@example.test",
    )
    lead_id = await _make_client(
        app_engine, tenant_id=tenant_a, owner=owner, name="Second", email="second@example.test"
    )

    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)
    with pytest.raises(EntitlementError) as excinfo:
        async with sessions() as session, session.begin():
            await scope_to(await session.connection(), tenant_a)
            await change_stage(
                session,
                tenant_id=tenant_a,
                client_id=lead_id,
                to_stage=ClientStage.ACTIVE,
                actor_user_id=owner,
            )

    error = excinfo.value
    assert error.status_code == 402
    assert error.details["limit"] == 1
    assert error.details["used"] == 1
    assert error.details["resource"] == "active_clients"
    # 🔒 FR-M0-045 — the upgrade path, so the UI needs no second call.
    assert error.details["upgrade_to"] == "growth"


async def test_the_lead_survives_a_refused_conversion(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    subscribed_tenants: tuple[uuid.UUID, ...],
    restore_plan_limits: None,
    clean_clients: None,
) -> None:
    """🔒 A refused transition changes nothing — not the stage, not the history.

    ⚠️ This is why the entitlement check runs *before* the write. A check placed
    after it would leave the stage changed and the history row written, and the
    rollback would be the only thing standing between the tenant and a client
    they were refused.
    """
    tenant_a, _ = subscribed_tenants
    await _set_active_client_limit(migrator_engine, tenant_id=tenant_a, limit=0)

    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner = await _owner_of(connection, tenant_a)

    lead_id = await _make_client(app_engine, tenant_id=tenant_a, owner=owner)

    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)
    with pytest.raises(EntitlementError):
        async with sessions() as session, session.begin():
            await scope_to(await session.connection(), tenant_a)
            await change_stage(
                session,
                tenant_id=tenant_a,
                client_id=lead_id,
                to_stage=ClientStage.ACTIVE,
                actor_user_id=owner,
            )

    async with app_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        stage = (
            await connection.execute(
                text("SELECT stage FROM clients WHERE id = :c"), {"c": lead_id}
            )
        ).scalar_one()
        transitions = (
            await connection.execute(
                text("SELECT count(*) FROM client_stage_history WHERE client_id = :c"),
                {"c": lead_id},
            )
        ).scalar_one()

    assert stage == "lead"
    assert transitions == 1, "the refused transition left a history row behind"


async def test_moving_between_lead_stages_is_never_metered(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    subscribed_tenants: tuple[uuid.UUID, ...],
    restore_plan_limits: None,
    clean_clients: None,
) -> None:
    """🔒 FR-M1-003 / EC-M2-06 — a tenant at their limit still works their funnel.

    Charging for leads would push the pipeline back to WhatsApp, which is the
    whole rationale the PRD gives for free lead capture.
    """
    tenant_a, _ = subscribed_tenants
    await _set_active_client_limit(migrator_engine, tenant_id=tenant_a, limit=0)

    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner = await _owner_of(connection, tenant_a)

    lead_id = await _make_client(app_engine, tenant_id=tenant_a, owner=owner)

    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        moved = await change_stage(
            session,
            tenant_id=tenant_a,
            client_id=lead_id,
            to_stage=ClientStage.CONSULTATION_SCHEDULED,
            actor_user_id=owner,
            reason="Booked for Tuesday",
        )
        assert moved.stage is ClientStage.CONSULTATION_SCHEDULED


async def test_creating_a_client_at_active_is_metered(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    subscribed_tenants: tuple[uuid.UUID, ...],
    restore_plan_limits: None,
    clean_clients: None,
) -> None:
    """🔒 API §7.1 — "Metered if stage=`active`".

    ⚠️ The gap this closes: Slice A's ``create_client`` accepted ``stage=active``
    with no check at all, so the limit could be walked straight past by creating
    clients already active rather than converting them.
    """
    tenant_a, _ = subscribed_tenants
    await _set_active_client_limit(migrator_engine, tenant_id=tenant_a, limit=0)

    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner = await _owner_of(connection, tenant_a)

    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)
    with pytest.raises(EntitlementError):
        async with sessions() as session, session.begin():
            await scope_to(await session.connection(), tenant_a)
            await create_client(
                session,
                tenant_id=tenant_a,
                payload=ClientCreate(
                    full_name="Born Active",
                    owner_user_id=owner,
                    email="born@example.test",
                    stage=ClientStage.ACTIVE,
                ),
                actor_user_id=owner,
            )

    async with app_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        remaining = (await connection.execute(text("SELECT count(*) FROM clients"))).scalar_one()
    assert remaining == 0, "a refused creation left a client row behind"


async def test_capture_still_works_at_the_limit(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    subscribed_tenants: tuple[uuid.UUID, ...],
    restore_plan_limits: None,
    clean_clients: None,
) -> None:
    """🔒 EC-M2-06 — the counterpart to the test above, and the more important half.

    A limit that also blocked lead capture would make the product refuse work the
    practitioner is not being charged for.
    """
    tenant_a, _ = subscribed_tenants
    await _set_active_client_limit(migrator_engine, tenant_id=tenant_a, limit=0)

    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner = await _owner_of(connection, tenant_a)

    created = await _make_client(app_engine, tenant_id=tenant_a, owner=owner)
    assert created is not None


async def test_two_simultaneous_activations_cannot_share_one_slot(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    subscribed_tenants: tuple[uuid.UUID, ...],
    restore_plan_limits: None,
    clean_clients: None,
) -> None:
    """🔒 The race a row lock cannot fix, and the reason for the advisory lock.

    Two transactions activating *different* clients touch different rows and
    conflict on nothing. Without a tenant-wide mutex both read the same count,
    both pass the check, and the tenant ends up over their plan — a billing
    boundary M1.5 requires to be predictable.

    ⚠️ **Constructed deterministically, not with ``asyncio.gather``.** A gather of
    two activations passes with the lock removed: the event loop runs the first
    coroutine to its commit before the second issues its count, so the second
    correctly sees one active client and refuses. The race needs both
    transactions to have read *before* either commits, which only an explicit
    overlap produces — so this test holds the first transaction open across the
    second attempt.

    With the mutex the second attempt blocks on the lock and the ``wait_for``
    expires. Without it, the second reads a count of zero — the first has not
    committed — passes the check, and both activations land.
    """
    tenant_a, _ = subscribed_tenants
    await _set_active_client_limit(migrator_engine, tenant_id=tenant_a, limit=1)

    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner = await _owner_of(connection, tenant_a)

    first = await _make_client(
        app_engine, tenant_id=tenant_a, owner=owner, name="One", email="one@example.test"
    )
    second = await _make_client(
        app_engine, tenant_id=tenant_a, owner=owner, name="Two", email="two@example.test"
    )

    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as holder, holder.begin():
        await scope_to(await holder.connection(), tenant_a)
        await change_stage(
            holder,
            tenant_id=tenant_a,
            client_id=first,
            to_stage=ClientStage.ACTIVE,
            actor_user_id=owner,
        )

        # 🔒 The first transaction now holds the tenant's entitlement lock and has
        # NOT committed. A second activation must not be able to read the count.
        async with sessions() as contender, contender.begin():
            await scope_to(await contender.connection(), tenant_a)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(
                    change_stage(
                        contender,
                        tenant_id=tenant_a,
                        client_id=second,
                        to_stage=ClientStage.ACTIVE,
                        actor_user_id=owner,
                    ),
                    timeout=2.0,
                )
            await contender.rollback()

    async with app_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        active = (
            await connection.execute(
                text("SELECT count(*) FROM clients WHERE stage = 'active' AND archived_at IS NULL")
            )
        ).scalar_one()

    assert active == 1, f"two activations shared one slot; {active} clients are active"


async def test_the_entitlement_lock_is_per_resource(
    app_engine: AsyncEngine,
    subscribed_tenants: tuple[uuid.UUID, ...],
) -> None:
    """⚠️ The counterpart: the mutex must not serialise unrelated decisions.

    A single key per tenant would make a file upload wait behind a client
    activation. Keyed per ``(resource, tenant)``, the two are independent — and
    counter-backed resources take no lock at all, because their upsert is already
    atomic and locking them would serialise every AI draft in a clinic.
    """
    tenant_a, _ = subscribed_tenants
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as holder, holder.begin():
        await scope_to(await holder.connection(), tenant_a)
        await serialise_tenant(holder, tenant_id=tenant_a, resource=ResourceCode.ACTIVE_CLIENTS)

        async with sessions() as other, other.begin():
            await scope_to(await other.connection(), tenant_a)
            # A different resource: must not block.
            await asyncio.wait_for(
                serialise_tenant(other, tenant_id=tenant_a, resource=ResourceCode.STORAGE_MB),
                timeout=2.0,
            )


async def test_the_entitlement_lock_is_per_tenant(
    app_engine: AsyncEngine,
    subscribed_tenants: tuple[uuid.UUID, ...],
) -> None:
    """⚠️ One tenant's activation must not block another's.

    A key that ignored the tenant would serialise the whole estate behind one
    lock — correct, and unusable.
    """
    tenant_a, tenant_b = subscribed_tenants
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as holder, holder.begin():
        await scope_to(await holder.connection(), tenant_a)
        await serialise_tenant(holder, tenant_id=tenant_a, resource=ResourceCode.ACTIVE_CLIENTS)

        async with sessions() as other, other.begin():
            await scope_to(await other.connection(), tenant_b)
            await asyncio.wait_for(
                serialise_tenant(other, tenant_id=tenant_b, resource=ResourceCode.ACTIVE_CLIENTS),
                timeout=2.0,
            )


# ─── Archive and restore (FR-M1-010, AC-M1-007, EC-M1-02) ────────────────


async def test_archiving_preserves_the_stage_and_frees_the_slot(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    subscribed_tenants: tuple[uuid.UUID, ...],
    clean_clients: None,
) -> None:
    """🔒 AC-M1-005 / AC-M1-007 — out of the count, out of the views, nothing deleted.

    The preserved stage is what makes restore able to put the client back where
    they were rather than guessing (EC-M1-02).
    """
    tenant_a, _ = subscribed_tenants
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner = await _owner_of(connection, tenant_a)

    client_id = await _make_client(
        app_engine, tenant_id=tenant_a, owner=owner, stage=ClientStage.ACTIVE
    )

    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        assert await count_active_clients(session, tenant_id=tenant_a) == 1
        archived = await archive(
            session, tenant_id=tenant_a, client_id=client_id, actor_user_id=owner
        )
        assert archived.archived_at is not None
        # 🔒 The stage is untouched — DB §5.2's predicate reads both columns.
        assert archived.stage is ClientStage.ACTIVE
        assert await count_active_clients(session, tenant_id=tenant_a) == 0


async def test_restore_returns_the_client_to_their_original_stage(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    subscribed_tenants: tuple[uuid.UUID, ...],
    clean_clients: None,
) -> None:
    """🔒 EC-M1-02 — the original record returns, never a second one."""
    tenant_a, _ = subscribed_tenants
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner = await _owner_of(connection, tenant_a)

    client_id = await _make_client(
        app_engine, tenant_id=tenant_a, owner=owner, stage=ClientStage.PAUSED
    )

    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await archive(session, tenant_id=tenant_a, client_id=client_id, actor_user_id=owner)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        restored = await restore(
            session, tenant_id=tenant_a, client_id=client_id, actor_user_id=owner
        )

    assert restored.id == client_id, "restore created a new record"
    assert restored.archived_at is None
    assert restored.stage is ClientStage.PAUSED


async def test_restoring_an_active_client_is_metered(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    subscribed_tenants: tuple[uuid.UUID, ...],
    restore_plan_limits: None,
    clean_clients: None,
) -> None:
    """🔒 EC-M1-06 — archiving frees a slot, so restoring takes one back.

    Without this a practitioner could archive their way under a limit and undo it
    for free, which makes the limit advisory rather than a billing boundary.
    """
    tenant_a, _ = subscribed_tenants
    await _set_active_client_limit(migrator_engine, tenant_id=tenant_a, limit=1)

    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner = await _owner_of(connection, tenant_a)

    archived_id = await _make_client(
        app_engine,
        tenant_id=tenant_a,
        owner=owner,
        stage=ClientStage.ACTIVE,
        name="Archived Active",
        email="arch@example.test",
    )

    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await archive(session, tenant_id=tenant_a, client_id=archived_id, actor_user_id=owner)

    # The freed slot is taken by somebody else.
    await _make_client(
        app_engine,
        tenant_id=tenant_a,
        owner=owner,
        stage=ClientStage.ACTIVE,
        name="Took The Slot",
        email="took@example.test",
    )

    with pytest.raises(EntitlementError):
        async with sessions() as session, session.begin():
            await scope_to(await session.connection(), tenant_a)
            await restore(session, tenant_id=tenant_a, client_id=archived_id, actor_user_id=owner)


async def test_restoring_a_paused_client_is_not_metered(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    subscribed_tenants: tuple[uuid.UUID, ...],
    restore_plan_limits: None,
    clean_clients: None,
) -> None:
    """The counterpart: a restore that consumes nothing must never be refused.

    ⚠️ Both directions matter. A restore that checked the entitlement
    unconditionally would refuse to un-archive a *paused* client at the limit —
    a client who costs nothing and whom the practitioner may need to see.
    """
    tenant_a, _ = subscribed_tenants
    await _set_active_client_limit(migrator_engine, tenant_id=tenant_a, limit=0)

    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner = await _owner_of(connection, tenant_a)

    client_id = await _make_client(
        app_engine, tenant_id=tenant_a, owner=owner, stage=ClientStage.PAUSED
    )

    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await archive(session, tenant_id=tenant_a, client_id=client_id, actor_user_id=owner)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        restored = await restore(
            session, tenant_id=tenant_a, client_id=client_id, actor_user_id=owner
        )
        assert restored.archived_at is None


async def test_an_archived_client_cannot_change_stage(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    subscribed_tenants: tuple[uuid.UUID, ...],
    clean_clients: None,
) -> None:
    """FR-M1-010 — restore first. A stage change on a soft-deleted record would be
    invisible until somebody restored it."""
    tenant_a, _ = subscribed_tenants
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner = await _owner_of(connection, tenant_a)

    client_id = await _make_client(app_engine, tenant_id=tenant_a, owner=owner)

    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await archive(session, tenant_id=tenant_a, client_id=client_id, actor_user_id=owner)

    with pytest.raises(ValidationError):
        async with sessions() as session, session.begin():
            await scope_to(await session.connection(), tenant_a)
            await change_stage(
                session,
                tenant_id=tenant_a,
                client_id=client_id,
                to_stage=ClientStage.ACTIVE,
                actor_user_id=owner,
            )


async def test_archiving_twice_is_refused(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    subscribed_tenants: tuple[uuid.UUID, ...],
    clean_clients: None,
) -> None:
    """Idempotent by refusal, not by silence.

    Returning success for a no-op archive would let a double-submitted request
    report an archive this call did not perform.
    """
    tenant_a, _ = subscribed_tenants
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner = await _owner_of(connection, tenant_a)

    client_id = await _make_client(app_engine, tenant_id=tenant_a, owner=owner)
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await archive(session, tenant_id=tenant_a, client_id=client_id, actor_user_id=owner)

    with pytest.raises(ValidationError):
        async with sessions() as session, session.begin():
            await scope_to(await session.connection(), tenant_a)
            await archive(session, tenant_id=tenant_a, client_id=client_id, actor_user_id=owner)


# ─── History and the activation anchor (FR-M1-015, FR-M8-023) ────────────


async def test_every_transition_is_recorded_with_its_actor(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    subscribed_tenants: tuple[uuid.UUID, ...],
    clean_clients: None,
) -> None:
    """🔒 FR-M1-015 — timestamp and actor, on every transition.

    This is what the timeline (FR-M1-018) and the conversion metrics (FR-M9-006)
    are built from, which is why it is domain history rather than an audit row.
    """
    tenant_a, _ = subscribed_tenants
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner = await _owner_of(connection, tenant_a)

    client_id = await _make_client(app_engine, tenant_id=tenant_a, owner=owner)
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    for stage in (ClientStage.CONTACTED, ClientStage.ACTIVE):
        async with sessions() as session, session.begin():
            await scope_to(await session.connection(), tenant_a)
            await change_stage(
                session,
                tenant_id=tenant_a,
                client_id=client_id,
                to_stage=stage,
                actor_user_id=owner,
                reason=f"moving to {stage.value}",
            )

    async with app_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        rows = (
            await connection.execute(
                text(
                    "SELECT from_stage, to_stage, changed_by_user_id, reason "
                    "FROM client_stage_history WHERE client_id = :c ORDER BY changed_at"
                ),
                {"c": client_id},
            )
        ).all()

    assert [(r.from_stage, r.to_stage) for r in rows] == [
        (None, "lead"),
        ("lead", "contacted"),
        ("contacted", "active"),
    ]
    assert all(uuid.UUID(str(r.changed_by_user_id)) == owner for r in rows[1:])
    assert rows[-1].reason == "moving to active"


async def test_reactivation_keeps_the_original_activation_anchor(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    subscribed_tenants: tuple[uuid.UUID, ...],
    clean_clients: None,
) -> None:
    """🔒 FR-M8-023 / EC-M1-02 — ``activated_at`` is the *first* activation.

    ⚠️ Overwriting it on reactivation would restart a returning client's check-in
    schedule as though they were new, losing the fact that they are returning —
    which is the whole subject of EC-M1-02.
    """
    tenant_a, _ = subscribed_tenants
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner = await _owner_of(connection, tenant_a)

    client_id = await _make_client(
        app_engine, tenant_id=tenant_a, owner=owner, stage=ClientStage.ACTIVE
    )
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        first = await change_stage(
            session,
            tenant_id=tenant_a,
            client_id=client_id,
            to_stage=ClientStage.CHURNED,
            actor_user_id=owner,
        )
        anchor = first.activated_at

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        reactivated = await change_stage(
            session,
            tenant_id=tenant_a,
            client_id=client_id,
            to_stage=ClientStage.ACTIVE,
            actor_user_id=owner,
        )

    assert reactivated.id == client_id, "reactivation created a second record"
    assert reactivated.activated_at == anchor


async def test_a_missing_client_is_a_not_found(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 API §5.4 — indistinguishable from another tenant's client.

    RLS makes it so at the database, and the transition service must not undo
    that by reporting a different error for "exists but not yours".
    """
    tenant_a, _ = subscribed_tenants
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    with pytest.raises(NotFoundError):
        async with sessions() as session, session.begin():
            await scope_to(await session.connection(), tenant_a)
            await change_stage(
                session,
                tenant_id=tenant_a,
                client_id=uuid.uuid4(),
                to_stage=ClientStage.ACTIVE,
                actor_user_id=None,
            )


# ─── The archive encoding, at the table (migration 0010) ─────────────────


async def test_the_database_refuses_the_archived_stage(
    migrator_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 Migration 0010 — the constraint, not the service, is what makes this true.

    ⚠️ Inserted as ``app_migrator`` and bypassing every service, which is the
    point: a row arriving by backfill or by psql must be refused too, or the
    single-valued archive encoding holds only for code that remembers.
    """
    tenant_a = seeded_tenants[0]

    with pytest.raises(IntegrityError) as excinfo:
        async with migrator_engine.begin() as connection:
            await scope_to(connection, tenant_a)
            owner = await _owner_of(connection, tenant_a)
            await connection.execute(
                text(
                    "INSERT INTO clients (tenant_id, full_name, email, stage, owner_user_id) "
                    "VALUES (:t, 'Archived Stage', 'x@example.test', 'archived', :o)"
                ),
                {"t": tenant_a, "o": owner},
            )

    assert "ck_clients__stage_not_archived" in str(excinfo.value)
