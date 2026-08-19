"""Lifecycle transitions — ADR-A06, FR-M1-002, FR-M1-010, FR-M1-015.

🔒 **Why this is a separate file from ``service.py``.** ADR-A06: a stage change is
not a field edit. It checks an entitlement, stamps an activation anchor, appends
history and publishes an event, and API §7.1 gives it its own endpoint precisely
so those consequences are explicit, separately authorizable and separately
audited. ``ClientUpdate`` has no ``stage`` field at all, so the only way to move a
client is through this module — the absence is enforced by the schema rather than
by review.

Three actions, and the split between them is a modelling decision:

* :func:`change_stage` — moves between lifecycle stages.
* :func:`archive` — a soft delete (FR-M1-010). Sets ``archived_at`` and **leaves
  ``stage`` alone**.
* :func:`restore` — clears ``archived_at``, returning the client to the stage they
  were archived at (EC-M1-02, AC-M1-007).

🔒 **Archiving is orthogonal to stage, and that is load-bearing.** DB §5.2 counts
the entitlement as ``stage = 'active' AND archived_at IS NULL``; the second clause
only earns its place if a row can be archived while its stage still says
``active``. Keeping the stage is what lets a restore put the client back where
they were without reconstructing it from history — and it is why
``ClientStage.ARCHIVED`` is unreachable through the API (see
``kernel.clients.assert_transition_allowed`` and migration 0010).

⚠️ **Every function here locks the client row first.** ``SELECT ... FOR UPDATE``,
not a plain read. Two concurrent transitions on one client would otherwise both
read stage ``lead``, both pass the entitlement check, and both append a history
row claiming to be the transition from ``lead`` — one entitlement slot consumed
twice, and a history that describes a sequence that never happened.

⚠️ Functions take an ``AsyncSession`` rather than opening one (ADR-04). The
transaction belongs to the request pipeline, so the stage change, its history row,
its audit entry and its event all commit together or not at all.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.clients import (
    ClientArchived,
    ClientRestored,
    ClientStage,
    ClientStageChanged,
    assert_not_archived,
    assert_transition_allowed,
    consumes_entitlement_on_entry,
    restore_consumes_entitlement,
)
from app.kernel.errors import NotFoundError, ValidationError
from app.kernel.events import publish
from app.modules.clients.metering import require_active_client_headroom
from app.modules.clients.models import Client, ClientStageHistory
from app.modules.clients.service import now

#: 🔒 Longest practitioner-supplied reason. Matches the API contract (§7.1) and
#: the ``reason`` column. Long enough for "moved to Bangalore, continuing
#: remotely"; short enough that the field is not a clinical note in disguise —
#: ``client_notes`` (Slice C) is where prose belongs, and it is the table with
#: the retention rules for it.
MAX_REASON_LENGTH = 500


def _validated_reason(reason: str | None) -> str | None:
    """Trim a transition reason, or refuse one that is too long.

    An empty or whitespace-only reason becomes ``None`` rather than ``""``: the
    column is nullable and "no reason given" has exactly one representation, so a
    query for transitions with a reason cannot be wrong about the empty string.
    """
    if reason is None:
        return None
    trimmed = reason.strip()
    if not trimmed:
        return None
    if len(trimmed) > MAX_REASON_LENGTH:
        raise ValidationError(
            "That reason is too long.",
            action=f"Use {MAX_REASON_LENGTH} characters or fewer.",
            details={"max_length": MAX_REASON_LENGTH},
        )
    return trimmed


async def _lock(session: AsyncSession, *, tenant_id: uuid.UUID, client_id: uuid.UUID) -> Client:
    """Load a client for update, holding a row lock until the transaction ends.

    🔒 ``FOR UPDATE`` rather than a plain read. The transition decision is
    read-then-write — read the current stage, decide, write the new one — and
    without the lock two concurrent requests interleave between the two halves.
    The visible symptoms are a doubled history row and an entitlement slot
    consumed twice.

    ⚠️ ``NotFoundError`` is raised by the caller through
    :func:`app.modules.clients.service.get_client` semantics rather than here:
    this returns the row and lets each action shape its own absence message.
    """
    client = await session.scalar(
        select(Client)
        .where(Client.tenant_id == tenant_id, Client.id == client_id)
        .with_for_update()
    )
    if client is None:
        raise NotFoundError(
            "That client could not be found.",
            action="Check the link, or search for them by name.",
        )
    return client


async def _record(
    session: AsyncSession,
    *,
    client: Client,
    from_stage: ClientStage | None,
    to_stage: ClientStage,
    actor_user_id: uuid.UUID | None,
    reason: str | None,
    moment: datetime,
) -> None:
    """Append the history row for a transition — FR-M1-015.

    🔒 Append-only: ``app_user`` holds INSERT and SELECT on
    ``client_stage_history`` and nothing else (migration 0009). Every transition
    is recorded with its timestamp and actor, which is what the timeline
    (FR-M1-018) and the conversion metrics (FR-M9-006) are built from.
    """
    session.add(
        ClientStageHistory(
            tenant_id=client.tenant_id,
            client_id=client.id,
            from_stage=from_stage,
            to_stage=to_stage,
            changed_by_user_id=actor_user_id,
            reason=reason,
            changed_at=moment,
        )
    )


async def change_stage(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    to_stage: ClientStage,
    actor_user_id: uuid.UUID | None,
    reason: str | None = None,
) -> Client:
    """Move a client between lifecycle stages — ADR-A06, FR-M1-015.

    The order of operations is the whole design, and each step is where it is
    because of a specific failure:

    1. **Lock the row.** Everything after this reads a stage that cannot change
       underneath it.
    2. **Refuse if archived.** A soft-deleted client is not a lifecycle
       participant (FR-M1-010).
    3. **Refuse an illegal transition** — a no-op, or ``archived`` as a target.
    4. **Check the entitlement, only on entry to ``active``** (FR-M1-002,
       FR-M1-003). Before the write, so a refused transition changes nothing;
       after the lock, so the count it reads is the one the write will alter.
    5. **Stamp ``activated_at`` on first activation only** — the check-in anchor
       (FR-M8-023). A reactivated client keeps their original date.
    6. **Write, record history, publish.**

    🔒 AC-M1-003 is satisfied by construction: this changes a column on the
    existing row. The record, its identifier and all its prior history — including
    the enquiry that created it — survive because nothing is copied or recreated.

    Returns:
        The updated client.

    Raises:
        NotFoundError: No such client in this tenant.
        ValidationError: The transition is a no-op, targets ``archived``, or the
            client is archived.
        EntitlementError: 402 — entering ``active`` would exceed the plan's
            ``active_clients`` limit (FR-M1-002).
    """
    client = await _lock(session, tenant_id=tenant_id, client_id=client_id)

    assert_not_archived(archived_at_is_set=client.archived_at is not None)
    from_stage = client.stage
    assert_transition_allowed(from_stage, to_stage)
    trimmed_reason = _validated_reason(reason)

    if consumes_entitlement_on_entry(from_stage, to_stage):
        await require_active_client_headroom(session, tenant_id=tenant_id)

    moment = now()
    client.stage = to_stage
    # 🔒 First activation only (FR-M8-023). EC-M1-02 brings a churned client back
    # to `active`, and overwriting the anchor would restart their check-in
    # schedule as though they were new — losing the fact that this is a returning
    # client, which is exactly what the edge case is about.
    if to_stage is ClientStage.ACTIVE and client.activated_at is None:
        client.activated_at = moment
    client.updated_at = moment

    await _record(
        session,
        client=client,
        from_stage=from_stage,
        to_stage=to_stage,
        actor_user_id=actor_user_id,
        reason=trimmed_reason,
        moment=moment,
    )
    await session.flush()

    # 🔒 Published inside the transaction (DDR-06). Slice D's timeline subscriber
    # runs transactionally, so the entry and the transition commit together —
    # a timeline missing the event that caused it would be worse than no timeline.
    await publish(
        ClientStageChanged(
            client_id=client.id,
            tenant_id=tenant_id,
            from_stage=from_stage,
            to_stage=to_stage,
            changed_by_user_id=actor_user_id,
            changed_at=moment,
        ),
        session,
    )
    return client


async def archive(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
) -> Client:
    """Soft-delete a client — FR-M1-010, AC-M1-007.

    ⚠️ **Takes no reason, deliberately.** There is nowhere safe to put one in this
    slice. ``client_stage_history`` cannot hold it — the stage did not change, and
    ``ck_client_stage_history__actual_transition`` refuses a row whose
    ``from_stage`` equals its ``to_stage``. ``audit_log.metadata`` is the wrong
    home for practitioner prose (NFR-033: identifiers and enums), since an archive
    reason can easily name a clinical circumstance and the audit log is retained
    long and read by operators. Accepting the field and discarding it would be
    worse than not offering it. ``client_notes`` (Slice C) is where prose belongs,
    and it is the table that will carry the retention rules for it.

    🔒 **Sets ``archived_at`` and nothing else.** The stage is left exactly as it
    was, which is what lets :func:`restore` return the client to it. AC-M1-007
    requires archiving to remove a client from default views *without deleting any
    data*, and rewriting their stage would destroy the one fact a restore needs.

    🔒 The freed entitlement is immediate and requires no counter update: the
    count is a live query whose predicate already excludes archived rows
    (DB §5.2). Archiving an ``active`` client releases their slot the moment this
    commits.

    ⚠️ Idempotent by refusal rather than by silence. Archiving an
    already-archived client raises, because the alternative — returning success —
    would let a double-submitted request report an archive that this call did not
    perform, and the history would show one event for two actions.

    Raises:
        NotFoundError: No such client in this tenant.
        ValidationError: The client is already archived.
    """
    client = await _lock(session, tenant_id=tenant_id, client_id=client_id)

    if client.archived_at is not None:
        raise ValidationError(
            "That client is already archived.",
            action="Restore them if you want them back in your lists.",
        )

    moment = now()
    client.archived_at = moment
    client.updated_at = moment

    # 🔒 No `client_stage_history` row — the stage did not change. Inventing one
    # would put a transition in the conversion metrics (FR-M9-006) that never
    # occurred. The archive is recorded by the audit log and by `ClientArchived`.
    await session.flush()

    await publish(
        ClientArchived(
            client_id=client.id,
            tenant_id=tenant_id,
            stage=client.stage,
            archived_by_user_id=actor_user_id,
            archived_at=moment,
        ),
        session,
    )
    return client


async def restore(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
) -> Client:
    """Bring an archived client back — EC-M1-02, AC-M1-007.

    🔒 Returns them to the stage they were archived at, because
    :func:`archive` never changed it. A returning client is reactivated in place;
    there is no second record, which is the whole of EC-M1-02.

    🔒 **Metered when the restored stage is ``active``** (EC-M1-06). Archiving an
    active client frees a slot, so restoring one takes it back — and a
    practitioner who archived their way under a limit must not cross it again by
    restoring. Refusing here rather than silently restoring them to ``paused``
    keeps the practitioner in control of a billing decision.

    Raises:
        NotFoundError: No such client in this tenant.
        ValidationError: The client is not archived.
        EntitlementError: 402 — restoring an ``active`` client would exceed the
            plan's limit.
    """
    client = await _lock(session, tenant_id=tenant_id, client_id=client_id)

    if client.archived_at is None:
        raise ValidationError(
            "That client is not archived.",
            action="No action is needed — they are already in your lists.",
        )

    if restore_consumes_entitlement(client.stage):
        await require_active_client_headroom(session, tenant_id=tenant_id)

    moment = now()
    client.archived_at = None
    client.updated_at = moment
    await session.flush()

    await publish(
        ClientRestored(
            client_id=client.id,
            tenant_id=tenant_id,
            stage=client.stage,
            restored_by_user_id=actor_user_id,
            restored_at=moment,
        ),
        session,
    )
    return client
