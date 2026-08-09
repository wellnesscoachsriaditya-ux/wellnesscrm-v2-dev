"""Tags — the tenant's own vocabulary, and its application to clients.

🔒 DB §5.4, FR-M1-008. Two things with different lifetimes: a **tag** belongs to
the tenant and outlives any client that carries it, while a **client_tag** is the
assertion that one client carries one label. Creating a tag and applying it are
therefore separate operations, and deleting the application does not delete the
tag.

⚠️ Uniqueness is case-insensitive and enforced by a partial unique index
(migration 0011). ``kernel.collaboration.tag_match_key`` computes the same value
so the application's "does this already exist" and the database's constraint
cannot disagree — but the index is what makes it true under concurrency, and
:func:`create_tag` translates its violation rather than pre-checking alone.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.collaboration import TagColour, tag_match_key, validate_tag_name
from app.kernel.errors import NotFoundError, ValidationError
from app.modules.clients.models import ClientTag, Tag
from app.modules.clients.service import now


async def list_tags(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[Tag]:
    """Every live tag in the tenant, alphabetical.

    Ordered case-insensitively: a list where "PCOS" sorts before "anaemia"
    because of capitalisation is one a practitioner has to search rather than
    scan.
    """
    rows = await session.scalars(
        select(Tag)
        .where(Tag.tenant_id == tenant_id, Tag.archived_at.is_(None))
        .order_by(func.lower(Tag.name))
    )
    return list(rows)


async def create_tag(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    colour: TagColour = TagColour.SLATE,
) -> Tag:
    """Define a new tag — FR-M1-008.

    🔒 Refuses a duplicate under case-insensitive comparison. Checked here *and*
    caught from the index: the pre-check gives a message naming the existing tag,
    and the ``IntegrityError`` catch is what holds when two requests create the
    same tag at the same moment — a pre-check alone is a race with a nicer error.

    Raises:
        ValidationError: The name is empty, too long, or already in use.
    """
    validated = validate_tag_name(name)
    existing = await find_tag_by_name(session, tenant_id=tenant_id, name=validated)
    if existing is not None:
        raise ValidationError(
            f"You already have a tag called “{existing.name}”.",
            action="Use that one, or choose a different name.",
            details={"existing_tag_id": str(existing.id)},
        )

    tag = Tag(tenant_id=tenant_id, name=validated, colour=colour)
    session.add(tag)
    try:
        await session.flush()
    except IntegrityError as exc:
        # 🔒 The concurrent case the pre-check cannot cover. Surfaced as the same
        # validation error rather than a 500: the practitioner did nothing wrong
        # and the remedy is identical.
        if "uq_tags__tenant_name" not in str(exc):
            raise
        raise ValidationError(
            f"You already have a tag called “{validated}”.",
            action="Use that one, or choose a different name.",
        ) from exc
    return tag


async def find_tag_by_name(session: AsyncSession, *, tenant_id: uuid.UUID, name: str) -> Tag | None:
    """A live tag matching this name, case-insensitively.

    ⚠️ Compares on ``lower(name)`` to match ``uq_tags__tenant_name`` exactly. A
    comparison the index cannot serve would both miss the index and disagree with
    the constraint at the margins.
    """
    result: Tag | None = await session.scalar(
        select(Tag).where(
            Tag.tenant_id == tenant_id,
            func.lower(Tag.name) == tag_match_key(name),
            Tag.archived_at.is_(None),
        )
    )
    return result


async def get_tag(session: AsyncSession, *, tenant_id: uuid.UUID, tag_id: uuid.UUID) -> Tag:
    """One tag by id.

    Raises:
        NotFoundError: Absent, or another tenant's — indistinguishable by design.
    """
    tag = await session.scalar(select(Tag).where(Tag.tenant_id == tenant_id, Tag.id == tag_id))
    if tag is None:
        raise NotFoundError(
            "That tag could not be found.",
            action="Reload the page to see your current tags.",
        )
    return tag


async def archive_tag(session: AsyncSession, *, tenant_id: uuid.UUID, tag_id: uuid.UUID) -> Tag:
    """Retire a tag — a soft delete (DB §22.2).

    ⚠️ **Leaves its applications in place.** ``client_tags`` rows survive, so a
    tag archived by mistake can be restored with its assignments intact. The list
    query joins through to live tags only, so an archived tag simply stops
    appearing — and the name is released for reuse, because
    ``uq_tags__tenant_name`` is partial on ``archived_at IS NULL``.

    Raises:
        NotFoundError: No such tag in this tenant.
        ValidationError: It is already archived.
    """
    tag = await get_tag(session, tenant_id=tenant_id, tag_id=tag_id)
    if tag.archived_at is not None:
        raise ValidationError(
            "That tag has already been removed.",
            action="Reload the page to see your current tags.",
        )
    tag.archived_at = now()
    await session.flush()
    return tag


# ─── Applying tags to clients ────────────────────────────────────────────


async def list_client_tags(
    session: AsyncSession, *, tenant_id: uuid.UUID, client_id: uuid.UUID
) -> list[Tag]:
    """The live tags carried by one client.

    ⚠️ Joins to ``tags`` and filters archived ones out there. A client keeps the
    junction row for a retired tag (see :func:`archive_tag`), so reading the
    junction alone would show labels the tenant has withdrawn.
    """
    rows = await session.scalars(
        select(Tag)
        .join(ClientTag, ClientTag.tag_id == Tag.id)
        .where(
            ClientTag.tenant_id == tenant_id,
            ClientTag.client_id == client_id,
            Tag.archived_at.is_(None),
        )
        .order_by(func.lower(Tag.name))
    )
    return list(rows)


async def attach_tag(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    tag_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
) -> None:
    """Apply a tag to a client — idempotent.

    🔒 Idempotent by *silence* here, unlike archive, and the difference is
    deliberate: applying a label twice is indistinguishable in outcome from
    applying it once, so a second call has nothing to report and no state to
    corrupt. Refusing it would make the UI's "toggle a tag" behave differently
    depending on how fast the practitioner clicks.

    Raises:
        NotFoundError: No such tag in this tenant, or it is archived.
    """
    tag = await get_tag(session, tenant_id=tenant_id, tag_id=tag_id)
    if tag.archived_at is not None:
        raise NotFoundError(
            "That tag has been removed.",
            action="Choose a different tag, or create it again.",
        )

    existing = await session.scalar(
        select(ClientTag).where(ClientTag.client_id == client_id, ClientTag.tag_id == tag_id)
    )
    if existing is not None:
        return

    session.add(
        ClientTag(
            tenant_id=tenant_id,
            client_id=client_id,
            tag_id=tag_id,
            tagged_by_user_id=actor_user_id,
        )
    )
    await session.flush()


async def detach_tag(
    session: AsyncSession, *, tenant_id: uuid.UUID, client_id: uuid.UUID, tag_id: uuid.UUID
) -> None:
    """Remove a tag from a client — a real DELETE.

    🔒 Not a soft delete, and this is the one place in the module where that is
    true. The junction row asserts "this client carries this label"; withdrawn,
    it records nothing that happened. Compare ``client_assignments``, which is
    revoked rather than deleted precisely because *it* is a record of a decision.

    Idempotent for the same reason as :func:`attach_tag`.
    """
    await session.execute(
        delete(ClientTag).where(
            ClientTag.tenant_id == tenant_id,
            ClientTag.client_id == client_id,
            ClientTag.tag_id == tag_id,
        )
    )
