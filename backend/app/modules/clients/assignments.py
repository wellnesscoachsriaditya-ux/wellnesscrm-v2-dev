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
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.collaboration import assert_grant_is_meaningful, may_manage_access
from app.kernel.context import UserRole
from app.kernel.errors import AuthorizationError, NotFoundError, ValidationError
from app.modules.clients.models import ClientAssignment
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

    return GrantRecord(
        user_id=row.user_id,
        granted_by_user_id=row.granted_by_user_id,
        granted_at=row.granted_at,
        revoked_at=None,
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
    actor_role: UserRole | None,
) -> None:
    """Move a client to a different owning practitioner — EC-M1-04.

    🔒 Owner-only, for the same reason as granting: it changes who is accountable
    for a client, and FR-M0-017 makes that the owner's decision.

    ⚠️ **Single client, not bulk.** EC-M1-04 describes reassigning a departing
    practitioner's caseload *in bulk*; that needs a selection UI and a
    partial-failure story — what happens when 40 of 50 succeed — and neither
    exists yet. One at a time is correct and useful now; bulk is a Slice E
    concern, alongside the list that would drive the selection.

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

    client.owner_user_id = new_owner_user_id
    client.updated_at = now()
    await session.flush()
