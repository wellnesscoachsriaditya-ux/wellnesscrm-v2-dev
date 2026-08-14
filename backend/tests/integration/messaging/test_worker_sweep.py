"""The worker's messaging sweep — M8's single scheduler, in the process it runs in.

🔒 **Why the sweep lives in the worker's tick rather than as a self-perpetuating
job.** A job that re-enqueues itself stops forever the first time it
dead-letters, and nothing notices until a practitioner asks why check-ins
stopped. The tick cannot stop without the process stopping.

⚠️ These drive ``Worker._sweep_messages`` directly rather than running the poll
loop. The loop's own behaviour — signals, drain, backoff — is
``test_worker_lifecycle``'s subject, and re-testing it here would give this file
a second reason to fail.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.kernel.messaging import utc_now
from app.modules.messaging import MessageRequest, schedule
from app.worker import Worker
from tests.integration.conftest import scope_to
from tests.integration.messaging.conftest import SessionFactory, TenantFixture

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _purge_swept_jobs(migrator_engine: AsyncEngine) -> AsyncIterator[None]:
    """Remove the dispatch jobs a sweep queued, for *every* tenant.

    ⚠️ 🔒 **The sweep is global by design**, so a test that runs it touches every
    tenant in the database — including ones other suites are still using. Their
    teardown removes their own rows, but a job this suite queued for them
    outlives it, and a leftover `dispatch_scheduled_message` is *claimable*: the
    next suite's "claim the job I just enqueued" test claims it instead and fails
    on an id comparison that names neither cause nor culprit.

    `jobs` carries no RLS (migration 0005), so an unscoped delete is the right
    tool here and the only place in this suite where one is.
    """
    yield
    async with migrator_engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM jobs WHERE job_type = 'dispatch_scheduled_message'")
        )


@pytest.fixture
def worker(app_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> Worker:
    """A worker whose transactions reach the throwaway database.

    🔒 **The safety-critical override.** `app.worker` calls
    `platform.db.transaction()`, which reads `.env` and would reach Supabase.
    Patching the name the module resolves at call time is what keeps this suite
    off a hosted database it has no business touching.
    """
    factory = async_sessionmaker(app_engine, expire_on_commit=False)

    @asynccontextmanager
    async def transaction(*, tenant_id: uuid.UUID | None = None, **_: Any) -> Any:
        session = factory()
        try:
            await scope_to(await session.connection(), tenant_id)
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    monkeypatch.setattr("app.worker.transaction", transaction)
    return Worker(poll_interval_seconds=60)


async def _queue_due(session_for: SessionFactory, tenant: TenantFixture) -> uuid.UUID:
    async with session_for(tenant.tenant_id) as session:
        message = await schedule(
            session,
            tenant_id=tenant.tenant_id,
            request=MessageRequest(
                template_code="plan_delivered",
                occasion=f"plan:{uuid.uuid4()}",
                scheduled_for=utc_now() - timedelta(minutes=1),
                source_module="tests",
                client_id=tenant.client_id,
                variables={
                    "client_name": "Anjali Rao",
                    "practitioner_name": "Priya",
                    "plan_url": "https://portal.example.test/p/1",
                },
            ),
            # ⚠️ Off, so what is measured is the sweep rather than the
            # scheduling path's own latency shortcut.
            enqueue_if_due=False,
        )
        assert message is not None
        return message.id


async def _dispatch_jobs(session_for: SessionFactory, tenant: TenantFixture) -> int:
    async with session_for(tenant.tenant_id) as session:
        return int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM jobs WHERE tenant_id = :t "
                        "AND job_type = 'dispatch_scheduled_message'"
                    ),
                    {"t": tenant.tenant_id},
                )
            ).scalar_one()
        )


async def test_the_sweep_queues_every_tenants_due_messages(
    worker: Worker,
    session_for: SessionFactory,
    tenant_a: TenantFixture,
    tenant_b: TenantFixture,
) -> None:
    """🔒 One transaction per tenant — RLS scopes a transaction to one tenant, so
    there is no cross-tenant "what is due" query for the application role, and
    there must not be."""
    await _queue_due(session_for, tenant_a)
    await _queue_due(session_for, tenant_b)

    queued = await worker._sweep_messages()

    assert queued >= 2
    assert await _dispatch_jobs(session_for, tenant_a) == 1
    assert await _dispatch_jobs(session_for, tenant_b) == 1


async def test_sweeping_twice_does_not_double_queue(
    worker: Worker, session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 The idempotency key is the message id, so a sweep racing a previous one
    cannot produce two jobs for one message."""
    await _queue_due(session_for, tenant_a)

    await worker._sweep_messages()
    await worker._sweep_messages()

    assert await _dispatch_jobs(session_for, tenant_a) == 1


async def test_a_suspended_tenant_is_skipped(
    worker: Worker,
    session_for: SessionFactory,
    tenant_a: TenantFixture,
    migrator_engine: AsyncEngine,
) -> None:
    """⚠️ An optimisation, not the enforcement. FR-M8-007 is enforced at dispatch
    and records `tenant_suspended` as the reason; if this filter were the only
    check, a tenant suspended after their messages were queued would have them
    sit pending with no explanation."""
    await _queue_due(session_for, tenant_a)
    async with migrator_engine.begin() as connection:
        await connection.execute(
            text("UPDATE tenants SET status = 'suspended' WHERE id = :t"),
            {"t": tenant_a.tenant_id},
        )

    await worker._sweep_messages()

    assert await _dispatch_jobs(session_for, tenant_a) == 0


async def test_one_tenants_failure_does_not_stop_the_others(
    worker: Worker,
    session_for: SessionFactory,
    tenant_a: TenantFixture,
    tenant_b: TenantFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔒 A sweep that aborted on the first bad tenant would stop every other
    practice's messages — the failure mode that turns one tenant's data problem
    into an outage."""
    await _queue_due(session_for, tenant_a)
    await _queue_due(session_for, tenant_b)

    real = worker._sweep_messages.__self__
    assert real is worker

    from app.modules.messaging import sweep_tenant as real_sweep

    async def flaky(session: Any, *, tenant_id: uuid.UUID, **kwargs: Any) -> Any:
        if tenant_id == tenant_a.tenant_id:
            raise RuntimeError("this tenant's sweep is broken")
        return await real_sweep(session, tenant_id=tenant_id, **kwargs)

    monkeypatch.setattr("app.worker.sweep_tenant", flaky)

    await worker._sweep_messages()

    assert await _dispatch_jobs(session_for, tenant_a) == 0
    assert await _dispatch_jobs(session_for, tenant_b) == 1


async def test_the_scheduler_can_be_turned_off(
    app_engine: AsyncEngine, session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """⚠️ For a deployment running more than one worker: exactly one may sweep,
    or two schedulers race to queue the same messages. The idempotency key makes
    that harmless, but "harmless" is not the same as "intended"."""
    await _queue_due(session_for, tenant_a)
    worker = Worker(poll_interval_seconds=60, run_scheduler=False)

    # ⚠️ `_tick`, the internal step, rather than `run()`: the poll loop would
    # sleep out its interval and this test is about one pass.
    await worker._tick()

    assert await _dispatch_jobs(session_for, tenant_a) == 0
