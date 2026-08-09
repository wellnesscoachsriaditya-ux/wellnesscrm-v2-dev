"""Collaboration against a live PostgreSQL — S2 Slice C.

🔒 **Everything asserted here is invisible to a unit test**, which is why the file
exists. The rules are pure and tested in ``tests/test_kernel_collaboration.py``;
what needs a real cluster is everything underneath them:

* 🔒 **AC-M1-006** — "in a two-practitioner tenant, a non-owner practitioner
  cannot see or search clients assigned to the other." The decision is authz, not
  RLS (DB §17.2), so it is only real once the grants are read from the table the
  policy reasons about.
* 🔒 **Tag uniqueness is case-insensitive**, enforced by a partial functional
  index. No application check can prove that; a second request racing the first
  is what the index is for.
* 🔒 **Grants are revoked, not deleted** (EC-M1-04), and at most one is live per
  pair — a partial unique index, again unprovable without PostgreSQL.
* 🔒 **The soft-delete grants**: ``client_notes`` and ``tags`` lose DELETE,
  ``client_tags`` keeps it. A grant is the absence of a privilege, and only the
  database can be asked whether it is absent.
* 🔒 **RLS isolates all four tables** — AC-M0-003 applied to this slice.

⚠️ Needs the same setup as the tenant-isolation gate — see
``tests/integration/test_tenant_isolation.py`` for the provisioning sequence.
These fail rather than skip in CI, where ``REQUIRE_LIVE_DATABASE`` is set.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, async_sessionmaker

from app.kernel.authz import owner_or_assigned
from app.kernel.clients import ClientStage
from app.kernel.collaboration import TagColour
from app.kernel.context import Actor, ActorType, AuthRealm, UserRole
from app.kernel.errors import AuthorizationError, NotFoundError, ValidationError
from app.modules.clients import (
    ClientCreate,
    NoteAuthor,
    add_note,
    archive_note,
    archive_tag,
    attach_tag,
    create_client,
    create_tag,
    detach_tag,
    edit_note,
    grant_access,
    list_client_tags,
    list_grants,
    list_notes,
    list_tags,
    load_for_access,
    reassign_owner,
    revoke_access,
)
from tests.integration.conftest import scope_to

pytestmark = pytest.mark.asyncio


async def _users_of(connection: AsyncConnection, tenant_id: uuid.UUID) -> list[uuid.UUID]:
    rows = (
        await connection.execute(
            text("SELECT id FROM users WHERE tenant_id = :t ORDER BY created_at"), {"t": tenant_id}
        )
    ).all()
    return [uuid.UUID(str(row.id)) for row in rows]


async def _add_second_practitioner(
    migrator_engine: AsyncEngine, *, tenant_id: uuid.UUID
) -> uuid.UUID:
    """Give the tenant a colleague — AC-M1-006 needs two practitioners.

    ``seeded_tenants`` creates one user per tenant, which is enough for every
    earlier slice and not enough for this one: the whole criterion is about what
    the *second* practitioner cannot see.
    """
    subject = uuid.uuid4()
    async with migrator_engine.begin() as connection:
        await scope_to(connection, tenant_id)
        row = (
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "  (tenant_id, auth_subject_id, email, full_name, role, status) "
                    "VALUES (:t, :subject, :email, 'Colleague', 'practitioner', 'active') "
                    "RETURNING id"
                ),
                {
                    "t": tenant_id,
                    "subject": f"gotrue-colleague-{subject}",
                    "email": f"colleague-{subject}@example.test",
                },
            )
        ).one()
    return uuid.UUID(str(row.id))


@pytest_asyncio.fixture
async def clean_collaboration(
    migrator_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> AsyncIterator[None]:
    """Remove everything these tests create, children first.

    ⚠️ Per tenant and under scope — every table here is Pattern A with FORCE, so
    an unscoped DELETE removes zero rows and returns quietly, leaving data that
    breaks the next test's uniqueness assertions.
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
                text("DELETE FROM users WHERE tenant_id = :t AND full_name = 'Colleague'"),
                {"t": tenant_id},
            )


def _sessions(engine: AsyncEngine) -> async_sessionmaker:  # type: ignore[type-arg]
    return async_sessionmaker(bind=engine, expire_on_commit=False)


