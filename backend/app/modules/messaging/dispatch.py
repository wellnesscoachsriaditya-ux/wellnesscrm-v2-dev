"""The dispatch engine — the single path every message takes to a person.

🔒 **One dispatch path** (M8.3, FR-M8-001). Nothing else in the product sends a
message. A module creates a ``scheduled_messages`` row and this decides, at the
moment of sending and against live state, whether it goes.

The order of the eleven steps below is not arbitrary, and two of them are the
whole reason this module exists rather than a `send()` helper:

🔒 **Suppression is evaluated here, not at scheduling** (DB §11.5). A client's
stage, consent or tenant status can change between the two. A check-in scheduled
Monday for Friday must be suppressed if the client is paused on Wednesday —
checking only at scheduling would send it, which is exactly the failure that
erodes practitioner trust.

🔒 **Quiet hours defer, they never drop** (AC-M8-006). A message due at 02:00
moves to the next permitted window with its ``deferred_from`` recorded. Dropping
it would lose a plan a practitioner had already approved.

⚠️ **At-least-once, and the residual window is named rather than hidden.** The
send and the row that records it commit together, so a transaction that fails
after the provider accepted a message will re-send it on the next attempt. The
guards that bound this are the pending-state check on re-entry and the
already-succeeded check below; what would close it entirely is a provider-side
idempotency key, and neither Meta's Cloud API nor SMTP offers one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.clients import ContactDetails, get_client_directory
from app.kernel.entitlements import ResourceCode, get_entitlement_guard
from app.kernel.errors import EntitlementError, NotFoundError
from app.kernel.events import publish
from app.kernel.messaging import (
    DispatchStatus,
    MessageDispatched,
    ProviderTemplateStatus,
    RecipientPolicy,
    ScheduledState,
    SuppressionReason,
    defer_past_quiet_hours,
    evaluate_suppression,
    is_stale,
    utc_now,
)
from app.kernel.messaging import MessageCategory as EngineCategory
from app.kernel.messaging import TransportType as EngineTransport
from app.kernel.models import Subscription, Tenant, TenantStatus, TransportType, User
from app.kernel.notifications import (
    MessageCategory as ConsentCategory,
)
from app.kernel.notifications import (
    Notification,
    Recipient,
    TransportNotConfiguredError,
    available_transports,
    get_transport,
    purpose_for,
)
from app.modules.messaging.consent import has_withdrawn
from app.modules.messaging.models import MessageDispatch, ScheduledMessage
from app.modules.messaging.preferences import resolve
from app.modules.messaging.templates import Template, load_by_id, render
from app.modules.messaging.transports import TransportChoice, choose

#: 🔒 How the engine's four categories map onto the three the consent model
#: knows about (``kernel.notifications.MessageCategory``).
#:
#: ⚠️ ``NUDGE`` maps to ``TRANSACTIONAL``, not ``REMINDER``, and that is a
#: decision worth defending. `REMINDER` binds to the `appointment_reminders`
#: purpose, and a progress check-in is not an appointment reminder — a client
#: who declined appointment reminders has said nothing about their weekly
#: check-in. Both are service delivery, which is an essential purpose and
#: therefore in force for any live engagement.
#:
#: ⚠️ Nothing maps to ``MARKETING``. The engine has no marketing category at
#: MVP, and M8.4 puts marketing-style messaging over this channel out of scope
#: entirely — a mapping here would be the first step to acquiring one.
_CONSENT_CATEGORY: dict[EngineCategory, ConsentCategory] = {
    EngineCategory.TRANSACTIONAL: ConsentCategory.TRANSACTIONAL,
    EngineCategory.REMINDER: ConsentCategory.REMINDER,
    EngineCategory.NUDGE: ConsentCategory.TRANSACTIONAL,
    EngineCategory.NOTIFICATION: ConsentCategory.TRANSACTIONAL,
}

#: Statuses that mean the frequency cap should count this attempt — DB §11.5.
#: 🔒 A failed attempt does not count. Capping a client because we tried and
#: failed six times would silence the one message that finally worked.
_COUNTED_STATUSES: tuple[DispatchStatus, ...] = (
    DispatchStatus.SENT,
    DispatchStatus.DELIVERED,
    DispatchStatus.READ,
)

#: Failure codes that must not be retried. 🔒 Retrying these buys an identical
#: failure per attempt and delays the dead-letter that tells someone the real
#: problem (`kernel.jobs.mark_failed`'s `terminal` argument makes the same
#: argument for the queue).
NON_RETRYABLE_CODES: frozenset[str] = frozenset(
    {"no_address", "provider_rejected", "invalid_recipient", "template_not_approved"}
)


class RetryableDispatchError(RuntimeError):
    """A transport failed in a way that another attempt could survive.

    🔒 Raised so the *existing* job queue applies its retry policy
    (`JobClass.DISPATCH`: three attempts, exponential backoff). Messaging does
    not implement retries of its own — a second retry mechanism would have its
    own backoff, its own ceiling and its own dead-letter, none of which an
    operator would find.
    """


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """What one pass of the engine did with one scheduled message."""

    scheduled_message_id: uuid.UUID
    state: ScheduledState
    dispatch_id: uuid.UUID | None = None
    status: DispatchStatus | None = None
    suppression_reason: SuppressionReason | None = None
    #: Set when quiet hours moved the message — AC-M8-006's visible outcome.
    deferred_to: datetime | None = None


async def dispatch_scheduled(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    scheduled_message_id: uuid.UUID,
    now: datetime | None = None,
) -> DispatchOutcome:
    """Take one due message all the way to a transport, or record why not.

    The whole of FR-M8-003…010 happens here, in this order:

    1. Load the queued row and refuse to act twice on it.
    2. Load the template *version it was scheduled against*.
    3. Load live recipient, client and tenant state.
    4. Apply quiet hours — defer, never drop.
    5. Apply staleness — expire rather than send late.
    6. Choose a transport and resolve the address.
    7. Evaluate suppression through the kernel, on live facts.
    8. Write the immutable attempt row.
    9. Send.
    10. Record the outcome on the attempt row.
    11. Publish, so the timeline shows it.

    Raises:
        RetryableDispatchError: The transport failed transiently. The job queue
            retries; the attempt is already recorded either way.
    """
    moment = now or utc_now()

    message = await session.scalar(
        select(ScheduledMessage).where(
            ScheduledMessage.tenant_id == tenant_id,
            ScheduledMessage.id == scheduled_message_id,
        )
    )
    if message is None:
        raise NotFoundError(
            "That scheduled message no longer exists.",
            action="Nothing to do — it was cancelled or purged.",
        )

    # 🔒 Step 1. Re-entry guard. A lease can expire while a job legitimately
    # runs (DB §13.3) and the queue will hand the job to another worker; a
    # message already resolved must not be sent a second time.
    if message.state is not ScheduledState.PENDING:
        return DispatchOutcome(
            scheduled_message_id=message.id,
            state=message.state,
            suppression_reason=message.suppression_reason,
        )

    template = await load_by_id(session, template_id=message.template_id)
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:  # pragma: no cover — the FK makes this unreachable
        raise NotFoundError("Unknown tenant.", action="Contact support.")

    contact, client_is_active = await _resolve_recipient(session, message, tenant_id=tenant_id)

    preference_scope = message.client_id
    tenant_preference = await resolve(
        session, tenant_id=tenant_id, client_id=None, template_code=template.code
    )
    preference = await resolve(
        session,
        tenant_id=tenant_id,
        client_id=preference_scope,
        template_code=template.code,
    )

    # ─── Step 4: quiet hours (AC-M8-006) ─────────────────────────────────
    #
    # 🔒 Evaluated in the *tenant's* timezone, which is the practice's local
    # time. The client's own zone is not recorded anywhere at MVP, and guessing
    # one from a phone number's country code would be wrong for exactly the
    # diaspora clients an Indian practice serves.
    local_moment = moment.astimezone(_zone(tenant.timezone))
    permitted_local = defer_past_quiet_hours(
        local_moment,
        start=preference.quiet_hours_start,
        end=preference.quiet_hours_end,
    )
    if permitted_local != local_moment:
        deferred_to = permitted_local.astimezone(UTC)
        # ⚠️ `deferred_from` is written only once. A message deferred on two
        # consecutive nights should still record when it was *originally* due;
        # overwriting would lose the fact the first deferral happened.
        if message.deferred_from is None:
            message.deferred_from = message.scheduled_for
        message.scheduled_for = deferred_to
        await session.flush()
        return DispatchOutcome(
            scheduled_message_id=message.id,
            state=ScheduledState.PENDING,
            deferred_to=deferred_to,
        )

    # ─── Step 5: staleness (EC-M8-05) ────────────────────────────────────
    #
    # 🔒 After quiet hours, not before. A message deferred from 02:00 to 08:00
    # is not late — it is waiting — and measuring staleness against its original
    # due time would expire every message the quiet window ever touched.
    if is_stale(
        message.scheduled_for,
        now=moment,
        tolerance_minutes=template.staleness_tolerance_minutes,
    ):
        message.state = ScheduledState.EXPIRED
        message.resolved_at = moment
        await session.flush()
        return DispatchOutcome(scheduled_message_id=message.id, state=ScheduledState.EXPIRED)

    # ─── Step 6: transport and address ───────────────────────────────────
    choice = choose(
        template=template,
        contact=contact,
        available=available_transports(),
        preferred=preference.transport,
    )

    # ─── Step 7: suppression, on live facts (DB §11.5) ───────────────────
    reason = await _suppression_for(
        session,
        tenant=tenant,
        message=message,
        template=template,
        choice=choice,
        client_is_active=client_is_active,
        tenant_enabled=tenant_preference.is_enabled,
        client_enabled=preference.is_enabled,
        weekly_cap=preference.max_messages_per_week,
        moment=moment,
    )
    if reason is not None:
        message.state = ScheduledState.SUPPRESSED
        message.suppression_reason = reason
        message.resolved_at = moment
        await session.flush()
        return DispatchOutcome(
            scheduled_message_id=message.id,
            state=ScheduledState.SUPPRESSED,
            suppression_reason=reason,
        )

    # 🔒 A message whose earlier attempt already succeeded must not be sent
    # again. This is the second half of the re-entry guard, and it catches the
    # case the state check cannot: a transaction that committed the send and
    # failed before committing the state change.
    already_sent = await session.scalar(
        select(MessageDispatch.id).where(
            MessageDispatch.tenant_id == tenant_id,
            MessageDispatch.scheduled_message_id == message.id,
            MessageDispatch.status.in_(_COUNTED_STATUSES),
        )
    )
    if already_sent is not None:
        message.state = ScheduledState.DISPATCHED
        message.resolved_at = moment
        await session.flush()
        return DispatchOutcome(
            scheduled_message_id=message.id,
            state=ScheduledState.DISPATCHED,
            dispatch_id=already_sent,
            status=DispatchStatus.SENT,
        )

    # ─── Steps 8–11 ──────────────────────────────────────────────────────
    message.attempt_count += 1
    return await _attempt(
        session,
        tenant_id=tenant_id,
        message=message,
        template=template,
        choice=choice,
        moment=moment,
    )


async def _attempt(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    message: ScheduledMessage,
    template: Template,
    choice: TransportChoice,
    moment: datetime,
) -> DispatchOutcome:
    """Write the attempt row, send, and record what happened.

    🔒 The row is written **before** the send, so a crash mid-send leaves
    evidence that an attempt was made. FR-M8-003 requires every attempt to be
    recorded, and an INSERT that only happens on success records successes.
    """
    dispatch = MessageDispatch(
        tenant_id=tenant_id,
        client_id=message.client_id,
        scheduled_message_id=message.id,
        template_id=template.id,
        template_version=template.version,
        transport=choice.transport,
        # ⚠️ A dash rather than an empty string when there is no address: the
        # column is NOT NULL with a non-blank check, and the row must still
        # exist to carry `failure_code = 'no_address'`.
        recipient_address=choice.address or "-",
        status=DispatchStatus.QUEUED,
        attempt_number=message.attempt_count,
    )
    session.add(dispatch)
    await session.flush()

    if not choice.address:
        return await _record_failure(
            session,
            message=message,
            dispatch=dispatch,
            template=template,
            code="no_address",
            reason=f"No {choice.transport.value} address on file for this recipient.",
            moment=moment,
        )

    notification = Notification(
        tenant_id=tenant_id,
        recipient=Recipient(address=choice.address, subject_id=message.client_id),
        template_code=template.code,
        category=_CONSENT_CATEGORY[template.category],
        transports=(choice.transport,),
        variables=dict(message.template_variables),
        client_id=message.client_id,
        provider_template_name=template.provider_template_name,
        body=_render(template, message),
        dispatch_id=dispatch.id,
    )

    try:
        record = await get_transport(choice.transport).send(notification)
    except TransportNotConfiguredError as error:
        # A wiring fault, not a provider fault. Retrying will not fix it, and the
        # dead-letter is what tells an operator the process is misconfigured.
        return await _record_failure(
            session,
            message=message,
            dispatch=dispatch,
            template=template,
            code="transport_not_configured",
            reason=str(error),
            moment=moment,
            retryable=False,
        )
    except Exception as error:
        # 🔒 An adapter *should* return a failed `DeliveryRecord` rather than
        # raise (`NotificationTransport.send`'s contract). One that raises is
        # treated as a transient transport failure, because the alternative —
        # letting it escape — would fail the job without recording the attempt.
        return await _record_failure(
            session,
            message=message,
            dispatch=dispatch,
            template=template,
            code="transport_error",
            reason=f"{type(error).__name__}: {error}",
            moment=moment,
        )

    if record.status.value in {"failed", "suppressed"}:
        return await _record_failure(
            session,
            message=message,
            dispatch=dispatch,
            template=template,
            code=_failure_code(record.failure_reason),
            reason=record.failure_reason or "The transport reported a failure.",
            moment=moment,
        )

    dispatch.status = DispatchStatus.SENT
    dispatch.provider_message_id = record.provider_message_id
    dispatch.sent_at = moment
    dispatch.updated_at = moment
    message.state = ScheduledState.DISPATCHED
    message.resolved_at = moment
    await session.flush()

    await _meter(session, tenant_id=tenant_id, dispatch=dispatch)
    await _publish(session, message=message, dispatch=dispatch, template=template, moment=moment)

    return DispatchOutcome(
        scheduled_message_id=message.id,
        state=ScheduledState.DISPATCHED,
        dispatch_id=dispatch.id,
        status=DispatchStatus.SENT,
    )


async def _record_failure(
    session: AsyncSession,
    *,
    message: ScheduledMessage,
    dispatch: MessageDispatch,
    template: Template,
    code: str,
    reason: str,
    moment: datetime,
    retryable: bool | None = None,
) -> DispatchOutcome:
    """Record a failed attempt, then decide whether the queue should try again.

    🔒 The attempt row is completed either way. AC-M8-007 requires terminal
    failure to be visible to the practitioner, and a failure that only exists as
    a raised exception is visible to nobody.
    """
    dispatch.status = DispatchStatus.FAILED
    dispatch.failure_code = code
    # ⚠️ Bounded before storage. A provider error can carry a whole request body,
    # and this column is read by operators (NFR-033).
    dispatch.failure_reason = reason[:1000]
    dispatch.updated_at = moment
    await session.flush()

    await _publish(session, message=message, dispatch=dispatch, template=template, moment=moment)

    may_retry = (code not in NON_RETRYABLE_CODES) if retryable is None else retryable
    if not may_retry:
        # 🔒 Terminal. The message is resolved so it stops being scanned as due;
        # the delivery log carries why. `scheduled_state` has no `failed` member
        # by design — the queue records intent, the log records outcome.
        message.state = ScheduledState.DISPATCHED
        message.resolved_at = moment
        await session.flush()
        return DispatchOutcome(
            scheduled_message_id=message.id,
            state=ScheduledState.DISPATCHED,
            dispatch_id=dispatch.id,
            status=DispatchStatus.FAILED,
        )

    raise RetryableDispatchError(f"{code}: {reason[:200]}")


async def resolve_exhausted(
    session: AsyncSession, *, tenant_id: uuid.UUID, scheduled_message_id: uuid.UUID
) -> None:
    """Close out a message whose job exhausted its retries — FR-M8-004.

    🔒 Called by the job handler when the queue reports this was the final
    attempt. Without it a message that failed three times would stay ``pending``
    and be re-enqueued by the next sweep, retrying forever outside the queue's
    own ceiling — the bounded attempt count would be bounded per job and
    unbounded per message.
    """
    message = await session.scalar(
        select(ScheduledMessage).where(
            ScheduledMessage.tenant_id == tenant_id,
            ScheduledMessage.id == scheduled_message_id,
            ScheduledMessage.state == ScheduledState.PENDING,
        )
    )
    if message is None:
        return
    message.state = ScheduledState.DISPATCHED
    message.resolved_at = utc_now()
    await session.flush()


# ─── The inputs suppression is judged on ─────────────────────────────────


async def _resolve_recipient(
    session: AsyncSession, message: ScheduledMessage, *, tenant_id: uuid.UUID
) -> tuple[ContactDetails, bool]:
    """Live contact details and engagement state for the recipient.

    🔒 Read **at dispatch**, never carried on the scheduled row (EC-M8-08). A
    client who changed number between scheduling and sending is reached at the
    new one, and the log keeps the address each attempt actually used.
    """
    if message.client_id is not None:
        directory = get_client_directory()
        identity = await directory.find(session, tenant_id=tenant_id, client_id=message.client_id)
        contact = await directory.contact_for(
            session, tenant_id=tenant_id, client_id=message.client_id
        )
        if identity is None or contact is None:
            # An erased or purged client. Not a suppression — there is nobody to
            # suppress — so it fails as an attempt with a reason.
            return ContactDetails(mobile=None, email="-"), False
        return contact, identity.receives_engagement

    user = await session.get(User, message.recipient_user_id)
    if user is None or user.archived_at is not None:
        return ContactDetails(mobile=None, email="-"), False
    # 🔒 A practitioner recipient is never "stage inactive": FR-M1-014 is about
    # clients. Their own account status is what matters, and an archived user is
    # an inbox nobody reads.
    return ContactDetails(mobile=user.mobile, email=user.email), True


async def _suppression_for(
    session: AsyncSession,
    *,
    tenant: Tenant,
    message: ScheduledMessage,
    template: Template,
    choice: TransportChoice,
    client_is_active: bool,
    tenant_enabled: bool,
    client_enabled: bool,
    weekly_cap: int,
    moment: datetime,
) -> SuppressionReason | None:
    """Assemble live facts and ask the kernel — `evaluate_suppression`.

    🔒 **The decision is the kernel's, not this function's.** Everything here is
    a lookup; the ordering of the rules, the essential-template exemption and
    which reason is recorded all live in `kernel.messaging`, tested without a
    database. A second implementation of the ordering here would be a second
    answer to "why didn't it send?".
    """
    # 🔒 Three separate facts collapse into `template_paused`, because that is
    # the kernel's single answer for "this message type is not sendable right
    # now", and each of them is exactly that:
    #
    #   * Meta has not approved the template — **but only on WhatsApp.** Applying
    #     a WhatsApp approval state to an email send would take the whole product
    #     offline while verification is pending, which is precisely what the
    #     sprint plan says must not happen.
    #   * The practitioner disabled the type tenant-wide (FR-M8-027).
    #   * The template is no longer published.
    provider_status = template.provider_template_status
    if choice.transport is not TransportType.WHATSAPP:
        provider_status = ProviderTemplateStatus.APPROVED
    if not tenant_enabled:
        provider_status = ProviderTemplateStatus.PAUSED

    consent_withdrawn = False
    if message.client_id is not None:
        consent_withdrawn = await has_withdrawn(
            session,
            tenant_id=tenant.id,
            client_id=message.client_id,
            purpose_code=purpose_for(choice.transport, _CONSENT_CATEGORY[template.category]),
        )

    recipient = RecipientPolicy(
        client_is_active=client_is_active,
        consent_granted=not consent_withdrawn,
        # 🔒 `trial` is an active tenant. FR-M8-007 suppresses for a *suspended*
        # tenant; treating a trial as suspended would mean a practitioner
        # evaluating the product never sees a message work.
        tenant_is_active=tenant.status in {TenantStatus.TRIAL, TenantStatus.ACTIVE},
        # 🔒 Client-level mute only. A tenant-wide disable is `template_paused`
        # above — recording it as `client_unsubscribed` would tell a support
        # conversation the client opted out when their practitioner did.
        type_is_muted=tenant_enabled and not client_enabled,
        messages_sent_this_week=await _sent_this_week(
            session, tenant_id=tenant.id, client_id=message.client_id, moment=moment
        ),
        max_messages_per_week=weekly_cap,
        quota_remaining=await _quota_remaining(
            session, tenant_id=tenant.id, template=template, transport=choice.transport
        ),
    )

    return evaluate_suppression(template.policy(provider_status=provider_status), recipient)


async def _sent_this_week(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID | None,
    moment: datetime,
) -> int:
    """How many messages this client has actually received in the last 7 days.

    🔒 **Across all message types** (EC-M8-09). The cap protects the client from
    the *practice*, not from one feature, so counting per type would let three
    modules each send their quota on the same afternoon.

    ⚠️ A rolling seven days rather than a calendar week. A calendar week resets
    at midnight on Sunday, which would let a client receive a fortnight's worth
    of messages across a weekend and stay inside the cap both times.
    """
    if client_id is None:
        # 🔒 A practitioner's own notifications are not capped by a limit
        # designed to protect clients from over-contact. Their inbox is theirs,
        # and a missed new-lead notification is a lost sale (US-M2-03).
        return 0

    since = moment.timestamp() - 7 * 24 * 3600
    count = await session.scalar(
        select(func.count())
        .select_from(MessageDispatch)
        .where(
            MessageDispatch.tenant_id == tenant_id,
            MessageDispatch.client_id == client_id,
            MessageDispatch.status.in_(_COUNTED_STATUSES),
            MessageDispatch.created_at >= datetime.fromtimestamp(since, tz=UTC),
        )
    )
    return int(count or 0)


async def _quota_remaining(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    template: Template,
    transport: TransportType,
) -> int | None:
    """Whether the tenant's messaging entitlement still permits a send.

    🔒 FR-M8-010. ``None`` means "no quota applies", which the kernel treats as
    unlimited — correct for the transports that cost nothing. Only WhatsApp is a
    metered resource (``whatsapp_messages``, DB §14.1), because it is the only
    one with a per-message cost (M8.4's ₹60–90/tenant/month risk).

    ⚠️ Essential templates are not checked at all rather than checked and
    exempted. `get_entitlement_guard().require` *raises* on refusal and records
    nothing on success, so calling it for a magic link would produce an
    exception the caller must remember to ignore — an exemption that depends on
    a `try` block is one someone eventually removes.
    """
    if transport is not TransportType.WHATSAPP or template.is_essential:
        return None

    # 🔒 **No subscription means no quota, not an exhausted one.** The guard
    # treats an indeterminate allowance as a refusal (FR-M0-046, fail-safe),
    # which is right for an action a practitioner takes — adding a client
    # consumes a slot somebody is billed for. It is wrong here: billing lands in
    # S10, so a tenant in this build has no `subscriptions` row at all, and
    # recording `quota_exceeded` against them would be a false statement about a
    # plan they were never sold — printed on the practitioner's own screen as the
    # reason their client heard nothing.
    #
    # ⚠️ `subscriptions` is a kernel table (DB §14.2), so reading it here crosses
    # no module boundary. Once a tenant *has* a subscription the guard decides,
    # and an exhausted quota suppresses exactly as FR-M8-010 requires.
    has_subscription = await session.scalar(
        select(Subscription.id).where(Subscription.tenant_id == tenant_id).limit(1)
    )
    if has_subscription is None:
        return None

    try:
        await get_entitlement_guard().require(
            session, tenant_id=tenant_id, resource=ResourceCode.WHATSAPP_MESSAGES, amount=1
        )
    except EntitlementError:
        return 0
    return None


async def _meter(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    dispatch: MessageDispatch,
) -> None:
    """Record the consumption a successful send represents — FR-M8-010.

    ⚠️ **After** the send, not before. A reservation taken before an attempt
    would have to be released on failure, and a release that fails leaves a
    tenant billed for a message nobody received. Metering what actually went out
    is the version that cannot drift in the direction that costs a customer
    money.

    ⚠️ Only WhatsApp is metered — see :func:`_quota_remaining`.
    """
    if dispatch.transport is not TransportType.WHATSAPP:
        return

    # ⚠️ Metered only when there is a plan to meter against — the same reason
    # `_quota_remaining` gives. A `usage_events` row for a tenant with no
    # subscription would be a consumption nobody can reconcile.
    if (
        await session.scalar(
            select(Subscription.id).where(Subscription.tenant_id == tenant_id).limit(1)
        )
        is None
    ):
        return

    await get_entitlement_guard().record(
        session,
        tenant_id=tenant_id,
        resource=ResourceCode.WHATSAPP_MESSAGES,
        amount=1,
        source_module="messaging",
        source_record_id=dispatch.id,
    )


async def _publish(
    session: AsyncSession,
    *,
    message: ScheduledMessage,
    dispatch: MessageDispatch,
    template: Template,
    moment: datetime,
) -> None:
    """Announce the attempt so the timeline can record it (FR-M1-018)."""
    await publish(
        MessageDispatched(
            tenant_id=message.tenant_id,
            client_id=message.client_id,
            dispatch_id=dispatch.id,
            template_code=template.code,
            transport=_engine_transport(dispatch.transport),
            status=dispatch.status,
            occurred_at=moment,
        ),
        session,
    )


def _engine_transport(transport: TransportType) -> EngineTransport:
    """Translate the database's transport enum into the engine's own.

    ⚠️ The two vocabularies differ deliberately (see `models.py`): the database
    enum still carries `sms`, which the engine has no adapter for. A `sms` value
    reaching here would raise, and that is correct — it would mean a message was
    dispatched over a transport this sprint does not implement.
    """
    return EngineTransport(transport.value)


def _render(template: Template, message: ScheduledMessage) -> str:
    """The body, rendered once, from the versioned template."""
    return render(template, {key: str(value) for key, value in message.template_variables.items()})


def _failure_code(reason: str | None) -> str:
    """Classify a provider's failure so the retry decision is not guesswork."""
    if reason and "reject" in reason.lower():
        return "provider_rejected"
    return "transport_error"


def _zone(name: str) -> ZoneInfo:
    """The tenant's timezone, falling back to IST.

    ⚠️ A missing tzdata entry must not stop a message. Falling back to the
    product's launch market is a *stated* approximation; raising here would turn
    a packaging problem into an outage of the whole engine.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Asia/Kolkata")
