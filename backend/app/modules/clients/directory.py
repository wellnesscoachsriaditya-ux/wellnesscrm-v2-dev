"""The ``ClientDirectory`` and ``ClientIntake`` implementations.

🔒 DB §5 ("Readers: via kernel ports"), Arch R3/R6. Five modules need client
identity and stage, and none of them may import this module or read its tables.
These classes are the sanctioned seam: the kernel declares the protocols, these
satisfy them, and the entry point wires them together.

⚠️ **The directory is read-only.** ``kernel.clients.ClientDirectory`` declares no
write method and this adds none. The one write another module may cause is
:meth:`ClientRepositoryIntake.create_lead` — a *separate* port, wired only to
``leads``, that calls ``service.create_client`` rather than reaching the table
itself. DB §5's "Writers: `clients` only" holds either way.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.clients import ClientIdentity, ClientStage, LeadIntake
from app.kernel.context import UserRole as ContextUserRole
from app.kernel.errors import NotFoundError

# ⚠️ `UserRole` from `kernel.models`, not `kernel.context`. Both exist: the
# context one is the request-scoped role a token carries, this one is the
# database enum `users.role` is mapped to. Comparing a column against the wrong
# enum type is accepted by SQLAlchemy and matches nothing at runtime.
from app.kernel.models import User, UserRole, UserStatus
from app.modules.clients.models import Client, ClientAssignment
from app.modules.clients.queries import count_active_clients
from app.modules.clients.service import ClientCreate, create_client


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

    async def find_many(
        self, session: AsyncSession, /, *, tenant_id: uuid.UUID, client_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, ClientIdentity]:
        """Several clients at once — the list-projection read.

        🔒 One query regardless of how many ids are asked for. The alternative a
        caller reaches for without this — a join onto ``clients`` from their own
        module — is the R6 violation the port exists to make unnecessary.

        ⚠️ Ids that resolve to nothing are simply absent from the mapping.
        Archived clients *are* returned: a row referencing one still needs to
        render it, and hiding it here would make the reference look broken.
        """
        if not client_ids:
            # ⚠️ Short-circuited: `IN ()` is a syntax error in some dialects and
            # a full scan in others.
            return {}

        rows = await session.scalars(
            select(Client).where(Client.tenant_id == tenant_id, Client.id.in_(client_ids))
        )
        return {row.id: _identity(row) for row in rows}

    def visible_client_ids(
        self, *, actor_user_id: uuid.UUID, role: ContextUserRole | None
    ) -> Select[tuple[uuid.UUID]]:
        """🔒 The client ids this practitioner may see, as an embeddable SELECT.

        **The same rule as ``discovery._visible_to``, in a form another module can
        use.** That function narrows a statement already selecting ``clients``;
        this returns a standalone id query for a module whose rows merely
        *reference* a client. Both must agree — an enquiry visible when its client
        is not would be the AC-M1-006 leak arriving through a new door.

        🔒 An owner's query carries no user predicate (FR-M0-017). It is still
        tenant-scoped: RLS applies to a subquery exactly as it does to a
        top-level one, so this cannot reach another tenant's clients even though
        no ``tenant_id`` appears below.

        ⚠️ ``EXISTS`` rather than a join to ``client_assignments``, for the reason
        ``_visible_to`` gives: a join multiplies rows for a client with several
        grants, and the duplicates would then need a DISTINCT that defeats the
        caller's keyset cursor.
        """
        statement = select(Client.id)
        if role is ContextUserRole.OWNER:
            return statement

        granted = select(1).where(
            ClientAssignment.client_id == Client.id,
            ClientAssignment.user_id == actor_user_id,
            ClientAssignment.revoked_at.is_(None),
        )
        return statement.where(
            or_(Client.owner_user_id == actor_user_id, granted.exists()),
        )


class ClientRepositoryIntake:
    """Satisfies ``kernel.clients.ClientIntake`` — the enquiry's way in.

    🔒 **The write still happens inside this module** (DB §5: "Writers: `clients`
    only"). ``leads`` calls a protocol the kernel declares; what runs is
    ``service.create_client``, the same function the practitioner's own create
    route uses. That is what makes this a call into the owner rather than a way
    around it — there is no second INSERT path into ``clients``, and the opening
    ``client_stage_history`` row (FR-M1-015) is written by the same code that
    always writes it.

    ⚠️ **Separate from :class:`ClientRepositoryDirectory`, deliberately.** That
    class is read-only and its docstring says so; folding a create method into it
    would hand every one of the five modules that read the directory the ability
    to write clients. Only ``leads`` is wired to this one.
    """

    async def create_lead(
        self,
        session: AsyncSession,
        /,
        *,
        tenant_id: uuid.UUID,
        intake: LeadIntake,
    ) -> ClientIdentity:
        """Create a client at stage ``lead`` — FR-M2-005.

        🔒 **Stage is hardcoded, not passed.** ``LeadIntake`` carries no stage
        field and this supplies :attr:`ClientStage.LEAD` literally, so no caller
        can reach the metered ``active`` stage through the public enquiry path
        (FR-M1-003, EC-M2-06). ``create_client`` skips the entitlement check
        entirely for any stage but ``active``, so a tenant at their limit still
        accepts enquiries.

        🔒 ``actor_user_id=None`` — an enquiry is system-driven. The history row
        records no user, which is what distinguishes "a prospect submitted this"
        from "a practitioner added them" in the conversion metrics (FR-M9-006)
        and on the timeline.
        """
        owner_user_id = await _account_owner_id(session, tenant_id=tenant_id)

        client = await create_client(
            session,
            tenant_id=tenant_id,
            payload=ClientCreate(
                full_name=intake.full_name,
                owner_user_id=owner_user_id,
                mobile=intake.mobile,
                email=intake.email,
                stage=ClientStage.LEAD,
                source=intake.source,
                source_detail=intake.source_detail,
            ),
            actor_user_id=None,
        )
        return _identity(client)


async def _account_owner_id(session: AsyncSession, *, tenant_id: uuid.UUID) -> uuid.UUID:
    """The user a lead with no chosen practitioner belongs to — FR-M1-009.

    🔒 The tenant's **account owner**, and the choice is load-bearing rather than
    arbitrary. ``clients.owner_user_id`` is NOT NULL and a prospect cannot name a
    practitioner, so something must decide. The owner is the one role guaranteed
    to exist for every tenant (registration creates it) and the one FR-M0-017
    already gives a view of the whole tenant — so a captured lead is never
    invisible to the person who has to act on it (US-M2-03). Assigning to an
    arbitrary practitioner would put new enquiries in a caseload nobody agreed
    to, and under AC-M1-006 the owner would still see them anyway.

    ⚠️ Active owners only. A deactivated owner's account is one nobody is
    reading, and FR-M1-009's "every client has an owning practitioner" means a
    real one.

    Raises:
        NotFoundError: If the tenant has no active owner. 🔒 A tenant in that
            state cannot own a client at all — the enquiry is refused rather than
            assigned to a departed account. The caller must not report this to
            the submitter as their mistake.
    """
    owner_id = await session.scalar(
        select(User.id)
        .where(
            User.tenant_id == tenant_id,
            User.role == UserRole.OWNER,
            User.status == UserStatus.ACTIVE,
        )
        .order_by(User.created_at)
        .limit(1)
    )
    if owner_id is None:
        raise NotFoundError(
            "This practice is not accepting enquiries right now.",
            action="Please contact the practice directly.",
        )
    return owner_id