def _practitioner(subject: uuid.UUID, tenant_id: uuid.UUID) -> Actor:
    return Actor(
        actor_type=ActorType.PRACTITIONER,
        realm=AuthRealm.PRACTITIONER,
        subject_id=subject,
        tenant_id=tenant_id,
        role=UserRole.PRACTITIONER,
    )


async def _make_client(
    engine: AsyncEngine, *, tenant_id: uuid.UUID, owner: uuid.UUID, name: str = "Asha Menon"
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
                email=f"{uuid.uuid4()}@example.test",
                stage=ClientStage.LEAD,
            ),
            actor_user_id=owner,
        )
        return created.id


# ─── AC-M1-006 — practitioner scoping ────────────────────────────────────


async def test_a_practitioner_cannot_reach_a_colleagues_client(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_collaboration: None,
) -> None:
    """🔒 **AC-M1-006**, and the reason this slice is a security slice.

    Slices A and B shipped these actions with no policy, because the grant model
    they need did not exist. This is the assertion that closes it.

    ⚠️ Exercised through ``load_for_access`` + ``owner_or_assigned`` rather than
    through HTTP, because the decision is what is under test — the status code it
    becomes is asserted in ``tests/test_http_pipeline.py``.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    colleague = await _add_second_practitioner(migrator_engine, tenant_id=tenant_a)

    client_id = await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user)

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        _, view = await load_for_access(session, tenant_id=tenant_a, client_id=client_id)

    # The owning practitioner reaches it.
    assert owner_or_assigned(_practitioner(owner_user, tenant_a), view)

    # 🔒 The colleague does not — and the reason is the one the HTTP layer maps
    # to 404 rather than 403 (API §5.4).
    decision = owner_or_assigned(_practitioner(colleague, tenant_a), view)
    assert not decision
    assert decision.reason == "not_assigned_to_actor"


async def test_an_explicit_grant_opens_the_client_to_a_colleague(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_collaboration: None,
) -> None:
    """🔒 EC-M0-04 — shared care, without a second owner.

    This is the round trip the whole design rests on: the grant is written to
    ``client_assignments``, read back by ``load_for_access``, and turned into an
    allow by a policy that never touches the database.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    colleague = await _add_second_practitioner(migrator_engine, tenant_id=tenant_a)
    client_id = await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user)

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
        _, view = await load_for_access(session, tenant_id=tenant_a, client_id=client_id)

    decision = owner_or_assigned(_practitioner(colleague, tenant_a), view)
    assert decision
    assert decision.reason == "explicit_grant"


async def test_revoking_a_grant_closes_the_client_again(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_collaboration: None,
) -> None:
    """🔒 A revoked grant must stop conferring access *immediately*.

    ⚠️ The bug this guards against is subtle: ``load_grants`` filtering on the
    wrong column, or not at all, would keep a revoked colleague's access alive
    while the access screen correctly showed it as removed — the worst
    combination, because the UI says the right thing.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    colleague = await _add_second_practitioner(migrator_engine, tenant_id=tenant_a)
    client_id = await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user)

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

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        _, view = await load_for_access(session, tenant_id=tenant_a, client_id=client_id)
        history = await list_grants(
            session, tenant_id=tenant_a, client_id=client_id, include_revoked=True
        )

    assert not owner_or_assigned(_practitioner(colleague, tenant_a), view)
    # 🔒 EC-M1-04 — the history survives the revocation.
    assert len(history) == 1
    assert history[0].revoked_at is not None
    assert not history[0].is_live


async def test_a_colleague_can_be_granted_again_after_a_revoke(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_collaboration: None,
) -> None:
    """🔒 Why ``granted_at`` is part of the primary key.

    DB §5.5 sketches the key as ``(client_id, user_id)``. With revocation that is
    wrong: the pair legitimately recurs, and the narrower key would make
    re-granting a colleague impossible — a practitioner who came back from leave
    could never be given access again.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    colleague = await _add_second_practitioner(migrator_engine, tenant_id=tenant_a)
    client_id = await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user)
    sessions = _sessions(app_engine)

    for _ in range(2):
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

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        history = await list_grants(
            session, tenant_id=tenant_a, client_id=client_id, include_revoked=True
        )
    assert len(history) == 2, "the second grant was rejected by the primary key"


