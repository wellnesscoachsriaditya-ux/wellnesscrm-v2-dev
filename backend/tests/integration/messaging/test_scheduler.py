"""The single scheduler and the check-in cadence — FR-M8-001, FR-M8-022…025.

🔒 **The sweep never sends.** Every test here asserts that it produced *intent*
and handed it to the queue; the one thing that talks to a transport is the
dispatch engine, and `test_dispatch.py` is where that is exercised. A sweep that
sent inline would be a second dispatch path with no retry policy and no
suppression, which is precisely what M8.3 forbids.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.kernel.clients import ClientArchived, ClientStage, ClientStageChanged
from app.kernel.context import RequestContext, context_scope
from app.kernel.events import publish
from app.kernel.messaging import CheckinFrequency, ScheduledState, utc_now
from app.modules.messaging import (
    CheckinSettings,
    MessageRequest,
    ScheduledMessage,
    configure_checkin,
    generate_due_checkins,
    load_checkin_schedule,
    pause_checkins,
    resume_checkins,
    schedule,
    sweep_tenant,
)
from tests.integration.messaging.conftest import (
    SessionFactory,
    TenantFixture,
    practitioner,
    set_client_stage,
)

pytestmark = pytest.mark.asyncio


async def _pending(session_for: SessionFactory, tenant: TenantFixture) -> list[ScheduledMessage]:
    async with session_for(tenant.tenant_id) as session:
        rows = await session.scalars(
            select(ScheduledMessage)
            .where(
                ScheduledMessage.tenant_id == tenant.tenant_id,
                ScheduledMessage.state == ScheduledState.PENDING,
            )
            .order_by(ScheduledMessage.scheduled_for)
        )
        return list(rows)


async def _jobs(session_for: SessionFactory, tenant: TenantFixture) -> list[str]:
    async with session_for(tenant.tenant_id) as session:
        rows = (
            await session.execute(
                text("SELECT job_type FROM jobs WHERE tenant_id = :t"), {"t": tenant.tenant_id}
            )
        ).all()
        return [row[0] for row in rows]


# ─── The sweep ───────────────────────────────────────────────────────────


async def test_a_due_message_is_queued_for_dispatch(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 The sweep's whole job: hand what is due to the existing job queue."""
    async with session_for(tenant_a.tenant_id) as session:
        await schedule(
            session,
            tenant_id=tenant_a.tenant_id,
            request=MessageRequest(
                template_code="plan_delivered",
                occasion="plan:1",
                scheduled_for=utc_now() - timedelta(minutes=5),
                source_module="tests",
                client_id=tenant_a.client_id,
                variables={
                    "client_name": "Anjali Rao",
                    "practitioner_name": "Priya",
                    "plan_url": "https://portal.example.test/p/1",
                },
            ),
            # ⚠️ The immediate enqueue is turned off so the *sweep* is what is
            # being measured here rather than the scheduling path's own latency
            # shortcut (both produce the same job, by design).
            enqueue_if_due=False,
        )

    async with session_for(tenant_a.tenant_id) as session:
        result = await sweep_tenant(session, tenant_id=tenant_a.tenant_id)

    assert result.dispatches_queued == 1
    assert await _jobs(session_for, tenant_a) == ["dispatch_scheduled_message"]


