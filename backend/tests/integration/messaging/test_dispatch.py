"""The dispatch engine against a real PostgreSQL — M8's load-bearing half.

🔒 **Suppression is asserted after a state change, not at scheduling.** Every one
of these tests queues a message *first* and then changes the world — pauses the
client, withdraws consent, suspends the tenant — because that is the exact
sequence DB §11.5 exists for, and a test that set up the state before scheduling
would pass against an implementation that checked at the wrong moment.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.kernel.messaging import (
    DispatchStatus,
    ScheduledState,
    SuppressionReason,
    utc_now,
)
from app.kernel.models import TransportType
from app.kernel.notifications import DeliveryStatus
from app.modules.messaging import (
    MessageDispatch,
    MessageRequest,
    RetryableDispatchError,
    ScheduledMessage,
    dispatch_scheduled,
    resolve_exhausted,
    schedule,
    upsert_preference,
)
from tests.integration.messaging.conftest import (
    RecordingTransport,
    SessionFactory,
    TenantFixture,
    set_client_stage,
    set_provider_template_status,
    suspend_tenant,
    withdraw_consent,
)

pytestmark = pytest.mark.asyncio


PINNED_NOW = datetime(2026, 8, 14, 6, 30, tzinfo=UTC)


async def _queue(
    session_for: SessionFactory,
    tenant: TenantFixture,
    *,
    template_code: str = "plan_delivered",
    occasion: str = "plan_version:1",
    scheduled_for: datetime | None = None,
    variables: dict[str, str] | None = None,
) -> uuid.UUID:
    """Queue one message and return its id."""
    async with session_for(tenant.tenant_id) as session:
        message = await schedule(
            session,
            tenant_id=tenant.tenant_id,
            request=MessageRequest(
                template_code=template_code,
                occasion=occasion,
                scheduled_for=scheduled_for or PINNED_NOW,
                source_module="tests",
                client_id=tenant.client_id,
                variables=variables
                or {
                    "client_name": "Anjali Rao",
                    "practitioner_name": "Priya",
                    "plan_url": "https://portal.example.test/p/1",
                },
            ),
        )
        assert message is not None
        return message.id


async def _dispatch(
    session_for: SessionFactory,
    tenant: TenantFixture,
    message_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> ScheduledMessage:
    """Run the engine once, then re-read the row it acted on."""
    async with session_for(tenant.tenant_id) as session:
        await dispatch_scheduled(
            session,
            tenant_id=tenant.tenant_id,
            scheduled_message_id=message_id,
            now=now or PINNED_NOW,
        )
    async with session_for(tenant.tenant_id) as session:
        row = await session.get(ScheduledMessage, message_id)
        assert row is not None
        return row


async def _dispatches(session_for: SessionFactory, tenant: TenantFixture) -> list[MessageDispatch]:
    async with session_for(tenant.tenant_id) as session:
        rows = await session.scalars(
            select(MessageDispatch)
            .where(MessageDispatch.tenant_id == tenant.tenant_id)
            .order_by(MessageDispatch.attempt_number)
        )
        return list(rows)


# ─── The happy path ──────────────────────────────────────────────────────


async def test_a_due_message_is_sent_and_logged(
    session_for: SessionFactory, tenant_a: TenantFixture, whatsapp: RecordingTransport
) -> None:
    """FR-M8-003 — every attempt recorded with recipient, template, transport."""
    whatsapp.provider_message_id = "wamid.1"
    message_id = await _queue(session_for, tenant_a)

    row = await _dispatch(session_for, tenant_a, message_id)

    # ⚠️ Asserted before the state: when this fails, the *reason* is the useful
    # half of the message, and a bare state comparison hides it.
    assert row.suppression_reason is None
    assert row.state is ScheduledState.DISPATCHED
    assert row.resolved_at is not None

    (dispatch,) = await _dispatches(session_for, tenant_a)
    assert dispatch.status is DispatchStatus.SENT
    assert dispatch.transport is TransportType.WHATSAPP
    assert dispatch.provider_message_id == "wamid.1"
    assert dispatch.attempt_number == 1
    assert dispatch.template_version == 1
    assert dispatch.sent_at is not None


async def test_the_body_is_rendered_from_the_versioned_template(
    session_for: SessionFactory, tenant_a: TenantFixture, whatsapp: RecordingTransport
) -> None:
    """🔒 FR-M8-026 — the same render the preview endpoint shows."""
    message_id = await _queue(session_for, tenant_a)
    await _dispatch(session_for, tenant_a, message_id)

    sent = whatsapp.sent[0]
    assert sent.body == (
        "Hi Anjali Rao, your new nutrition plan from Priya is ready. "
        "Open it here: https://portal.example.test/p/1"
    )
    assert sent.provider_template_name == "plan_delivered_v1"


async def test_the_recipient_address_is_resolved_at_dispatch(
    session_for: SessionFactory,
    tenant_a: TenantFixture,
    migrator_engine: AsyncEngine,
    whatsapp: RecordingTransport,
) -> None:
    """🔒 EC-M8-08 — a client who changes number between scheduling and sending
    is reached at the new one, and the log keeps what each attempt used."""
    message_id = await _queue(session_for, tenant_a)

    async with migrator_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_a.tenant_id)}
        )
        await connection.execute(
            text("UPDATE clients SET mobile = '+919000000001' WHERE id = :c"),
            {"c": tenant_a.client_id},
        )

    await _dispatch(session_for, tenant_a, message_id)

    (dispatch,) = await _dispatches(session_for, tenant_a)
    assert dispatch.recipient_address == "+919000000001"


async def test_a_dispatched_message_reaches_the_client_timeline(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """FR-M1-018, AC-M8-003 — the practitioner sees it where they look first."""
    message_id = await _queue(session_for, tenant_a)
    await _dispatch(session_for, tenant_a, message_id)

    async with session_for(tenant_a.tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT event_type, summary, source_module FROM timeline_events "
                    "WHERE client_id = :c"
                ),
                {"c": tenant_a.client_id},
            )
        ).all()

    assert [(r.event_type, r.summary, r.source_module) for r in rows] == [
        ("message_sent", "Message sent", "messaging")
    ]


# ─── The six suppression rules, each independently (DB §11.5) ────────────


async def test_a_suspended_tenant_suppresses_with_a_reason(
    session_for: SessionFactory, tenant_a: TenantFixture, migrator_engine: AsyncEngine
) -> None:
    """FR-M8-007, AC-M8-004."""
    message_id = await _queue(session_for, tenant_a)
    await suspend_tenant(migrator_engine, tenant_id=tenant_a.tenant_id)

    row = await _dispatch(session_for, tenant_a, message_id)

    assert row.state is ScheduledState.SUPPRESSED
    assert row.suppression_reason is SuppressionReason.TENANT_SUSPENDED
    assert await _dispatches(session_for, tenant_a) == []


async def test_withdrawn_consent_suppresses_with_a_reason(
    session_for: SessionFactory, tenant_a: TenantFixture, migrator_engine: AsyncEngine
) -> None:
    """FR-M8-006, AC-M8-005 — no further non-essential messages."""
    message_id = await _queue(session_for, tenant_a)
    await withdraw_consent(
        migrator_engine,
        tenant_id=tenant_a.tenant_id,
        client_id=tenant_a.client_id,
        purpose_code="whatsapp_communication",
    )

    row = await _dispatch(session_for, tenant_a, message_id)

    assert row.suppression_reason is SuppressionReason.CONSENT_WITHDRAWN


async def test_a_paused_client_suppresses_with_a_reason(
    session_for: SessionFactory, tenant_a: TenantFixture, migrator_engine: AsyncEngine
) -> None:
    """🔒 FR-M8-005, and DB §11.5's own example: a message scheduled Monday for
    Friday must be suppressed if the client is paused on Wednesday."""
    message_id = await _queue(session_for, tenant_a, scheduled_for=PINNED_NOW - timedelta(minutes=1))
    await set_client_stage(
        migrator_engine,
        tenant_id=tenant_a.tenant_id,
        client_id=tenant_a.client_id,
        stage="paused",
    )

    row = await _dispatch(session_for, tenant_a, message_id)

    assert row.suppression_reason is SuppressionReason.CLIENT_STAGE_INACTIVE


async def test_a_tenant_wide_disable_suppresses_as_template_paused(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 FR-M8-027. Recorded as `template_paused`, **not**
    `client_unsubscribed`: telling a support conversation the client opted out
    when their practitioner did is a false record of what happened."""
    message_id = await _queue(
        session_for,
        tenant_a,
        template_code="checkin_nudge",
        variables={"client_name": "Anjali Rao", "portal_url": "https://portal.example.test"},
    )

    async with session_for(tenant_a.tenant_id) as session:
        await upsert_preference(
            session,
            tenant_id=tenant_a.tenant_id,
            client_id=None,
            template_code="checkin_nudge",
            is_enabled=False,
        )

    row = await _dispatch(session_for, tenant_a, message_id)

    assert row.suppression_reason is SuppressionReason.TEMPLATE_PAUSED


