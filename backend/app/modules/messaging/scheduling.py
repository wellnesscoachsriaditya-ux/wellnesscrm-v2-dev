"""Creating message intent — the one way anything gets messaged (FR-M8-001).

🔒 **No module schedules its own messages.** A module that wants a client
contacted calls :func:`schedule`, which writes a ``scheduled_messages`` row and
stops. What happens next — suppression, quiet hours, transport selection,
retries, logging — belongs to the dispatch engine and to nothing else. M8.3 is
binding on this point, and the reason is V1's most expensive failure: three
reminder systems means three retry paths and three failure logs, and the answer
to "why didn't my client get it?" depends on which one sent it.

🔒 **Suppression is not evaluated here** (DB §11.5). A stage, a consent or a
tenant's status can change between scheduling and sending. What *is* evaluated
here is the caller's contract: does the template exist, are its variables
supplied, is there a recipient. Those are caller bugs, and they must surface in
the request that made them rather than in a worker days later.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.errors import NotFoundError, ValidationError
from app.kernel.jobs import JobContractError, get_job_enqueuer
from app.kernel.messaging import ScheduledState, build_idempotency_key, utc_now
from app.modules.messaging.models import MessageTemplate, ScheduledMessage
from app.modules.messaging.templates import (
    Template,
    load_current,
    project,
    validate_variables,
)

# ⚠️ `logging.getLogger`, not `platform.logging.get_logger`: R5 forbids a module
# importing platform. The process-wide configuration applies either way, because
# it is set on the root logger.
logger = logging.getLogger(__name__)

#: The job type the queue runs one dispatch under. 🔒 Declared here rather than
#: in `scheduler`, because both the sweep and :func:`schedule` enqueue it and a
#: constant in the sweep would make this module import it — a cycle through
#: `checkins`.
DISPATCH_JOB_TYPE = "dispatch_scheduled_message"


@dataclass(frozen=True, slots=True)
class MessageRequest:
    """One message a module wants sent — the whole of what a producer supplies.

    🔒 ``occasion`` is the part that makes idempotency work, and it is the
    caller's job because only the caller knows what "the same message" means.
    DB §11.4 calls it the *logical occasion*: "the check-in for week 32", not
    "now". Two attempts to schedule the same occasion produce the same key and
    the unique constraint refuses the second — which is how a double-tapped
    button, a retried request and a replayed job all collapse to one message.

    ⚠️ ``occasion`` must not contain a timestamp of the moment it was built.
    ``f"plan:{plan_version_id}"`` is an occasion; ``f"plan:{datetime.now()}"``
    is a fresh key every time, and idempotency silently stops working.
    """

    template_code: str
    occasion: str
    scheduled_for: datetime
    source_module: str
    client_id: uuid.UUID | None = None
    recipient_user_id: uuid.UUID | None = None
    source_record_id: uuid.UUID | None = None
    variables: Mapping[str, Any] = field(default_factory=dict)


def recipient_key(request: MessageRequest) -> str:
    """The stable identity of the recipient, for the idempotency key.

    🔒 **An identifier, deliberately not the address.** EC-M8-08 lets a client
    change their number between scheduling and sending; keying idempotency on
    the address would make the same occasion produce two different keys either
    side of that change, and the client would receive the message twice.
    """
    if request.client_id is not None:
        return f"client:{request.client_id}"
    return f"user:{request.recipient_user_id}"


async def schedule(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    request: MessageRequest,
    enqueue_if_due: bool = True,
) -> ScheduledMessage | None:
    """Queue one message. Returns ``None`` if this occasion is already queued.

    🔒 **Suppression by unique constraint, not by a read-then-write** (DB §11.4).
    A `SELECT` that found nothing followed by an `INSERT` loses the race two
    concurrent producers create by construction — and this path exists precisely
    because they arrive together.

    ⚠️ Returning ``None`` rather than raising: a duplicate is the mechanism
    working, not a failure. A caller that wanted to know whether *it* created the
    row can compare against ``None``; most callers correctly do not care.

    Args:
        enqueue_if_due: Queue the dispatch job immediately when the message is
            already due. 🔒 On by default — see :func:`_enqueue_dispatch` for why
            this is latency rather than a second scheduler. Turned off only by
            callers that have no job queue configured, which in practice means
            unit tests of scheduling itself.

    Raises:
        ValidationError: No recipient, or variables that do not satisfy the
            template's declaration.
        NotFoundError: No published template with that code.
    """
    if request.client_id is None and request.recipient_user_id is None:
        raise ValidationError(
            "A message needs a recipient.",
            action="Name the client or the practitioner it is for.",
        )
    if not request.occasion.strip():
        raise ValidationError(
            "A scheduled message needs a logical occasion for idempotency.",
            action="Pass a stable identifier for what this message is about.",
        )

    template = await load_current(session, code=request.template_code)
    variables = validate_variables(template, request.variables)

    key = build_idempotency_key(
        tenant_id=tenant_id,
        recipient=recipient_key(request),
        template_code=template.code,
        occasion=request.occasion,
    )

    statement = (
        insert(ScheduledMessage)
        .values(
            tenant_id=tenant_id,
            client_id=request.client_id,
            recipient_user_id=request.recipient_user_id,
            template_id=template.id,
            template_variables=variables,
            scheduled_for=request.scheduled_for,
            state=ScheduledState.PENDING.value,
            idempotency_key=key,
            source_module=request.source_module,
            source_record_id=request.source_record_id,
        )
        # 🔒 The constraint named explicitly: PostgreSQL must be able to prove an
        # index covers the conflict target, and naming the columns rather than
        # the constraint would silently match a different index if one is ever
        # added.
        .on_conflict_do_nothing(constraint="uq_scheduled_messages__idempotency")
        .returning(ScheduledMessage.id)
    )

    scheduled_id = (await session.execute(statement)).scalar_one_or_none()
    if scheduled_id is None:
        return None

    row = await session.get(ScheduledMessage, scheduled_id)
    if row is not None and enqueue_if_due and request.scheduled_for <= utc_now():
        await _enqueue_dispatch(session, tenant_id=tenant_id, scheduled_message_id=row.id)
    return row


async def _enqueue_dispatch(
    session: AsyncSession, *, tenant_id: uuid.UUID, scheduled_message_id: uuid.UUID
) -> None:
    """Queue the dispatch job for a message that is already due — NFR-009.

    🔒 **Not a second scheduler.** It enqueues exactly the job the sweep would
    have enqueued, with exactly the same idempotency key, so the two can never
    produce two sends. What it buys is latency: NFR-009 budgets 60 seconds from
    an approved plan to a delivered message, and waiting for the next sweep would
    spend most of that budget before the queue had even seen the work.

    ⚠️ In the same transaction as the row it refers to (Arch §11.1). A job that
    committed while its message rolled back would fail on every attempt.

    🔒 **A failure here is swallowed, and that is the design.** The sweep is what
    *guarantees* a due message is dispatched; this only makes it sooner. So a
    process with no queue wired — a script, a test harness, a one-off migration
    that happens to publish an event — must not lose a plan delivery a
    practitioner already approved, and must not have their approval rolled back
    because an accelerator was unavailable. The message stays `pending` and the
    next sweep takes it.

    ⚠️ Logged at warning rather than silently, because in the *web* process this
    would mean NFR-009's 60-second budget is being missed on every message.
    """
    try:
        await get_job_enqueuer()(
            session,
            job_type=DISPATCH_JOB_TYPE,
            payload={"scheduled_message_id": str(scheduled_message_id)},
            tenant_id=tenant_id,
            idempotency_key=f"dispatch:{scheduled_message_id}",
        )
    except JobContractError:
        logger.warning(
            "Could not queue a due message for immediate dispatch; the scheduler "
            "sweep will pick it up",
            extra={"scheduled_message_id": str(scheduled_message_id)},
        )


async def cancel_for_source(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    source_module: str,
    source_record_id: uuid.UUID,
) -> int:
    """Cancel every pending message a record produced — EC-M8-08.

    Used when the thing a message was about stops being true: a client leaves
    ``active``, an appointment is cancelled, a plan is superseded before its
    delivery went out.

    🔒 Only ``pending`` rows are touched. A message already dispatched cannot be
    un-sent, and rewriting its row to ``cancelled`` would make the delivery log
    and the queue disagree about what the client received.

    Returns:
        How many messages were cancelled.
    """
    result = await session.execute(
        update(ScheduledMessage)
        .where(
            ScheduledMessage.tenant_id == tenant_id,
            ScheduledMessage.source_module == source_module,
            ScheduledMessage.source_record_id == source_record_id,
            ScheduledMessage.state == ScheduledState.PENDING,
        )
        .values(state=ScheduledState.CANCELLED.value, resolved_at=utc_now())
    )
    return result.rowcount or 0


async def cancel_for_client(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    template_codes: Sequence[str] | None = None,
) -> int:
    """Cancel a client's pending messages — FR-M8-025, EC-M8-08.

    ⚠️ **Not a substitute for suppression.** This is for the case where the
    message should never be sent *at all* — the client left ``active``, so the
    check-in that was queued for Friday is no longer about anything. Suppression
    handles the case where it should not be sent *now*, and records why. Using
    cancellation where suppression belongs would destroy the reason
    (AC-M8-004).

    Args:
        template_codes: Restrict to these message types. ``None`` cancels every
            pending message for the client, which is what an archive does.
    """
    statement = update(ScheduledMessage).where(
        ScheduledMessage.tenant_id == tenant_id,
        ScheduledMessage.client_id == client_id,
        ScheduledMessage.state == ScheduledState.PENDING,
    )
    if template_codes is not None:
        statement = statement.where(
            ScheduledMessage.template_id.in_(
                select(MessageTemplate.id).where(MessageTemplate.code.in_(template_codes))
            )
        )

    result = await session.execute(
        statement.values(state=ScheduledState.CANCELLED.value, resolved_at=utc_now())
    )
    return result.rowcount or 0


async def load_scheduled(
    session: AsyncSession, *, tenant_id: uuid.UUID, scheduled_message_id: uuid.UUID
) -> ScheduledMessage:
    """One scheduled message, for a caller that must authorize against its client.

    🔒 Separate from :func:`cancel_one` so the route can decide *before* the
    write. Authorizing after a mutation is a mutation an unauthorized caller
    performed.

    Raises:
        NotFoundError: If it does not exist for this tenant. ⚠️ RLS already makes
            another tenant's row invisible, so "not found" is the honest answer
            rather than a masked 403.
    """
    row = await session.scalar(
        select(ScheduledMessage).where(
            ScheduledMessage.tenant_id == tenant_id,
            ScheduledMessage.id == scheduled_message_id,
        )
    )
    if row is None:
        raise NotFoundError(
            "That scheduled message no longer exists.",
            action="Refresh the list of pending messages.",
        )
    return row


async def cancel_one(
    session: AsyncSession, *, tenant_id: uuid.UUID, scheduled_message_id: uuid.UUID
) -> ScheduledMessage:
    """Cancel one pending message — FR-M8-028's practitioner control.

    Raises:
        NotFoundError: If it does not exist for this tenant.
        ValidationError: If it is no longer pending. 🔒 There is no "unsend":
            once the engine has handed a message to a transport, the honest
            record is the delivery log, and an endpoint that appeared to undo a
            sent message would misinform the practitioner about what their
            client saw.
    """
    row = await session.scalar(
        select(ScheduledMessage).where(
            ScheduledMessage.tenant_id == tenant_id,
            ScheduledMessage.id == scheduled_message_id,
        )
    )
    if row is None:
        raise NotFoundError(
            "That scheduled message no longer exists.",
            action="Refresh the list of pending messages.",
        )
    if row.state is not ScheduledState.PENDING:
        raise ValidationError(
            f"This message is already {row.state.value} and cannot be cancelled.",
            action="Check the message history for what was actually sent.",
        )

    row.state = ScheduledState.CANCELLED
    row.resolved_at = utc_now()
    await session.flush()
    return row


async def list_pending(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    limit: int = 50,
) -> list[tuple[ScheduledMessage, Template]]:
    """A client's queued-but-unsent messages — FR-M8-028.

    🔒 Returned with their templates, because "a message is scheduled for
    Friday" is not information a practitioner can act on without knowing *which*
    message. The join is one query rather than N+1 across the page.
    """
    rows = (
        await session.execute(
            select(ScheduledMessage, MessageTemplate)
            .join(MessageTemplate, MessageTemplate.id == ScheduledMessage.template_id)
            .where(
                ScheduledMessage.tenant_id == tenant_id,
                ScheduledMessage.client_id == client_id,
                ScheduledMessage.state == ScheduledState.PENDING,
            )
            .order_by(ScheduledMessage.scheduled_for)
            .limit(limit)
        )
    ).all()
    return [(row[0], project(row[1])) for row in rows]
