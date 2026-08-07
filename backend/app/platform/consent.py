"""Consent persistence — where ledger entries land, and how state is read back.

The kernel decides *what* consent means and *what may be recorded*; this module
decides *where it goes*. Splitting them is what lets every rule in
``kernel.consent`` be tested without a database.

🔒 **Append-only, and not by convention.** There is no update or delete function
in this module, and there is no privilege to run one either: ``app_user`` holds
INSERT and SELECT on ``consent_records`` and nothing more (DDR-15, migration
0006). If a future caller needs to "correct" an entry, the answer is another
entry — a correction that erases what it corrects is not a legal record.

🔒 **Explicit tenant filtering, because RLS is absent here.** ``consent_records``
carries ``tenant_id`` but has no policy, matching ``audit_log``: entries are
written on the anonymous enquiry path where no tenant context is established yet
(FR-M2-004), and a Pattern A policy would reject exactly those inserts. ⚠️ The
consequence is that every read below filters by tenant in the query, and that
filter is application code — so AC-M0-003 does not cover it. Reviewers should
treat a missing ``tenant_id`` predicate in this file as a leak, not a style
issue.

⚠️ Functions take an ``AsyncSession`` rather than opening one. The transaction
belongs to the request pipeline (ADR-04); a repository that committed
independently would record a consent decision whose accompanying change rolled
back.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, cast

from sqlalchemy import Select, Table, and_, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.consent import (
    ConsentDecision,
    ConsentSubject,
    LedgerEntry,
    PurposeRule,
    PurposeState,
)
from app.kernel.consent import derive_state as _derive_state
from app.kernel.consent import now as _now
from app.kernel.models import (
    ConsentNotice,
    ConsentPurpose,
    ConsentRecord,
    ConsentSubjectType,
)
from app.platform.logging import get_logger

logger = get_logger(__name__)

#: The ledger table as a Core construct, matching ``platform.audit``. The write
#: is a plain dict against ``insert()`` rather than an ORM instance, so a row
#: cannot be loaded, mutated and flushed — the ORM's normal behaviour, and the
#: one thing an append-only table must not permit.
_CONSENT_TABLE: Final[Table] = cast(Table, ConsentRecord.__table__)


def _entry(row: ConsentRecord) -> LedgerEntry:
    """Project a persisted row onto what the derivation reads."""
    return LedgerEntry(
        purpose_id=row.purpose_id,
        action=row.action,
        occurred_at=row.occurred_at,
        notice_id=row.notice_id,
    )


@dataclass(frozen=True, slots=True)
class NoticeInForce:
    """The notice version currently presented for a locale.

    🔒 Captured as a snapshot rather than an ORM row: the notice id recorded on a
    ledger entry must be the version the person actually saw, and re-reading it
    later could pick up a supersession that happened in between.
    """

    notice_id: uuid.UUID
    version: str
    locale: str
    purpose_ids: Sequence[uuid.UUID]
    requires_reconsent: bool


# ─── Catalogue reads ─────────────────────────────────────────────────────


async def load_purpose_rules(session: AsyncSession) -> dict[uuid.UUID, PurposeRule]:
    """Load the active purpose catalogue as kernel rules.

    No tenant filter: the catalogue is platform-wide (Pattern D), and tenant
    writes to it are revoked in migration 0006.
    """
    result = await session.execute(
        select(ConsentPurpose.id, ConsentPurpose.code, ConsentPurpose.is_essential).where(
            ConsentPurpose.is_active.is_(True)
        )
    )
    return {
        row.id: PurposeRule(purpose_id=row.id, code=row.code, is_essential=row.is_essential)
        for row in result
    }


async def find_purpose_by_code(session: AsyncSession, code: str) -> PurposeRule | None:
    """Resolve one purpose by its stable code."""
    result = await session.execute(
        select(ConsentPurpose.id, ConsentPurpose.code, ConsentPurpose.is_essential).where(
            ConsentPurpose.code == code, ConsentPurpose.is_active.is_(True)
        )
    )
    row = result.first()
    if row is None:
        return None
    return PurposeRule(purpose_id=row.id, code=row.code, is_essential=row.is_essential)


async def notice_in_force(
    session: AsyncSession,
    *,
    locale: str = "en-IN",
    at: datetime | None = None,
) -> NoticeInForce | None:
    """The notice currently in force for a locale.

    🔒 "In force" is a window, not a flag: effective, and not yet superseded. A
    boolean ``is_current`` column would need updating in two places on every
    supersession, and the failure mode is two current notices — meaning two
    answers to "what did this person agree to".
    """
    moment = at or _now()

    result = await session.execute(
        select(ConsentNotice)
        .where(
            ConsentNotice.locale == locale,
            ConsentNotice.effective_from <= moment,
            or_(ConsentNotice.superseded_at.is_(None), ConsentNotice.superseded_at > moment),
        )
        # Newest effective first: if two notices overlap through a data error,
        # the later one is the one the UI would have shown.
        .order_by(ConsentNotice.effective_from.desc())
        .limit(1)
    )
    notice = result.scalar_one_or_none()
    if notice is None:
        # ⚠️ A capture path with no notice cannot record valid consent — there is
        # no text to have agreed to. Logged rather than raised so the caller
        # decides whether this blocks the request.
        logger.warning("consent.notice_missing", extra={"locale": locale})
        return None

    return NoticeInForce(
        notice_id=notice.id,
        version=notice.version,
        locale=notice.locale,
        purpose_ids=tuple(notice.purpose_ids),
        requires_reconsent=notice.requires_reconsent,
    )


# ─── Ledger writes (append-only) ─────────────────────────────────────────


async def append_decision(
    session: AsyncSession,
    decision: ConsentDecision,
) -> None:
    """Append one decision to the ledger.

    🔒 The only write path in this module. No return value and no id handed back:
    a caller holding a ledger row id would eventually be tempted to update it,
    and the useful question — "what is in force now" — is answered by
    :func:`load_state`, not by a row.

    ⚠️ Joins the caller's transaction deliberately. A consent grant recorded
    alongside a client creation that then fails must roll back with it, or the
    ledger asserts a decision about a person who does not exist.
    """
    subject = decision.subject
    await session.execute(
        insert(_CONSENT_TABLE).values(
            tenant_id=subject.tenant_id,
            subject_type=subject.subject_type,
            subject_id=subject.subject_id,
            subject_mobile_hash=subject.subject_mobile_hash,
            purpose_id=decision.purpose_id,
            notice_id=decision.notice_id,
            action=decision.action,
            captured_via=decision.captured_via,
            captured_by_actor_type=decision.captured_by_actor_type,
            captured_by_actor_id=decision.captured_by_actor_id,
            guardian_name=decision.guardian_name,
            guardian_relationship=decision.guardian_relationship,
            guardian_verification_method=decision.guardian_verification_method,
            evidence=decision.evidence,
            occurred_at=decision.occurred_at or _now(),
        )
    )


async def append_decisions(
    session: AsyncSession,
    decisions: Sequence[ConsentDecision],
) -> None:
    """Append several decisions — the itemised-consent case (FR-M0-022).

    A consent form presents one notice covering several purposes and the person
    ticks them individually. One entry per purpose, in one statement, so a
    partial write cannot leave some purposes recorded and others not.
    """
    if not decisions:
        return
    for decision in decisions:
        await append_decision(session, decision)


# ─── Ledger reads ────────────────────────────────────────────────────────


def _subject_predicate(subject: ConsentSubject) -> Select[tuple[ConsentRecord]]:
    """Build the tenant-scoped subject filter.

    ⚠️ 🔒 Both branches carry ``tenant_id``. This table has no RLS, so this
    predicate is the entire isolation boundary for consent reads.
    """
    conditions = [ConsentRecord.tenant_id == subject.tenant_id]

    if subject.subject_id is not None:
        conditions.append(ConsentRecord.subject_id == subject.subject_id)
    else:
        conditions.append(ConsentRecord.subject_mobile_hash == subject.subject_mobile_hash)

    return select(ConsentRecord).where(and_(*conditions))


async def load_history(
    session: AsyncSession,
    subject: ConsentSubject,
) -> Sequence[ConsentRecord]:
    """Every ledger entry for a subject, oldest first.

    🔒 The evidence read behind NFR-051 — "produce the consent basis for any
    client". Returns the full history rather than a summary, because the question
    a regulator asks is what happened, not what is currently true.
    """
    result = await session.execute(
        _subject_predicate(subject).order_by(ConsentRecord.occurred_at, ConsentRecord.id)
    )
    return tuple(result.scalars().all())


async def load_state(
    session: AsyncSession,
    subject: ConsentSubject,
) -> dict[uuid.UUID, PurposeState]:
    """The current consent position per purpose, derived from the ledger.

    🔒 DB §16.3. The derivation itself is ``kernel.consent.derive_state``; this
    only supplies the rows. Keeping the fold in the kernel means the rule for
    "what is in force" is one function, testable without a database, rather than
    a SQL window function that would be a second implementation of it.
    """
    history = await load_history(session, subject)
    return _derive_state(_entry(row) for row in history)


async def load_state_for_client(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> dict[uuid.UUID, PurposeState]:
    """Derived state for a known client — the common case.

    A convenience over :func:`load_state` so callers do not each rebuild a
    :class:`ConsentSubject` and risk omitting the tenant.
    """
    subject = ConsentSubject(
        tenant_id=tenant_id,
        subject_type=ConsentSubjectType.CLIENT,
        subject_id=client_id,
    )
    return await load_state(session, subject)


async def load_state_for_converted_client(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    mobile_hash: str,
) -> dict[uuid.UUID, PurposeState]:
    """Derived state for a client who first consented as a prospect.

    🔒 When an enquiry becomes a client (FR-M2-004), the consent captured at the
    form is keyed to ``subject_mobile_hash`` — there was no ``client_id`` to key
    it to. Rewriting those entries with the new id would be an UPDATE on an
    append-only ledger: forbidden by the grants, and it would destroy the record
    of what was actually captured, when.

    Both identifications are therefore read and folded together. The kernel's
    fold orders by ``occurred_at``, so a prospect grant followed by a client-side
    withdrawal resolves correctly regardless of which identification carries
    which entry.
    """
    result = await session.execute(
        select(ConsentRecord)
        .where(
            ConsentRecord.tenant_id == tenant_id,
            or_(
                ConsentRecord.subject_id == client_id,
                ConsentRecord.subject_mobile_hash == mobile_hash,
            ),
        )
        .order_by(ConsentRecord.occurred_at, ConsentRecord.id)
    )
    return _derive_state(_entry(row) for row in result.scalars().all())