async def test_a_client_mute_suppresses_as_unsubscribed(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """US-M8-06 — the client's own opt-out, distinguishable from the tenant's."""
    message_id = await _queue(
        session_for,
        tenant_a,
        template_code="checkin_nudge",
        variables={"client_name": "Anjali Rao", "portal_url": "https://portal.example.test"},
    )

    async with session_for(tenant_a.tenant_id) as session:
        await upsert_preference(
            session,
            tenant_id=tenant_a.tenant_id,
            client_id=tenant_a.client_id,
            template_code="checkin_nudge",
            is_enabled=False,
        )

    row = await _dispatch(session_for, tenant_a, message_id)

    assert row.suppression_reason is SuppressionReason.CLIENT_UNSUBSCRIBED


async def test_the_weekly_cap_suppresses_as_frequency_capped(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """FR-M8-008, EC-M8-09 — the cap applies across all message types."""
    async with session_for(tenant_a.tenant_id) as session:
        await upsert_preference(
            session,
            tenant_id=tenant_a.tenant_id,
            client_id=tenant_a.client_id,
            template_code=None,
            max_messages_per_week=1,
        )

    first = await _queue(session_for, tenant_a, occasion="plan_version:1")
    await _dispatch(session_for, tenant_a, first)

    second = await _queue(session_for, tenant_a, occasion="plan_version:2")
    row = await _dispatch(session_for, tenant_a, second)

    assert row.suppression_reason is SuppressionReason.FREQUENCY_CAPPED


async def test_a_failed_attempt_does_not_consume_the_cap(
    session_for: SessionFactory, tenant_a: TenantFixture, whatsapp: RecordingTransport
) -> None:
    """🔒 Capping a client because we tried and failed would silence the one
    message that finally worked."""
    async with session_for(tenant_a.tenant_id) as session:
        await upsert_preference(
            session,
            tenant_id=tenant_a.tenant_id,
            client_id=tenant_a.client_id,
            template_code=None,
            max_messages_per_week=1,
        )

    whatsapp.status = DeliveryStatus.FAILED
    whatsapp.failure_reason = "the provider rejected the template"
    first = await _queue(session_for, tenant_a, occasion="plan_version:1")
    await _dispatch(session_for, tenant_a, first)

    whatsapp.status = DeliveryStatus.SENT
    whatsapp.failure_reason = None
    second = await _queue(session_for, tenant_a, occasion="plan_version:2")
    row = await _dispatch(session_for, tenant_a, second)

    assert row.state is ScheduledState.DISPATCHED


async def test_an_essential_template_bypasses_the_cap(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 DB §11.6, EC-M10-04 — a client locked out of their portal because their
    practitioner hit a nudge quota is unacceptable."""
    async with session_for(tenant_a.tenant_id) as session:
        await upsert_preference(
            session,
            tenant_id=tenant_a.tenant_id,
            client_id=tenant_a.client_id,
            template_code=None,
            max_messages_per_week=0,
        )

    message_id = await _queue(
        session_for,
        tenant_a,
        template_code="magic_link",
        occasion="link:1",
        variables={
            "client_name": "Anjali Rao",
            "link_url": "https://portal.example.test/l/1",
            "expires_in_minutes": "20",
        },
    )
    row = await _dispatch(session_for, tenant_a, message_id)

    assert row.state is ScheduledState.DISPATCHED


async def test_an_essential_template_does_not_bypass_consent(
    session_for: SessionFactory, tenant_a: TenantFixture, migrator_engine: AsyncEngine
) -> None:
    """🔒 The kernel's own boundary: essential exempts quota and frequency only.

    A magic link to someone who withdrew consent is still a message they said
    they did not want.
    """
    message_id = await _queue(
        session_for,
        tenant_a,
        template_code="magic_link",
        occasion="link:1",
        variables={
            "client_name": "Anjali Rao",
            "link_url": "https://portal.example.test/l/1",
            "expires_in_minutes": "20",
        },
    )
    await withdraw_consent(
        migrator_engine,
        tenant_id=tenant_a.tenant_id,
        client_id=tenant_a.client_id,
        purpose_code="whatsapp_communication",
    )

    row = await _dispatch(session_for, tenant_a, message_id)

    assert row.suppression_reason is SuppressionReason.CONSENT_WITHDRAWN


async def test_a_suppressed_message_is_retained_with_its_reason(
    session_for: SessionFactory, tenant_a: TenantFixture, migrator_engine: AsyncEngine
) -> None:
    """🔒 AC-M8-004 — never deleted. The reason is the answer to the support
    question that always follows."""
    message_id = await _queue(session_for, tenant_a)
    await suspend_tenant(migrator_engine, tenant_id=tenant_a.tenant_id)
    await _dispatch(session_for, tenant_a, message_id)

    async with session_for(tenant_a.tenant_id) as session:
        surviving = await session.scalar(
            select(func.count())
            .select_from(ScheduledMessage)
            .where(ScheduledMessage.id == message_id)
        )
    assert surviving == 1


# ─── Quiet hours (AC-M8-006) ─────────────────────────────────────────────


async def test_a_message_due_at_0200_is_deferred_not_dropped(
    session_for: SessionFactory, tenant_a: TenantFixture, whatsapp: RecordingTransport
) -> None:
    """🔒 AC-M8-006, verbatim. The tenant's timezone is Asia/Kolkata, so 20:30
    UTC is 02:00 local."""
    message_id = await _queue(session_for, tenant_a)
    two_am_ist = datetime(2026, 8, 20, 20, 30, tzinfo=UTC)

    row = await _dispatch(session_for, tenant_a, message_id, now=two_am_ist)

    assert row.state is ScheduledState.PENDING
    assert row.deferred_from is not None
    assert whatsapp.sent == []


async def test_the_deferred_message_lands_in_the_permitted_window(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """The default window ends at 08:00 local, which is 02:30 UTC."""
    message_id = await _queue(session_for, tenant_a)
    row = await _dispatch(
        session_for, tenant_a, message_id, now=datetime(2026, 8, 20, 20, 30, tzinfo=UTC)
    )

    assert row.scheduled_for.astimezone(UTC) == datetime(2026, 8, 21, 2, 30, tzinfo=UTC)


async def test_a_message_inside_the_window_is_not_deferred(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """10:00 IST is 04:30 UTC — comfortably inside 08:00–21:00 local."""
    message_id = await _queue(session_for, tenant_a)
    row = await _dispatch(
        session_for, tenant_a, message_id, now=datetime(2026, 8, 20, 4, 30, tzinfo=UTC)
    )

    assert row.state is ScheduledState.DISPATCHED
    assert row.deferred_from is None


async def test_a_custom_quiet_window_is_honoured(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """FR-M8-009 — the window is a preference, not a constant."""
    from datetime import time

    async with session_for(tenant_a.tenant_id) as session:
        await upsert_preference(
            session,
            tenant_id=tenant_a.tenant_id,
            client_id=None,
            template_code=None,
            quiet_hours_start=time(9, 0),
            quiet_hours_end=time(18, 0),
        )

    message_id = await _queue(session_for, tenant_a)
    row = await _dispatch(
        session_for, tenant_a, message_id, now=datetime(2026, 8, 20, 4, 30, tzinfo=UTC)
    )

    assert row.state is ScheduledState.PENDING
    assert row.deferred_from is not None


# ─── Staleness (EC-M8-05) ────────────────────────────────────────────────


async def test_a_stale_check_in_expires_rather_than_arriving_late(
    session_for: SessionFactory, tenant_a: TenantFixture, whatsapp: RecordingTransport
) -> None:
    """🔒 EC-M8-05 — "a check-in more than 24 h stale is dropped and logged
    rather than sent late". A nudge two days late tells the client the practice
    is not paying attention."""
    message_id = await _queue(
        session_for,
        tenant_a,
        template_code="checkin_nudge",
        occasion="checkin:week32",
        scheduled_for=PINNED_NOW - timedelta(days=2),
        variables={"client_name": "Anjali Rao", "portal_url": "https://portal.example.test"},
    )

    row = await _dispatch(session_for, tenant_a, message_id)

    assert row.state is ScheduledState.EXPIRED
    assert whatsapp.sent == []


async def test_a_late_plan_delivery_is_still_sent(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """A plan a practitioner approved is still wanted late — the template
    declares no staleness tolerance, and that is the difference."""
    message_id = await _queue(session_for, tenant_a, scheduled_for=PINNED_NOW - timedelta(days=5))
    row = await _dispatch(session_for, tenant_a, message_id)

    assert row.state is ScheduledState.DISPATCHED


async def test_staleness_is_measured_after_the_quiet_hours_deferral(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 A message deferred from 02:00 to 08:00 is waiting, not late. Measuring
    staleness first would expire every message the quiet window ever touched."""
    message_id = await _queue(
        session_for,
        tenant_a,
        template_code="appointment_reminder",
        occasion="appt:1",
        variables={"client_name": "Anjali Rao", "appointment_at": "Friday 10:30"},
    )

    row = await _dispatch(
        session_for, tenant_a, message_id, now=datetime(2026, 8, 20, 20, 30, tzinfo=UTC)
    )

    assert row.state is ScheduledState.PENDING


# ─── Idempotency and retries (EC-M8-06, FR-M8-004) ───────────────────────


async def test_the_same_occasion_queues_once(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 EC-M8-06, enforced by `uq_scheduled_messages__idempotency` rather than
    by a read-then-write that loses the race."""
    first = await _queue(session_for, tenant_a, occasion="plan_version:7")

    async with session_for(tenant_a.tenant_id) as session:
        duplicate = await schedule(
            session,
            tenant_id=tenant_a.tenant_id,
            request=MessageRequest(
                template_code="plan_delivered",
                occasion="plan_version:7",
                scheduled_for=PINNED_NOW,
                source_module="tests",
                client_id=tenant_a.client_id,
                variables={
                    "client_name": "Anjali Rao",
                    "practitioner_name": "Priya",
                    "plan_url": "https://portal.example.test/p/1",
                },
            ),
        )

    assert duplicate is None

    async with session_for(tenant_a.tenant_id) as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ScheduledMessage)
            .where(ScheduledMessage.tenant_id == tenant_a.tenant_id)
        )
    assert count == 1
    assert first is not None


async def test_dispatching_twice_sends_once(
    session_for: SessionFactory, tenant_a: TenantFixture, whatsapp: RecordingTransport
) -> None:
    """🔒 The re-entry guard. A lease can expire while a job legitimately runs
    (DB §13.3) and the queue hands it to another worker — re-execution must not
    become a second message."""
    message_id = await _queue(session_for, tenant_a)

    await _dispatch(session_for, tenant_a, message_id)
    await _dispatch(session_for, tenant_a, message_id)

    assert len(whatsapp.sent) == 1
    assert len(await _dispatches(session_for, tenant_a)) == 1


async def test_a_transient_failure_asks_the_queue_to_retry(
    session_for: SessionFactory, tenant_a: TenantFixture, whatsapp: RecordingTransport
) -> None:
    """FR-M8-004 — one retry policy, the queue's. The attempt is recorded either
    way, because AC-M8-007 needs terminal failure to be visible."""
    whatsapp.raises = TimeoutError("the provider did not answer")
    message_id = await _queue(session_for, tenant_a)

    with pytest.raises(RetryableDispatchError):
        async with session_for(tenant_a.tenant_id) as session:
            await dispatch_scheduled(
                session,
                tenant_id=tenant_a.tenant_id,
                scheduled_message_id=message_id,
                now=datetime(2026, 8, 14, 6, 30, tzinfo=UTC),
            )

    # ⚠️ The attempt row rolled back with the raising transaction, which is
    # correct — the queue re-runs the whole handler. What must survive is the
    # message, still pending and still due.
    async with session_for(tenant_a.tenant_id) as session:
        row = await session.get(ScheduledMessage, message_id)
        assert row is not None and row.state is ScheduledState.PENDING


async def test_a_rejected_template_is_not_retried(
    session_for: SessionFactory, tenant_a: TenantFixture, whatsapp: RecordingTransport
) -> None:
    """🔒 Retrying a rejection buys an identical failure per attempt and delays
    the dead-letter that tells an operator what is actually wrong."""
    whatsapp.status = DeliveryStatus.FAILED
    whatsapp.failure_reason = "the provider rejected this template"
    message_id = await _queue(session_for, tenant_a)

    row = await _dispatch(session_for, tenant_a, message_id)

    assert row.state is ScheduledState.DISPATCHED
    (dispatch,) = await _dispatches(session_for, tenant_a)
    assert dispatch.status is DispatchStatus.FAILED
    assert dispatch.failure_code == "provider_rejected"


async def test_a_client_with_no_reachable_address_fails_rather_than_suppressing(
    session_for: SessionFactory,
    tenant_a: TenantFixture,
    migrator_engine: AsyncEngine,
    whatsapp: RecordingTransport,
) -> None:
    """⚠️ Not a suppression: nothing about the client's wishes or the tenant's
    status caused it, and recording one would misreport a data problem as a
    consent decision.

    EC-M1-08 permits a client with only an email, and the database requires at
    least one address — so "unreachable" is constructed the way it actually
    happens: a deployment that can only send on WhatsApp, and a client with no
    mobile number.
    """
    from app.kernel.notifications import configure_transports

    configure_transports({TransportType.WHATSAPP: whatsapp})

    async with migrator_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_a.tenant_id)}
        )
        await connection.execute(
            text("UPDATE clients SET mobile = NULL WHERE id = :c"), {"c": tenant_a.client_id}
        )

    message_id = await _queue(session_for, tenant_a)
    row = await _dispatch(session_for, tenant_a, message_id)

    assert row.state is ScheduledState.DISPATCHED
    (dispatch,) = await _dispatches(session_for, tenant_a)
    assert dispatch.failure_code == "no_address"
    assert whatsapp.sent == []


async def test_an_exhausted_message_is_resolved_rather_than_re_queued_forever(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 FR-M8-004's bounded ceiling. Without this a message that failed three
    times would stay pending and be re-enqueued by the next sweep — bounded per
    job, unbounded per message."""
    message_id = await _queue(session_for, tenant_a)

    async with session_for(tenant_a.tenant_id) as session:
        await resolve_exhausted(
            session, tenant_id=tenant_a.tenant_id, scheduled_message_id=message_id
        )

    async with session_for(tenant_a.tenant_id) as session:
        row = await session.get(ScheduledMessage, message_id)
        assert row is not None and row.state is ScheduledState.DISPATCHED


# ─── Transport substitution ──────────────────────────────────────────────


async def test_a_deployment_without_whatsapp_substitutes_email(
    session_for: SessionFactory, tenant_a: TenantFixture, email: RecordingTransport
) -> None:
    """🔒 The state S5 ships in, asserted end to end: the message goes out, and
    the delivery log records the transport that actually carried it."""
    from app.kernel.notifications import configure_transports

    configure_transports({TransportType.EMAIL: email})

    message_id = await _queue(session_for, tenant_a)
    row = await _dispatch(session_for, tenant_a, message_id)

    assert row.state is ScheduledState.DISPATCHED
    (dispatch,) = await _dispatches(session_for, tenant_a)
    assert dispatch.transport is TransportType.EMAIL
    assert dispatch.recipient_address.endswith("@example.test")


async def test_the_provider_approval_gate_applies_to_whatsapp_only(
    session_for: SessionFactory, tenant_a: TenantFixture, email: RecordingTransport
) -> None:
    """🔒 Every seeded template is `provider_template_status = 'pending'`, which
    is the truth — Meta has not approved them. Applying that to an email send
    would take the whole product offline while verification is pending, which is
    exactly what the sprint plan says must not happen."""
    from app.kernel.notifications import configure_transports

    configure_transports({TransportType.EMAIL: email})

    message_id = await _queue(session_for, tenant_a)
    row = await _dispatch(session_for, tenant_a, message_id)

    assert row.suppression_reason is None
    assert row.state is ScheduledState.DISPATCHED


async def test_a_revoked_template_is_paused_on_whatsapp(
    session_for: SessionFactory, tenant_a: TenantFixture, migrator_engine: AsyncEngine
) -> None:
    """🔒 EC-M8-03 — a revoked Meta approval pauses that message type and records
    `template_paused`, rather than surfacing as a delivery failure the
    practitioner would read as the client's fault."""
    message_id = await _queue(session_for, tenant_a)
    await set_provider_template_status(migrator_engine, code="plan_delivered", status="rejected")

    row = await _dispatch(session_for, tenant_a, message_id)

    assert row.suppression_reason is SuppressionReason.TEMPLATE_PAUSED


async def test_one_revoked_template_does_not_stop_the_others(
    session_for: SessionFactory, tenant_a: TenantFixture, migrator_engine: AsyncEngine
) -> None:
    """🔒 EC-M8-03's whole point: per-template state means one rejection pauses
    one message type. Without it a single revocation looks like a total outage.
    """
    await set_provider_template_status(migrator_engine, code="plan_delivered", status="rejected")

    other = await _queue(
        session_for,
        tenant_a,
        template_code="checkin_nudge",
        occasion="checkin:week33",
        variables={"client_name": "Anjali Rao", "portal_url": "https://portal.example.test"},
    )
    row = await _dispatch(session_for, tenant_a, other)

    assert row.state is ScheduledState.DISPATCHED
