"""Writing and reading the timeline projection — DDR-06, FR-M1-018.

🔒 **The subscribers are the only writers.** Nothing calls :func:`record`
directly from a route: a timeline entry exists because something happened, and
coupling it to the HTTP layer would mean an event published from a job or a
future module silently produced no entry.

🔒 **Transactional subscribers, not deferred** (Arch §3.4). The entry and the
change that caused it commit together or not at all. The alternative — a queued
job — would make the timeline eventually consistent with the record it describes,
so a practitioner who changed a stage and immediately looked at the timeline
would see nothing and reasonably conclude the change failed. DDR-06 chose this
disposition explicitly, and `kernel.events` names it as the motivating case.

⚠️ **A handler that raises rolls back the user's action.** That is the accepted
cost of the guarantee above, and it is why every handler here does the minimum:
an INSERT with values already carried on the event. None of them reads another
table, so there is no lookup to fail and no lock to wait on.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.clients import ClientArchived, ClientRestored, ClientStageChanged
from app.kernel.events import subscribe
from app.kernel.timeline import (
    ClientAccessChanged,
    ClientNoteAdded,
    ClientOwnershipChanged,
    ClientTagsChanged,
    TimelineActorType,
    TimelineEventType,
    summarise,
    summarise_stage_change,
)
from app.modules.clients.models import TimelineEvent

#: 🔒 Provenance — DB §5.6 `source_module`. A literal rather than `__name__`,
#: which would embed a Python path in a data column and change under a refactor.
_SOURCE = "clients"

#: The default page size for a timeline read — API §6.1.
DEFAULT_LIMIT = 25

#: 🔒 The most a caller may ask for in one page (API §6.1). NFR-006 budgets
#: 800 ms for 20 events; 100 is the documented ceiling and stays comfortably
#: inside it on the covering index.
MAX_LIMIT = 100


# ─── Writing ─────────────────────────────────────────────────────────────


async def record(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    event_type: TimelineEventType,
    summary: str,
    occurred_at: datetime,
    actor_type: TimelineActorType,
    actor_id: uuid.UUID | None,
    source_record_id: uuid.UUID | None = None,
) -> TimelineEvent:
    """Append one entry.

    🔒 Takes an already-built ``summary`` rather than building one, so every
    caller goes through ``kernel.timeline``'s label functions and none can pass
    prose assembled locally.
    """
    entry = TimelineEvent(
        tenant_id=tenant_id,
        client_id=client_id,
        event_type=event_type,
        occurred_at=occurred_at,
        source_module=_SOURCE,
        source_record_id=source_record_id,
        summary=summary,
        actor_type=actor_type,
        actor_id=actor_id,
    )
    session.add(entry)
    await session.flush()
    return entry


def _actor(user_id: uuid.UUID | None) -> tuple[TimelineActorType, uuid.UUID | None]:
    """Classify who acted.

    ⚠️ A missing user id means the system acted — a retention sweep, a scheduled
    rule. It does **not** mean "a practitioner we failed to identify": every
    practitioner-initiated path carries the subject from the request context, so
    a ``None`` arriving here is genuinely unattended work.
    """
    if user_id is None:
        return TimelineActorType.SYSTEM, None
    return TimelineActorType.PRACTITIONER, user_id


# ─── Subscribers (DDR-06) ────────────────────────────────────────────────


async def on_stage_changed(event: ClientStageChanged, session: AsyncSession, /) -> None:
    """FR-M1-015 — the timeline's first real event.

    ⚠️ ``source_record_id`` is left NULL. The `client_stage_history` row has a
    composite identity rather than a single id the UI could link to, and a
    fabricated one would be a dead link.
    """
    actor_type, actor_id = _actor(event.changed_by_user_id)
    await record(
        session,
        tenant_id=event.tenant_id,
        client_id=event.client_id,
        event_type=TimelineEventType.STAGE_CHANGED,
        summary=summarise_stage_change(from_stage=event.from_stage, to_stage=event.to_stage),
        occurred_at=event.changed_at,
        actor_type=actor_type,
        actor_id=actor_id,
    )


async def on_archived(event: ClientArchived, session: AsyncSession, /) -> None:
    """FR-M1-010 — archiving is a timeline event, not a disappearance."""
    actor_type, actor_id = _actor(event.archived_by_user_id)
    await record(
        session,
        tenant_id=event.tenant_id,
        client_id=event.client_id,
        event_type=TimelineEventType.CLIENT_ARCHIVED,
        summary=summarise(TimelineEventType.CLIENT_ARCHIVED),
        occurred_at=event.archived_at,
        actor_type=actor_type,
        actor_id=actor_id,
    )


async def on_restored(event: ClientRestored, session: AsyncSession, /) -> None:
    """EC-M1-02 — and the restore is recorded as its own event.

    ⚠️ The archive row is left standing rather than removed. "Archived in March,
    restored in April" is the history; a timeline that erased the archive would
    make the restore inexplicable.
    """
    actor_type, actor_id = _actor(event.restored_by_user_id)
    await record(
        session,
        tenant_id=event.tenant_id,
        client_id=event.client_id,
        event_type=TimelineEventType.CLIENT_RESTORED,
        summary=summarise(TimelineEventType.CLIENT_RESTORED),
        occurred_at=event.restored_at,
        actor_type=actor_type,
        actor_id=actor_id,
    )


async def on_note_added(event: ClientNoteAdded, session: AsyncSession, /) -> None:
    """FR-M1-007 — that a note exists, never what it says.

    🔒 ``summary`` is the fixed string "Note added". The body is not carried on
    the event (``kernel.events`` would refuse it) and is not read here. A
    practitioner opens the note to read it, under the authorization that governs
    notes; the timeline says only that there is one.
    """
    await record(
        session,
        tenant_id=event.tenant_id,
        client_id=event.client_id,
        event_type=TimelineEventType.NOTE_ADDED,
        summary=summarise(TimelineEventType.NOTE_ADDED),
        occurred_at=event.created_at,
        actor_type=TimelineActorType.PRACTITIONER,
        actor_id=event.author_user_id,
        source_record_id=event.note_id,
    )


async def on_tags_changed(event: ClientTagsChanged, session: AsyncSession, /) -> None:
    """FR-M1-008 — one summary for both directions.

    ⚠️ "Tags updated" rather than "PCOS applied". A tag name in a timeline row
    would outlive the tag being renamed or retired, and a row naming a tag that
    no longer exists reads as corruption. The tag id is kept so a deep link
    resolves while it does.
    """
    actor_type, actor_id = _actor(event.actor_user_id)
    await record(
        session,
        tenant_id=event.tenant_id,
        client_id=event.client_id,
        event_type=TimelineEventType.TAG_APPLIED,
        summary=summarise(TimelineEventType.TAG_APPLIED),
        occurred_at=event.changed_at,
        actor_type=actor_type,
        actor_id=actor_id,
        source_record_id=event.tag_id,
    )


async def on_ownership_changed(event: ClientOwnershipChanged, session: AsyncSession, /) -> None:
    """EC-M1-04 — a handover is a care fact, not only an admin one."""
    actor_type, actor_id = _actor(event.actor_user_id)
    await record(
        session,
        tenant_id=event.tenant_id,
        client_id=event.client_id,
        event_type=TimelineEventType.OWNERSHIP_CHANGED,
        summary=summarise(TimelineEventType.OWNERSHIP_CHANGED),
        occurred_at=event.changed_at,
        actor_type=actor_type,
        actor_id=actor_id,
        source_record_id=event.to_user_id,
    )


async def on_access_changed(event: ClientAccessChanged, session: AsyncSession, /) -> None:
    """EC-M0-04 / EC-M1-04 — when a colleague joined or left the care of a client."""
    actor_type, actor_id = _actor(event.actor_user_id)
    await record(
        session,
        tenant_id=event.tenant_id,
        client_id=event.client_id,
        event_type=TimelineEventType.ACCESS_CHANGED,
        summary=summarise(TimelineEventType.ACCESS_CHANGED),
        occurred_at=event.changed_at,
        actor_type=actor_type,
        actor_id=actor_id,
        source_record_id=event.grantee_user_id,
    )


def register_subscribers() -> None:
    """Wire the DDR-06 subscribers.

    🔒 Called from the entry points rather than at import time. Import-time
    subscription would make the handler set depend on which modules a test
    happened to import, and `kernel.events.reset_subscriptions` exists precisely
    because the registry is process-global.

    ⚠️ Idempotent — ``subscribe`` dedupes by handler identity, so calling this
    twice (web and worker in one process, or a test re-registering) does not
    double-write every entry.
    """
    subscribe(ClientStageChanged, transactional=on_stage_changed)
    subscribe(ClientArchived, transactional=on_archived)
    subscribe(ClientRestored, transactional=on_restored)
    subscribe(ClientNoteAdded, transactional=on_note_added)
    subscribe(ClientTagsChanged, transactional=on_tags_changed)
    subscribe(ClientOwnershipChanged, transactional=on_ownership_changed)
    subscribe(ClientAccessChanged, transactional=on_access_changed)


# ─── Reading (FR-M1-018, ADR-A05) ────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TimelineCursor:
    """Where a page resumes — ADR-A05.

    🔒 **Two fields, because ``occurred_at`` alone is not unique.** A stage
    change and the events it triggers commit in one transaction and can share a
    timestamp to the microsecond. A cursor keyed on time alone would skip or
    repeat whatever else shared that instant, and the bug would appear only on
    clients busy enough for a second page — i.e. never in testing.
    """

    occurred_at: datetime
    event_id: uuid.UUID

    def encode(self) -> str:
        """Opaque to the caller (ADR-A05), trivially decodable by us.

        ⚠️ Not encrypted and not signed. A cursor names a position in the
        caller's *own* tenant-scoped, authorization-checked result set — RLS and
        `owner_or_assigned` both run again on the next page, so a forged cursor
        buys nothing but a different offset into data the caller may already
        read.
        """
        return f"{self.occurred_at.isoformat()}|{self.event_id}"

    @classmethod
    def decode(cls, raw: str) -> TimelineCursor | None:
        """Parse a cursor, or ``None`` if it is unusable.

        ⚠️ Returns ``None`` rather than raising. A stale or malformed cursor is
        a client-side artefact — a bookmarked URL, a truncated copy-paste — and
        the useful response is the first page, not a 400 telling a practitioner
        their link is malformed.
        """
        moment, _, identifier = raw.partition("|")
        if not moment or not identifier:
            return None
        try:
            return cls(occurred_at=datetime.fromisoformat(moment), event_id=uuid.UUID(identifier))
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class TimelinePage:
    """One page of timeline entries, plus where to resume."""

    items: list[TimelineEvent]
    next_cursor: str | None

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None


def _apply_cursor(
    statement: Select[tuple[TimelineEvent]], cursor: TimelineCursor
) -> Select[tuple[TimelineEvent]]:
    """Resume strictly after the cursor's row, in the index's own order.

    🔒 The row comparison ``(occurred_at, id) < (…, …)`` is what makes this
    correct *and* fast: it matches the index's column order exactly, so
    PostgreSQL seeks rather than scans, and it treats the pair as one ordering
    key so ties cannot straddle a page boundary.

    ⚠️ ``tuple_()`` rather than a Python tuple. A bare ``(a, b) < (c, d)`` on ORM
    columns compares the *tuples* in Python — which is a type error here, and
    would silently be the wrong query if it type-checked. This emits SQL's own
    row constructor, which is the thing PostgreSQL can satisfy from the index.
    """
    return statement.where(
        tuple_(TimelineEvent.occurred_at, TimelineEvent.id) < (cursor.occurred_at, cursor.event_id)
    )


async def read_timeline(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    event_types: frozenset[TimelineEventType] | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> TimelinePage:
    """A page of a client's timeline, newest first — FR-M1-018, NFR-006.

    Args:
        event_types: FR-M1-019's filter. ``None`` means everything; an empty set
            is treated the same way, because "filter by nothing" is what an
            unticked filter UI sends and returning zero rows for it would look
            broken.
        cursor: Opaque, from a previous page. Ignored if unparseable.
        limit: Clamped to :data:`MAX_LIMIT` rather than rejected — a caller
            asking for 500 wants "as many as you'll give me", and a 400 here
            would be pedantry with a worse outcome.

    ⚠️ **Authorization is the caller's job.** This function applies the tenant
    scope only. Every route reaching it must have gone through
    ``access.load_for_access`` first, or a practitioner would read the timeline
    of a client they cannot open — the AC-M1-006 leak through a new door.
    """
    effective_limit = max(1, min(limit, MAX_LIMIT))

    statement = select(TimelineEvent).where(
        TimelineEvent.tenant_id == tenant_id,
        TimelineEvent.client_id == client_id,
    )

    if event_types:
        statement = statement.where(TimelineEvent.event_type.in_(event_types))

    if cursor is not None:
        decoded = TimelineCursor.decode(cursor)
        if decoded is not None:
            statement = _apply_cursor(statement, decoded)

    # 🔒 Ordered by the index's exact key, including the `id` tiebreaker. Sorting
    # by `occurred_at` alone would let two rows sharing a timestamp swap places
    # between requests, which is how a paginating client sees one row twice.
    statement = statement.order_by(TimelineEvent.occurred_at.desc(), TimelineEvent.id.desc())

    # One extra row answers "is there another page" without a COUNT — API §6.1
    # omits `total` by default for exactly this reason.
    rows = list(await session.scalars(statement.limit(effective_limit + 1)))

    if len(rows) > effective_limit:
        last = rows[effective_limit - 1]
        return TimelinePage(
            items=rows[:effective_limit],
            next_cursor=TimelineCursor(occurred_at=last.occurred_at, event_id=last.id).encode(),
        )

    return TimelinePage(items=rows, next_cursor=None)
