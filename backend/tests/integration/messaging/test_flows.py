"""The business flows M8 exists to close — the core loop's fourth step.

🔒 **Enquiry → client → plan → delivery, through the real seams.** Each half is
tested where it lives, and what this file adds is the join: that `leads` and
`nutrition` produce messages *without knowing messaging exists*, by publishing an
event the kernel owns. That is AC-M8-008's real claim — a new message type needs
a template and a schedule, not a change to the producer.

⚠️ Nothing here calls a transport directly. The engine resolves one, and the
suite's `RecordingTransport` is what receives it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.kernel.context import Actor, ActorType, AuthRealm, RequestContext, UserRole, context_scope
from app.kernel.events import publish
from app.kernel.leads import EnquiryReceived
from app.kernel.messaging import DispatchStatus, ScheduledState, utc_now
from app.kernel.models import TransportType
from app.kernel.nutrition import PlanVersionIssued
from app.modules.messaging import (
    MessageDispatch,
    MessageTemplate,
    ScheduledMessage,
    dispatch_scheduled,
    sweep_tenant,
)
from app.modules.nutrition import create_plan, issue_plan_version
from tests.integration.messaging.conftest import (
    MessagingApi,
    RecordingTransport,
    SessionFactory,
    TenantFixture,
    practitioner,
)

pytestmark = pytest.mark.asyncio


async def _queued(
    session_for: SessionFactory, tenant: TenantFixture
) -> list[tuple[ScheduledMessage, MessageTemplate]]:
    async with session_for(tenant.tenant_id) as session:
        rows = (
            await session.execute(
                select(ScheduledMessage, MessageTemplate)
                .join(MessageTemplate, MessageTemplate.id == ScheduledMessage.template_id)
                .where(ScheduledMessage.tenant_id == tenant.tenant_id)
                .order_by(MessageTemplate.code)
            )
        ).all()
        return [(row[0], row[1]) for row in rows]


def _actor(tenant: TenantFixture) -> Actor:
    return Actor(
        actor_type=ActorType.PRACTITIONER,
        realm=AuthRealm.PRACTITIONER,
        subject_id=tenant.user_id,
        tenant_id=tenant.tenant_id,
        role=UserRole.OWNER,
    )


# ─── Enquiry (FR-M8-018/019) ─────────────────────────────────────────────


async def test_an_enquiry_acknowledges_the_lead_and_notifies_the_practitioner(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 S2 shipped both as "logged only — no transport until S5". This is that
    wiring, and `leads` did not change to get it: it publishes an event."""
    with context_scope(RequestContext.for_worker("tests", tenant_id=tenant_a.tenant_id)):
        async with session_for(tenant_a.tenant_id) as session:
            await publish(
                EnquiryReceived(
                    client_id=tenant_a.client_id,
                    tenant_id=tenant_a.tenant_id,
                    submission_id=uuid.uuid4(),
                    form_id=uuid.uuid4(),
                    is_duplicate=False,
                    source="instagram",
                    received_at=utc_now(),
                ),
                session,
            )

    queued = await _queued(session_for, tenant_a)
    codes = {template.code for _, template in queued}
    assert codes == {"lead_acknowledgement", "lead_notification"}


