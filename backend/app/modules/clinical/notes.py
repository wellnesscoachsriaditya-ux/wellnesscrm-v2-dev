"""Consultation notes — FR-M3-018…021, AC-M3-006.

🔒 **Never client-visible.** Three independent mechanisms hold that, and none of
them is in this file — the same disposition as ``client_notes`` (S2 Slice C):

1. `consultation_notes` has **no client-realm RLS policy at all** (migration
   0016). Absence is stronger than a condition.
2. The client portal's timeline projection (S6) must exclude the event type —
   DB §5.6 is explicit that even the *existence* of a note is a leak.
3. :class:`ConsultationNoteRecorded` is documented as practitioner-only.

⚠️ Functions take an ``AsyncSession`` rather than opening one (ADR-04).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.clinical import ConsultationNoteRecorded
from app.kernel.context import UserRole
from app.kernel.errors import ConflictError, NotFoundError, ValidationError
from app.kernel.events import publish
from app.modules.clinical.models import ConsultationNote

#: 🔒 The most text a note may carry. A note is meant to be short enough to
#: re-read before the next consultation; 10 000 characters is a few minutes of
#: dictation and no legitimate note runs longer.
MAX_NOTE_LENGTH = 10_000


def now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class NoteAuthor:
    """Who is writing, for the author-only edit rule — FR-M3-020."""

    user_id: uuid.UUID
    role: UserRole | None


def validate_body(body: str) -> str:
    stripped = body.strip()
    if not stripped:
        raise ValidationError(
            "A consultation note cannot be empty.",
            action="Write down what you discussed and agreed.",
        )
    if len(stripped) > MAX_NOTE_LENGTH:
        raise ValidationError(
            f"A consultation note must be under {MAX_NOTE_LENGTH} characters.",
            action="Split it into a couple of notes, or keep the summary shorter.",
        )
    return stripped


async def add(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    note_date: date,
    body: str,
    author: NoteAuthor,
) -> ConsultationNote:
    """Record a dated consultation note — FR-M3-018.

    ⚠️ ``note_date`` defaults to today at the call site, not here: an API that
    records a backdated note needs to say so, and silently overwriting the
    caller's date would make backdating impossible to audit.
    """
    if note_date > now().date():
        raise ValidationError(
            "A consultation note cannot be dated in the future.",
            action="Check the date and try again.",
        )
    note = ConsultationNote(
        tenant_id=tenant_id,
        client_id=client_id,
        note_date=note_date,
        body=validate_body(body),
        author_user_id=author.user_id,
    )
    session.add(note)
    await session.flush()

    await publish(
        ConsultationNoteRecorded(
            client_id=client_id,
            tenant_id=tenant_id,
            note_id=note.id,
            occurred_at=now(),
            actor_user_id=author.user_id,
        ),
        session,
    )
    return note


async def edit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    note_id: uuid.UUID,
    body: str,
    author: NoteAuthor,
) -> ConsultationNote:
    """Rewrite a note — FR-M3-020, author-only.

    🔒 **Edits are recorded in the audit log.** The router audits the write; this
    function does not need to know how. "Editable by their author" is the rule;
    FR-M0-033 (every write audited) is the pipeline's job.

    ⚠️ The owner may *not* edit another practitioner's note, exactly as with
    ``client_notes`` (FR-M3-020 names the author, not the practice).
    """
    note = await session.get(ConsultationNote, note_id)
    if note is None or note.tenant_id != tenant_id:
        raise NotFoundError(
            "That note could not be found.",
            action="Refresh the client's notes and try again.",
        )
    if note.author_user_id != author.user_id:
        raise ConflictError(
            "Only the practitioner who wrote this note can edit it.",
            action="Ask them to make the change, or write a follow-up note.",
        )
    if note.archived_at is not None:
        raise ConflictError(
            "This note has been archived.",
            action="Write a new note instead.",
        )
    note.body = validate_body(body)
    note.updated_at = now()
    await session.flush()
    return note


async def archive(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    note_id: uuid.UUID,
    author: NoteAuthor,
) -> ConsultationNote:
    """Hide a note — author, or the owner on their behalf.

    ⚠️ Archive, never delete: the grant revokes DELETE. A note is a record of a
    consultation that happened; removing it would erase what was discussed, and
    the audit log would keep an entry pointing at nothing.
    """
    note = await session.get(ConsultationNote, note_id)
    if note is None or note.tenant_id != tenant_id:
        raise NotFoundError(
            "That note could not be found.",
            action="Refresh the client's notes and try again.",
        )
    is_author = note.author_user_id == author.user_id
    is_owner = author.role is UserRole.OWNER
    if not (is_author or is_owner):
        raise ConflictError(
            "You can only archive notes you wrote.",
            action="Ask the note's author, or the account owner, to archive it.",
        )
    if note.archived_at is not None:
        return note
    note.archived_at = now()
    await session.flush()
    return note


async def list_for_client(
    session: AsyncSession, *, tenant_id: uuid.UUID, client_id: uuid.UUID
) -> list[ConsultationNote]:
    """The client's consultation notes, newest first — FR-M3-018.

    ⚠️ Archived notes are included; a practitioner needs to know a consultation
    happened even when its note was retired. The UI marks them.
    """
    statement = (
        select(ConsultationNote)
        .where(
            ConsultationNote.tenant_id == tenant_id,
            ConsultationNote.client_id == client_id,
        )
        .order_by(ConsultationNote.note_date.desc(), ConsultationNote.created_at.desc())
    )
    return list((await session.execute(statement)).scalars().all())