async def test_a_future_message_is_left_alone(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    async with session_for(tenant_a.tenant_id) as session:
        await schedule(
            session,
            tenant_id=tenant_a.tenant_id,
            request=MessageRequest(
                template_code="plan_delivered",
                occasion="plan:future",
                scheduled_for=utc_now() + timedelta(days=1),
                source_module="tests",
                client_id=tenant_a.client_id,
                variables={
                    "client_name": "Anjali Rao",
                    "practitioner_name": "Priya",
                    "plan_url": "https://portal.example.test/p/1",
                },
            ),
        )

    async with session_for(tenant_a.tenant_id) as session:
        result = await sweep_tenant(session, tenant_id=tenant_a.tenant_id)

    assert result.dispatches_queued == 0
    assert await _jobs(session_for, tenant_a) == []


async def test_sweeping_twice_queues_one_job(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 `uq_jobs__idempotency` on `dispatch:{id}`. Two jobs for one message is
    a queue an operator cannot reason about, even though the engine's own
    re-entry guard would make the second harmless."""
    async with session_for(tenant_a.tenant_id) as session:
        await schedule(
            session,
            tenant_id=tenant_a.tenant_id,
            request=MessageRequest(
                template_code="plan_delivered",
                occasion="plan:1",
                scheduled_for=utc_now() - timedelta(minutes=5),
                source_module="tests",
                client_id=tenant_a.client_id,
                variables={
                    "client_name": "Anjali Rao",
                    "practitioner_name": "Priya",
                    "plan_url": "https://portal.example.test/p/1",
                },
            ),
            enqueue_if_due=False,
        )

    for _ in range(2):
        async with session_for(tenant_a.tenant_id) as session:
            await sweep_tenant(session, tenant_id=tenant_a.tenant_id)

    assert len(await _jobs(session_for, tenant_a)) == 1


async def test_a_due_message_is_queued_immediately_at_scheduling(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 NFR-009 — 60 seconds from an approved plan to a delivered message.

    Waiting for the next sweep would spend most of that budget before the queue
    had seen the work; the key is the same, so the two paths can never produce
    two sends.
    """
    async with session_for(tenant_a.tenant_id) as session:
        await schedule(
            session,
            tenant_id=tenant_a.tenant_id,
            request=MessageRequest(
                template_code="plan_delivered",
                occasion="plan:1",
                scheduled_for=utc_now(),
                source_module="tests",
                client_id=tenant_a.client_id,
                variables={
                    "client_name": "Anjali Rao",
                    "practitioner_name": "Priya",
                    "plan_url": "https://portal.example.test/p/1",
                },
            ),
        )

    assert await _jobs(session_for, tenant_a) == ["dispatch_scheduled_message"]

    async with session_for(tenant_a.tenant_id) as session:
        await sweep_tenant(session, tenant_id=tenant_a.tenant_id)

    assert len(await _jobs(session_for, tenant_a)) == 1


async def test_the_sweep_does_not_cross_tenants(
    session_for: SessionFactory, tenant_a: TenantFixture, tenant_b: TenantFixture
) -> None:
    """One transaction per tenant, under that tenant's scope — the reason the
    sweep is shaped the way it is."""
    async with session_for(tenant_b.tenant_id) as session:
        await schedule(
            session,
            tenant_id=tenant_b.tenant_id,
            request=MessageRequest(
                template_code="plan_delivered",
                occasion="plan:b",
                scheduled_for=utc_now() - timedelta(minutes=1),
                source_module="tests",
                client_id=tenant_b.client_id,
                variables={
                    "client_name": "Anjali Rao",
                    "practitioner_name": "Priya",
                    "plan_url": "https://portal.example.test/p/1",
                },
            ),
            enqueue_if_due=False,
        )

    async with session_for(tenant_a.tenant_id) as session:
        result = await sweep_tenant(session, tenant_id=tenant_a.tenant_id)

    assert result.dispatches_queued == 0


# ─── Check-in generation (FR-M8-022) ─────────────────────────────────────


async def test_a_due_schedule_generates_a_scheduled_message(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 A schedule generates intent; it does not send."""
    async with session_for(tenant_a.tenant_id) as session:
        row = await configure_checkin(
            session,
            tenant_id=tenant_a.tenant_id,
            client_id=tenant_a.client_id,
            settings=CheckinSettings(frequency=CheckinFrequency.WEEKLY),
            starting_from=date(2026, 8, 1),
        )
        assert row.next_due_on == date(2026, 8, 8)

    async with session_for(tenant_a.tenant_id) as session:
        created = await generate_due_checkins(
            session, tenant_id=tenant_a.tenant_id, today=date(2026, 8, 8)
        )

    assert created == 1
    pending = await _pending(session_for, tenant_a)
    assert len(pending) == 1
    assert pending[0].source_module == "messaging.checkins"


async def test_generating_twice_for_the_same_day_produces_one_message(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 The occasion is the schedule *and the date*, so a worker restart, a
    double sweep or a replayed job are all harmless."""
    async with session_for(tenant_a.tenant_id) as session:
        await configure_checkin(
            session,
            tenant_id=tenant_a.tenant_id,
            client_id=tenant_a.client_id,
            settings=CheckinSettings(),
            starting_from=date(2026, 8, 1),
        )

    async with session_for(tenant_a.tenant_id) as session:
        await generate_due_checkins(session, tenant_id=tenant_a.tenant_id, today=date(2026, 8, 8))

    # ⚠️ Reset `next_due_on` so the second pass genuinely retries the *same*
    # occasion rather than being skipped as not-yet-due — which would prove
    # nothing about idempotency.
    async with session_for(tenant_a.tenant_id) as session:
        await session.execute(
            text("UPDATE checkin_schedules SET next_due_on = :d WHERE client_id = :c"),
            {"d": date(2026, 8, 8), "c": tenant_a.client_id},
        )

    async with session_for(tenant_a.tenant_id) as session:
        created = await generate_due_checkins(
            session, tenant_id=tenant_a.tenant_id, today=date(2026, 8, 8)
        )

    assert created == 0
    assert len(await _pending(session_for, tenant_a)) == 1


async def test_a_paused_schedule_generates_nothing(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """FR-M8-024 — pausable without changing the lifecycle stage."""
    async with session_for(tenant_a.tenant_id) as session:
        await configure_checkin(
            session,
            tenant_id=tenant_a.tenant_id,
            client_id=tenant_a.client_id,
            settings=CheckinSettings(),
            starting_from=date(2026, 8, 1),
        )
    async with session_for(tenant_a.tenant_id) as session:
        await pause_checkins(session, tenant_id=tenant_a.tenant_id, client_id=tenant_a.client_id)

    async with session_for(tenant_a.tenant_id) as session:
        created = await generate_due_checkins(
            session, tenant_id=tenant_a.tenant_id, today=date(2026, 8, 8)
        )

    assert created == 0


async def test_pausing_cancels_what_was_already_queued(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 A pause that left Friday's nudge in the queue would send one more
    message after the practitioner asked us to stop."""
    async with session_for(tenant_a.tenant_id) as session:
        await configure_checkin(
            session,
            tenant_id=tenant_a.tenant_id,
            client_id=tenant_a.client_id,
            settings=CheckinSettings(),
            starting_from=date(2026, 8, 1),
        )
    async with session_for(tenant_a.tenant_id) as session:
        await generate_due_checkins(session, tenant_id=tenant_a.tenant_id, today=date(2026, 8, 8))

    async with session_for(tenant_a.tenant_id) as session:
        await pause_checkins(session, tenant_id=tenant_a.tenant_id, client_id=tenant_a.client_id)

    assert await _pending(session_for, tenant_a) == []


async def test_resuming_does_not_fire_immediately(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """⚠️ A schedule paused for two months would otherwise be immediately overdue
    and fire on the next sweep — a nudge triggered by the act of resuming."""
    async with session_for(tenant_a.tenant_id) as session:
        await configure_checkin(
            session,
            tenant_id=tenant_a.tenant_id,
            client_id=tenant_a.client_id,
            settings=CheckinSettings(),
            starting_from=date(2026, 1, 1),
        )
    async with session_for(tenant_a.tenant_id) as session:
        await pause_checkins(session, tenant_id=tenant_a.tenant_id, client_id=tenant_a.client_id)
    async with session_for(tenant_a.tenant_id) as session:
        row = await resume_checkins(
            session, tenant_id=tenant_a.tenant_id, client_id=tenant_a.client_id
        )

    assert row.is_paused is False
    assert row.next_due_on is not None
    assert row.next_due_on > utc_now().date()


async def test_the_cadence_advances_after_generating(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    async with session_for(tenant_a.tenant_id) as session:
        await configure_checkin(
            session,
            tenant_id=tenant_a.tenant_id,
            client_id=tenant_a.client_id,
            settings=CheckinSettings(frequency=CheckinFrequency.FORTNIGHTLY),
            starting_from=date(2026, 8, 1),
        )
    async with session_for(tenant_a.tenant_id) as session:
        await generate_due_checkins(session, tenant_id=tenant_a.tenant_id, today=date(2026, 8, 15))

    async with session_for(tenant_a.tenant_id) as session:
        row = await load_checkin_schedule(
            session, tenant_id=tenant_a.tenant_id, client_id=tenant_a.client_id
        )

    assert row is not None
    assert row.last_generated_for == date(2026, 8, 15)
    assert row.next_due_on == date(2026, 8, 29)


async def test_a_schedule_uses_its_configured_time_of_day(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    async with session_for(tenant_a.tenant_id) as session:
        await configure_checkin(
            session,
            tenant_id=tenant_a.tenant_id,
            client_id=tenant_a.client_id,
            settings=CheckinSettings(time_of_day=time(7, 30)),
            starting_from=date(2026, 8, 1),
        )
    async with session_for(tenant_a.tenant_id) as session:
        await generate_due_checkins(session, tenant_id=tenant_a.tenant_id, today=date(2026, 8, 8))

    (message,) = await _pending(session_for, tenant_a)
    assert message.scheduled_for.astimezone(UTC) == datetime(2026, 8, 8, 7, 30, tzinfo=UTC)


# ─── Stage changes (FR-M8-023, FR-M8-025) ────────────────────────────────


async def _publish(session_for: SessionFactory, tenant: TenantFixture, event: object) -> None:
    """Publish an event the way a request would, with a worker context."""
    with context_scope(RequestContext.for_worker("tests", tenant_id=tenant.tenant_id)):
        async with session_for(tenant.tenant_id) as session:
            await publish(event, session)  # type: ignore[arg-type]


async def test_becoming_active_starts_a_weekly_cadence(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🟡 FR-M8-023 — weekly, on the weekday the client became active. Without a
    default, US-M8-02 ("check-ins sent without me remembering") is false for
    every client until someone configures one."""
    activated = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)  # a Monday
    await _publish(
        session_for,
        tenant_a,
        ClientStageChanged(
            client_id=tenant_a.client_id,
            tenant_id=tenant_a.tenant_id,
            from_stage=ClientStage.CONSULTATION_SCHEDULED,
            to_stage=ClientStage.ACTIVE,
            changed_by_user_id=tenant_a.user_id,
            changed_at=activated,
        ),
    )

    async with session_for(tenant_a.tenant_id) as session:
        row = await load_checkin_schedule(
            session, tenant_id=tenant_a.tenant_id, client_id=tenant_a.client_id
        )

    assert row is not None
    assert row.frequency is CheckinFrequency.WEEKLY
    assert row.day_of_week == 1
    assert row.next_due_on == date(2026, 8, 24)


async def test_leaving_active_stops_the_cadence(
    session_for: SessionFactory, tenant_a: TenantFixture, migrator_engine: AsyncEngine
) -> None:
    """🔒 FR-M8-025, event-driven rather than reconciled overnight: a client
    paused on Wednesday must not be nudged on Friday."""
    async with session_for(tenant_a.tenant_id) as session:
        await configure_checkin(
            session,
            tenant_id=tenant_a.tenant_id,
            client_id=tenant_a.client_id,
            settings=CheckinSettings(),
            starting_from=date(2026, 8, 1),
        )

    await _publish(
        session_for,
        tenant_a,
        ClientStageChanged(
            client_id=tenant_a.client_id,
            tenant_id=tenant_a.tenant_id,
            from_stage=ClientStage.ACTIVE,
            to_stage=ClientStage.PAUSED,
            changed_by_user_id=tenant_a.user_id,
            changed_at=utc_now(),
        ),
    )

    async with session_for(tenant_a.tenant_id) as session:
        row = await load_checkin_schedule(
            session, tenant_id=tenant_a.tenant_id, client_id=tenant_a.client_id
        )
    assert row is not None and row.is_paused

    # ⚠️ And the generator agrees, which is the half that matters: a paused flag
    # nothing reads would be a setting, not a stop.
    await set_client_stage(
        migrator_engine,
        tenant_id=tenant_a.tenant_id,
        client_id=tenant_a.client_id,
        stage="paused",
    )
    async with session_for(tenant_a.tenant_id) as session:
        created = await generate_due_checkins(
            session, tenant_id=tenant_a.tenant_id, today=date(2026, 9, 1)
        )
    assert created == 0


async def test_archiving_stops_the_cadence(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """An archive is not a stage change, so it needs its own subscriber — without
    one a client archived from `active` keeps generating nudges."""
    async with session_for(tenant_a.tenant_id) as session:
        await configure_checkin(
            session,
            tenant_id=tenant_a.tenant_id,
            client_id=tenant_a.client_id,
            settings=CheckinSettings(),
            starting_from=date(2026, 8, 1),
        )

    await _publish(
        session_for,
        tenant_a,
        ClientArchived(
            client_id=tenant_a.client_id,
            tenant_id=tenant_a.tenant_id,
            stage=ClientStage.ACTIVE,
            archived_by_user_id=tenant_a.user_id,
            archived_at=utc_now(),
        ),
    )

    async with session_for(tenant_a.tenant_id) as session:
        row = await load_checkin_schedule(
            session, tenant_id=tenant_a.tenant_id, client_id=tenant_a.client_id
        )
    assert row is not None and row.is_paused


async def test_the_generator_pauses_a_schedule_whose_client_left_active(
    session_for: SessionFactory, tenant_a: TenantFixture, migrator_engine: AsyncEngine
) -> None:
    """⚠️ The reconciliation half. The `StageChanged` subscriber is the fast path;
    this catches a stage change that happened while the process was down."""
    async with session_for(tenant_a.tenant_id) as session:
        await configure_checkin(
            session,
            tenant_id=tenant_a.tenant_id,
            client_id=tenant_a.client_id,
            settings=CheckinSettings(),
            starting_from=date(2026, 8, 1),
        )

    await set_client_stage(
        migrator_engine,
        tenant_id=tenant_a.tenant_id,
        client_id=tenant_a.client_id,
        stage="churned",
    )

    async with session_for(tenant_a.tenant_id) as session:
        created = await generate_due_checkins(
            session, tenant_id=tenant_a.tenant_id, today=date(2026, 8, 8)
        )

    assert created == 0
    async with session_for(tenant_a.tenant_id) as session:
        row = await load_checkin_schedule(
            session, tenant_id=tenant_a.tenant_id, client_id=tenant_a.client_id
        )
    assert row is not None and row.is_paused


async def test_a_practitioner_actor_is_scoped_to_one_tenant(tenant_a: TenantFixture) -> None:
    """A guard on the fixture itself: an actor with the wrong tenant would make
    every authorization assertion in this suite vacuous."""
    actor = practitioner(tenant_a)
    assert actor.tenant_id == tenant_a.tenant_id
    assert isinstance(actor.subject_id, uuid.UUID)
