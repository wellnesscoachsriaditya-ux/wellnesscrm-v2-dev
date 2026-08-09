"""Notes on a client — FR-M1-007, FR-M3-020.

🔒 **Never client-visible** (FR-M3-021). See ``models.ClientNote`` for the three
independent mechanisms that hold that; none of them is in this file, because a
rule enforced by application code is the one that eventually gets bypassed.

⚠️ Functions take an ``AsyncSession`` rather than opening one (ADR-04). The
transaction belongs to the request pipeline, so a note and its audit entry commit
together or not at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.collaboration import (
    assert_may_archive_note,
    assert_may_edit_note,
    validate_note_body,
)
from app.kernel.context import UserRole
from app.kernel.errors import NotFoundError, ValidationError
from app.kernel.events import publish
from app.kernel.timeline import ClientNoteAdded
from app.modules.clients.models import ClientNote
from app.modules.clients.service import now


@dataclass(frozen=True, slots=True)
class NoteAuthor:
    """Who is acting, for the rules that key off authorship.

    Bundled rather than passed as two arguments because
    :func:`~app.kernel.collaboration.assert_may_edit_note` and its archive
    counterpart need both, and a call site that supplied the id without the role
    would silently lose the owner's archive right.
    """

    user_id: uuid.UUID
    role: UserRole | None


async def list_notes(
    session: AsyncSession, *, tenant_id: uuid.UUID, client_id: uuid.UUID
) -> list[ClientNote]:
    """The client's note thread, newest first — FR-M1-007.

    🔒 Reverse-chronological because that is how a practitioner reads it before a
    consultation: the last thing said matters most. FR-M1-007's "appended
    chronologically" describes how notes accumulate, not the order they are read.

    Archived notes are excluded. Served by ``ix_client_notes__client_created``,
    whose partial predicate carries the same exclusion.
    """
    rows = await session.scalars(
        select(ClientNote)
        .where(
            ClientNote.tenant_id == tenant_id,
            ClientNote.client_id == client_id,
            ClientNote.archived_at.is_(None),
        )
        .order_by(ClientNote.created_at.desc())
    )
    return list(rows)


async def add_note(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    body: str,
    author: NoteAuthor,
) -> ClientNote:
    """Append a note — FR-M1-007.

    ⚠️ Does not verify the client exists. The caller has already loaded it to
    authorize the write (``access.load_for_access``), and re-reading it here
    would be a second query answering a question already answered — while the FK
    catches the case the caller somehow skipped it.
    """
    note = ClientNote(
        tenant_id=tenant_id,
        client_id=client_id,
        body=validate_note_body(body),
        author_user_id=author.user_id,
    )
    session.add(note)
    await session.flush()

    # 🔒 DDR-06 — published inside the caller's transaction, so the note and its
    # timeline entry commit together. ⚠️ The body is deliberately not carried:
    # `kernel.events` refuses prose, and the timeline records only that a note
    # exists (FR-M1-018), never what it says.
    await publish(
        ClientNoteAdded(
            client_id=client_id,
            tenant_id=tenant_id,
            note_id=note.id,
            author_user_id=author.user_id,
            created_at=note.created_at,
        ),
        session,
    )
    return note


async def get_note(
    session: AsyncSession, *, tenant_id: uuid.UUID, note_id: uuid.UUID
) -> ClientNote:
    """One note by id.

    Raises:
        NotFoundError: 🔒 Also when the note belongs to another tenant — RLS
            makes that indistinguishable from absent, which API §5.4 requires.
    """
    note = await session.scalar(
        select(ClientNote).where(ClientNote.tenant_id == tenant_id, ClientNote.id == note_id)
    )
    if note is None:
        raise NotFoundError(
            "That note could not be found.",
            action="It may have been removed. Reload the client to see the current notes.",
        )
    return note


async def edit_note(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    note_id: uuid.UUID,
    body: str,
    author: NoteAuthor,
) -> ClientNote:
    """Rewrite a note's body — FR-M3-020.

    🔒 Author only, including against the tenant owner. The note carries an
    author's name, so only that author may change what it says; the owner's
    remedy for a note that should not stand is to archive it, which is recorded
    as their action rather than disguised as the author's.

    ⚠️ The edit is audited by the pipeline through the action's declaration, not
    written here — FR-M3-020's "edits recorded in the audit log" is satisfied by
    the framework-written entry, which cannot be forgotten.

    Raises:
        NotFoundError: No such note in this tenant.
        ValidationError: The actor is not the author, or the body is invalid.
    """
    note = await get_note(session, tenant_id=tenant_id, note_id=note_id)
    assert_may_edit_note(
        author_user_id=note.author_user_id,
        actor_user_id=author.user_id,
        actor_role=author.role,
    )

    note.body = validate_note_body(body)
    note.updated_at = now()
    await session.flush()
    return note


async def archive_note(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    note_id: uuid.UUID,
    author: NoteAuthor,
) -> ClientNote:
    """Remove a note from the thread — a soft delete (DB §22.2).

    🔒 The author, or the tenant owner. Wider than editing on purpose: the owner
    is accountable for what the practice records and must be able to withdraw
    something posted in error, which is a different act from rewriting it.

    ⚠️ Archiving an already-archived note is refused rather than ignored, for the
    same reason as archiving a client: a success reported for work not done lets
    a double-submitted request claim an action it did not perform.

    Raises:
        NotFoundError: No such note in this tenant.
        ValidationError: The actor may not archive it, or it is already archived.
    """
    note = await get_note(session, tenant_id=tenant_id, note_id=note_id)
    assert_may_archive_note(
        author_user_id=note.author_user_id,
        actor_user_id=author.user_id,
        actor_role=author.role,
    )

    if note.archived_at is not None:
        raise ValidationError(
            "That note has already been removed.",
            action="Reload the client to see the current notes.",
        )

    moment = now()
    note.archived_at = moment
    note.updated_at = moment
    await session.flush()
    return note
