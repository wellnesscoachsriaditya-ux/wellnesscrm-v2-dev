"""Applying a provider's delivery receipt — status only (EC-M8-07).

🔒 **Status only, and that is a scope decision the spec makes explicitly.**
API §11.3: "MVP handles delivery status only. Inbound WhatsApp *replies* are not
processed — they reach the practitioner's own WhatsApp (EC-M8-07)." Nothing here
parses a message body, and adding one would be building the two-way inbox that
FR-M8-030 defers to Phase 2 — with none of the consent, retention or
authorization design that would need.

🔒 **Idempotent by the provider's own id.** `uq_message_dispatches__provider_id`
makes the lookup exact, and a status that does not advance the row is a no-op.
Providers retry aggressively (API §11.3), so a duplicate receipt is the normal
case rather than an anomaly.

🔒 **Forward-only.** A `delivered` receipt arriving after `read` — which happens,
because a provider's callbacks are not ordered — must not walk the row
backwards. The delivery log would then report that a message the client had
already opened was merely delivered.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.messaging import DispatchStatus
from app.modules.messaging.models import MessageDispatch

#: 🔒 How far along the lifecycle each status is. `failed` and `rejected` sit at
#: the top because they are terminal: a provider that reports a failure and then
#: a stale `sent` must not have the failure erased.
_RANK: dict[DispatchStatus, int] = {
    DispatchStatus.QUEUED: 0,
    DispatchStatus.SENT: 1,
    DispatchStatus.DELIVERED: 2,
    DispatchStatus.READ: 3,
    DispatchStatus.FAILED: 4,
    DispatchStatus.REJECTED: 4,
}

#: The session variable `message_dispatches__provider_lookup` reads. 🔒 Set with
#: `SET LOCAL`, so it lives for one transaction and cannot leak into the next
#: request on a pooled connection — the same discipline as `app.tenant_id`.
_LOOKUP_SETTING = "app.provider_message_id"


@dataclass(frozen=True, slots=True)
class StatusUpdate:
    """One delivery-status fact, already parsed out of a provider's payload.

    Provider-independent by construction: the adapter that understands Meta's
    JSON produces this, and everything below this line is the same for any
    provider we ever add.
    """

    provider_message_id: str
    status: DispatchStatus
    occurred_at: datetime
    failure_code: str | None = None
    failure_reason: str | None = None
    #: 🔒 NFR-088 — per-tenant messaging spend, when the provider reports it.
    #: Never estimated (see `message_dispatches.cost_amount`).
    cost_amount: Decimal | None = None


async def tenant_for_provider_message(
    session: AsyncSession, *, provider_message_id: str
) -> uuid.UUID | None:
    """Resolve which tenant a provider's message belongs to, with no tenant scope.

    🔒 **The one read in the product that crosses a tenant-less boundary, and it
    is bounded by policy rather than by trust.** Migration 0021's
    `message_dispatches__provider_lookup` admits a row only when *all three* hold:
    the session has no tenant, the row has a provider id, and that id equals the
    value set here. There is no wildcard: the caller must already possess an
    opaque provider-issued identifier for the single row it can see.

    ⚠️ The caller must have verified the provider's signature first. This
    function is not an authorization check — it is a lookup that authorization
    (the signature) has already permitted.

    Returns ``None`` for an unknown id, which is the common and harmless case:
    providers send receipts for messages other systems sent, and for messages
    whose rows we have since purged.
    """
    # ⚠️ Parameterised. The value comes from an HTTP body, and `set_config` with
    # an interpolated string would be an injection into a session variable that
    # an RLS policy reads (NFR-038).
    await session.execute(
        text(f"SELECT set_config('{_LOOKUP_SETTING}', :value, true)"),
        {"value": provider_message_id},
    )
    tenant_id: uuid.UUID | None = await session.scalar(
        select(MessageDispatch.tenant_id).where(
            MessageDispatch.provider_message_id == provider_message_id
        )
    )
    return tenant_id


async def apply_status(
    session: AsyncSession, *, tenant_id: uuid.UUID, update: StatusUpdate
) -> bool:
    """Advance one dispatch's status. Returns whether anything changed.

    ⚠️ Runs under the tenant's own scope, through the ordinary isolation policy
    — not through the provider-lookup policy above. By this point the tenant is
    known, so there is no reason to be outside it.

    🔒 The columns written here are exactly the ones migration 0021 grants
    column-level UPDATE on. The attempt's identity — template, version,
    transport, address, attempt number — is unreachable, so a webhook can report
    what happened to a message but can never change which message it was.
    """
    dispatch = await session.scalar(
        select(MessageDispatch).where(
            MessageDispatch.tenant_id == tenant_id,
            MessageDispatch.provider_message_id == update.provider_message_id,
        )
    )
    if dispatch is None:
        return False

    if _RANK[update.status] <= _RANK[dispatch.status]:
        # A duplicate or an out-of-order receipt. Not an error — see the module
        # docstring — and deliberately not recorded as one.
        return False

    dispatch.status = update.status
    dispatch.updated_at = update.occurred_at

    match update.status:
        case DispatchStatus.DELIVERED:
            dispatch.delivered_at = update.occurred_at
        case DispatchStatus.READ:
            dispatch.read_at = update.occurred_at
            # 🔒 A `read` receipt implies delivery even when the `delivered` one
            # never arrived. Leaving `delivered_at` NULL would make the delivery
            # report understate what actually reached clients.
            if dispatch.delivered_at is None:
                dispatch.delivered_at = update.occurred_at
        case DispatchStatus.FAILED | DispatchStatus.REJECTED:
            # ⚠️ `ck_message_dispatches__failure_coded` requires a code, so a
            # provider that reports a failure without one gets a generic one
            # rather than a constraint violation that loses the whole receipt.
            dispatch.failure_code = update.failure_code or "provider_reported_failure"
            dispatch.failure_reason = (update.failure_reason or "")[:1000] or None
        case _:
            pass

    if update.cost_amount is not None:
        dispatch.cost_amount = update.cost_amount

    await session.flush()
    return True
