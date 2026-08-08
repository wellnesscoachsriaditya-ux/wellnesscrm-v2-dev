"""The ``ClientDirectory`` implementation — how other modules see clients.

🔒 DB §5 ("Readers: via kernel ports"), Arch R3/R6. Five modules need client
identity and stage, and none of them may import this module or read its tables.
This class is the sanctioned seam: the kernel declares the protocol, this
satisfies it, and the entry point wires the two together.

⚠️ **Read-only.** ``kernel.clients.ClientDirectory`` declares no write method and
this adds none. Every write to ``clients`` goes through ``service.py``, called by
the ``clients`` router — DB §5's "Writers: `clients` only", made structural.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.clients import ClientIdentity
from app.modules.clients.models import Client
from app.modules.clients.queries import count_active_clients


def _identity(row: Client) -> ClientIdentity:
    """Project a persisted row onto the thin view other modules get.

    🔒 A projection rather than the ORM object. Handing another module a
    ``Client`` would give it every column, lazy loading included — and the fields
    it came to depend on would become fields this module could not change.
    """
    return ClientIdentity(
        id=row.id,
        tenant_id=row.tenant_id,
        full_name=row.full_name,
        stage=row.stage,
        owner_user_id=row.owner_user_id,
        is_archived=row.archived_at is not None,
        dietary_class=row.dietary_class,
        preferred_language=row.preferred_language,
    )


class ClientRepositoryDirectory:
    """Satisfies ``kernel.clients.ClientDirectory`` against the real tables.

    ⚠️ Every method takes the caller's session, so reads run inside the caller's
    transaction and under the tenant scope it established. A directory holding
    its own engine would read outside that scope — which, with RLS forced, means
    reading nothing at all and reporting it as "no such client".
    """

    async def find(
        self, session: AsyncSession, /, *, tenant_id: uuid.UUID, client_id: uuid.UUID
    ) -> ClientIdentity | None:
        """One client by id, or ``None``.

        ⚠️ ``None`` rather than raising: a caller asking "does this client exist"
        is not in an error state when the answer is no. The HTTP layer turns
        absence into a 404; a module deciding whether to send a message turns it
        into "skip".
        """
        row = await session.scalar(
            select(Client).where(Client.tenant_id == tenant_id, Client.id == client_id)
        )
        return _identity(row) if row is not None else None

    async def find_by_mobile(
        self, session: AsyncSession, /, *, tenant_id: uuid.UUID, mobile: str
    ) -> list[ClientIdentity]:
        """Every non-archived client on this number — EC-M2-02.

        🔒 A list, because EC-M1-01 permits family members sharing a handset and
        DB §5.1 therefore has no unique constraint. Returning one record would
        force an arbitrary choice between two real people.

        ⚠️ Archived clients are excluded. A resubmitted enquiry should create a
        fresh lead rather than silently attach to a record the practitioner
        archived — restoring that record is a decision, not a side effect.
        """
        rows = await session.scalars(
            select(Client)
            .where(
                Client.tenant_id == tenant_id,
                Client.mobile == mobile,
                Client.archived_at.is_(None),
            )
            .order_by(Client.created_at)
        )
        return [_identity(row) for row in rows]

    async def count_active(self, session: AsyncSession, /, *, tenant_id: uuid.UUID) -> int:
        """Clients consuming the entitlement — M1.5.

        🔒 Delegates to ``queries.count_active_clients`` rather than restating
        DB §5.2's predicate. The transition path enforces the same limit
        (FR-M1-002), and two copies of a billing predicate is two things that can
        drift — the drift being a practitioner billed for a client their list does
        not show.
        """
        return await count_active_clients(session, tenant_id=tenant_id)