async def test_only_one_live_grant_per_pair(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_collaboration: None,
) -> None:
    """🔒 ``uq_client_assignments__live``.

    Two live grants would make a revoke appear to do nothing — the second row
    would keep the access alive, and the owner would have no way to tell.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    colleague = await _add_second_practitioner(migrator_engine, tenant_id=tenant_a)
    client_id = await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user)
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

    with pytest.raises(ValidationError):
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


async def test_a_practitioner_cannot_grant_access(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_collaboration: None,
) -> None:
    """🔒 Owner-only (FR-M0-017). A practitioner who could widen access could
    grant themselves a colleague's whole caseload."""
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    colleague = await _add_second_practitioner(migrator_engine, tenant_id=tenant_a)
    client_id = await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user)
    sessions = _sessions(app_engine)

    with pytest.raises(AuthorizationError):
        async with sessions() as session, session.begin():
            await scope_to(await session.connection(), tenant_a)
            await grant_access(
                session,
                tenant_id=tenant_a,
                client_id=client_id,
                owner_user_id=owner_user,
                grantee_user_id=colleague,
                actor_user_id=colleague,
                actor_role=UserRole.PRACTITIONER,
            )


async def test_reassigning_the_owner_moves_access(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_collaboration: None,
) -> None:
    """🔒 EC-M1-04 — a practitioner leaves and their clients are reassigned.

    Asserts both halves: the new owner gains access and the old one loses it.
    A reassignment that left the previous owner able to see the client would
    defeat the point of doing it when somebody leaves the practice.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    colleague = await _add_second_practitioner(migrator_engine, tenant_id=tenant_a)
    client_id = await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user)
    sessions = _sessions(app_engine)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await reassign_owner(
            session,
            tenant_id=tenant_a,
            client_id=client_id,
            new_owner_user_id=colleague,
            actor_user_id=owner_user,
            actor_role=UserRole.OWNER,
        )

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        _, view = await load_for_access(session, tenant_id=tenant_a, client_id=client_id)

    assert owner_or_assigned(_practitioner(colleague, tenant_a), view)
    # ⚠️ `owner_user` here is the tenant's seeded user, whose role in these
    # assertions is PRACTITIONER — an *owner* would still reach every client.
    assert not owner_or_assigned(_practitioner(owner_user, tenant_a), view)


# ─── Notes (FR-M1-007, FR-M3-020) ────────────────────────────────────────


async def test_notes_are_threaded_newest_first(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_collaboration: None,
) -> None:
    """FR-M1-007 — timestamp and author on every note."""
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    client_id = await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user)
    author = NoteAuthor(user_id=owner_user, role=UserRole.PRACTITIONER)
    sessions = _sessions(app_engine)

    for body in ("First contact", "Second consultation"):
        async with sessions() as session, session.begin():
            await scope_to(await session.connection(), tenant_a)
            await add_note(
                session, tenant_id=tenant_a, client_id=client_id, body=body, author=author
            )

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        thread = await list_notes(session, tenant_id=tenant_a, client_id=client_id)

    assert [note.body for note in thread] == ["Second consultation", "First contact"]
    assert all(note.author_user_id == owner_user for note in thread)


async def test_only_the_author_may_edit_a_note_against_the_database(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_collaboration: None,
) -> None:
    """🔒 FR-M3-020, end to end — the rule holds against a persisted author id."""
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    colleague = await _add_second_practitioner(migrator_engine, tenant_id=tenant_a)
    client_id = await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user)
    sessions = _sessions(app_engine)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        note = await add_note(
            session,
            tenant_id=tenant_a,
            client_id=client_id,
            body="Original wording",
            author=NoteAuthor(user_id=owner_user, role=UserRole.PRACTITIONER),
        )
        note_id = note.id

    # 🔒 Even the tenant owner cannot rewrite it.
    with pytest.raises(ValidationError):
        async with sessions() as session, session.begin():
            await scope_to(await session.connection(), tenant_a)
            await edit_note(
                session,
                tenant_id=tenant_a,
                note_id=note_id,
                body="Rewritten by somebody else",
                author=NoteAuthor(user_id=colleague, role=UserRole.OWNER),
            )

    # ...but they may remove it.
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await archive_note(
            session,
            tenant_id=tenant_a,
            note_id=note_id,
            author=NoteAuthor(user_id=colleague, role=UserRole.OWNER),
        )

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        assert await list_notes(session, tenant_id=tenant_a, client_id=client_id) == []


async def test_the_application_cannot_delete_a_note(app_engine: AsyncEngine) -> None:
    """🔒 Soft delete only (DB §22.2) — enforced by the absent grant.

    No amount of care in the module would stop a future caller issuing the
    statement; what must be proven is that the privilege is not there.
    """
    with pytest.raises(DBAPIError) as excinfo:
        async with app_engine.begin() as connection:
            await connection.execute(text("DELETE FROM client_notes"))
    assert "permission denied" in str(excinfo.value).lower()


async def test_a_blank_note_is_refused_by_the_database(
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_collaboration: None,
) -> None:
    """``ck_client_notes__body_not_blank`` — the constraint behind the rule.

    ⚠️ Inserted as the migrator, bypassing the service: a row arriving by
    backfill must be refused too, or the rule holds only for code that remembers.
    """
    tenant_a = seeded_tenants[0]
    async with migrator_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
        client_row = (
            await connection.execute(
                text(
                    "INSERT INTO clients (tenant_id, full_name, email, stage, owner_user_id) "
                    "VALUES (:t, 'Blank Note', 'blank@example.test', 'lead', :o) RETURNING id"
                ),
                {"t": tenant_a, "o": owner_user},
            )
        ).one()

    with pytest.raises(IntegrityError) as excinfo:
        async with migrator_engine.begin() as connection:
            await scope_to(connection, tenant_a)
            await connection.execute(
                text(
                    "INSERT INTO client_notes (tenant_id, client_id, body, author_user_id) "
                    "VALUES (:t, :c, '   ', :a)"
                ),
                {"t": tenant_a, "c": client_row.id, "a": owner_user},
            )
    assert "ck_client_notes__body_not_blank" in str(excinfo.value)


async def test_notes_are_isolated_between_tenants(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_collaboration: None,
) -> None:
    """🔒 AC-M0-003 for this slice. Notes are the most sensitive table in it."""
    tenant_a, tenant_b = seeded_tenants
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    client_id = await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user)
    sessions = _sessions(app_engine)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await add_note(
            session,
            tenant_id=tenant_a,
            client_id=client_id,
            body="Tenant A's private note",
            author=NoteAuthor(user_id=owner_user, role=UserRole.PRACTITIONER),
        )

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_b)
        assert await list_notes(session, tenant_id=tenant_a, client_id=client_id) == []


# ─── Tags (FR-M1-008) ────────────────────────────────────────────────────


async def test_a_duplicate_tag_name_is_refused_case_insensitively(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_collaboration: None,
) -> None:
    """🔒 ``uq_tags__tenant_name`` on ``lower(name)``.

    Without it a practitioner ends up with "PCOS" and "pcos" as two labels that
    look identical in a filter list, splitting their own caseload across both.
    """
    tenant_a = seeded_tenants[0]
    sessions = _sessions(app_engine)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await create_tag(session, tenant_id=tenant_a, name="PCOS", colour=TagColour.VIOLET)

    with pytest.raises(ValidationError):
        async with sessions() as session, session.begin():
            await scope_to(await session.connection(), tenant_a)
            await create_tag(session, tenant_id=tenant_a, name="pcos")


async def test_archiving_a_tag_releases_its_name(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_collaboration: None,
) -> None:
    """The unique index is partial on ``archived_at IS NULL``.

    ⚠️ A practitioner who retired "Weight loss" and later recreates it should not
    be told the name is taken by something they can no longer see.
    """
    tenant_a = seeded_tenants[0]
    sessions = _sessions(app_engine)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        tag = await create_tag(session, tenant_id=tenant_a, name="Weight loss")
        tag_id = tag.id

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await archive_tag(session, tenant_id=tenant_a, tag_id=tag_id)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        recreated = await create_tag(session, tenant_id=tenant_a, name="Weight loss")
        assert recreated.id != tag_id


async def test_two_tenants_may_use_the_same_tag_name(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_collaboration: None,
) -> None:
    """Uniqueness is per tenant. "PCOS" is not a global namespace."""
    tenant_a, tenant_b = seeded_tenants
    sessions = _sessions(app_engine)

    for tenant_id in (tenant_a, tenant_b):
        async with sessions() as session, session.begin():
            await scope_to(await session.connection(), tenant_id)
            await create_tag(session, tenant_id=tenant_id, name="PCOS")

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_b)
        assert [tag.name for tag in await list_tags(session, tenant_id=tenant_b)] == ["PCOS"]


async def test_applying_a_tag_twice_is_a_no_op(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_collaboration: None,
) -> None:
    """Idempotent, so the UI's tag toggle behaves the same however fast it is
    clicked."""
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    client_id = await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user)
    sessions = _sessions(app_engine)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        tag = await create_tag(session, tenant_id=tenant_a, name="Diabetes")
        tag_id = tag.id

    for _ in range(2):
        async with sessions() as session, session.begin():
            await scope_to(await session.connection(), tenant_a)
            await attach_tag(
                session,
                tenant_id=tenant_a,
                client_id=client_id,
                tag_id=tag_id,
                actor_user_id=owner_user,
            )

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        assert len(await list_client_tags(session, tenant_id=tenant_a, client_id=client_id)) == 1


async def test_an_archived_tag_disappears_from_a_clients_list(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_collaboration: None,
) -> None:
    """🔒 The junction row survives, so restoring the tag restores its clients —
    but a retired tag must not keep appearing on the client."""
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    client_id = await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user)
    sessions = _sessions(app_engine)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        tag = await create_tag(session, tenant_id=tenant_a, name="Retired")
        tag_id = tag.id
        await attach_tag(
            session,
            tenant_id=tenant_a,
            client_id=client_id,
            tag_id=tag_id,
            actor_user_id=owner_user,
        )

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await archive_tag(session, tenant_id=tenant_a, tag_id=tag_id)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        assert await list_client_tags(session, tenant_id=tenant_a, client_id=client_id) == []

    # The junction row is still there — the archive is reversible.
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        remaining = (
            await connection.execute(
                text("SELECT count(*) FROM client_tags WHERE client_id = :c"), {"c": client_id}
            )
        ).scalar_one()
    assert remaining == 1


async def test_detaching_a_tag_removes_only_the_junction(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_collaboration: None,
) -> None:
    """Untagging a client must not destroy the tenant's tag."""
    tenant_a = seeded_tenants[0]
    async with migrator_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        owner_user = (await _users_of(connection, tenant_a))[0]
    client_id = await _make_client(app_engine, tenant_id=tenant_a, owner=owner_user)
    sessions = _sessions(app_engine)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        tag = await create_tag(session, tenant_id=tenant_a, name="Vegetarian")
        tag_id = tag.id
        await attach_tag(
            session,
            tenant_id=tenant_a,
            client_id=client_id,
            tag_id=tag_id,
            actor_user_id=owner_user,
        )

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await detach_tag(session, tenant_id=tenant_a, client_id=client_id, tag_id=tag_id)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        assert await list_client_tags(session, tenant_id=tenant_a, client_id=client_id) == []
        assert [t.name for t in await list_tags(session, tenant_id=tenant_a)] == ["Vegetarian"]


async def test_the_application_cannot_delete_a_tag(app_engine: AsyncEngine) -> None:
    """🔒 Tags are retired, not destroyed — the grant says so."""
    with pytest.raises(DBAPIError) as excinfo:
        async with app_engine.begin() as connection:
            await connection.execute(text("DELETE FROM tags"))
    assert "permission denied" in str(excinfo.value).lower()


async def test_a_missing_client_is_a_not_found_for_notes(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 API §5.4 — indistinguishable from another tenant's client."""
    tenant_a = seeded_tenants[0]
    sessions = _sessions(app_engine)

    with pytest.raises(NotFoundError):
        async with sessions() as session, session.begin():
            await scope_to(await session.connection(), tenant_a)
            await load_for_access(session, tenant_id=tenant_a, client_id=uuid.uuid4())