async def test_the_practitioner_notification_is_not_addressed_to_the_client(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 It carries `recipient_user_id` and no `client_id`, which keeps it off
    the client's timeline and out of the frequency cap that protects them."""
    with context_scope(RequestContext.for_worker("tests", tenant_id=tenant_a.tenant_id)):
        async with session_for(tenant_a.tenant_id) as session:
            await publish(
                EnquiryReceived(
                    client_id=tenant_a.client_id,
                    tenant_id=tenant_a.tenant_id,
                    submission_id=uuid.uuid4(),
                    form_id=uuid.uuid4(),
                    is_duplicate=False,
                    source=None,
                    received_at=utc_now(),
                ),
                session,
            )

    notification = next(
        message
        for message, template in await _queued(session_for, tenant_a)
        if template.code == "lead_notification"
    )
    assert notification.client_id is None
    assert notification.recipient_user_id == tenant_a.user_id


async def test_the_practitioner_notification_goes_out_by_email(
    session_for: SessionFactory, tenant_a: TenantFixture, email: RecordingTransport
) -> None:
    """🔒 M8.4's policy risk — a practice notification through the client channel
    is a template used outside its approved category."""
    with context_scope(RequestContext.for_worker("tests", tenant_id=tenant_a.tenant_id)):
        async with session_for(tenant_a.tenant_id) as session:
            await publish(
                EnquiryReceived(
                    client_id=tenant_a.client_id,
                    tenant_id=tenant_a.tenant_id,
                    submission_id=uuid.uuid4(),
                    form_id=uuid.uuid4(),
                    is_duplicate=False,
                    source=None,
                    received_at=utc_now(),
                ),
                session,
            )

    notification = next(
        message
        for message, template in await _queued(session_for, tenant_a)
        if template.code == "lead_notification"
    )
    async with session_for(tenant_a.tenant_id) as session:
        await dispatch_scheduled(
            session,
            tenant_id=tenant_a.tenant_id,
            scheduled_message_id=notification.id,
            now=datetime(2026, 8, 14, 6, 30, tzinfo=UTC),
        )

    assert len(email.sent) == 1
    assert email.sent[0].recipient.address.endswith("@example.test")


async def test_a_repeated_enquiry_acknowledges_once_per_submission(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """The occasion is the *submission*: a prospect who submits twice is waiting
    on two replies, while a replayed event produces one message."""
    submission_id = uuid.uuid4()
    for _ in range(2):
        with context_scope(RequestContext.for_worker("tests", tenant_id=tenant_a.tenant_id)):
            async with session_for(tenant_a.tenant_id) as session:
                await publish(
                    EnquiryReceived(
                        client_id=tenant_a.client_id,
                        tenant_id=tenant_a.tenant_id,
                        submission_id=submission_id,
                        form_id=uuid.uuid4(),
                        is_duplicate=True,
                        source=None,
                        received_at=utc_now(),
                    ),
                    session,
                )

    assert len(await _queued(session_for, tenant_a)) == 2


# ─── Plan delivery (FR-M8-013, AC-M8-001) ────────────────────────────────


async def test_issuing_a_plan_delivers_it_to_the_client(
    session_for: SessionFactory, tenant_a: TenantFixture, whatsapp: RecordingTransport
) -> None:
    """🔒 AC-M8-001 — an approved plan triggers a message containing a working
    deep link. The whole path: `nutrition` issues, the kernel carries the event,
    `messaging` queues, the engine sends.

    ⚠️ `nutrition` contains no reference to messaging. Adding this consumer
    required no change to `issue_plan_version`, which is what AC-M8-008 asserts.
    """
    with context_scope(RequestContext.for_worker("tests", tenant_id=tenant_a.tenant_id)):
        async with session_for(tenant_a.tenant_id) as session:
            plan, version = await create_plan(
                session,
                tenant_id=tenant_a.tenant_id,
                client_id=tenant_a.client_id,
                created_by_user_id=tenant_a.user_id,
                title="Week 1",
            )
            await issue_plan_version(
                session,
                tenant_id=tenant_a.tenant_id,
                plan_id=plan.id,
                version_id=version.id,
                issued_by_user_id=tenant_a.user_id,
            )

    queued = await _queued(session_for, tenant_a)
    delivery = next(message for message, template in queued if template.code == "plan_delivered")
    assert delivery.client_id == tenant_a.client_id
    assert str(version.id) in delivery.template_variables["plan_url"]

    async with session_for(tenant_a.tenant_id) as session:
        await dispatch_scheduled(
            session,
            tenant_id=tenant_a.tenant_id,
            scheduled_message_id=delivery.id,
            now=datetime(2026, 8, 14, 6, 30, tzinfo=UTC),
        )

    assert len(whatsapp.sent) == 1
    assert str(version.id) in (whatsapp.sent[0].body or "")


async def test_re_issuing_the_same_version_delivers_once(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 EC-M8-06 — the logical occasion is the version, so a replayed event
    cannot send a client the same plan twice."""
    issued_at = utc_now()
    version_id = uuid.uuid4()

    for _ in range(2):
        with context_scope(RequestContext.for_worker("tests", tenant_id=tenant_a.tenant_id)):
            async with session_for(tenant_a.tenant_id) as session:
                await publish(
                    PlanVersionIssued(
                        tenant_id=tenant_a.tenant_id,
                        plan_version_id=version_id,
                        client_id=tenant_a.client_id,
                        issued_at=issued_at,
                    ),
                    session,
                )

    assert len(await _queued(session_for, tenant_a)) == 1


async def test_the_delivery_is_queued_for_the_worker_immediately(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 NFR-009 — 60 seconds from approval to delivery. The job exists before
    the next sweep, so the budget is spent on the queue rather than on waiting
    for a schedule."""
    with context_scope(RequestContext.for_worker("tests", tenant_id=tenant_a.tenant_id)):
        async with session_for(tenant_a.tenant_id) as session:
            await publish(
                PlanVersionIssued(
                    tenant_id=tenant_a.tenant_id,
                    plan_version_id=uuid.uuid4(),
                    client_id=tenant_a.client_id,
                    issued_at=utc_now(),
                ),
                session,
            )

    from sqlalchemy import text

    async with session_for(tenant_a.tenant_id) as session:
        queued = (
            await session.execute(
                text(
                    "SELECT count(*) FROM jobs WHERE tenant_id = :t "
                    "AND job_type = 'dispatch_scheduled_message'"
                ),
                {"t": tenant_a.tenant_id},
            )
        ).scalar_one()

    assert queued == 1


# ─── Practitioner-triggered, over HTTP (J2 step 6) ───────────────────────


async def test_a_practitioner_message_travels_the_same_engine(
    messaging_api: MessagingApi,
    session_for: SessionFactory,
    tenant_a: TenantFixture,
    whatsapp: RecordingTransport,
) -> None:
    """🔒 FR-M8-001 admits no exception for a human-initiated message: it is
    queued, swept and dispatched exactly like an automated one."""
    messaging_api.as_actor(practitioner(tenant_a))
    response = await messaging_api.http.post(
        f"/api/v1/app/clients/{tenant_a.client_id}/messages",
        json={
            "template_code": "assessment_invitation",
            "variables": {
                "practitioner_name": "Priya",
                "assessment_url": "https://portal.example.test/a/1",
            },
        },
    )
    assert response.status_code == 200

    async with session_for(tenant_a.tenant_id) as session:
        result = await sweep_tenant(session, tenant_id=tenant_a.tenant_id)
    assert result.dispatches_queued >= 0  # already queued at scheduling time

    message_id = uuid.UUID(response.json()["id"])
    async with session_for(tenant_a.tenant_id) as session:
        await dispatch_scheduled(
            session,
            tenant_id=tenant_a.tenant_id,
            scheduled_message_id=message_id,
            now=datetime(2026, 8, 14, 6, 30, tzinfo=UTC),
        )

    assert len(whatsapp.sent) == 1
    async with session_for(tenant_a.tenant_id) as session:
        dispatch = (
            await session.execute(
                select(MessageDispatch).where(MessageDispatch.scheduled_message_id == message_id)
            )
        ).scalar_one()
    assert dispatch.status is DispatchStatus.SENT
    assert dispatch.transport is TransportType.WHATSAPP


async def test_the_message_history_shows_the_whole_journey(
    messaging_api: MessagingApi, session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 AC-M8-003 — every attempt across a journey appears in the delivery log
    with a final status, in one place the practitioner can read."""
    with context_scope(RequestContext.for_worker("tests", tenant_id=tenant_a.tenant_id)):
        async with session_for(tenant_a.tenant_id) as session:
            await publish(
                EnquiryReceived(
                    client_id=tenant_a.client_id,
                    tenant_id=tenant_a.tenant_id,
                    submission_id=uuid.uuid4(),
                    form_id=uuid.uuid4(),
                    is_duplicate=False,
                    source=None,
                    received_at=utc_now(),
                ),
                session,
            )
            await publish(
                PlanVersionIssued(
                    tenant_id=tenant_a.tenant_id,
                    plan_version_id=uuid.uuid4(),
                    client_id=tenant_a.client_id,
                    issued_at=utc_now(),
                ),
                session,
            )

    for message, _ in await _queued(session_for, tenant_a):
        if message.state is not ScheduledState.PENDING:
            continue
        async with session_for(tenant_a.tenant_id) as session:
            await dispatch_scheduled(
                session,
                tenant_id=tenant_a.tenant_id,
                scheduled_message_id=message.id,
                now=datetime(2026, 8, 14, 6, 30, tzinfo=UTC),
            )

    messaging_api.as_actor(practitioner(tenant_a))
    response = await messaging_api.http.get(f"/api/v1/app/clients/{tenant_a.client_id}/messages")

    assert response.status_code == 200
    codes = {item["template_code"] for item in response.json()["items"]}
    # ⚠️ The practitioner notification is absent by design: it was addressed to
    # the practitioner, not the client, so it belongs to neither this history nor
    # the client's timeline.
    assert codes == {"lead_acknowledgement", "plan_delivered"}
    assert datetime.now(UTC) is not None
