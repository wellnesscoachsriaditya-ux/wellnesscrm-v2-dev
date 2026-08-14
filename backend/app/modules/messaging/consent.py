"""Reading a recipient's consent position at dispatch — FR-M8-006, AC-M8-005.

⚠️ **This duplicates a query, not a rule.** ``platform.consent`` holds the same
reads, and R5 forbids a module importing it. What is *not* duplicated is the
decision: ``kernel.consent.derive_state`` remains the only definition of "what
is in force", and this module folds the ledger with it rather than expressing
the rule again in SQL. ``consent_records`` and ``consent_purposes`` are kernel
tables (DB §16), so reading them here crosses no module boundary.

🔒 **"Withdrawn", not "never granted" — and the distinction is deliberate.**
``kernel.consent.is_granted`` denies by default, which is right for a processing
activity that must prove a lawful basis before it starts. FR-M8-006 asks a
narrower question: messages "MUST NOT be sent where the recipient **has
withdrawn** the relevant consent", and AC-M8-005 says the same
("A client who withdraws messaging consent receives no further non-essential
messages").

Applying deny-by-default here would suppress every message to a client whose
practitioner recorded their consent on paper — which is most clients of a solo
Indian practice — and the suppression reason would read `consent_withdrawn` for
someone who withdrew nothing. That is a false record of a legal fact, which is
worse than the conservatism it buys. The consent *gate* for starting an
engagement lives where the engagement starts; this is the messaging half of it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.consent import LedgerEntry, PurposeState, derive_state
from app.kernel.models import ConsentPurpose, ConsentRecord, ConsentSubjectType


async def _purpose_id(session: AsyncSession, *, code: str) -> uuid.UUID | None:
    """Resolve a purpose code to its id, or ``None`` if the catalogue lacks it.

    ⚠️ ``None`` is not "no consent needed" — it is a seeding fault, and the
    caller decides what to do with it. See :func:`has_withdrawn`.
    """
    purpose_id: uuid.UUID | None = await session.scalar(
        select(ConsentPurpose.id).where(
            ConsentPurpose.code == code, ConsentPurpose.is_active.is_(True)
        )
    )
    return purpose_id


async def _state_for_client(
    session: AsyncSession, *, tenant_id: uuid.UUID, client_id: uuid.UUID
) -> dict[uuid.UUID, PurposeState]:
    """The derived consent position for one client.

    🔒 Both branches of the subject predicate carry ``tenant_id``:
    ``consent_records`` has no RLS (DB §16.4), so this filter *is* the isolation
    boundary for the read.
    """
    rows = await session.scalars(
        select(ConsentRecord)
        .where(
            ConsentRecord.tenant_id == tenant_id,
            ConsentRecord.subject_id == client_id,
            ConsentRecord.subject_type == ConsentSubjectType.CLIENT,
        )
        .order_by(ConsentRecord.occurred_at, ConsentRecord.id)
    )
    return derive_state(
        LedgerEntry(
            purpose_id=row.purpose_id,
            action=row.action,
            occurred_at=row.occurred_at,
            notice_id=row.notice_id,
        )
        for row in rows
    )


async def has_withdrawn(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    purpose_code: str,
) -> bool:
    """Whether this client has withdrawn consent for a purpose — FR-M8-006.

    Returns ``False`` when the purpose is not in the catalogue at all. ⚠️ That is
    the one place this fails *open*, and it is bounded: the purposes messaging
    asks for are seeded by migration 0006, so a missing one means a broken
    deployment rather than a client's decision — and refusing every message in
    that state would take the product offline for a seeding bug while recording
    a withdrawal that never happened.
    """
    purpose_id = await _purpose_id(session, code=purpose_code)
    if purpose_id is None:
        return False

    state = await _state_for_client(session, tenant_id=tenant_id, client_id=client_id)
    position = state.get(purpose_id)
    return position is not None and not position.is_granted
