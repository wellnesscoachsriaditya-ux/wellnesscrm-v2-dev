"""Discovery against a live PostgreSQL — S2 Slice E.

🔒 **Everything asserted here is invisible to a unit test.** The parsing rules are
pure and covered in ``tests/test_kernel_discovery.py``; what needs a real cluster
is everything the query does with their output:

* 🔒 **AC-M1-006 for a *collection***. ``tests/test_client_access.py`` exempts
  ``client.list`` from the ownership policy on the promise that the query scopes
  instead. This file is that promise, discharged — and it is the only place it can
  be, because the predicate is SQL.
* 🔒 **AC-M1-002 — the last 4 digits of a mobile find a client.** The whole point
  of migration 0013's reversed index is that `to_tsvector` cannot serve a suffix
  match. Only PostgreSQL can be asked whether the implementation actually does.
* 🔒 **The `search_vector` generated column exists and is populated.** It is added
  by raw ALTER and deliberately not mapped on ``Client``, so nothing in Python
  knows it is there. If the migration stopped adding it, every unit test would
  still pass and search would return nothing.
* 🔒 **Keyset pagination across a timestamp tie** (ADR-A05) — the failure mode is
  a skipped client, which no practitioner will report as a bug because it looks
  like a client they never had.
* 🔒 **Bulk reassignment is all-or-nothing** (EC-M1-04).

⚠️ Needs the same setup as the tenant-isolation gate — see
``tests/integration/test_tenant_isolation.py``. These fail rather than skip in CI,
where ``REQUIRE_LIVE_DATABASE`` is set.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, async_sessionmaker

from app.kernel.clients import ClientStage
from app.kernel.context import UserRole
from app.kernel.discovery import (
    MAX_BULK_REASSIGN,
    ArchivedFilter,
    ClientSort,
    SearchTerms,
    SortOrder,
    parse_search,
    parse_sort,
)
from app.kernel.errors import AuthorizationError, NotFoundError, ValidationError
from app.modules.clients import (
    ClientCreate,
    attach_tag,
    build_filters,
    bulk_reassign_owner,
    create_client,
    create_tag,
    grant_access,
    list_clients,
    revoke_access,
)
from app.modules.clients.discovery import _apply_search
from app.modules.clients.models import Client
from app.modules.clients.transitions import archive
from tests.integration.conftest import scope_to

pytestmark = pytest.mark.asyncio

#: The ordering most tests use — name ascending is the only one whose expected
#: result is obvious from the fixture, so a failure names the filter rather than
#: leaving the reader to work out which row should have come first.
BY_NAME = SortOrder(field=ClientSort.NAME, descending=False)


async def _users_of(connection: AsyncConnection, tenant_id: uuid.UUID) -> list[uuid.UUID]:
    rows = (
        await connection.execute(
            text("SELECT id FROM users WHERE tenant_id = :t ORDER BY created_at"), {"t": tenant_id}
        )
    ).all()
    return [uuid.UUID(str(row.id)) for row in rows]


async def _add_practitioner(
    migrator_engine: AsyncEngine, *, tenant_id: uuid.UUID, name: str = "Colleague"
) -> uuid.UUID:
    """Give the tenant a colleague — AC-M1-006 is about what the *second*
    practitioner cannot see, and ``seeded_tenants`` creates only one user."""
    subject = uuid.uuid4()
    async with migrator_engine.begin() as connection:
        await scope_to(connection, tenant_id)
        row = (
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "  (tenant_id, auth_subject_id, email, full_name, role, status) "
                    "VALUES (:t, :subject, :email, :name, 'practitioner', 'active') "
                    "RETURNING id"
                ),
                {
                    "t": tenant_id,
                    "subject": f"gotrue-{name}-{subject}",
                    "email": f"{subject}@example.test",
                    "name": name,
                },
            )
        ).one()
    return uuid.UUID(str(row.id))


@pytest_asyncio.fixture
async def clean_discovery(
    migrator_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> AsyncIterator[None]:
    """Remove everything these tests create, children first.

    ⚠️ Per tenant and under scope — every table here is Pattern A with FORCE, so
    an unscoped DELETE removes zero rows and returns quietly, leaving data the
    next test's counts trip over.
    """
    yield
    async with migrator_engine.begin() as connection:
        for tenant_id in seeded_tenants:
            await scope_to(connection, tenant_id)
            for table in (
                "client_assignments",
                "client_tags",
                "tags",
                "client_notes",
                "client_stage_history",
                "clients",
            ):
                await connection.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id}
                )
            await connection.execute(
                text("DELETE FROM users WHERE tenant_id = :t AND full_name <> 'Practitioner 0'"),
                {"t": tenant_id},
            )


def _sessions(engine: AsyncEngine) -> async_sessionmaker:  # type: ignore[type-arg]
    return async_sessionmaker(bind=engine, expire_on_commit=False)


async def _make_client(
    engine: AsyncEngine,
    *,
    tenant_id: uuid.UUID,
    owner: uuid.UUID,
    name: str = "Asha Menon",
    mobile: str | None = None,
    email: str | None = None,
    city: str | None = None,
    stage: ClientStage = ClientStage.LEAD,
) -> uuid.UUID:
    sessions = _sessions(engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_id)
        created = await create_client(
            session,
            tenant_id=tenant_id,
            payload=ClientCreate(
                full_name=name,
                owner_user_id=owner,
                mobile=mobile,
                email=email or f"{uuid.uuid4()}@example.test",
                city=city,
                stage=stage,
            ),
            actor_user_id=owner,
        )
        return created.id


async def _list(
    engine: AsyncEngine,
    *,
    tenant_id: uuid.UUID,
    actor: uuid.UUID,
    role: UserRole | None = UserRole.PRACTITIONER,
    sort: SortOrder = BY_NAME,
    **filter_kwargs: object,
) -> list[str]:
    """List, and return the names — what a failure message should say.

    ⚠️ Names rather than ids deliberately: an assertion that fails on
    ``['Asha Menon'] != ['Asha Menon', 'Bhavna Rao']`` is readable, and one that
    fails on two UUIDs is not.
    """
    sessions = _sessions(engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_id)
        page = await list_clients(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor,
            actor_role=role,
            filters=build_filters(**filter_kwargs),  # type: ignore[arg-type]
            sort=sort,
        )
        return [item.full_name for item in page.items]


# ─── AC-M1-006 — the list half of practitioner scoping ───────────────────


async def test_a_practitioner_does_not_see_a_colleagues_clients_in_the_list(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """🔒 **AC-M1-006, and the promise `test_client_access.py` makes on credit.**

    That file exempts ``client.list`` from the ownership policy because a list has
    no single resource to scope by. The exemption is only defensible if the query
    scopes instead — this is the assertion that it does.

    ⚠️ Asserted on rows, not on a decision object. The unit-level equivalent would
    pass against a query that filtered *after* loading the tenant, which pages
    short and leaks the total.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    colleague = await _add_practitioner(migrator_engine, tenant_id=tenant_a)

    await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user, name="Owner's Client")
    await _make_client(app_engine, tenant_id=tenant_a, owner=colleague, name="Colleague's Client")

    assert await _list(app_engine, tenant_id=tenant_a, actor=colleague) == ["Colleague's Client"]


