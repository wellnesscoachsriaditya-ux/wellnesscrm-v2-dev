"""The timeline against a live PostgreSQL — S2 Slice D.

🔒 **Everything here is invisible to a unit test.** The summary vocabulary is
pure and covered in ``tests/test_kernel_timeline.py``; what needs a real cluster
is everything underneath it:

* 🔒 **DDR-06 end to end** — that publishing a domain event actually writes a
  projection row, in the *publisher's own transaction*. A unit test with a fake
  session proves the handler was called; only a real transaction proves the two
  commit together and roll back together.
* 🔒 **Append-only by grant.** ``app_user`` holds SELECT and INSERT and nothing
  else. A grant is the *absence* of a privilege, and only the database can be
  asked whether it is absent.
* 🔒 **Cursor pagination is stable across a timestamp tie** — the failure mode
  the ``id`` tiebreaker exists for, and one that needs several rows committed in
  a single transaction to reproduce at all.
* 🔒 **RLS isolates the projection** — AC-M0-003 applied to a table that, by
  construction, restates data from six modules.
* 🔒 **The check constraints** — a `system` actor carrying an id, a blank
  summary. Application code never produces either; the constraint is what makes
  that permanent.

⚠️ Needs the same setup as the tenant-isolation gate. These fail rather than skip
in CI, where ``REQUIRE_LIVE_DATABASE`` is set.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.kernel.clients import ClientStage
from app.kernel.context import UserRole
from app.kernel.events import reset_subscriptions
from app.kernel.timeline import TimelineActorType, TimelineEventType
from app.modules.clients import (
    ClientCreate,
    NoteAuthor,
    add_note,
    archive,
    attach_tag,
    change_stage,
    create_client,
    create_tag,
    detach_tag,
    grant_access,
    read_timeline,
    reassign_owner,
    record,
    register_subscribers,
    restore,
    revoke_access,
)
from tests.integration.conftest import scope_to

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _subscribers() -> Iterator[None]:
    """Wire the DDR-06 handlers, then clear them.

    🔒 Subscriptions are process-global. Without the reset a handler leaks into
    every later test in the session, and the symptom — duplicate timeline rows
    in an unrelated file — is nearly impossible to attribute.
    """
    reset_subscriptions()
    register_subscribers()
    yield
    reset_subscriptions()


@pytest_asyncio.fixture
async def clean_timeline(
    migrator_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> AsyncIterator[None]:
    """Remove what these tests create, children first.

    ⚠️ Under scope — the table is Pattern A with FORCE, so an unscoped DELETE
    removes nothing and reports success.
    """
    yield
    async with migrator_engine.begin() as connection:
        for tenant_id in seeded_tenants:
            await scope_to(connection, tenant_id)
            for table in (
                "timeline_events",
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


def _sessions(engine: AsyncEngine) -> async_sessionmaker:  # type: ignore[type-arg]
    return async_sessionmaker(bind=engine, expire_on_commit=False)


# ⚠️ No request-context wrapper anywhere in this file, unlike the collaboration
# suite. Nothing on these paths reads the ambient actor: every function takes
# `actor_user_id` explicitly, and `to_payload` falls back to an anonymous
# context for the request id. Wrapping them would imply a dependency that does
# not exist, and hide it if one were ever introduced.


async def _owner_of(engine: AsyncEngine, tenant_id: uuid.UUID) -> uuid.UUID:
    async with engine.connect() as connection:
        await scope_to(connection, tenant_id)
        row = (
            await connection.execute(
                text("SELECT id FROM users WHERE tenant_id = :t ORDER BY created_at LIMIT 1"),
                {"t": tenant_id},
            )
        ).one()
    return uuid.UUID(str(row.id))


async def _make_client(engine: AsyncEngine, *, tenant_id: uuid.UUID, owner: uuid.UUID) -> uuid.UUID:
    sessions = _sessions(engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_id)
        created = await create_client(
            session,
            tenant_id=tenant_id,
            payload=ClientCreate(
                full_name="Asha Menon",
                owner_user_id=owner,
                email=f"{uuid.uuid4()}@example.test",
                stage=ClientStage.LEAD,
            ),
            actor_user_id=owner,
        )
        return created.id


async def _entries(
    engine: AsyncEngine, *, tenant_id: uuid.UUID, client_id: uuid.UUID
) -> list[tuple[str, str]]:
    """(event_type, summary) for a client, newest first."""
    sessions = _sessions(engine)
    async with sessions() as session:
        await scope_to(await session.connection(), tenant_id)
        page = await read_timeline(session, tenant_id=tenant_id, client_id=client_id)
        return [(entry.event_type.value, entry.summary) for entry in page.items]


# ─── DDR-06: events become rows ──────────────────────────────────────────


async def test_a_stage_change_writes_a_timeline_row(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_timeline: None
) -> None:
    """🔒 DDR-06 end to end — the whole point of the slice.

    The subscriber is transactional, so the entry exists the moment the
    transition commits. A practitioner who changes a stage and looks at the
    timeline sees it, which an eventually-consistent queue could not promise.
    """
    tenant = seeded_tenants[0]
    owner = await _owner_of(app_engine, tenant)
    client_id = await _make_client(app_engine, tenant_id=tenant, owner=owner)

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant)
        await change_stage(
            session,
            tenant_id=tenant,
            client_id=client_id,
            to_stage=ClientStage.CONTACTED,
            actor_user_id=owner,
            reason="Called back",
        )

    assert ("stage_changed", "New enquiry → Contacted") in await _entries(
        app_engine, tenant_id=tenant, client_id=client_id
    )


async def test_the_reason_never_reaches_the_timeline(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_timeline: None
) -> None:
    """🔒 NFR-033 — a transition reason is practitioner free text.

    It is stored on `client_stage_history` under that table's retention rules.
    The timeline is the most-read screen in the product, and a reason there
    would be the easiest possible clinical leak into a screenshot.
    """
    tenant = seeded_tenants[0]
    owner = await _owner_of(app_engine, tenant)
    client_id = await _make_client(app_engine, tenant_id=tenant, owner=owner)
    secret = "Discussed her thyroid results"

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant)
        # ⚠️ `contacted`, not `active`. Entering `active` consumes an
        # entitlement and these tenants have no subscription — the refusal
        # would fail this test for a reason that has nothing to do with what
        # it asserts.
        await change_stage(
            session,
            tenant_id=tenant,
            client_id=client_id,
            to_stage=ClientStage.CONTACTED,
            actor_user_id=owner,
            reason=secret,
        )

    summaries = [
        summary for _, summary in await _entries(app_engine, tenant_id=tenant, client_id=client_id)
    ]
    assert all(secret not in summary for summary in summaries)


async def test_a_note_appears_without_its_body(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_timeline: None
) -> None:
    """🔒 FR-M1-018 records that a note exists; FR-M3-021 keeps what it says."""
    tenant = seeded_tenants[0]
    owner = await _owner_of(app_engine, tenant)
    client_id = await _make_client(app_engine, tenant_id=tenant, owner=owner)
    body = "BP 130/85, advised low sodium"

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant)
        await add_note(
            session,
            tenant_id=tenant,
            client_id=client_id,
            body=body,
            author=NoteAuthor(user_id=owner, role=UserRole.OWNER),
        )

    entries = await _entries(app_engine, tenant_id=tenant, client_id=client_id)
    assert ("note_added", "Note added") in entries
    assert all(body not in summary for _, summary in entries)


async def test_archive_and_restore_both_stand(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_timeline: None
) -> None:
    """⚠️ Append-only: the archive row is not removed by the restore.

    "Archived in March, restored in April" is the history. A timeline that
    erased the archive would make the restore inexplicable.
    """
    tenant = seeded_tenants[0]
    owner = await _owner_of(app_engine, tenant)
    client_id = await _make_client(app_engine, tenant_id=tenant, owner=owner)

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant)
        await archive(session, tenant_id=tenant, client_id=client_id, actor_user_id=owner)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant)
        await restore(session, tenant_id=tenant, client_id=client_id, actor_user_id=owner)

    types = [
        event_type
        for event_type, _ in await _entries(app_engine, tenant_id=tenant, client_id=client_id)
    ]
    assert "client_archived" in types
    assert "client_restored" in types


async def test_tagging_and_untagging_each_record(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_timeline: None
) -> None:
    """FR-M1-008 — one event type for both directions, and one summary."""
    tenant = seeded_tenants[0]
    owner = await _owner_of(app_engine, tenant)
    client_id = await _make_client(app_engine, tenant_id=tenant, owner=owner)

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant)
        tag = await create_tag(session, tenant_id=tenant, name="PCOS", colour=None)  # type: ignore[arg-type]
        await attach_tag(
            session,
            tenant_id=tenant,
            client_id=client_id,
            tag_id=tag.id,
            actor_user_id=owner,
        )
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant)
        await detach_tag(
            session,
            tenant_id=tenant,
            client_id=client_id,
            tag_id=tag.id,
            actor_user_id=owner,
        )

    entries = await _entries(app_engine, tenant_id=tenant, client_id=client_id)
    assert [event_type for event_type, _ in entries].count("tag_applied") == 2
    # ⚠️ The tag's *name* must not appear — a row naming a retired tag reads as
    # corruption months later.
    assert all("PCOS" not in summary for _, summary in entries)


async def test_reapplying_a_tag_adds_no_second_entry(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_timeline: None
) -> None:
    """⚠️ `attach_tag` is idempotent, so a fast double-click must not append.

    The event is published only when a row was genuinely inserted; without that
    guard the timeline grows an entry per click on an idempotent endpoint.
    """
    tenant = seeded_tenants[0]
    owner = await _owner_of(app_engine, tenant)
    client_id = await _make_client(app_engine, tenant_id=tenant, owner=owner)

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant)
        tag = await create_tag(session, tenant_id=tenant, name="Weight loss", colour=None)  # type: ignore[arg-type]
        for _ in range(3):
            await attach_tag(
                session,
                tenant_id=tenant,
                client_id=client_id,
                tag_id=tag.id,
                actor_user_id=owner,
            )

    entries = await _entries(app_engine, tenant_id=tenant, client_id=client_id)
    assert [event_type for event_type, _ in entries].count("tag_applied") == 1


async def test_detaching_an_absent_tag_records_nothing(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_timeline: None
) -> None:
    """⚠️ `DELETE` on an absent row succeeds silently — the rowcount guard is
    what stops a no-op appending history."""
    tenant = seeded_tenants[0]
    owner = await _owner_of(app_engine, tenant)
    client_id = await _make_client(app_engine, tenant_id=tenant, owner=owner)

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant)
        tag = await create_tag(session, tenant_id=tenant, name="Unused", colour=None)  # type: ignore[arg-type]
        await detach_tag(
            session,
            tenant_id=tenant,
            client_id=client_id,
            tag_id=tag.id,
            actor_user_id=owner,
        )

    entries = await _entries(app_engine, tenant_id=tenant, client_id=client_id)
    assert not [event_type for event_type, _ in entries if event_type == "tag_applied"]


async def test_access_and_ownership_changes_reach_the_timeline(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_timeline: None,
) -> None:
    """EC-M0-04 / EC-M1-04 — a colleague joining is a care fact.

    Without it, notes written during a period of shared care read as though
    they came from nowhere.
    """
    tenant = seeded_tenants[0]
    owner = await _owner_of(app_engine, tenant)
    client_id = await _make_client(app_engine, tenant_id=tenant, owner=owner)

    colleague = uuid.uuid4()
    async with migrator_engine.begin() as connection:
        await scope_to(connection, tenant)
        row = (
            await connection.execute(
                text(
                    "INSERT INTO users (tenant_id, auth_subject_id, email, full_name, role, status)"
                    " VALUES (:t, :s, :e, 'Colleague', 'practitioner', 'active') RETURNING id"
                ),
                {"t": tenant, "s": f"gotrue-{colleague}", "e": f"{colleague}@example.test"},
            )
        ).one()
        colleague_id = uuid.UUID(str(row.id))

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant)
        await grant_access(
            session,
            tenant_id=tenant,
            client_id=client_id,
            owner_user_id=owner,
            grantee_user_id=colleague_id,
            actor_user_id=owner,
            actor_role=UserRole.OWNER,
        )
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant)
        await revoke_access(
            session,
            tenant_id=tenant,
            client_id=client_id,
            grantee_user_id=colleague_id,
            actor_user_id=owner,
            actor_role=UserRole.OWNER,
        )
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant)
        await reassign_owner(
            session,
            tenant_id=tenant,
            client_id=client_id,
            new_owner_user_id=colleague_id,
            actor_user_id=owner,
            actor_role=UserRole.OWNER,
        )

    types = [
        event_type
        for event_type, _ in await _entries(app_engine, tenant_id=tenant, client_id=client_id)
    ]
    assert types.count("access_changed") == 2
    assert types.count("ownership_changed") == 1


async def test_a_failed_transition_leaves_no_timeline_row(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_timeline: None
) -> None:
    """🔒 The transactional guarantee, in its negative form.

    DDR-06's whole argument for a transactional subscriber is that the entry
    and its cause commit together. This asserts the other half: a rolled-back
    action leaves no trace claiming it happened.
    """
    tenant = seeded_tenants[0]
    owner = await _owner_of(app_engine, tenant)
    client_id = await _make_client(app_engine, tenant_id=tenant, owner=owner)

    sessions = _sessions(app_engine)
    with pytest.raises(RuntimeError, match="deliberate"):
        async with sessions() as session, session.begin():
            await scope_to(await session.connection(), tenant)
            await change_stage(
                session,
                tenant_id=tenant,
                client_id=client_id,
                to_stage=ClientStage.CONTACTED,
                actor_user_id=owner,
                reason="Called back",
            )
            raise RuntimeError("deliberate — roll the transaction back")

    assert await _entries(app_engine, tenant_id=tenant, client_id=client_id) == []


# ─── Append-only, by grant ───────────────────────────────────────────────


async def test_the_application_cannot_rewrite_history(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_timeline: None
) -> None:
    """🔒 UPDATE and DELETE are revoked from `app_user` (migration 0012).

    A timeline the application can rewrite is not a history. Only the database
    can be asked whether a privilege is absent, which is why this cannot be a
    unit test.
    """
    tenant = seeded_tenants[0]
    owner = await _owner_of(app_engine, tenant)
    client_id = await _make_client(app_engine, tenant_id=tenant, owner=owner)

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant)
        await add_note(
            session,
            tenant_id=tenant,
            client_id=client_id,
            body="A note",
            author=NoteAuthor(user_id=owner, role=UserRole.OWNER),
        )

    async with app_engine.connect() as connection:
        await scope_to(connection, tenant)
        with pytest.raises(DBAPIError):
            await connection.execute(
                text("UPDATE timeline_events SET summary = 'rewritten' WHERE tenant_id = :t"),
                {"t": tenant},
            )

    async with app_engine.connect() as connection:
        await scope_to(connection, tenant)
        with pytest.raises(DBAPIError):
            await connection.execute(
                text("DELETE FROM timeline_events WHERE tenant_id = :t"), {"t": tenant}
            )


async def test_the_system_may_not_be_given_a_face(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_timeline: None
) -> None:
    """🔒 `ck_timeline_events__system_has_no_actor`.

    Attributing an automated action to a person is the exact misreading
    `actor_type` exists to prevent, so it is refused at the table rather than
    trusted to every future producer.
    """
    tenant = seeded_tenants[0]
    owner = await _owner_of(app_engine, tenant)
    client_id = await _make_client(app_engine, tenant_id=tenant, owner=owner)

    sessions = _sessions(app_engine)
    with pytest.raises(IntegrityError):
        async with sessions() as session, session.begin():
            await scope_to(await session.connection(), tenant)
            await record(
                session,
                tenant_id=tenant,
                client_id=client_id,
                event_type=TimelineEventType.CLIENT_ARCHIVED,
                summary="Client archived",
                occurred_at=datetime.now(UTC),
                actor_type=TimelineActorType.SYSTEM,
                actor_id=owner,
            )


async def test_a_blank_summary_is_refused(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_timeline: None
) -> None:
    """`ck_timeline_events__summary_not_blank` — an empty row renders as a gap
    in the history with no explanation."""
    tenant = seeded_tenants[0]
    owner = await _owner_of(app_engine, tenant)
    client_id = await _make_client(app_engine, tenant_id=tenant, owner=owner)

    sessions = _sessions(app_engine)
    with pytest.raises(IntegrityError):
        async with sessions() as session, session.begin():
            await scope_to(await session.connection(), tenant)
            await record(
                session,
                tenant_id=tenant,
                client_id=client_id,
                event_type=TimelineEventType.NOTE_ADDED,
                summary="   ",
                occurred_at=datetime.now(UTC),
                actor_type=TimelineActorType.PRACTITIONER,
                actor_id=owner,
            )


# ─── Reading: order, filters and the cursor (FR-M1-018/019, ADR-A05) ─────


async def _seed_entries(
    engine: AsyncEngine, *, tenant_id: uuid.UUID, client_id: uuid.UUID, count: int, actor: uuid.UUID
) -> None:
    """`count` entries, all sharing one timestamp.

    🔒 The tie is the point. Several events committed in one transaction
    genuinely share a timestamp to the microsecond, and that is precisely when a
    cursor keyed on time alone skips or repeats rows.
    """
    moment = datetime.now(UTC) - timedelta(days=1)
    sessions = _sessions(engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_id)
        for _ in range(count):
            await record(
                session,
                tenant_id=tenant_id,
                client_id=client_id,
                event_type=TimelineEventType.NOTE_ADDED,
                summary="Note added",
                occurred_at=moment,
                actor_type=TimelineActorType.PRACTITIONER,
                actor_id=actor,
            )


async def test_paging_a_timestamp_tie_neither_skips_nor_repeats(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_timeline: None
) -> None:
    """🔒 The reason the index and the cursor both carry `id`.

    Without the tiebreaker these ten identical-timestamp rows have no stable
    order, and a page boundary landing inside them loses or duplicates entries —
    a bug that only appears on clients busy enough to need a second page.
    """
    tenant = seeded_tenants[0]
    owner = await _owner_of(app_engine, tenant)
    client_id = await _make_client(app_engine, tenant_id=tenant, owner=owner)
    await _seed_entries(app_engine, tenant_id=tenant, client_id=client_id, count=10, actor=owner)

    sessions = _sessions(app_engine)
    seen: list[uuid.UUID] = []
    cursor: str | None = None

    async with sessions() as session:
        await scope_to(await session.connection(), tenant)
        for _ in range(5):
            page = await read_timeline(
                session, tenant_id=tenant, client_id=client_id, cursor=cursor, limit=3
            )
            seen.extend(entry.id for entry in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break

    assert len(seen) == 10
    assert len(set(seen)) == 10, "a page boundary inside a timestamp tie duplicated a row"


async def test_the_timeline_is_newest_first(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_timeline: None
) -> None:
    """FR-M1-018 — reverse-chronological is what makes it readable."""
    tenant = seeded_tenants[0]
    owner = await _owner_of(app_engine, tenant)
    client_id = await _make_client(app_engine, tenant_id=tenant, owner=owner)

    base = datetime.now(UTC) - timedelta(days=3)
    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant)
        for offset in range(3):
            await record(
                session,
                tenant_id=tenant,
                client_id=client_id,
                event_type=TimelineEventType.NOTE_ADDED,
                summary="Note added",
                occurred_at=base + timedelta(hours=offset),
                actor_type=TimelineActorType.PRACTITIONER,
                actor_id=owner,
            )

    async with sessions() as session:
        await scope_to(await session.connection(), tenant)
        page = await read_timeline(session, tenant_id=tenant, client_id=client_id)

    moments = [entry.occurred_at for entry in page.items]
    assert moments == sorted(moments, reverse=True)


async def test_filtering_narrows_to_the_requested_types(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_timeline: None
) -> None:
    """FR-M1-019."""
    tenant = seeded_tenants[0]
    owner = await _owner_of(app_engine, tenant)
    client_id = await _make_client(app_engine, tenant_id=tenant, owner=owner)

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant)
        await add_note(
            session,
            tenant_id=tenant,
            client_id=client_id,
            body="A note",
            author=NoteAuthor(user_id=owner, role=UserRole.OWNER),
        )
        await change_stage(
            session,
            tenant_id=tenant,
            client_id=client_id,
            to_stage=ClientStage.CONTACTED,
            actor_user_id=owner,
            reason=None,
        )

    async with sessions() as session:
        await scope_to(await session.connection(), tenant)
        page = await read_timeline(
            session,
            tenant_id=tenant,
            client_id=client_id,
            event_types=frozenset({TimelineEventType.NOTE_ADDED}),
        )

    assert page.items
    assert all(entry.event_type is TimelineEventType.NOTE_ADDED for entry in page.items)


async def test_an_empty_filter_set_means_everything(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_timeline: None
) -> None:
    """⚠️ "No filters ticked" is what an untouched filter UI sends, and
    returning zero rows for it would look broken."""
    tenant = seeded_tenants[0]
    owner = await _owner_of(app_engine, tenant)
    client_id = await _make_client(app_engine, tenant_id=tenant, owner=owner)
    await _seed_entries(app_engine, tenant_id=tenant, client_id=client_id, count=2, actor=owner)

    sessions = _sessions(app_engine)
    async with sessions() as session:
        await scope_to(await session.connection(), tenant)
        page = await read_timeline(
            session, tenant_id=tenant, client_id=client_id, event_types=frozenset()
        )

    assert len(page.items) == 2


async def test_a_malformed_cursor_yields_the_first_page(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_timeline: None
) -> None:
    """⚠️ A stale bookmark is a client-side artefact.

    The useful response is the first page, not a 400 telling a practitioner
    their link is malformed.
    """
    tenant = seeded_tenants[0]
    owner = await _owner_of(app_engine, tenant)
    client_id = await _make_client(app_engine, tenant_id=tenant, owner=owner)
    await _seed_entries(app_engine, tenant_id=tenant, client_id=client_id, count=3, actor=owner)

    sessions = _sessions(app_engine)
    async with sessions() as session:
        await scope_to(await session.connection(), tenant)
        page = await read_timeline(
            session, tenant_id=tenant, client_id=client_id, cursor="not-a-cursor"
        )

    assert len(page.items) == 3


async def test_an_over_large_limit_is_clamped_not_refused(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_timeline: None
) -> None:
    """A caller asking for 500 wants "as many as you'll give me"."""
    tenant = seeded_tenants[0]
    owner = await _owner_of(app_engine, tenant)
    client_id = await _make_client(app_engine, tenant_id=tenant, owner=owner)
    await _seed_entries(app_engine, tenant_id=tenant, client_id=client_id, count=4, actor=owner)

    sessions = _sessions(app_engine)
    async with sessions() as session:
        await scope_to(await session.connection(), tenant)
        page = await read_timeline(session, tenant_id=tenant, client_id=client_id, limit=100_000)

    assert len(page.items) == 4


async def test_the_last_page_offers_no_cursor(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_timeline: None
) -> None:
    """`has_more` false is what stops the UI offering "load older" forever."""
    tenant = seeded_tenants[0]
    owner = await _owner_of(app_engine, tenant)
    client_id = await _make_client(app_engine, tenant_id=tenant, owner=owner)
    await _seed_entries(app_engine, tenant_id=tenant, client_id=client_id, count=2, actor=owner)

    sessions = _sessions(app_engine)
    async with sessions() as session:
        await scope_to(await session.connection(), tenant)
        page = await read_timeline(session, tenant_id=tenant, client_id=client_id, limit=25)

    assert page.next_cursor is None
    assert page.has_more is False


# ─── Isolation (AC-M0-003) ───────────────────────────────────────────────


async def test_a_tenant_cannot_read_another_tenants_timeline(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_timeline: None
) -> None:
    """🔒 AC-M0-003 applied to a table that restates six modules' data.

    A projection is exactly where a missing policy would be least noticed, so
    the isolation is asserted here rather than assumed from the pattern.
    """
    tenant_a, tenant_b = seeded_tenants[0], seeded_tenants[1]
    owner_a = await _owner_of(app_engine, tenant_a)
    client_id = await _make_client(app_engine, tenant_id=tenant_a, owner=owner_a)
    await _seed_entries(app_engine, tenant_id=tenant_a, client_id=client_id, count=2, actor=owner_a)

    sessions = _sessions(app_engine)
    async with sessions() as session:
        await scope_to(await session.connection(), tenant_b)
        page = await read_timeline(session, tenant_id=tenant_a, client_id=client_id)

    assert page.items == []
