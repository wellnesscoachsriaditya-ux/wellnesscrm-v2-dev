"""Who may see a client — the read that makes AC-M1-006 enforceable.

🔒 **FR-M0-017 / AC-M1-006.** An owner reaches every client in their tenant; a
practitioner reaches only those they own or have been granted (EC-M0-04). DB
§17.2 is explicit that this is *not* an RLS concern:

    RLS answers "which tenant?" — a coarse, universal boundary. Ownership
    answers "which practitioner, for this specific client, given assignments
    and role?" ... RLS is the tenant seatbelt; authz is the steering.

So the decision lives in ``kernel.authz.owner_or_assigned``, and this module's
job is to hand that policy the facts it needs. A policy is synchronous by design
— a predicate that queried the database would put I/O inside a permission check,
where a failure is an exception mid-decision rather than a denial — so the grants
are **loaded first and passed in** as a value.

⚠️ **Every client-bound route must go through :func:`load_for_access`.** A route
that loads a client with ``get_client`` and skips the authorization call is
reachable by any practitioner in the tenant. Nothing in this file can detect
that; ``tests/test_client_access.py`` asserts the route list instead.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.clients.models import Client, ClientAssignment
from app.modules.clients.service import get_client


@dataclass(frozen=True, slots=True)
class ClientAccessView:
    """A client, plus exactly the facts the ownership policy reasons about.

    🔒 Satisfies ``kernel.authz.Resource`` (``tenant_id``, ``owner_user_id``) and
    ``kernel.authz.SharedResource`` (``granted_user_ids``) structurally, without
    importing either — which is what lets the authorization matrix be a table
    test over fakes rather than an integration test.

    ⚠️ Deliberately **not** the ORM ``Client``. Handing the policy a live ORM
    object would let a lazy load fire inside an authorization decision, and would
    couple the permission rules to every column of the table.
    """

    id: uuid.UUID
    tenant_id: uuid.UUID
    owner_user_id: uuid.UUID
    #: 🔒 **Live grants only.** A revoked grant is history; including one would
    #: keep access alive after the owner removed it.
    granted_user_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)


async def load_grants(
    session: AsyncSession, *, tenant_id: uuid.UUID, client_id: uuid.UUID
) -> frozenset[uuid.UUID]:
    """Every practitioner with a live grant on this client — EC-M0-04.

    Served by ``uq_client_assignments__live``'s sibling index; the partial
    predicate means the index holds only rows that are still in force.
    """
    rows = await session.scalars(
        select(ClientAssignment.user_id).where(
            ClientAssignment.tenant_id == tenant_id,
            ClientAssignment.client_id == client_id,
            ClientAssignment.revoked_at.is_(None),
        )
    )
    return frozenset(rows)


async def load_for_access(
    session: AsyncSession, *, tenant_id: uuid.UUID, client_id: uuid.UUID
) -> tuple[Client, ClientAccessView]:
    """Load a client together with the grants that bear on access.

    Returns both the row and the view: the caller needs the row to do its work
    and the view to authorize it, and loading them separately would mean two
    round trips and a window in which they disagree.

    ⚠️ Returns rather than raising on a client belonging to another tenant — RLS
    has already made that row invisible, so the caller's ``NotFoundError`` is the
    correct and only outcome. See :func:`app.modules.clients.service.get_client`.

    Raises:
        NotFoundError: No such client in this tenant.
    """
    client = await get_client(session, tenant_id=tenant_id, client_id=client_id)
    grants = await load_grants(session, tenant_id=tenant_id, client_id=client.id)

    return client, ClientAccessView(
        id=client.id,
        tenant_id=client.tenant_id,
        owner_user_id=client.owner_user_id,
        granted_user_ids=grants,
    )