async def test_the_tenant_owner_sees_every_client(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """🔒 FR-M0-017 — the owner's exemption is the *absence* of the predicate.

    ⚠️ The role is what grants this, not the ownership of any row. An owner who
    personally owns none of the clients still sees all of them.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    colleague = await _add_practitioner(migrator_engine, tenant_id=tenant_a)

    await _make_client(app_engine, tenant_id=tenant_a, owner=colleague, name="Asha Menon")
    await _make_client(app_engine, tenant_id=tenant_a, owner=colleague, name="Bhavna Rao")

    listed = await _list(app_engine, tenant_id=tenant_a, actor=owner_user, role=UserRole.OWNER)
    assert listed == ["Asha Menon", "Bhavna Rao"]


async def test_a_grant_puts_a_colleagues_client_into_the_list(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """🔒 EC-M0-04 — the list and the detail route must agree.

    A client reachable by `GET /clients/{id}` but missing from the list is as much
    a bug as the reverse, and harder to notice.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    colleague = await _add_practitioner(migrator_engine, tenant_id=tenant_a)
    client_id = await _make_client(
        app_engine, tenant_id=tenant_a, owner=owner_user, name="Shared Client"
    )

    assert await _list(app_engine, tenant_id=tenant_a, actor=colleague) == []

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await grant_access(
            session,
            tenant_id=tenant_a,
            client_id=client_id,
            owner_user_id=owner_user,
            grantee_user_id=colleague,
            actor_user_id=owner_user,
            actor_role=UserRole.OWNER,
        )

    assert await _list(app_engine, tenant_id=tenant_a, actor=colleague) == ["Shared Client"]


async def test_revoking_a_grant_removes_the_client_from_the_list(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """🔒 A revoked grant must stop conferring visibility immediately.

    ⚠️ The row survives the revocation (EC-M1-04 keeps the record), so the
    predicate's `revoked_at IS NULL` is the only thing standing between a
    withdrawn colleague and a caseload they no longer have access to.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    colleague = await _add_practitioner(migrator_engine, tenant_id=tenant_a)
    client_id = await _make_client(
        app_engine, tenant_id=tenant_a, owner=owner_user, name="Shared Client"
    )

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await grant_access(
            session,
            tenant_id=tenant_a,
            client_id=client_id,
            owner_user_id=owner_user,
            grantee_user_id=colleague,
            actor_user_id=owner_user,
            actor_role=UserRole.OWNER,
        )
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await revoke_access(
            session,
            tenant_id=tenant_a,
            client_id=client_id,
            grantee_user_id=colleague,
            actor_user_id=owner_user,
            actor_role=UserRole.OWNER,
        )

    assert await _list(app_engine, tenant_id=tenant_a, actor=colleague) == []


async def test_the_list_never_crosses_a_tenant_boundary(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """🔒 AC-M0-003 applied to this slice.

    ⚠️ Two independent mechanisms must both hold — the `tenant_id` predicate in
    the query *and* RLS. This asserts the visible consequence; if either were
    removed the other would still produce this result, which is the point of
    having both.
    """
    tenant_a, tenant_b = seeded_tenants
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_a = (await _users_of(connection, tenant_a))[0]
        await scope_to(connection, tenant_b)
        owner_b = (await _users_of(connection, tenant_b))[0]

    await _make_client(app_engine, tenant_id=tenant_a, owner=owner_a, name="Tenant A Client")
    await _make_client(app_engine, tenant_id=tenant_b, owner=owner_b, name="Tenant B Client")

    assert await _list(app_engine, tenant_id=tenant_a, actor=owner_a, role=UserRole.OWNER) == [
        "Tenant A Client"
    ]


# ─── FR-M1-021 / AC-M1-002 — search ──────────────────────────────────────


async def test_the_generated_search_column_exists_and_is_populated(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """🔒 Nothing in Python knows this column exists.

    Migration 0009 adds it by raw ALTER and ``Client`` deliberately does not map
    it, so a migration that stopped creating it would break search while every
    unit test stayed green. This is the only assertion standing under that.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    client_id = await _make_client(
        app_engine, tenant_id=tenant_a, owner=owner_user, name="Asha Menon"
    )

    async with app_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        vector = (
            await connection.execute(
                text("SELECT search_vector::text FROM clients WHERE id = :id"), {"id": client_id}
            )
        ).scalar()

    assert vector, "search_vector is empty — the generated column is not populating"
    assert "asha" in str(vector).lower()


async def test_a_partial_name_finds_a_client_as_it_is_typed(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """🔒 FR-M1-021 — results as the practitioner types.

    ⚠️ This is the assertion that the `:*` prefix marker survives all the way to
    `to_tsquery`. Without it "ash" matches nothing and the search box appears to
    work only on complete words.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]

    await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user, name="Asha Menon")
    await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user, name="Bhavna Rao")

    found = await _list(
        app_engine, tenant_id=tenant_a, actor=owner_user, role=UserRole.OWNER, search="ash"
    )
    assert found == ["Asha Menon"]


async def test_the_last_four_digits_of_a_mobile_find_a_client(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """🔒 **AC-M1-002**, and the reason migration 0013 exists.

    `to_tsvector` tokenises `+919876543210` as one term, so this cannot be served
    by `search_vector` at all. It works only if the reversed-column prefix match
    is written the way `ix_clients__tenant_mobile_reversed` expects — and a query
    that got it wrong would still return the right rows, just via a sequential
    scan. Correctness here; the index is asserted separately.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]

    await _make_client(
        app_engine,
        tenant_id=tenant_a,
        owner=owner_user,
        name="Asha Menon",
        mobile="+919876543210",
    )
    await _make_client(
        app_engine, tenant_id=tenant_a, owner=owner_user, name="Bhavna Rao", mobile="+919812340000"
    )

    found = await _list(
        app_engine, tenant_id=tenant_a, actor=owner_user, role=UserRole.OWNER, search="3210"
    )
    assert found == ["Asha Menon"]


async def test_the_mobile_suffix_search_uses_its_index(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """🔒 The half the previous test cannot see — NFR-005.

    ⚠️ Migration 0013's whole argument is that a suffix match becomes a *prefix*
    match on `reverse(mobile)`, which btree can seek. A `LIKE '%4321'` on the
    forward column returns identical rows and cannot use any index, so the only
    symptom of getting this wrong is a list that is merely slow — which nobody
    files a bug about. Asserted by asking the planner directly.

    ⚠️ `enable_seqscan` is turned off for the check. On a table holding a handful
    of fixture rows a sequential scan is genuinely cheaper, so the planner would
    choose one no matter how good the index is; disabling it asks the question
    this test actually means — *can* the index serve this predicate.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    # 🔒 Populated through the application, then inflated to a caseload where a
    # sequential scan is clearly the wrong choice — at which point the plan must
    # show the reversed index, because a `LIKE '%…'` on the forward column could
    # not use *any* index and would scan 2,500 rows.
    #
    # ⚠️ The filler mobiles are **distinct**. Giving them all one number leaves
    # `mobile_reversed` with a single most-common value at ~100% frequency, and
    # the planner abandons its histogram for a default range estimate — it then
    # picks an unrelated index and the test fails while the production query is
    # perfectly correct. Distinct numbers are also what real data looks like.
    #
    # ⚠️ Volume is what makes this test able to see the truth. The predicate
    # costs the same through every index on a table holding one row, so a
    # zero-volume assertion "passes" no matter what the index is — and the
    # migration's whole argument is that a wrong-shaped query is silently slow,
    # never wrong.
    await _make_client(
        app_engine,
        tenant_id=tenant_a,
        owner=owner_user,
        name="Asha Menon",
        mobile="+919876543210",
    )
    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        await connection.execute(
            text(
                "INSERT INTO clients (tenant_id, full_name, mobile, email, stage, "
                "owner_user_id, created_at, updated_at) "
                "SELECT :t, 'Row ' || g, '+9198' || lpad(g::text, 8, '0'), "
                "  :email || g || '@example.test', 'lead', :owner, now(), now() "
                "FROM generate_series(1, 2500) AS g"
            ),
            {"t": tenant_a, "owner": owner_user, "email": "bulk"},
        )

    # 🔒 ANALYZE as the *owner*. `app_user` is refused ("permission denied to
    # analyze") and — critically — the refusal is a WARNING, not an error, so an
    # ANALYZE issued on the app connection succeeds silently while updating
    # nothing. The planner then works from empty-table statistics and picks an
    # arbitrary index, which is exactly the false result this test exists to
    # avoid.
    async with migrator_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        await connection.execute(text("ANALYZE clients"))

    # 🔒 EXPLAIN the **production predicate**, compiled from the module's own
    # `_apply_search`, rather than a hand-copied equivalent. A test that
    # re-types the SQL proves only that the *test's* query uses the index, and
    # goes on passing after the real query drifts away from it.
    #
    # ⚠️ A purely numeric search so only the suffix half is built. `parse_search`
    # populates both halves for a numeric query, and the tsquery half carries a
    # `regconfig` parameter SQLAlchemy cannot render as a literal — which is why
    # this compiles with real bound parameters rather than `literal_binds`.
    terms = parse_search("9876543210")
    statement = _apply_search(
        select(Client.id).where(Client.tenant_id == tenant_a, Client.archived_at.is_(None)),
        SearchTerms(tsquery="", mobile_suffix=terms.mobile_suffix),
    )
    # ⚠️ Kept as the compiler rather than `str(...)`: `construct_params()` below
    # supplies the bound values `EXPLAIN` needs, which a rendered string loses.
    compiled = statement.compile(dialect=postgresql.dialect())

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        await connection.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            str(row[0])
            for row in (
                await connection.exec_driver_sql(f"EXPLAIN {compiled}", compiled.construct_params())
            ).all()
        )

    # 🔒 The index must appear in an **Index Cond**, not merely somewhere in the
    # plan. Under RLS the planner will happily bitmap-scan every row in the
    # tenant through this same index and apply the suffix as a post-hoc Filter —
    # that plan names the index while doing none of its work, so asserting only
    # on the name passes at 91x the cost of a real seek. See migration 0014:
    # `LIKE` and `reverse()` are not leakproof, so neither can be pushed into an
    # Index Cond on an RLS-protected table.
    index_cond = "\n".join(line for line in plan.splitlines() if "Index Cond" in line)
    assert "mobile_reversed" in index_cond, (
        "the mobile-suffix search is not seeking its index — the predicate is "
        "being applied as a Filter after a full scan of the tenant, or has "
        "drifted from the index in migration 0014. Plan was:\n" + plan
    )

    # 🔒 And the fast plan must still be the *correct* plan. A range that seeks
    # efficiently past the row it was supposed to find is the failure mode an
    # index-only assertion cannot see.
    found = await _list(
        app_engine,
        tenant_id=tenant_a,
        actor=owner_user,
        role=UserRole.OWNER,
        search="9876543210",
    )
    assert found == ["Asha Menon"]


async def test_an_email_finds_a_client(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """FR-M1-021 names email alongside name and mobile."""
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]

    await _make_client(
        app_engine,
        tenant_id=tenant_a,
        owner=owner_user,
        name="Asha Menon",
        email="asha.unique@example.test",
    )
    await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user, name="Bhavna Rao")

    found = await _list(
        app_engine,
        tenant_id=tenant_a,
        actor=owner_user,
        role=UserRole.OWNER,
        search="asha.unique",
    )
    assert found == ["Asha Menon"]


async def test_a_search_box_full_of_operators_does_not_reach_postgres_as_syntax(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """🔒 The end of the contract that `parse_search` starts.

    ⚠️ A unit test proves the operators are stripped from the *string*. Only this
    proves the result is something `to_tsquery` accepts — the failure mode is a
    `DBAPIError` surfacing as a 500 on every keystroke, and it depends on
    PostgreSQL's parser rather than on our regex.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user, name="O'Brien Fernandes")

    hostile_queries = (
        "'",
        "&&",
        "!(",
        "a & b",
        "a:*|b",
        "O'Brien",
        "'; DROP TABLE clients; --",
        "<->",
    )
    for hostile in hostile_queries:
        # The assertion is that this does not raise. What it returns is the
        # subject of the tests above.
        await _list(
            app_engine, tenant_id=tenant_a, actor=owner_user, role=UserRole.OWNER, search=hostile
        )

    # 🔒 And the table is still there — the point of binding rather than
    # interpolating, asserted rather than assumed.
    assert await _list(app_engine, tenant_id=tenant_a, actor=owner_user, role=UserRole.OWNER) == [
        "O'Brien Fernandes"
    ]


# ─── FR-M1-022 — filters ─────────────────────────────────────────────────


async def test_stages_are_or_within_the_field(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """API §6.2 — "active or paused" is what ticking two stages means.

    Making them AND would return nothing, which is the more confusing failure.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]

    await _make_client(
        app_engine, tenant_id=tenant_a, owner=owner_user, name="A Lead", stage=ClientStage.LEAD
    )
    await _make_client(
        app_engine,
        tenant_id=tenant_a,
        owner=owner_user,
        name="B Contacted",
        stage=ClientStage.CONTACTED,
    )

    found = await _list(
        app_engine,
        tenant_id=tenant_a,
        actor=owner_user,
        role=UserRole.OWNER,
        stages=frozenset({ClientStage.LEAD, ClientStage.CONTACTED}),
    )
    assert found == ["A Lead", "B Contacted"]

    narrowed = await _list(
        app_engine,
        tenant_id=tenant_a,
        actor=owner_user,
        role=UserRole.OWNER,
        stages=frozenset({ClientStage.LEAD}),
    )
    assert narrowed == ["A Lead"]


async def test_tags_are_and_across_the_field(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """🔒 The one place API §6.2's OR default is deliberately not followed.

    Tags are how a caseload is *narrowed*. "PCOS and post-natal" is a cohort;
    "PCOS or post-natal" is a longer list than the practitioner started with,
    which is the opposite of what picking a second filter is for.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]

    both = await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user, name="Has Both")
    one = await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user, name="Has One")

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        pcos = await create_tag(session, tenant_id=tenant_a, name="PCOS")
        postnatal = await create_tag(session, tenant_id=tenant_a, name="Post-natal")
        await session.flush()
        pcos_id, postnatal_id = pcos.id, postnatal.id
        for client_id, tag_id in ((both, pcos_id), (both, postnatal_id), (one, pcos_id)):
            await attach_tag(
                session,
                tenant_id=tenant_a,
                client_id=client_id,
                tag_id=tag_id,
                actor_user_id=owner_user,
            )

    assert await _list(
        app_engine,
        tenant_id=tenant_a,
        actor=owner_user,
        role=UserRole.OWNER,
        tag_ids=frozenset({pcos_id}),
    ) == ["Has Both", "Has One"]

    assert await _list(
        app_engine,
        tenant_id=tenant_a,
        actor=owner_user,
        role=UserRole.OWNER,
        tag_ids=frozenset({pcos_id, postnatal_id}),
    ) == ["Has Both"]


async def test_archived_clients_are_out_of_the_list_but_findable(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """🔒 DB §22.2 excludes them; EC-M1-02 requires they can still be found.

    ⚠️ Without `ONLY`, a client archived by mistake cannot be located and
    therefore cannot be restored — the archive becomes a one-way door.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]

    await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user, name="Active Client")
    archived_id = await _make_client(
        app_engine, tenant_id=tenant_a, owner=owner_user, name="Archived Client"
    )

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await archive(session, tenant_id=tenant_a, client_id=archived_id, actor_user_id=owner_user)

    common = {"tenant_id": tenant_a, "actor": owner_user, "role": UserRole.OWNER}
    assert await _list(app_engine, **common) == ["Active Client"]  # type: ignore[arg-type]
    assert await _list(app_engine, archived=ArchivedFilter.ONLY, **common) == [  # type: ignore[arg-type]
        "Archived Client"
    ]
    assert await _list(app_engine, archived=ArchivedFilter.INCLUDE, **common) == [  # type: ignore[arg-type]
        "Active Client",
        "Archived Client",
    ]


async def test_filters_combine_with_and_across_fields(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """API §6.2 — OR within a field, AND across them.

    ⚠️ The interesting case is that the search and the stage filter must *both*
    apply. A search that replaced the filters rather than narrowing them would
    pass every single-filter test above.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]

    await _make_client(
        app_engine,
        tenant_id=tenant_a,
        owner=owner_user,
        name="Asha Lead",
        stage=ClientStage.LEAD,
    )
    await _make_client(
        app_engine,
        tenant_id=tenant_a,
        owner=owner_user,
        name="Asha Contacted",
        stage=ClientStage.CONTACTED,
    )

    found = await _list(
        app_engine,
        tenant_id=tenant_a,
        actor=owner_user,
        role=UserRole.OWNER,
        search="asha",
        stages=frozenset({ClientStage.LEAD}),
    )
    assert found == ["Asha Lead"]


# ─── ADR-A05 — pagination ────────────────────────────────────────────────


async def test_paging_across_a_timestamp_tie_neither_skips_nor_repeats(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """🔒 **The reason the cursor carries an id as well as a sort value.**

    ⚠️ Ties are routine rather than theoretical: a bulk reassignment stamps
    `updated_at` on a whole batch inside one transaction. A cursor with no unique
    tiebreaker skips or repeats rows at exactly that boundary — and a skipped
    client is invisible, because it looks like a client the practice never had.

    This forces the tie directly rather than hoping for one.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]

    for index in range(6):
        await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user, name=f"Client {index}")

    # 🔒 Every row given the *same* `updated_at`, which is what a bulk write does.
    async with migrator_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        await connection.execute(
            text("UPDATE clients SET updated_at = now() WHERE tenant_id = :t"), {"t": tenant_a}
        )

    sessions = _sessions(app_engine)
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(6):
        async with sessions() as session, session.begin():
            await scope_to(await session.connection(), tenant_a)
            page = await list_clients(
                session,
                tenant_id=tenant_a,
                actor_user_id=owner_user,
                actor_role=UserRole.OWNER,
                sort=parse_sort(None),
                cursor=cursor,
                limit=2,
            )
        seen.extend(item.full_name for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert cursor is None, "pagination did not terminate"
    assert sorted(seen) == [f"Client {index}" for index in range(6)]
    assert len(seen) == len(set(seen)), f"a client was returned twice: {seen}"


async def test_a_total_is_only_counted_when_it_is_asked_for(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """🔒 API §6.1 — and the total must agree with the filters beside it.

    ⚠️ A count rebuilt from scratch rather than from the filtered statement is
    the classic bug here: it reports the unfiltered total next to a filtered
    list, and the practitioner reads it as missing rows.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]

    await _make_client(
        app_engine, tenant_id=tenant_a, owner=owner_user, name="A Lead", stage=ClientStage.LEAD
    )
    for index in range(3):
        await _make_client(
            app_engine,
            tenant_id=tenant_a,
            owner=owner_user,
            name=f"Contacted {index}",
            stage=ClientStage.CONTACTED,
        )

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        without = await list_clients(
            session,
            tenant_id=tenant_a,
            actor_user_id=owner_user,
            actor_role=UserRole.OWNER,
            sort=BY_NAME,
            limit=2,
        )
        with_total = await list_clients(
            session,
            tenant_id=tenant_a,
            actor_user_id=owner_user,
            actor_role=UserRole.OWNER,
            filters=build_filters(stages=frozenset({ClientStage.CONTACTED})),
            sort=BY_NAME,
            limit=2,
            include_total=True,
        )

    assert without.total is None
    # 🔒 Three contacted clients, not four — the count carries the filter.
    assert with_total.total == 3
    assert len(with_total.items) == 2
    assert with_total.has_more


async def test_a_page_size_over_the_ceiling_is_clamped(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """🔒 NFR-005 budgets 300 ms — `MAX_PAGE_SIZE` is what keeps that reachable.

    Clamped rather than refused: the caller asked for more data, and giving them
    the maximum is a better answer than a validation error.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user, name="Only Client")

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        page = await list_clients(
            session,
            tenant_id=tenant_a,
            actor_user_id=owner_user,
            actor_role=UserRole.OWNER,
            sort=BY_NAME,
            limit=10_000,
        )

    assert [item.full_name for item in page.items] == ["Only Client"]


async def test_the_list_resolves_owners_and_tags_without_an_n_plus_one(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """🔒 API §7.1 embeds the owner's name and the client's tags.

    ⚠️ Loading those per row is the classic list-view mistake — 25 rows becomes
    51 queries, and it only hurts at the caseload where it starts to matter. This
    asserts the data arrives; the "in bulk" half is structural (`_project` runs
    two queries regardless of page size) and would be caught by a reviewer.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    client_id = await _make_client(
        app_engine, tenant_id=tenant_a, owner=owner_user, name="Asha Menon", city="Pune"
    )

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        tag = await create_tag(session, tenant_id=tenant_a, name="PCOS")
        await session.flush()
        await attach_tag(
            session,
            tenant_id=tenant_a,
            client_id=client_id,
            tag_id=tag.id,
            actor_user_id=owner_user,
        )

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        page = await list_clients(
            session,
            tenant_id=tenant_a,
            actor_user_id=owner_user,
            actor_role=UserRole.OWNER,
            sort=BY_NAME,
        )

    item = page.items[0]
    assert item.owner_name == "Practitioner 0"
    assert item.city == "Pune"
    assert [name for _, name, _ in item.tags] == ["PCOS"]


# ─── EC-M1-04 — bulk reassignment ────────────────────────────────────────


async def test_a_bulk_reassignment_moves_the_whole_caseload(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """🔒 EC-M1-04's motivating case — a practitioner leaves.

    ⚠️ Asserted through the *list*, from the new owner's point of view: the
    reassignment is only meaningful if the caseload actually appears in the
    screen they will open.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    leaving = await _add_practitioner(migrator_engine, tenant_id=tenant_a, name="Leaving")
    staying = await _add_practitioner(migrator_engine, tenant_id=tenant_a, name="Staying")

    ids = [
        await _make_client(app_engine, tenant_id=tenant_a, owner=leaving, name=f"Client {index}")
        for index in range(3)
    ]

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        moved = await bulk_reassign_owner(
            session,
            tenant_id=tenant_a,
            client_ids=ids,
            new_owner_user_id=staying,
            actor_user_id=owner_user,
            actor_role=UserRole.OWNER,
        )

    assert moved == 3
    assert await _list(app_engine, tenant_id=tenant_a, actor=leaving) == []
    assert await _list(app_engine, tenant_id=tenant_a, actor=staying) == [
        "Client 0",
        "Client 1",
        "Client 2",
    ]


async def test_clients_already_owned_by_the_target_are_skipped_not_refused(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """⚠️ An overlapping selection is not a mistake.

    Refusing the batch over it would make the operation unusable on exactly the
    "everything this person owns" case it exists for. The single-client path still
    raises, because there the same condition *is* the whole request.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    staying = await _add_practitioner(migrator_engine, tenant_id=tenant_a, name="Staying")

    mine = await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user, name="To Move")
    theirs = await _make_client(app_engine, tenant_id=tenant_a, owner=staying, name="Already")

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        moved = await bulk_reassign_owner(
            session,
            tenant_id=tenant_a,
            client_ids=[mine, theirs],
            new_owner_user_id=staying,
            actor_user_id=owner_user,
            actor_role=UserRole.OWNER,
        )

    assert moved == 1
    assert sorted(await _list(app_engine, tenant_id=tenant_a, actor=staying)) == [
        "Already",
        "To Move",
    ]


async def test_an_unknown_id_rolls_the_whole_batch_back(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """🔒 **All or nothing** — the guarantee the route's docstring promises.

    ⚠️ This is the assertion that matters most in this section. A half-completed
    handover splits a caseload between two practitioners with no record of the
    intent, and the practitioner cannot tell whether re-running it is safe.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    staying = await _add_practitioner(migrator_engine, tenant_id=tenant_a, name="Staying")
    real = await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user, name="Real Client")

    sessions = _sessions(app_engine)
    with pytest.raises(NotFoundError):
        async with sessions() as session, session.begin():
            await scope_to(await session.connection(), tenant_a)
            await bulk_reassign_owner(
                session,
                tenant_id=tenant_a,
                client_ids=[real, uuid.uuid4()],
                new_owner_user_id=staying,
                actor_user_id=owner_user,
                actor_role=UserRole.OWNER,
            )

    # 🔒 The real client did not move — nothing was written.
    assert await _list(app_engine, tenant_id=tenant_a, actor=staying) == []
    assert await _list(app_engine, tenant_id=tenant_a, actor=owner_user) == ["Real Client"]


async def test_a_client_from_another_tenant_cannot_be_reassigned(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """🔒 AC-M0-003 on a write path.

    ⚠️ The id is a valid client — just not one of ours. It must be indistinguishable
    from an id that does not exist, which is what the tenant predicate makes it.
    """
    tenant_a, tenant_b = seeded_tenants
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_a = (await _users_of(connection, tenant_a))[0]
        await scope_to(connection, tenant_b)
        owner_b = (await _users_of(connection, tenant_b))[0]

    theirs = await _make_client(app_engine, tenant_id=tenant_b, owner=owner_b, name="Their Client")
    staying = await _add_practitioner(migrator_engine, tenant_id=tenant_a, name="Staying")

    sessions = _sessions(app_engine)
    with pytest.raises(NotFoundError):
        async with sessions() as session, session.begin():
            await scope_to(await session.connection(), tenant_a)
            await bulk_reassign_owner(
                session,
                tenant_id=tenant_a,
                client_ids=[theirs],
                new_owner_user_id=staying,
                actor_user_id=owner_a,
                actor_role=UserRole.OWNER,
            )


async def test_only_the_tenant_owner_may_bulk_reassign(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """🔒 Reassigning changes who is accountable, and doing it to fifty at once
    does not make it a lesser act (FR-M0-017)."""
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    colleague = await _add_practitioner(migrator_engine, tenant_id=tenant_a)
    client_id = await _make_client(
        app_engine, tenant_id=tenant_a, owner=owner_user, name="A Client"
    )

    sessions = _sessions(app_engine)
    with pytest.raises(AuthorizationError):
        async with sessions() as session, session.begin():
            await scope_to(await session.connection(), tenant_a)
            await bulk_reassign_owner(
                session,
                tenant_id=tenant_a,
                client_ids=[client_id],
                new_owner_user_id=colleague,
                actor_user_id=colleague,
                actor_role=UserRole.PRACTITIONER,
            )


async def test_an_oversized_or_empty_batch_is_refused(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_discovery: None,
) -> None:
    """🔒 The bound is what makes the failure mode describable — see
    `MAX_BULK_REASSIGN`.

    ⚠️ Duplicates are deduplicated *before* the count, so a UI that submits an id
    twice is not refused for exceeding a limit it has not reached.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    staying = await _add_practitioner(migrator_engine, tenant_id=tenant_a, name="Staying")
    client_id = await _make_client(
        app_engine, tenant_id=tenant_a, owner=owner_user, name="A Client"
    )

    sessions = _sessions(app_engine)
    # ⚠️ 101 *distinct* ids. `[uuid4()] * 101` would be the same id repeated,
    # which deduplicates to one and reaches the not-found check instead — the
    # test would pass for the wrong reason and prove nothing about the ceiling.
    oversized = [uuid.uuid4() for _ in range(MAX_BULK_REASSIGN + 1)]
    for batch in ([], oversized):
        with pytest.raises(ValidationError):
            async with sessions() as session, session.begin():
                await scope_to(await session.connection(), tenant_a)
                await bulk_reassign_owner(
                    session,
                    tenant_id=tenant_a,
                    client_ids=batch,
                    new_owner_user_id=staying,
                    actor_user_id=owner_user,
                    actor_role=UserRole.OWNER,
                )

    # 🔒 The same id over the limit is one client, not an oversized batch — the
    # dedup happens before the count, so this must succeed.
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        moved = await bulk_reassign_owner(
            session,
            tenant_id=tenant_a,
            client_ids=[client_id] * (MAX_BULK_REASSIGN + 1),
            new_owner_user_id=staying,
            actor_user_id=owner_user,
            actor_role=UserRole.OWNER,
        )
    assert moved == 1
