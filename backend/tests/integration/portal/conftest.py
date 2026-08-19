"""Fixtures for the client-portal suite — S6 Slice 2.

🔒 **These tests only ever reach the throwaway PostgreSQL.** ``app_engine`` and
``migrator_engine`` come from ``TEST_DATABASE_URL`` / ``TEST_DATABASE_MIGRATION_URL``
(``tests/integration/conftest.py``), which are deliberately different variables
from the ``DATABASE_URL`` in ``.env``. :func:`portal_api` overrides the
transaction provider before any request is made, for the reason the plan suite
states at length: the default provider reads ``.env`` and would reach Supabase.

🔒 **The provider applies the *whole* scope, not just the tenant.** This is the
one thing this conftest does differently from every other suite's, and it is
load-bearing. Pattern C reads ``app.actor_role`` and ``app.actor_id``; a provider
that set only ``app.tenant_id`` would leave ``portal_client_id()`` NULL, every
client-realm policy would short-circuit to true, and the isolation tests would
pass **without any client-level isolation existing**. It therefore calls the
production ``platform.db.set_tenant_scope`` rather than the tenant-only
``scope_to`` helper, so what the tests exercise is what a request applies.

⚠️ **Two clients per tenant, deliberately.** One client per tenant proves
cross-*tenant* isolation only, which Pattern A already gave us. The interesting
boundary in S6 is between two clients of the *same* practitioner, in the same
tenant, where the tenant predicate is satisfied and only Pattern C stands
between them.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, async_sessionmaker

from app.kernel.context import Actor, ActorType, AuthRealm, UserRole
from app.main import create_app
from app.platform.db import set_tenant_scope
from app.platform.http import pipeline
from tests.integration.conftest import scope_to

#: A curated food with figures a reader can verify by hand: 100 kcal / 100 g,
#: and one katori weighs 150 g, so one katori is 150 kcal.
KCAL_PER_100G = Decimal("100")
PROTEIN_PER_100G = Decimal("6")
KATORI_GRAMS = Decimal("150")

#: What the portal must render for ``2 katori`` of it.
EXPECTED_QUANTITY_DISPLAY_SUFFIX = "(300 g)"

#: The figures the fixture's issued version froze — 300 g of the food above.
#:
#: ⚠️ They must *match* what resolving the plan recomputes, or
#: ``composition._warn_on_issued_drift`` reports a drift the test never asked
#: for. That warning is EC-M4-03's tripwire and it should stay meaningful.
ISSUED_TOTALS = '{"energy_kcal": 300, "protein_g": 18, "carbs_g": 0, "fat_g": 0, "fibre_g": 0}'

SLOT_TYPE = "breakfast"
CLIENT_NOTE = "Soak overnight."
PRACTITIONER_NOTE = "Client dislikes this; swap if they complain."


@dataclass(frozen=True, slots=True)
class PortalFood:
    food_id: uuid.UUID
    unit_id: uuid.UUID
    category_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class PortalClient:
    """One client, their tenant, and the plan seeded for them."""

    tenant_id: uuid.UUID
    client_id: uuid.UUID
    owner_user_id: uuid.UUID
    plan_id: uuid.UUID
    version_id: uuid.UUID
    slot_id: uuid.UUID
    content_hash: str
    food_name: str


@pytest_asyncio.fixture
async def portal_food(migrator_engine: AsyncEngine) -> AsyncIterator[PortalFood]:
    """One curated food, visible to every tenant through the Pattern B policy.

    🔒 Curated (``tenant_id IS NULL``) so the *food* is shared while the *plan*
    is not — which is what makes the isolation assertions about plans rather
    than about catalogue visibility.
    """
    ids = PortalFood(food_id=uuid.uuid4(), unit_id=uuid.uuid4(), category_id=uuid.uuid4())

    async with migrator_engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO food_categories (id, name) VALUES (:id, :n)"),
            {"id": ids.category_id, "n": f"portal-fixture-{ids.category_id}"},
        )
        await connection.execute(
            text("INSERT INTO measure_units (id, name) VALUES (:id, :n)"),
            {"id": ids.unit_id, "n": f"katori-{ids.unit_id}"},
        )
        await connection.execute(
            text(
                "INSERT INTO foods (id, name, category_id, dietary_class) "
                "VALUES (:id, :n, :c, 'vegetarian')"
            ),
            {"id": ids.food_id, "n": f"Portal Dal {ids.food_id}", "c": ids.category_id},
        )
        for code, display, unit in (("ENERC_KCAL", "Energy", "kcal"), ("PROCNT", "Protein", "g")):
            await connection.execute(
                text(
                    "INSERT INTO nutrients (code, name, unit) VALUES (:c, :n, :u) "
                    "ON CONFLICT (code) DO NOTHING"
                ),
                {"c": code, "n": display, "u": unit},
            )
        for code, amount in (("ENERC_KCAL", KCAL_PER_100G), ("PROCNT", PROTEIN_PER_100G)):
            await connection.execute(
                text(
                    "INSERT INTO food_nutrients (food_id, nutrient_id, amount_per_100g) "
                    "SELECT :f, id, :a FROM nutrients WHERE code = :c"
                ),
                {"f": ids.food_id, "a": amount, "c": code},
            )
        await connection.execute(
            text(
                "INSERT INTO food_portions (food_id, measure_unit_id, gram_weight) "
                "VALUES (:f, :u, :g)"
            ),
            {"f": ids.food_id, "u": ids.unit_id, "g": KATORI_GRAMS},
        )

    try:
        yield ids
    finally:
        async with migrator_engine.begin() as connection:
            for statement in (
                "DELETE FROM food_nutrients WHERE food_id = :f",
                "DELETE FROM food_portions WHERE food_id = :f",
                "DELETE FROM foods WHERE id = :f",
            ):
                await connection.execute(text(statement), {"f": ids.food_id})
            await connection.execute(
                text("DELETE FROM measure_units WHERE id = :u"), {"u": ids.unit_id}
            )
            await connection.execute(
                text("DELETE FROM food_categories WHERE id = :c"), {"c": ids.category_id}
            )


@pytest_asyncio.fixture
async def portal_clients(
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    portal_food: PortalFood,
) -> AsyncIterator[list[PortalClient]]:
    """Four clients — two in each seeded tenant — each with their own issued plan.

    ⚠️ Depends on ``portal_food`` for teardown ordering as much as for content:
    pytest tears down in reverse, so the plans referencing the food are purged
    before the food is, and ``plan_items_food_id_fkey`` is never violated.

    ⚠️ Seeded under each tenant's own scope. An unscoped owner connection is
    scoped to *nothing* and the ``WITH CHECK`` rejects the insert (see
    ``seeded_tenants``).
    """
    created: list[PortalClient] = []
    issued_at = datetime.now(UTC) - timedelta(days=1)

    async with migrator_engine.begin() as connection:
        for tenant_id in seeded_tenants:
            await scope_to(connection, tenant_id)
            owner_id = (
                await connection.execute(
                    text("SELECT id FROM users WHERE tenant_id = :t LIMIT 1"), {"t": tenant_id}
                )
            ).scalar_one()
            for index in range(2):
                created.append(
                    await _seed_client_with_plan(
                        connection,
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                        food=portal_food,
                        label=f"Client {index}",
                        issued_at=issued_at,
                    )
                )

    try:
        yield created
    finally:
        async with migrator_engine.begin() as connection:
            for tenant_id in seeded_tenants:
                await scope_to(connection, tenant_id)
                for statement in (
                    "DELETE FROM adherence_logs WHERE tenant_id = :t",
                    "DELETE FROM measurements WHERE tenant_id = :t",
                    # ⚠️ `/portal/sync` writes measurements, which publish
                    # `MeasurementRecorded`, which the timeline subscribes to.
                    # A teardown that missed these would fail on the client
                    # delete *after* the assertions passed, reading as a flaky
                    # suite rather than a fixture that had fallen behind.
                    "DELETE FROM timeline_events WHERE tenant_id = :t",
                    "DELETE FROM assessment_responses WHERE tenant_id = :t",
                    "DELETE FROM plan_items WHERE tenant_id = :t",
                    "DELETE FROM plan_slots WHERE tenant_id = :t",
                    "DELETE FROM plan_days WHERE tenant_id = :t",
                    "UPDATE diet_plans SET current_version_id = NULL WHERE tenant_id = :t",
                    "DELETE FROM plan_snapshots WHERE tenant_id = :t",
                    "DELETE FROM diet_plan_versions WHERE tenant_id = :t",
                    "DELETE FROM diet_plans WHERE tenant_id = :t",
                    "DELETE FROM jobs WHERE tenant_id = :t",
                    "DELETE FROM clients WHERE tenant_id = :t",
                ):
                    await connection.execute(text(statement), {"t": tenant_id})


async def _seed_client_with_plan(
    connection: AsyncConnection,
    *,
    tenant_id: uuid.UUID,
    owner_id: uuid.UUID,
    food: PortalFood,
    label: str,
    issued_at: datetime,
) -> PortalClient:
    """One active client, one issued single-day plan, one slot, one item.

    🔒 The food's *name* differs per client only through the plan's title; the
    item's ``client_note`` is what the cross-client tests assert on, because it
    is unique per client and would be the visible evidence of a leak.
    """
    client_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    version_id = uuid.uuid4()
    day_id = uuid.uuid4()
    slot_id = uuid.uuid4()
    content_hash = f"sha256:{uuid.uuid4().hex}"

    await connection.execute(
        text(
            "INSERT INTO clients "
            "(id, tenant_id, full_name, email, owner_user_id, stage, activated_at) "
            "VALUES (:id, :t, :n, :e, :o, 'active', now())"
        ),
        {
            "id": client_id,
            "t": tenant_id,
            "n": f"{label} {client_id}",
            "e": f"portal-{client_id}@example.test",
            "o": owner_id,
        },
    )
    await connection.execute(
        text(
            "INSERT INTO diet_plans (id, tenant_id, client_id, title, created_by_user_id) "
            "VALUES (:id, :t, :c, :title, :u)"
        ),
        {
            "id": plan_id,
            "t": tenant_id,
            "c": client_id,
            "title": f"Plan for {client_id}",
            "u": owner_id,
        },
    )
    await connection.execute(
        text(
            # ⚠️ ``computed_totals`` is not optional here:
            # ``ck_diet_plan_versions__issued_has_totals`` requires an issued
            # version to carry the figures it was issued with (EC-M4-03). The
            # portal never reads them — nutrition is recomputed per item — but a
            # fixture that skipped the column would be creating a row the schema
            # says cannot exist.
            "INSERT INTO diet_plan_versions "
            "(id, tenant_id, plan_id, version_number, state, origin, issued_at, "
            " issued_by_user_id, computed_totals) "
            "VALUES (:id, :t, :p, 1, 'issued', 'manual', :at, :u, CAST(:totals AS jsonb))"
        ),
        {
            "id": version_id,
            "t": tenant_id,
            "p": plan_id,
            "at": issued_at,
            "u": owner_id,
            "totals": ISSUED_TOTALS,
        },
    )
    await connection.execute(
        text("UPDATE diet_plans SET current_version_id = :v WHERE id = :p"),
        {"v": version_id, "p": plan_id},
    )
    await connection.execute(
        text(
            "INSERT INTO plan_snapshots "
            "(tenant_id, plan_version_id, document, document_schema_version, content_hash) "
            "VALUES (:t, :v, '{}'::jsonb, 1, :h)"
        ),
        {"t": tenant_id, "v": version_id, "h": content_hash},
    )
    await connection.execute(
        text(
            "INSERT INTO plan_days (id, tenant_id, plan_version_id, day_number, label) "
            "VALUES (:id, :t, :v, 1, 'Day 1')"
        ),
        {"id": day_id, "t": tenant_id, "v": version_id},
    )
    await connection.execute(
        text(
            "INSERT INTO plan_slots (id, tenant_id, plan_day_id, slot_type, sort_order) "
            "VALUES (:id, :t, :d, :s, 1)"
        ),
        {"id": slot_id, "t": tenant_id, "d": day_id, "s": SLOT_TYPE},
    )
    await connection.execute(
        text(
            "INSERT INTO plan_items "
            "(tenant_id, plan_slot_id, item_type, food_id, quantity, measure_unit_id, "
            " notes, client_note, resolved_grams, sort_order) "
            "VALUES (:t, :s, 'food', :f, 2, :u, :pn, :cn, :g, 1)"
        ),
        {
            "t": tenant_id,
            "s": slot_id,
            "f": food.food_id,
            "u": food.unit_id,
            "pn": PRACTITIONER_NOTE,
            "cn": f"{CLIENT_NOTE} {client_id}",
            "g": KATORI_GRAMS * 2,
        },
    )

    food_name = (
        await connection.execute(text("SELECT name FROM foods WHERE id = :f"), {"f": food.food_id})
    ).scalar_one()

    return PortalClient(
        tenant_id=tenant_id,
        client_id=client_id,
        owner_user_id=owner_id,
        plan_id=plan_id,
        version_id=version_id,
        slot_id=slot_id,
        content_hash=content_hash,
        food_name=food_name,
    )


# ─── Actors ──────────────────────────────────────────────────────────────


def client_actor(fixture: PortalClient) -> Actor:
    """🔒 A client-realm actor — ``subject_id`` is the *client's* id.

    That identity is what ``resolve_scope`` puts in ``app.actor_id`` and what
    Pattern C compares against. It is also why ``role`` must be present: the
    policy's realm test reads ``app.actor_role``, and ``kernel.authz.can()``
    refuses a roleless actor before a transaction is ever opened.
    """
    return Actor(
        actor_type=ActorType.CLIENT,
        realm=AuthRealm.CLIENT,
        subject_id=fixture.client_id,
        tenant_id=fixture.tenant_id,
        role=UserRole.CLIENT,
    )


def practitioner_actor(fixture: PortalClient) -> Actor:
    """A practitioner-realm actor — used to prove ``/portal`` refuses one."""
    return Actor(
        actor_type=ActorType.PRACTITIONER,
        realm=AuthRealm.PRACTITIONER,
        subject_id=fixture.owner_user_id,
        tenant_id=fixture.tenant_id,
        role=UserRole.OWNER,
    )


def anonymous_actor() -> Actor:
    """No credential at all.

    🔒 Stated explicitly rather than relying on the harness's default. A test
    that reaches an endpoint anonymously *after* another helper has set an actor
    would otherwise be calling it as that actor and asserting nothing — which is
    precisely how an "anonymous is refused" test passes while the endpoint is
    wide open.
    """
    return Actor.anonymous()


def operator_actor() -> Actor:
    """A platform operator — the second wrong realm.

    🔒 FR-M11-003: support sees counts, never content. ``portal.read_today`` is
    declared ``TENANT_PII``, which ``register_action`` refuses to combine with
    operator access at import time; this actor is how the *route* is shown to
    refuse as well, and how the refusal is shown to be identical to the
    practitioner's (NFR-043).
    """
    return Actor(
        actor_type=ActorType.OPERATOR,
        realm=AuthRealm.OPERATOR,
        subject_id=uuid.uuid4(),
        tenant_id=None,
        role=UserRole.PLATFORM_OPERATOR,
    )


# ─── The HTTP harness ────────────────────────────────────────────────────


class PortalApi:
    """An HTTP client for the portal routes, with a switchable actor."""

    def __init__(self, client: httpx.AsyncClient, set_actor: Callable[[Actor], None]) -> None:
        self.http = client
        self._set_actor = set_actor

    def as_actor(self, actor: Actor) -> None:
        self._set_actor(actor)


@pytest_asyncio.fixture
async def portal_api(app_engine: AsyncEngine) -> AsyncIterator[PortalApi]:
    """The real application, over ASGI, against the throwaway database.

    🔒 Only two seams are replaced, both through ``pipeline``'s own configuration
    functions and both restored on teardown — the transaction provider and the
    actor resolver. Nothing is monkeypatched: an attribute rebound on a module
    another module already imported at import time would be invisible to the
    code under test, and a test that "passed" that way would prove nothing.

    🔒 The provider calls ``platform.db.set_tenant_scope`` with the full scope,
    exactly as ``pipeline._database_transaction`` does. See this module's
    docstring for why the tenant alone would silently disarm Pattern C.
    """
    session_factory = async_sessionmaker(app_engine, expire_on_commit=False)
    current: dict[str, Actor] = {}

    @asynccontextmanager
    async def provider(scope: Any) -> Any:
        session = session_factory()
        try:
            await set_tenant_scope(
                session,
                tenant_id=scope.tenant_id,
                actor_id=scope.actor_id,
                actor_role=scope.actor_role,
            )
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def resolver(_request: httpx.Request) -> Actor:
        return current.get("actor", Actor.anonymous())

    original_provider = pipeline.get_transaction_provider()
    app = create_app()
    pipeline.configure_transaction_provider(provider)
    pipeline.configure_actor_resolver(resolver)

    transport = httpx.ASGITransport(app=app)
    http = httpx.AsyncClient(transport=transport, base_url="http://portal-tests")
    try:
        yield PortalApi(http, lambda actor: current.__setitem__("actor", actor))
    finally:
        await http.aclose()
        await transport.aclose()
        pipeline.configure_transaction_provider(original_provider)
        pipeline.configure_actor_resolver(_anonymous)


async def _anonymous(_request: httpx.Request) -> Actor:
    return Actor.anonymous()


# ─── Out-of-band writes ──────────────────────────────────────────────────
#
# ⚠️ Deliberately through `migrator_engine` rather than through a service. These
# tests are about what `/portal/today` *reports*, and routing every precondition
# through M1 and M3's write paths would make a portal test fail for reasons that
# have nothing to do with the portal.


async def record_weight(
    migrator_engine: AsyncEngine,
    *,
    fixture: PortalClient,
    weight_kg: Decimal,
    measured_on: date,
) -> None:
    async with migrator_engine.begin() as connection:
        await scope_to(connection, fixture.tenant_id)
        await connection.execute(
            text(
                "INSERT INTO measurements "
                "(tenant_id, client_id, measured_on, weight_kg, source) "
                "VALUES (:t, :c, :d, :w, 'client')"
            ),
            {
                "t": fixture.tenant_id,
                "c": fixture.client_id,
                "d": measured_on,
                "w": weight_kg,
            },
        )


async def set_nutrition_visibility(
    migrator_engine: AsyncEngine, *, fixture: PortalClient, visible: bool
) -> None:
    async with migrator_engine.begin() as connection:
        await scope_to(connection, fixture.tenant_id)
        await connection.execute(
            text("UPDATE clients SET client_nutrition_visibility = :v WHERE id = :c"),
            {"v": visible, "c": fixture.client_id},
        )


async def set_client_stage(
    migrator_engine: AsyncEngine, *, fixture: PortalClient, stage: str
) -> None:
    async with migrator_engine.begin() as connection:
        await scope_to(connection, fixture.tenant_id)
        await connection.execute(
            text("UPDATE clients SET stage = CAST(:s AS client_stage) WHERE id = :c"),
            {"s": stage, "c": fixture.client_id},
        )


async def archive_client(migrator_engine: AsyncEngine, *, fixture: PortalClient) -> None:
    async with migrator_engine.begin() as connection:
        await scope_to(connection, fixture.tenant_id)
        await connection.execute(
            text("UPDATE clients SET archived_at = now() WHERE id = :c"), {"c": fixture.client_id}
        )


async def suspend_tenant(migrator_engine: AsyncEngine, *, tenant_id: uuid.UUID) -> None:
    async with migrator_engine.begin() as connection:
        await connection.execute(
            text("UPDATE tenants SET status = 'suspended', suspended_at = now() WHERE id = :t"),
            {"t": tenant_id},
        )


async def rows_for(migrator_engine: AsyncEngine, *, table: str, fixture: PortalClient) -> list[Any]:
    """Every row of ``table`` belonging to one client, read out of band.

    🔒 Read through ``migrator_engine`` on purpose. The point of a sync
    assertion is *what is actually in the database*, and reading it back through
    the same client-scoped connection the write used would make a row invisible
    for the same reason it was refused — a leak and a correct rejection would
    look identical.
    """
    async with migrator_engine.begin() as connection:
        await scope_to(connection, fixture.tenant_id)
        result = await connection.execute(
            text(f"SELECT * FROM {table} WHERE client_id = :c ORDER BY id"),
            {"c": fixture.client_id},
        )
        return list(result.mappings().all())


async def adherence_rows(migrator_engine: AsyncEngine, *, fixture: PortalClient) -> list[Any]:
    return await rows_for(migrator_engine, table="adherence_logs", fixture=fixture)


async def measurement_rows(migrator_engine: AsyncEngine, *, fixture: PortalClient) -> list[Any]:
    return await rows_for(migrator_engine, table="measurements", fixture=fixture)


@pytest.fixture
def client_a(portal_clients: list[PortalClient]) -> PortalClient:
    """The first client of tenant A."""
    return portal_clients[0]


@pytest.fixture
def client_a2(portal_clients: list[PortalClient]) -> PortalClient:
    """🔒 A *second* client of the same tenant — the Pattern C boundary."""
    return portal_clients[1]


@pytest.fixture
def client_b(portal_clients: list[PortalClient]) -> PortalClient:
    """A client of tenant B — the Pattern A boundary."""
    return portal_clients[2]
