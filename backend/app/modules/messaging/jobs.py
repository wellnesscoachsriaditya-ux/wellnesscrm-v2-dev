"""Where messaging meets the queue and the event bus.

Two halves, and the split between them is the design:

🔒 **Intent is created transactionally; sending is deferred** (Arch §3.4). A plan
that is approved *cannot fail to schedule its delivery* — the `scheduled_messages`
row commits with the plan's own transaction, or neither exists. The send is a
different matter: it can fail on its own without invalidating the approval, so it
runs from the job queue with that queue's retry policy, backoff and dead-letter.

⚠️ **A transactional subscriber that raises rolls back the publisher's action.**
That is the accepted cost, and it is why each one here does the minimum: validate
against the template, insert one row. The alternative — a deferred subscriber
that enqueues intent — would mean an approved plan whose delivery quietly never
got scheduled because a job died, which is exactly the failure FR-M8-001 exists
to prevent.

🔒 **No second queue** (FR-M8-004). Retries, attempt counting and dead-lettering
are the existing `JobClass.DISPATCH` policy: three attempts, exponential backoff,
lease recovery. Messaging contributes only the decision about *which* failures
are worth retrying.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.clients import ClientArchived, ClientStage, ClientStageChanged, get_client_directory
from app.kernel.context import get_actor
from app.kernel.errors import NotFoundError
from app.kernel.events import subscribe
from app.kernel.jobs import JobContractError, register_handler
from app.kernel.leads import EnquiryReceived
from app.kernel.messaging import DispatchStatus, utc_now
from app.kernel.models import JobClass, User
from app.kernel.nutrition import PlanVersionIssued
from app.modules.messaging import checkins
from app.modules.messaging.dispatch import (
    RetryableDispatchError,
    dispatch_scheduled,
    resolve_exhausted,
)
from app.modules.messaging.links import plan_url
from app.modules.messaging.scheduling import DISPATCH_JOB_TYPE, MessageRequest, schedule
from app.modules.messaging.webhooks import StatusUpdate, apply_status

#: DB §11.2 `source_module` for messages this module's subscribers create. 🔒 A
#: literal rather than `__name__`: it is a data column, and a Python path in it
#: would change under a refactor and break EC-M8-08's cancellation lookups.
SOURCE_PLANS = "nutrition.plans"
SOURCE_LEADS = "leads.enquiry"

#: The job that applies one provider delivery receipt. 🔒 Must match the constant
#: the webhook router enqueues under; the startup handler check catches a
#: mismatch before any receipt is dropped.
STATUS_JOB_TYPE = "apply_message_status"


# ─── The dispatch job ────────────────────────────────────────────────────


async def dispatch_scheduled_message(payload: Mapping[str, Any], session: AsyncSession) -> None:
    """Send one due message — the queue's entry into the dispatch engine.

    🔒 The tenant comes from the **worker's context**, which the job runner set
    from `jobs.tenant_id`, never from the payload. A tenant id read out of
    caller-supplied data would let whoever enqueued the job choose whose messages
    to send.

    ⚠️ Idempotent by requirement (`JobHandler`'s contract): a lease can expire
    while this legitimately runs and the job will be handed to another worker.
    `dispatch_scheduled` refuses to act twice on a message that is no longer
    pending, which is what makes re-execution safe rather than a second message.

    Raises:
        RetryableDispatchError: A transient transport failure. The queue applies
            backoff and retries up to `JobClass.DISPATCH`'s ceiling; the attempt
            is already recorded in the delivery log either way.
    """
    raw = payload.get("scheduled_message_id")
    if not isinstance(raw, str):
        # 🔒 Not retryable: a malformed payload fails identically on every
        # attempt, so it must dead-letter now rather than after three backoffs.
        raise JobContractError(
            "dispatch_scheduled_message needs a `scheduled_message_id` in its payload."
        )

    tenant_id = get_actor().tenant_id
    if tenant_id is None:
        raise JobContractError(
            "dispatch_scheduled_message ran with no tenant scope. The job row must "
            "carry the tenant it belongs to."
        )

    try:
        await dispatch_scheduled(session, tenant_id=tenant_id, scheduled_message_id=uuid.UUID(raw))
    except NotFoundError:
        # 🔒 The message was cancelled or purged between enqueue and execution.
        # Succeeding is correct: there is nothing to send and nothing wrong.
        return


async def apply_message_status(payload: Mapping[str, Any], session: AsyncSession) -> None:
    """Apply one provider delivery receipt — API §11.3 step 3.

    🔒 The webhook route verifies the signature, resolves the tenant and stops;
    this does the work. That split is what lets the endpoint answer inside the
    ~2 seconds a provider allows before it starts retrying (API §11.3 step 2).

    ⚠️ Idempotent twice over: the queue refuses a duplicate job by key, and
    `apply_status` refuses a status that does not advance the row. Both are
    needed — a provider sends the *same* receipt more than once, and it also
    sends receipts out of order.
    """
    provider_message_id = payload.get("provider_message_id")
    raw_status = payload.get("status")
    if not isinstance(provider_message_id, str) or not isinstance(raw_status, str):
        raise JobContractError(
            "apply_message_status needs `provider_message_id` and `status` in its payload."
        )

    tenant_id = get_actor().tenant_id
    if tenant_id is None:
        raise JobContractError(
            "apply_message_status ran with no tenant scope. The job row must carry the "
            "tenant the dispatch belongs to."
        )

    occurred_raw = payload.get("occurred_at")
    occurred_at = (
        datetime.fromisoformat(occurred_raw) if isinstance(occurred_raw, str) else utc_now()
    )

    failure_code = payload.get("failure_code")
    failure_reason = payload.get("failure_reason")

    await apply_status(
        session,
        tenant_id=tenant_id,
        update=StatusUpdate(
            provider_message_id=provider_message_id,
            status=DispatchStatus(raw_status),
            occurred_at=occurred_at,
            failure_code=failure_code if isinstance(failure_code, str) else None,
            failure_reason=failure_reason if isinstance(failure_reason, str) else None,
        ),
    )


async def close_exhausted_dispatch(
    session: AsyncSession, *, tenant_id: uuid.UUID, scheduled_message_id: uuid.UUID
) -> None:
    """Resolve a message whose retries are spent — FR-M8-004's bounded ceiling.

    ⚠️ Exposed for the worker's dead-letter path rather than called from the
    handler: the handler cannot know it was the final attempt without asking the
    queue, and the queue only knows after the handler has raised.
    """
    await resolve_exhausted(session, tenant_id=tenant_id, scheduled_message_id=scheduled_message_id)


# ─── Producers (Arch §3.4a) ──────────────────────────────────────────────


async def on_plan_issued(event: PlanVersionIssued, session: AsyncSession, /) -> None:
    """Deliver an issued plan to the client — FR-M8-013, AC-M8-001.

    🔒 Transactional, so an approved plan cannot fail to schedule its delivery.
    ⚠️ `scheduled_for` is the issue moment, so the sweep picks it up on its next
    pass and NFR-009's 60-second budget is spent on queue latency rather than on
    waiting for a schedule.
    """
    identity = await get_client_directory().find(
        session, tenant_id=event.tenant_id, client_id=event.client_id
    )
    if identity is None:
        # The plan's client was erased between issue and this handler. Nothing to
        # deliver, and refusing the issue would be worse than not messaging.
        return

    await schedule(
        session,
        tenant_id=event.tenant_id,
        request=MessageRequest(
            template_code="plan_delivered",
            # 🔒 The logical occasion is the *version*: re-issuing the same
            # version delivers once, while a genuine new version is a new
            # occasion and delivers again.
            occasion=f"plan_version:{event.plan_version_id}",
            scheduled_for=event.issued_at,
            source_module=SOURCE_PLANS,
            source_record_id=event.plan_version_id,
            client_id=event.client_id,
            variables={
                "client_name": identity.full_name,
                "practitioner_name": await _practitioner_name(
                    session, user_id=identity.owner_user_id
                ),
                "plan_url": plan_url(event.plan_version_id),
            },
        ),
    )


async def on_enquiry_received(event: EnquiryReceived, session: AsyncSession, /) -> None:
    """Acknowledge a new enquiry and tell the practitioner — FR-M8-018/019.

    🔒 Two messages, two recipients, one transaction. S2 shipped both as
    "logged only — no transport until S5"; this is that wiring.

    ⚠️ The acknowledgement is scheduled even for a duplicate submission
    (EC-M2-02). A prospect who submits twice gets one acknowledgement, because
    the occasion is the *submission* and each submission is a real event they
    are waiting on a reply to.
    """
    identity = await get_client_directory().find(
        session, tenant_id=event.tenant_id, client_id=event.client_id
    )
    if identity is None:
        return

    practice_name = await _practice_name(session, tenant_id=event.tenant_id)

    await schedule(
        session,
        tenant_id=event.tenant_id,
        request=MessageRequest(
            template_code="lead_acknowledgement",
            occasion=f"enquiry:{event.submission_id}",
            scheduled_for=event.received_at,
            source_module=SOURCE_LEADS,
            source_record_id=event.submission_id,
            client_id=event.client_id,
            variables={"lead_name": identity.full_name, "practice_name": practice_name},
        ),
    )

    await schedule(
        session,
        tenant_id=event.tenant_id,
        request=MessageRequest(
            template_code="lead_notification",
            occasion=f"enquiry_notice:{event.submission_id}",
            scheduled_for=event.received_at,
            source_module=SOURCE_LEADS,
            source_record_id=event.submission_id,
            # 🔒 Directed at the owning practitioner, not the client — so it
            # carries `recipient_user_id` and no `client_id`. That also keeps it
            # off the client's timeline and out of their frequency cap, which is
            # a limit designed to protect them from over-contact.
            recipient_user_id=identity.owner_user_id,
            variables={
                "practitioner_name": await _practitioner_name(
                    session, user_id=identity.owner_user_id
                ),
                "lead_name": identity.full_name,
                "lead_source": event.source or "the enquiry form",
            },
        ),
    )


async def on_stage_changed(event: ClientStageChanged, session: AsyncSession, /) -> None:
    """Start or stop a client's check-ins — FR-M8-023, FR-M8-025.

    🔒 **Event-driven, not a nightly reconciliation.** FR-M8-025 requires
    check-ins to stop when a client leaves `active`, and a sweep that noticed the
    next morning would already have sent one.

    ⚠️ **Only check-ins are cancelled here.** Everything else queued for the
    client stays, and is suppressed at dispatch with `client_stage_inactive`
    recorded against it. That is deliberate: cancelling destroys the reason,
    while suppression preserves it, and AC-M8-004 asks for the reason. A
    check-in is the exception because its schedule is itself paused — leaving the
    row queued would produce a suppressed nudge every week for a client nobody is
    treating any more.
    """
    if event.to_stage is ClientStage.ACTIVE:
        await checkins.ensure_for_activation(
            session,
            tenant_id=event.tenant_id,
            client_id=event.client_id,
            activated_at=event.changed_at,
        )
        return

    await checkins.pause(session, tenant_id=event.tenant_id, client_id=event.client_id)


async def on_client_archived(event: ClientArchived, session: AsyncSession, /) -> None:
    """Stop check-ins for an archived client — FR-M1-014, FR-M8-005.

    An archive is not a stage change, so it needs its own subscriber; without it
    a client archived directly from `active` would keep generating nudges.
    """
    await checkins.pause(session, tenant_id=event.tenant_id, client_id=event.client_id)


# ─── Helpers ─────────────────────────────────────────────────────────────


async def _practitioner_name(session: AsyncSession, *, user_id: uuid.UUID) -> str:
    """The name a client sees in a message.

    ⚠️ Falls back to a neutral noun rather than raising. A missing user row means
    a deactivated account, and refusing to deliver a plan because the practitioner
    who wrote it has left is the wrong trade for the client waiting on it.
    """
    user = await session.get(User, user_id)
    return user.full_name if user is not None else "your practitioner"


async def _practice_name(session: AsyncSession, *, tenant_id: uuid.UUID) -> str:
    """The practice's own name, for the enquiry acknowledgement."""
    from app.kernel.models import Tenant

    tenant = await session.get(Tenant, tenant_id)
    return tenant.name if tenant is not None else "the practice"


# ─── Registration ────────────────────────────────────────────────────────


def register_jobs() -> None:
    """Wire the handler and the subscribers. Called by both entry points.

    ⚠️ Idempotent: `register_handler` ignores an identical re-registration and
    `subscribe` dedupes by handler identity, so the web and worker processes
    calling this — and a test importing it twice — do not double-execute.
    """
    register_handler(DISPATCH_JOB_TYPE, JobClass.DISPATCH, dispatch_scheduled_message)
    # 🔒 `DISPATCH` class too: a delivery receipt is small, quick and worth
    # retrying if the database blips, and it must not queue behind a maintenance
    # sweep while a practitioner is looking at a stale status.
    register_handler(STATUS_JOB_TYPE, JobClass.DISPATCH, apply_message_status)

    subscribe(PlanVersionIssued, transactional=on_plan_issued)
    subscribe(EnquiryReceived, transactional=on_enquiry_received)
    subscribe(ClientStageChanged, transactional=on_stage_changed)
    subscribe(ClientArchived, transactional=on_client_archived)


__all__ = [
    "STATUS_JOB_TYPE",
    "RetryableDispatchError",
    "apply_message_status",
    "close_exhausted_dispatch",
    "dispatch_scheduled_message",
    "register_jobs",
]
