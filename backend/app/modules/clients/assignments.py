"""Shared access to a client — EC-M0-04, EC-M1-04, FR-M0-017.

🔒 **One owner, N grants.** ``clients.owner_user_id`` is the owning practitioner
and stays the single answer to "whose client is this"; this module manages the
*additional* access a clinic owner hands to a colleague. Modelling shared care as
a second owner, or as a list of equals, is what makes accountability unanswerable.

🔒 **Grants are revoked, never deleted** (EC-M1-04). "Who could see this client
last March" is a question a DPDP access request can ask, and a deleted row cannot
answer it.

⚠️ These writes change who can see a client, so they are the most
security-relevant operations in the module. The role gate is on the action
(``CLIENT_MANAGE_ACCESS``, owner-only) and restated here through
``kernel.collaboration.may_manage_access`` — belt and braces, because a future
router that forgot the declaration would otherwise widen access silently.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.collaboration import assert_grant_is_meaningful, may_manage_access
from app.kernel.context import UserRole
from app.kernel.discovery import MAX_BULK_REASSIGN
from app.kernel.errors import AuthorizationError, NotFoundError, ValidationError
from app.kernel.events import publish
from app.kernel.timeline import ClientAccessChanged, ClientOwnershipChanged
from app.modules.clients.models import Client, ClientAssignment
from app.modules.clients.service import get_client, now


@dataclass(frozen=True, slots=True)
class GrantRecord:
    """One grant, as the API reports it.

    A projection rather than the ORM row: the wire shape is stable while the
    table is free to change, and the revocation fields are collapsed into
    ``is_live`` so no caller has to re-derive the rule.
    """

    user_id: uuid.UUID
    granted_by_user_id: uuid.UUID
    granted_at: datetime
    revoked_at: datetime | None

    @property
    def is_live(self) -> bool:
        return self.revoked_at is None


def _assert_may_manage(actor_role: UserRole | None) -> None:
    """🔒 Owner-only. See ``kernel.collaboration.may_manage_access`` for why.

    Raises:
        AuthorizationError: 403 rather than 404. Unlike an unassigned *client*,
            the existence of the access surface is not a secret — the caller can
            already see this client, and the honest answer is that granting is
            the owner's job. A 404 here would send a practitioner looking for a
            bug that is not there.
    """
    if not may_manage_access(actor_role):
        raise AuthorizationError(
            message="Only the account owner can change who has access to a client.",
            action="Ask your account owner to grant or remove access.",
        )


async def list_grants(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    include_revoked: bool = False,
) -> list[GrantRecord]:
    """Grants on this client, newest first.

    ⚠️ Live grants only by default. The full history is available to the access
    screen (EC-M1-04) but is noise on every other read, and a list mixing live
    and revoked rows invites a caller to forget the difference.
    """
    statement = select(ClientAssignment).where(
        ClientAssignment.tenant_id == tenant_id,
        ClientAssignment.client_id == client_id,
    )
    if not include_revoked:
        statement = statement.where(ClientAssignment.revoked_at.is_(None))

    rows = await session.scalars(statement.order_by(ClientAssignment.granted_at.desc()))
    return [
        GrantRecord(
            user_id=row.user_id,
            granted_by_user_id=row.granted_by_user_id,
            granted_at=row.granted_at,
            revoked_at=row.revoked_at,
        )
        for row in rows
    ]


async def grant_access(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    grantee_user_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    actor_role: UserRole | None,
) -> GrantRecord:
    """Give a colleague access to a client — EC-M0-04.

    Raises:
        AuthorizationError: The actor is not the tenant owner.
        ValidationError: The grantee already owns the client, or already holds a
            live grant.
    """
    _assert_may_manage(actor_role)
    assert_grant_is_meaningful(owner_user_id=owner_user_id, grantee_user_id=grantee_user_id)

    row = ClientAssignment(
        tenant_id=tenant_id,
        client_id=client_id,
        user_id=grantee_user_id,
        granted_by_user_id=actor_user_id,
        granted_at=now(),
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        # 🔒 `uq_client_assignments__live` — at most one live grant per pair. The
        # concurrent double-grant lands here; a pre-check alone would be a race.
        if "uq_client_assignments__live" not in str(exc):
            raise
        raise ValidationError(
            "That practitioner already has access to this client.",
            action="No further action is needed.",
        ) from exc

    await _publish_access_change(
        session,
        tenant_id=tenant_id,
        client_id=client_id,
        grantee_user_id=grantee_user_id,
        granted=True,
        actor_user_id=actor_user_id,
        moment=row.granted_at,
    )

    return GrantRecord(
        user_id=row.user_id,
        granted_by_user_id=row.granted_by_user_id,
        granted_at=row.granted_at,
        revoked_at=None,
    )


async def _publish_access_change(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    grantee_user_id: uuid.UUID,
    granted: bool,
    actor_user_id: uuid.UUID,
    moment: datetime,
) -> None:
    """🔒 DDR-06 — a colleague joining or leaving is a care fact, not only a
    security one. A practitioner reading a client's history needs to know when
    somebody else gained access, or the notes from that period read as though
    they came from nowhere.
    """
    await publish(
        ClientAccessChanged(
            client_id=client_id,
            tenant_id=tenant_id,
            grantee_user_id=grantee_user_id,
            granted=granted,
            actor_user_id=actor_user_id,
            changed_at=moment,
        ),
        session,
    )


async def revoke_access(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    grantee_user_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    actor_role: UserRole | None,
) -> GrantRecord:
    """Withdraw a colleague's access — EC-M1-04.

    🔒 Stamps ``revoked_at`` rather than deleting the row, so the history of who
    could see this client survives the revocation.

    Raises:
        AuthorizationError: The actor is not the tenant owner.
        NotFoundError: There is no live grant to revoke.
    """
    _assert_may_manage(actor_role)

    row = await session.scalar(
        select(ClientAssignment).where(
            ClientAssignment.tenant_id == tenant_id,
            ClientAssignment.client_id == client_id,
            ClientAssignment.user_id == grantee_user_id,
            ClientAssignment.revoked_at.is_(None),
        )
    )
    if row is None:
        raise NotFoundError(
            "That practitioner does not have access to this client.",
            action="Reload the page to see who currently has access.",
        )

    moment = now()
    row.revoked_at = moment
    row.revoked_by_user_id = actor_user_id
    await session.flush()

    await _publish_access_change(
        session,
        tenant_id=tenant_id,
        client_id=client_id,
        grantee_user_id=grantee_user_id,
        granted=False,
        actor_user_id=actor_user_id,
        moment=moment,
    )

    return GrantRecord(
        user_id=row.user_id,
        granted_by_user_id=row.granted_by_user_id,
        granted_at=row.granted_at,
        revoked_at=moment,
    )


async def reassign_owner(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    new_owner_user_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    actor_role: UserRole | None,
) -> None:
    """Move a client to a different owning practitioner — EC-M1-04.

    🔒 Owner-only, for the same reason as granting: it changes who is accountable
    for a client, and FR-M0-017 makes that the owner's decision.

    ⚠️ Any live grant to the *new* owner becomes redundant but is deliberately
    left in place: revoking it here would erase a record of a decision somebody
    made (EC-M1-04), and it grants nothing the ownership does not already.

    Raises:
        AuthorizationError: The actor is not the tenant owner.
        ValidationError: The client already has that owner.
    """
    _assert_may_manage(actor_role)

    client = await get_client(session, tenant_id=tenant_id, client_id=client_id)
    if client.owner_user_id == new_owner_user_id:
        raise ValidationError(
            "That practitioner already owns this client.",
            action="Choose a different practitioner.",
        )

    await _reassign_one(
        session,
        client=client,
        tenant_id=tenant_id,
        new_owner_user_id=new_owner_user_id,
        actor_user_id=actor_user_id,
    )


async def _reassign_one(
    session: AsyncSession,
    *,
    client: Client,
    tenant_id: uuid.UUID,
    new_owner_user_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
) -> uuid.UUID:
    """Move one already-loaded client, and announce it. Returns the old owner.

    🔒 Extracted so :func:`reassign_owner` and :func:`bulk_reassign_owner` cannot
    drift. A bulk path that wrote the column without publishing would be a
    caseload handover invisible to every client's timeline — and the divergence
    would only surface as "the timeline missed something", long after.
    """
    # Captured before the write — the event carries where the client came *from*,
    # and reading it after the assignment would report the destination twice.
    previous_owner_user_id = client.owner_user_id

    client.owner_user_id = new_owner_user_id
    client.updated_at = now()
    await session.flush()

    # 🔒 DDR-06. Both ids ride the event: "who was this client moved away from"
    # is the question a handover audit actually asks, and the timeline is where
    # a practitioner looks for it first.
    await publish(
        ClientOwnershipChanged(
            client_id=client.id,
            tenant_id=tenant_id,
            from_user_id=previous_owner_user_id,
            to_user_id=new_owner_user_id,
            actor_user_id=actor_user_id,
            changed_at=client.updated_at,
        ),
        session,
    )
    return previous_owner_user_id


async def bulk_reassign_owner(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_ids: Sequence[uuid.UUID],
    new_owner_user_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    actor_role: UserRole | None,
) -> int:
    """Hand a departing practitioner's caseload to somebody else — EC-M1-04.

    Returns the number of clients actually moved.

    🔒 **All or nothing.** One transaction, so a failure on client 40 of 50 rolls
    back the first 39. The alternative — a partial success reporting which ids
    failed — sounds friendlier and is worse: a half-completed handover leaves the
    caseload split between two practitioners with no record of the intent, and the
    practitioner cannot tell whether re-running it is safe.

    🔒 Every id is authorized and confirmed to exist *before* anything is written.
    A missing id is a 404 for the whole request rather than a silent skip: the
    practitioner selected from a list, so an id that is not there means their view
    disagrees with the database, and quietly moving 49 of 50 hides that.

    ⚠️ Clients already owned by the target are skipped rather than refused. In a
    bulk selection that is not a mistake — it is an overlapping selection, and
    refusing the batch over it would make the operation unusable on exactly the
    "everything this person owns" case it exists for. The single-client path still
    raises, because there the same condition *is* the whole request.

    Raises:
        AuthorizationError: The actor is not the tenant owner.
        ValidationError: Nothing was selected, or more than `MAX_BULK_REASSIGN`.
        NotFoundError: An id names a client this tenant does not have.
    """
    _assert_may_manage(actor_role)

    # 🔒 Deduplicated before counting, so a UI that submits an id twice is not
    # refused for exceeding a limit it has not reached.
    unique_ids = list(dict.fromkeys(client_ids))
    if not unique_ids:
        raise ValidationError(
            "Select at least one client to reassign.",
            action="Choose clients from the list, then reassign.",
        )
    if len(unique_ids) > MAX_BULK_REASSIGN:
        raise ValidationError(
            f"Reassign at most {MAX_BULK_REASSIGN} clients at once.",
            action="Narrow the selection and repeat for the rest.",
        )

    # One query for the whole batch. `get_client` per id would be N round trips
    # to prove a precondition, on a path already holding a write transaction.
    clients = list(
        await session.scalars(
            select(Client).where(Client.tenant_id == tenant_id, Client.id.in_(unique_ids))
        )
    )
    if len(clients) != len(unique_ids):
        raise NotFoundError(
            "One of those clients could not be found.",
            action="Reload the list — someone may have archived or removed them.",
        )

    moved = 0
    for client in clients:
        if client.owner_user_id == new_owner_user_id:
            continue
        await _reassign_one(
            session,
            client=client,
            tenant_id=tenant_id,
            new_owner_user_id=new_owner_user_id,
            actor_user_id=actor_user_id,
        )
        moved += 1

    return moved
