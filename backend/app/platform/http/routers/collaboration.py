"""Notes, tags and shared access — API §7.1.

🔒 Lives in ``platform/`` for the same reason as ``routers/clients.py``: Arch R5
forbids a module importing ``platform``, and a router needs ``realm_router``, the
session and the audit hook. The module owns the domain logic and the tables; this
owns the HTTP shape.

🔒 **Every client-bound route here authorizes against the client**, not merely
against the action. A practitioner who may not read a client must not be able to
read the notes written about them — that would be the AC-M1-006 leak arriving
through a different door. ``_authorized_client`` is the single seam, shared with
``routers/clients.py``.

⚠️ **The tag vocabulary is not client-bound.** ``/app/tags`` is the tenant's own
list, so it carries no client scope and is declared ``TENANT_METADATA`` — a tag
name is the practice's vocabulary, not a fact about a person.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import Request, Response, status
from pydantic import BaseModel, Field

from app.kernel.collaboration import MAX_NOTE_LENGTH, MAX_TAG_NAME_LENGTH, TagColour
from app.kernel.context import get_context
from app.modules.clients import (
    CLIENT_MANAGE_ACCESS,
    CLIENT_MANAGE_TAGS,
    CLIENT_READ_ACCESS,
    CLIENT_READ_NOTES,
    CLIENT_WRITE_NOTE,
    TAG_MANAGE,
    TAG_READ,
    NoteAuthor,
    add_note,
    archive_note,
    archive_tag,
    attach_tag,
    create_tag,
    detach_tag,
    edit_note,
    grant_access,
    list_client_tags,
    list_grants,
    list_notes,
    list_tags,
    reassign_owner,
    revoke_access,
)
from app.platform.http.authz import requires
from app.platform.http.pipeline import get_session, realm_router, record_audit
from app.platform.http.routers.clients import authorized_client

client_router = realm_router("/api/v1/app/clients", tags=["collaboration"])
tag_router = realm_router("/api/v1/app/tags", tags=["collaboration"])


# ─── Schemas ─────────────────────────────────────────────────────────────


class NoteResponse(BaseModel):
    """One note — FR-M1-007, "with timestamp and author"."""

    id: uuid.UUID
    client_id: uuid.UUID
    body: str
    author_user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class NoteWriteRequest(BaseModel):
    body: str = Field(min_length=1, max_length=MAX_NOTE_LENGTH)


class TagResponse(BaseModel):
    id: uuid.UUID
    name: str
    colour: TagColour


class TagCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_TAG_NAME_LENGTH)
    colour: TagColour = TagColour.SLATE


class GrantResponse(BaseModel):
    """One access grant — EC-M0-04.

    ⚠️ Identifiers only, no names. Resolving a user id to a name is the caller's
    job through the team endpoint; embedding it here would put a second copy of a
    person's name in a response that is already about access control (NFR-033).
    """

    user_id: uuid.UUID
    granted_by_user_id: uuid.UUID
    granted_at: datetime
    revoked_at: datetime | None
    is_live: bool


class GrantRequest(BaseModel):
    user_id: uuid.UUID


class ReassignRequest(BaseModel):
    owner_user_id: uuid.UUID


def _note(row: object) -> NoteResponse:
    return NoteResponse.model_validate(row, from_attributes=True)


def _tag(row: object) -> TagResponse:
    return TagResponse.model_validate(row, from_attributes=True)


def _author(request: Request) -> NoteAuthor:
    """Who is writing, with the role the archive rule needs."""
    actor = get_context().actor
    return NoteAuthor(user_id=actor.require_subject(), role=actor.role)


# ─── Notes (FR-M1-007, FR-M3-020) ────────────────────────────────────────


@client_router.get(
    "/{client_id}/notes",
    summary="List a client's notes",
    operation_id="clientNotesList",
)
@requires(CLIENT_READ_NOTES)
async def notes_list(request: Request, client_id: uuid.UUID) -> list[NoteResponse]:
    """The note thread, newest first — FR-M1-007.

    🔒 Never reachable from the client realm (FR-M3-021): this router is
    practitioner-only, the action permits practitioner roles alone, and
    ``client_notes`` has no client-realm RLS policy.
    """
    await authorized_client(request, client_id)
    rows = await list_notes(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        client_id=client_id,
    )
    return [_note(row) for row in rows]


@client_router.post(
    "/{client_id}/notes",
    status_code=status.HTTP_201_CREATED,
    summary="Add a note to a client",
    operation_id="clientNotesCreate",
)
@requires(CLIENT_WRITE_NOTE)
async def notes_create(
    request: Request, client_id: uuid.UUID, body: NoteWriteRequest
) -> NoteResponse:
    """Append a note — FR-M1-007."""
    await authorized_client(request, client_id)
    created = await add_note(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        client_id=client_id,
        body=body.body,
        author=_author(request),
    )
    record_audit(request, resource_id=created.id)
    return _note(created)


@client_router.patch(
    "/{client_id}/notes/{note_id}",
    summary="Edit a note",
    operation_id="clientNotesUpdate",
)
@requires(CLIENT_WRITE_NOTE)
async def notes_update(
    request: Request, client_id: uuid.UUID, note_id: uuid.UUID, body: NoteWriteRequest
) -> NoteResponse:
    """Rewrite a note's body — FR-M3-020.

    🔒 **Author only**, including against the tenant owner: the note carries an
    author's name, so only they may change what it says. The owner's remedy for a
    note that should not stand is to remove it, which is recorded as their act.

    🔒 The edit is audited by the pipeline, which is what satisfies FR-M3-020's
    "with edits recorded in the audit log" — a framework-written entry cannot be
    forgotten.
    """
    await authorized_client(request, client_id)
    updated = await edit_note(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        note_id=note_id,
        body=body.body,
        author=_author(request),
    )
    record_audit(request, resource_id=updated.id, changed_fields=["body"])
    return _note(updated)


@client_router.delete(
    "/{client_id}/notes/{note_id}",
    summary="Remove a note",
    operation_id="clientNotesArchive",
)
@requires(CLIENT_WRITE_NOTE)
async def notes_archive(request: Request, client_id: uuid.UUID, note_id: uuid.UUID) -> NoteResponse:
    """Take a note out of the thread — a soft delete (DB §22.2).

    ⚠️ ``DELETE`` in HTTP terms, ``archived_at`` in the database. The verb
    describes the caller's intent; nothing is destroyed, which is what AC-M1-007
    requires of every user-facing removal.
    """
    await authorized_client(request, client_id)
    archived = await archive_note(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        note_id=note_id,
        author=_author(request),
    )
    record_audit(request, resource_id=archived.id, changed_fields=["archived_at"])
    return _note(archived)


# ─── The tag vocabulary (FR-M1-008) ──────────────────────────────────────


@tag_router.get("", summary="List the tenant's tags", operation_id="tagsList")
@requires(TAG_READ)
async def tags_list(request: Request) -> list[TagResponse]:
    """Every live tag, alphabetical and case-insensitively ordered."""
    rows = await list_tags(get_session(request), tenant_id=get_context().actor.require_tenant())
    return [_tag(row) for row in rows]


@tag_router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a tag",
    operation_id="tagsCreate",
)
@requires(TAG_MANAGE)
async def tags_create(request: Request, body: TagCreateRequest) -> TagResponse:
    """Define a tag — FR-M1-008.

    🔒 Refuses a duplicate case-insensitively: a practitioner with "PCOS" who
    types "pcos" means the tag they already have, and a second one would split
    their caseload across two labels that look identical in a filter.
    """
    created = await create_tag(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        name=body.name,
        colour=body.colour,
    )
    record_audit(request, resource_id=created.id)
    return _tag(created)


@tag_router.delete("/{tag_id}", summary="Retire a tag", operation_id="tagsArchive")
@requires(TAG_MANAGE)
async def tags_archive(request: Request, tag_id: uuid.UUID) -> TagResponse:
    """Retire a tag — a soft delete.

    ⚠️ Leaves its applications in place, so a tag retired by mistake can be
    brought back with its clients intact. The name is released for reuse, because
    ``uq_tags__tenant_name`` is partial on the archive flag.
    """
    archived = await archive_tag(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        tag_id=tag_id,
    )
    record_audit(request, resource_id=archived.id, changed_fields=["archived_at"])
    return _tag(archived)


# ─── Applying tags to a client ───────────────────────────────────────────


@client_router.get(
    "/{client_id}/tags",
    summary="List a client's tags",
    operation_id="clientTagsList",
)
@requires(CLIENT_MANAGE_TAGS)
async def client_tags_list(request: Request, client_id: uuid.UUID) -> list[TagResponse]:
    await authorized_client(request, client_id)
    rows = await list_client_tags(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        client_id=client_id,
    )
    return [_tag(row) for row in rows]


@client_router.put(
    "/{client_id}/tags/{tag_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Apply a tag to a client",
    operation_id="clientTagsAttach",
)
@requires(CLIENT_MANAGE_TAGS)
async def client_tags_attach(request: Request, client_id: uuid.UUID, tag_id: uuid.UUID) -> Response:
    """Apply a tag — idempotent, and ``PUT`` for exactly that reason.

    Applying a label twice is indistinguishable from applying it once, so the
    second call has nothing to report. Refusing it would make the UI's tag toggle
    behave differently depending on how fast the practitioner clicks.
    """
    await authorized_client(request, client_id)
    actor = get_context().actor
    await attach_tag(
        get_session(request),
        tenant_id=actor.require_tenant(),
        client_id=client_id,
        tag_id=tag_id,
        actor_user_id=actor.subject_id,
    )
    record_audit(request, resource_id=client_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@client_router.delete(
    "/{client_id}/tags/{tag_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a tag from a client",
    operation_id="clientTagsDetach",
)
@requires(CLIENT_MANAGE_TAGS)
async def client_tags_detach(request: Request, client_id: uuid.UUID, tag_id: uuid.UUID) -> Response:
    """Remove a tag — a real delete, and the one place in the module that is true.

    The junction row asserts "this client carries this label"; withdrawn, it
    records nothing that happened. Compare ``client_assignments``, revoked rather
    than deleted precisely because *it* records a decision.
    """
    await authorized_client(request, client_id)
    await detach_tag(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        client_id=client_id,
        tag_id=tag_id,
    )
    record_audit(request, resource_id=client_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ─── Shared access (EC-M0-04, EC-M1-04) ──────────────────────────────────


@client_router.get(
    "/{client_id}/access",
    summary="Who can see this client",
    operation_id="clientAccessList",
)
@requires(CLIENT_READ_ACCESS)
async def access_list(
    request: Request, client_id: uuid.UUID, include_revoked: bool = False
) -> list[GrantResponse]:
    """Grants on this client — EC-M0-04.

    ``include_revoked`` surfaces the history EC-M1-04 requires be retained. Off
    by default: a list mixing live and revoked rows invites a caller to forget
    the difference.
    """
    await authorized_client(request, client_id)
    grants = await list_grants(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        client_id=client_id,
        include_revoked=include_revoked,
    )
    return [
        GrantResponse(
            user_id=grant.user_id,
            granted_by_user_id=grant.granted_by_user_id,
            granted_at=grant.granted_at,
            revoked_at=grant.revoked_at,
            is_live=grant.is_live,
        )
        for grant in grants
    ]


@client_router.post(
    "/{client_id}/access",
    status_code=status.HTTP_201_CREATED,
    summary="Grant a colleague access to this client",
    operation_id="clientAccessGrant",
)
@requires(CLIENT_MANAGE_ACCESS)
async def access_grant(request: Request, client_id: uuid.UUID, body: GrantRequest) -> GrantResponse:
    """Share a client with a colleague — EC-M0-04.

    🔒 Owner-only, twice over: the action's role gate and the check inside
    ``assignments.grant_access``. Deliberately redundant, because a future route
    that forgot the declaration would otherwise widen access silently.
    """
    client = await authorized_client(request, client_id)
    actor = get_context().actor
    grant = await grant_access(
        get_session(request),
        tenant_id=actor.require_tenant(),
        client_id=client_id,
        owner_user_id=client.owner_user_id,
        grantee_user_id=body.user_id,
        actor_user_id=actor.require_subject(),
        actor_role=actor.role,
    )
    record_audit(
        request,
        resource_id=client_id,
        metadata={"grantee_user_id": str(body.user_id), "operation": "grant"},
    )
    return GrantResponse(
        user_id=grant.user_id,
        granted_by_user_id=grant.granted_by_user_id,
        granted_at=grant.granted_at,
        revoked_at=grant.revoked_at,
        is_live=grant.is_live,
    )


@client_router.delete(
    "/{client_id}/access/{user_id}",
    summary="Remove a colleague's access",
    operation_id="clientAccessRevoke",
)
@requires(CLIENT_MANAGE_ACCESS)
async def access_revoke(
    request: Request, client_id: uuid.UUID, user_id: uuid.UUID
) -> GrantResponse:
    """Withdraw access — EC-M1-04.

    🔒 Stamps ``revoked_at`` rather than deleting: "who could see this client
    last March" is a question a DPDP access request can ask, and a deleted row
    cannot answer it.
    """
    await authorized_client(request, client_id)
    actor = get_context().actor
    grant = await revoke_access(
        get_session(request),
        tenant_id=actor.require_tenant(),
        client_id=client_id,
        grantee_user_id=user_id,
        actor_user_id=actor.require_subject(),
        actor_role=actor.role,
    )
    record_audit(
        request,
        resource_id=client_id,
        metadata={"grantee_user_id": str(user_id), "operation": "revoke"},
    )
    return GrantResponse(
        user_id=grant.user_id,
        granted_by_user_id=grant.granted_by_user_id,
        granted_at=grant.granted_at,
        revoked_at=grant.revoked_at,
        is_live=grant.is_live,
    )


@client_router.post(
    "/{client_id}/owner",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Reassign the owning practitioner",
    operation_id="clientOwnerReassign",
)
@requires(CLIENT_MANAGE_ACCESS)
async def owner_reassign(request: Request, client_id: uuid.UUID, body: ReassignRequest) -> Response:
    """Move a client to a different owning practitioner — EC-M1-04.

    ⚠️ **One client at a time.** EC-M1-04 describes reassigning a departing
    practitioner's caseload in bulk; that needs a selection UI and an answer for
    what happens when 40 of 50 succeed. Slice E owns it, alongside the list that
    would drive the selection.
    """
    actor = get_context().actor
    await authorized_client(request, client_id)
    await reassign_owner(
        get_session(request),
        tenant_id=actor.require_tenant(),
        client_id=client_id,
        new_owner_user_id=body.owner_user_id,
        actor_role=actor.role,
    )
    record_audit(request, resource_id=client_id, changed_fields=["owner_user_id"])
    return Response(status_code=status.HTTP_204_NO_CONTENT)
