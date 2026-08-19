"""Shared fixtures for the plan-authoring integration suite — M4 Slice 1.4a.

🔒 **These tests only ever reach the Docker database.** ``app_engine`` and
``migrator_engine`` are built from ``TEST_DATABASE_URL`` /
``TEST_DATABASE_MIGRATION_URL`` (see ``tests/integration/conftest.py``), which are
deliberately *different variables* from the ``DATABASE_URL`` in ``.env``. The
application's own settings point at Supabase; nothing here reads them.

⚠️ **The HTTP harness is where that guarantee could have been lost.**
``create_app()`` installs ``pipeline._database_transaction``, which calls
``app.platform.db.transaction()`` → ``get_settings()`` → ``.env`` → **Supabase**.
Building an app and firing requests at it would therefore write to the hosted
database. :func:`plan_api` overrides the provider with one bound to
``app_engine`` before any request is made, and restores it afterwards. That
override is not a convenience; it is the only thing standing between this suite
and a hosted database it has no business touching.

Everything else composes the existing integration fixtures rather than
introducing a parallel harness: ``seeded_tenants`` supplies the tenants and
users, ``scope_to`` applies the RLS scope, and ``installed_ports`` (autouse in
the parent conftest) wires the real kernel ports.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.kernel.context import Actor, ActorType, AuthRealm, UserRole
from app.main import create_app
from app.platform.http import pipeline
from tests.integration.conftest import scope_to

#: The hand-calculable catalogue every plan test is built on.
#:
#: 🔒 Values chosen so a reader can verify a total in their head — see
#: ``test_plan_authoring`` for the arithmetic. Curated (``tenant_id IS NULL``) so
#: both seeded tenants can see them through the Pattern B policy, which is also
#: what makes the cross-tenant tests meaningful: the *food* is shared, the *plan*
#: is not.
DAL_KCAL_PER_100G = Decimal("100")
DAL_PROTEIN_PER_100G = Decimal("5")
KATORI_GRAMS = Decimal("150")
ROTI_KCAL_PER_100G = Decimal("300")
PIECE_GRAMS = Decimal("40")


@dataclass(frozen=True, slots=True)
class Catalogue:
    """Ids for the curated rows the plan fixtures reference."""

    category_id: uuid.UUID
    dal_id: uuid.UUID
    roti_id: uuid.UUID
    katori_id: uuid.UUID
    piece_id: uuid.UUID


@pytest_asyncio.fixture
async def catalogue(migrator_engine: AsyncEngine) -> AsyncIterator[Catalogue]:
    """Two curated foods with known nutrients and portions.

    Seeded as ``app_migrator`` because curated rows carry ``tenant_id IS NULL``
    and the Pattern B ``WITH CHECK`` forbids ``app_user`` from writing one —
    which is itself the isolation property, so the fixture must not work around
    it by widening a policy.
    """
    ids = Catalogue(
        category_id=uuid.uuid4(),
        dal_id=uuid.uuid4(),
        roti_id=uuid.uuid4(),
        katori_id=uuid.uuid4(),
        piece_id=uuid.uuid4(),
    )

    async with migrator_engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO food_categories (id, name) VALUES (:id, :name)"),
            {"id": ids.category_id, "name": f"plan-fixture-{ids.category_id}"},
        )
        for unit_id, name in ((ids.katori_id, "katori"), (ids.piece_id, "piece")):
            await connection.execute(
                text("INSERT INTO measure_units (id, name) VALUES (:id, :name)"),
                {"id": unit_id, "name": f"{name}-{unit_id}"},
            )
        for food_id, name in ((ids.dal_id, "Fixture Dal"), (ids.roti_id, "Fixture Roti")):
            await connection.execute(
                text(
                    "INSERT INTO foods (id, name, category_id, dietary_class) "
                    "VALUES (:id, :name, :cat, 'vegetarian')"
                ),
                {"id": food_id, "name": f"{name} {food_id}", "cat": ids.category_id},
            )
        for code, display, unit in (
            ("ENERC_KCAL", "Energy", "kcal"),
            ("PROCNT", "Protein", "g"),
        ):
            await connection.execute(
                text(
                    "INSERT INTO nutrients (code, name, unit) VALUES (:c, :n, :u) "
                    "ON CONFLICT (code) DO NOTHING"
                ),
                {"c": code, "n": display, "u": unit},
            )
        for food_id, code, amount in (
            (ids.dal_id, "ENERC_KCAL", DAL_KCAL_PER_100G),
            (ids.dal_id, "PROCNT", DAL_PROTEIN_PER_100G),
            (ids.roti_id, "ENERC_KCAL", ROTI_KCAL_PER_100G),
        ):
            await connection.execute(
                text(
                    "INSERT INTO food_nutrients (food_id, nutrient_id, amount_per_100g) "
                    "SELECT :f, id, :a FROM nutrients WHERE code = :c"
                ),
                {"f": food_id, "a": amount, "c": code},
            )
        for food_id, unit_id, grams in (
            (ids.dal_id, ids.katori_id, KATORI_GRAMS),
            (ids.roti_id, ids.piece_id, PIECE_GRAMS),
        ):
            await connection.execute(
                text(
                    "INSERT INTO food_portions (food_id, measure_unit_id, gram_weight) "
                    "VALUES (:f, :u, :g)"
                ),
                {"f": food_id, "u": unit_id, "g": grams},
            )

    try:
        yield ids
    finally:
        async with migrator_engine.begin() as connection:
            for statement, params in (
                (
                    "DELETE FROM food_nutrients WHERE food_id = ANY(:f)",
                    {"f": [ids.dal_id, ids.roti_id]},
                ),
                (
                    "DELETE FROM food_portions WHERE food_id = ANY(:f)",
                    {"f": [ids.dal_id, ids.roti_id]},
                ),
                ("DELETE FROM foods WHERE id = ANY(:f)", {"f": [ids.dal_id, ids.roti_id]}),
                (
                    "DELETE FROM measure_units WHERE id = ANY(:u)",
                    {"u": [ids.katori_id, ids.piece_id]},
                ),
                ("DELETE FROM food_categories WHERE id = :c", {"c": ids.category_id}),
            ):
                await connection.execute(text(statement), params)


@pytest_asyncio.fixture
async def tenant_clients(
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    catalogue: Catalogue,
) -> AsyncIterator[dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]]]:
    """One client per seeded tenant, mapped ``tenant_id -> (client_id, owner_user_id)``.

    ⚠️ Seeded under each tenant's own scope, for the reason ``seeded_tenants``
    documents: an unscoped owner connection is scoped to *nothing*, so the
    ``WITH CHECK`` rejects the insert rather than waving it through.

    ⚠️ **Depends on ``catalogue`` purely for teardown ordering**, not because it
    uses it. pytest tears fixtures down in reverse setup order, so this ordering
    is what guarantees the plans are purged *before* the foods they reference are
    deleted — otherwise ``plan_items_food_id_fkey`` refuses the food deletion and
    the failure surfaces in teardown, far from its cause.
    """
    created: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]] = {}

    async with migrator_engine.begin() as connection:
        for tenant_id in seeded_tenants:
            await scope_to(connection, tenant_id)
            owner_id = (
                await connection.execute(
                    text("SELECT id FROM users WHERE tenant_id = :t LIMIT 1"), {"t": tenant_id}
                )
            ).scalar_one()
            client_id = uuid.uuid4()
            await connection.execute(
                text(
                    "INSERT INTO clients (id, tenant_id, full_name, email, owner_user_id) "
                    "VALUES (:id, :t, 'Plan Fixture Client', :e, :o)"
                ),
                {
                    "id": client_id,
                    "t": tenant_id,
                    "e": f"plan-{client_id}@example.test",
                    "o": owner_id,
                },
            )
            created[tenant_id] = (client_id, owner_id)

    try:
        yield created
    finally:
        async with migrator_engine.begin() as connection:
            for tenant_id, (client_id, _) in created.items():
                await scope_to(connection, tenant_id)
                await _purge_plans(connection, tenant_id)
                await connection.execute(
                    text("DELETE FROM clients WHERE id = :c"), {"c": client_id}
                )


async def _purge_plans(connection: Any, tenant_id: uuid.UUID) -> None:
    """Remove every plan row for one tenant, children first.

    ⚠️ Runs as ``app_migrator`` *and* is still subject to RLS (FORCE ROW LEVEL
    SECURITY), so the caller must have scoped the connection first. The
    restrictive DELETE policies do not apply to ``app_migrator``, which is why
    cleanup can remove an issued plan's rows that the application cannot.
    """
    for statement in (
        "DELETE FROM plan_items WHERE tenant_id = :t",
        "DELETE FROM plan_slots WHERE tenant_id = :t",
        "DELETE FROM plan_days WHERE tenant_id = :t",
        "UPDATE diet_plans SET current_version_id = NULL WHERE tenant_id = :t",
        "DELETE FROM plan_snapshots WHERE tenant_id = :t",
        "DELETE FROM diet_plan_versions WHERE tenant_id = :t",
        "DELETE FROM diet_plans WHERE tenant_id = :t",
        "DELETE FROM jobs WHERE tenant_id = :t",
    ):
        await connection.execute(text(statement), {"t": tenant_id})


def practitioner(tenant_id: uuid.UUID, user_id: uuid.UUID) -> Actor:
    """A practitioner-realm actor for ``tenant_id``."""
    return Actor(
        actor_type=ActorType.PRACTITIONER,
        realm=AuthRealm.PRACTITIONER,
        subject_id=user_id,
        tenant_id=tenant_id,
        role=UserRole.OWNER,
    )


class PlanApi:
    """An HTTP client for the plan routes, with a switchable actor.

    ``as_actor`` swaps who the pipeline believes is calling, which is how the
    cross-tenant and cross-realm assertions are made without rebuilding the app.
    """

    def __init__(self, client: httpx.AsyncClient, set_actor: Callable[[Actor], None]) -> None:
        self.http = client
        self._set_actor = set_actor

    def as_actor(self, actor: Actor) -> None:
        self._set_actor(actor)


@pytest_asyncio.fixture
async def plan_api(app_engine: AsyncEngine) -> AsyncIterator[PlanApi]:
    """The real application, over ASGI, against the **Docker** database.

    🔒 Two seams are overridden, both of which ``pipeline`` exposes for exactly
    this purpose, and both restored on teardown:

    * ``configure_transaction_provider`` — 🔒 **the safety-critical one.** The
      default reads ``.env`` and would reach Supabase. This one opens a session
      on ``app_engine``, which is built from ``TEST_DATABASE_URL``, and applies
      the tenant scope the same way ``_database_transaction`` does.
    * ``configure_actor_resolver`` — no token exists in a test, and
      authentication is not what these tests are about.

    ⚠️ Everything *else* about the request is real: realm check, coarse
    authorization, the row-bound ``authorize()`` call, the transaction, the audit
    entry and the commit. That is the point — a service-level test cannot show
    that a route is wired to the pipeline at all.

    ⚠️ The lifespan is not run, so the audit sink stays the in-memory default and
    no ``audit_log`` rows are written, matching the rest of this suite.
    """
    session_factory = async_sessionmaker(app_engine, expire_on_commit=False)
    current: dict[str, Actor] = {}

    @asynccontextmanager
    async def provider(scope: Any) -> Any:
        """One transaction per request, tenant-scoped, committed on success.

        🔒 Mirrors ``pipeline._database_transaction`` exactly, including the
        commit. ADR-04 puts transaction ownership at the HTTP layer precisely so
        services never commit — a provider that only closed the session would
        roll every request back, and every test would then be asserting against
        a database that had discarded the write it just made.
        """
        session = session_factory()
        try:
            await scope_to(await session.connection(), scope.tenant_id)
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def resolver(_request: httpx.Request) -> Actor:
        return current["actor"]

    original_provider = pipeline.get_transaction_provider()
    app = create_app()
    pipeline.configure_transaction_provider(provider)
    pipeline.configure_actor_resolver(resolver)

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://plan-tests")
    try:
        yield PlanApi(client, lambda actor: current.__setitem__("actor", actor))
    finally:
        await client.aclose()
        await transport.aclose()
        # 🔒 Restored so a later test cannot inherit a provider bound to an
        # engine this fixture already disposed, nor an actor it never chose.
        pipeline.configure_transaction_provider(original_provider)
        pipeline.configure_actor_resolver(_unset_actor_resolver)


async def _unset_actor_resolver(_request: httpx.Request) -> Actor:
    """The resolver installed after teardown: anonymous, i.e. denied."""
    return Actor.anonymous()


@pytest.fixture
def tenant_a(seeded_tenants: tuple[uuid.UUID, ...]) -> uuid.UUID:
    return seeded_tenants[0]


@pytest.fixture
def tenant_b(seeded_tenants: tuple[uuid.UUID, ...]) -> uuid.UUID:
    return seeded_tenants[1]
