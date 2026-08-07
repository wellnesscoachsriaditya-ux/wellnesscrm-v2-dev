"""The job queue, against a live PostgreSQL — C6.

🔒 **Everything asserted here is invisible to a unit test**, which is the reason
the file exists. `SKIP LOCKED`, lease expiry, partial-index conflict targets and
`NULLS NOT DISTINCT` are database behaviours; a fake queue would happily agree
with whatever the code believed, including when the code was wrong.

What this proves:

* two concurrent workers never claim the same job (Arch §11.3);
* a lease that expires returns its job to the queue rather than losing it
  (DB §13.3, NFR-025), and does **not** reset the attempt counter;
* retries back off and dead-letter at the class ceiling (Arch §11.4);
* 🔒 `generation` is refused a retry budget *by the database*, not just by
  policy — the constraint is what stops a future caller re-introducing the cost;
* idempotent enqueue suppresses duplicates through the unique index, including
  the NULL-tenant case that default NULL semantics would let through;
* enqueue is transactional: a rolled-back publisher leaves no job.

⚠️ These need the same setup as the tenant-isolation gate — see
`tests/integration/test_tenant_isolation.py` for the provisioning sequence. They
fail rather than skip in CI, where `REQUIRE_LIVE_DATABASE` is set.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.kernel.jobs import register_handler, reset_handlers
from app.kernel.models import JobClass, JobStatus
from app.platform.jobs import (
    ClaimedJob,
    claim,
    enqueue,
    get_job,
    get_runs,
    mark_failed,
    mark_succeeded,
    recover_expired_leases,
)

#: Job types registered for these tests. Handlers are never executed here — the
#: runner's behaviour is C5's suite; this file exercises the storage layer.
_DISPATCH_JOB = "integration_send_message"
_GENERATION_JOB = "integration_ai_draft"


async def _noop(_payload: Mapping[str, Any], _session: AsyncSession) -> None:
    pass


@pytest.fixture(autouse=True)
def _handlers() -> AsyncIterator[None]:
    """Register the job types these tests enqueue.

    `enqueue` reads the class and retry ceiling from the registry rather than
    from its caller — that is what stops a caller making a generation job
    retryable — so the registry must be populated for any enqueue to succeed.
    """
    reset_handlers()
    register_handler(_DISPATCH_JOB, JobClass.DISPATCH, _noop)
    register_handler(_GENERATION_JOB, JobClass.GENERATION, _noop)
    yield
    reset_handlers()


@pytest_asyncio.fixture
async def sessions(app_engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Session factory on the application role.

    🔒 `app_user`, not the migrator. `jobs` has no RLS by design (DB §17.1
    Pattern D), and this suite is where that claim is exercised for real: if
    someone adds a policy, the claim query returns nothing and these tests fail
    rather than the queue silently stopping in production.
    """
    yield async_sessionmaker(bind=app_engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture(autouse=True)
async def _clean_jobs(migrator_engine: AsyncEngine) -> AsyncIterator[None]:
    """Remove this suite's rows before and after.

    Scoped by `job_type` rather than truncating: the same database may hold rows
    from a developer's manual poking, and a TRUNCATE here would delete them
    without saying so.
    """
    await _purge(migrator_engine)
    yield
    await _purge(migrator_engine)


async def _purge(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        # `job_runs` cascades from `jobs`, so one delete is enough.
        await connection.execute(
            text("DELETE FROM jobs WHERE job_type = ANY(:types)"),
            {"types": [_DISPATCH_JOB, _GENERATION_JOB]},
        )


# ─── Claiming (DB §13.2) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claim_marks_the_job_and_takes_a_lease(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The happy path: pending → claimed, with a lease and an attempt recorded."""
    async with sessions() as session, session.begin():
        job_id = await enqueue(session, job_type=_DISPATCH_JOB, payload={"client_id": "abc"})

    assert job_id is not None

    async with sessions() as session, session.begin():
        claimed = await claim(session, worker_id="worker-a")

    assert len(claimed) == 1
    assert claimed[0].id == job_id

    async with sessions() as session:
        row = await get_job(session, job_id)
        runs = await get_runs(session, job_id)

    assert row is not None
    assert row["status"] == JobStatus.CLAIMED.value
    assert row["claimed_by"] == "worker-a"
    assert row["lease_expires_at"] is not None
    # 🔒 Incremented at claim, not at completion — a worker that dies mid-job
    # never reports, and counting at completion would let it retry forever.
    assert row["attempt_count"] == 1
    assert len(runs) == 1
    assert runs[0]["attempt_number"] == 1


@pytest.mark.asyncio
async def test_concurrent_workers_never_claim_the_same_job(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """🔒 The `SKIP LOCKED` guarantee (Arch §11.3), and the reason for the CTE.

    ⚠️ This is the test that cannot be faked and cannot be reasoned about from
    the code alone. Two workers poll simultaneously against one pending job; the
    second must skip the locked row rather than block on it or claim it too.
    Without `SKIP LOCKED` this either deadlocks or sends the same WhatsApp
    message twice, and with one worker in MVP neither would ever be noticed.
    """
    async with sessions() as session, session.begin():
        job_id = await enqueue(session, job_type=_DISPATCH_JOB, payload={"client_id": "abc"})

    async def claim_as(worker: str) -> list[ClaimedJob]:
        async with sessions() as session, session.begin():
            return await claim(session, worker_id=worker)

    first, second = await asyncio.gather(claim_as("worker-a"), claim_as("worker-b"))

    claimed_ids = [job.id for job in (*first, *second)]
    assert claimed_ids == [job_id], (
        "exactly one worker must claim the job; "
        f"got {len(claimed_ids)} claims across two concurrent workers"
    )


@pytest.mark.asyncio
async def test_claim_respects_run_after(sessions: async_sessionmaker[AsyncSession]) -> None:
    """A job scheduled for the future is not claimable yet.

    This is what makes backoff work: a failed job sets `run_after` forward, and
    the claim query must honour it against the *database's* clock.
    """
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO jobs (job_type, job_class, payload, max_attempts, run_after) "
                "VALUES (:t, 'dispatch', '{}'::jsonb, 3, now() + interval '1 hour')"
            ),
            {"t": _DISPATCH_JOB},
        )

    async with sessions() as session, session.begin():
        claimed = await claim(session, worker_id="worker-a")

    assert claimed == []


@pytest.mark.asyncio
async def test_higher_priority_is_claimed_first(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Lower `priority` runs sooner — a reminder must not queue behind a purge."""
    async with sessions() as session, session.begin():
        low = await enqueue(session, job_type=_DISPATCH_JOB, payload={"n": 1}, priority=300)
        high = await enqueue(session, job_type=_DISPATCH_JOB, payload={"n": 2}, priority=10)

    assert low is not None and high is not None

    async with sessions() as session, session.begin():
        claimed = await claim(session, worker_id="worker-a", limit=1)

    assert [job.id for job in claimed] == [high]


# ─── Lease expiry (DB §13.3, NFR-025) ────────────────────────────────────


@pytest.mark.asyncio
async def test_expired_lease_returns_the_job_to_the_queue(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """🔒 NFR-025 — a job is not lost when its worker dies.

    Simulated by claiming with a lease already in the past, which is what a
    killed worker leaves behind: `status='claimed'` and a lease nothing renews.
    """
    async with sessions() as session, session.begin():
        job_id = await enqueue(session, job_type=_DISPATCH_JOB, payload={"client_id": "abc"})

    async with sessions() as session, session.begin():
        claimed = await claim(session, worker_id="doomed", lease=timedelta(seconds=-1))
    assert len(claimed) == 1

    async with sessions() as session, session.begin():
        recovered = await recover_expired_leases(session)
    assert recovered == 1

    async with sessions() as session:
        row = await get_job(session, job_id)

    assert row is not None
    assert row["status"] == JobStatus.PENDING.value
    assert row["claimed_by"] is None
    assert row["lease_expires_at"] is None
    # 🔒 Deliberately *not* reset. The attempt was made and the worker died
    # during it; resetting would make a job that reliably kills its worker retry
    # forever, which is the failure this whole mechanism exists to bound.
    assert row["attempt_count"] == 1


@pytest.mark.asyncio
async def test_expired_lease_on_the_final_attempt_dead_letters(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A job that exhausted its budget must not be re-queued by the sweep.

    Otherwise the recovery path becomes a way to exceed `max_attempts`: each
    expiry hands back a job that has already used every attempt it was allowed.
    """
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO jobs "
                "  (job_type, job_class, payload, status, max_attempts, attempt_count, "
                "   claimed_by, lease_expires_at) "
                "VALUES (:t, 'dispatch', '{}'::jsonb, 'claimed', 1, 1, "
                "        'doomed', now() - interval '1 minute')"
            ),
            {"t": _DISPATCH_JOB},
        )

    async with sessions() as session, session.begin():
        assert await recover_expired_leases(session) == 1

    async with sessions() as session:
        status = (
            await session.execute(
                text("SELECT status FROM jobs WHERE job_type = :t"), {"t": _DISPATCH_JOB}
            )
        ).scalar_one()

    assert status == JobStatus.DEAD.value


@pytest.mark.asyncio
async def test_a_live_lease_is_not_recovered(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """⚠️ The other half of the sweep, and the more dangerous direction.

    Recovering a job whose worker is alive and mid-execution means running it
    twice, concurrently. A sweep that is too eager is worse than one that is too
    slow.
    """
    async with sessions() as session, session.begin():
        await enqueue(session, job_type=_DISPATCH_JOB, payload={"client_id": "abc"})

    async with sessions() as session, session.begin():
        await claim(session, worker_id="alive", lease=timedelta(minutes=10))

    async with sessions() as session, session.begin():
        assert await recover_expired_leases(session) == 0


# ─── Retry and dead-letter (Arch §11.4) ──────────────────────────────────


@pytest.mark.asyncio
async def test_failure_schedules_a_retry_with_backoff(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A non-final failure returns to `pending` with `run_after` in the future."""
    async with sessions() as session, session.begin():
        job_id = await enqueue(session, job_type=_DISPATCH_JOB, payload={"client_id": "abc"})

    async with sessions() as session, session.begin():
        claimed = await claim(session, worker_id="worker-a")

    async with sessions() as session, session.begin():
        status = await mark_failed(
            session,
            claimed[0],
            error_class="IntegrationError",
            error_message="provider timed out",
            duration_ms=120,
        )

    assert status is JobStatus.PENDING

    async with sessions() as session:
        row = await get_job(session, job_id)
        runs = await get_runs(session, job_id)
        due = (
            await session.execute(
                text("SELECT run_after > now() FROM jobs WHERE id = :id"), {"id": job_id}
            )
        ).scalar_one()

    assert row is not None
    assert row["status"] == JobStatus.PENDING.value
    assert row["last_error"] == "provider timed out"
    assert due is True, "a retry must be scheduled in the future, or it re-claims immediately"
    assert runs[0]["outcome"] == "failure"


@pytest.mark.asyncio
async def test_final_failure_dead_letters_and_keeps_its_history(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """🔒 AC-M8-007 — terminal failures are visible, never silently discarded.

    The per-attempt history is the point of a separate `job_runs` table: an
    operator asking "why did this take three tries" needs all three, which one
    mutable row on `jobs` cannot express.
    """
    async with sessions() as session, session.begin():
        job_id = await enqueue(session, job_type=_DISPATCH_JOB, payload={"client_id": "abc"})

    status = None
    for _attempt in range(3):
        async with sessions() as session, session.begin():
            claimed = await claim(session, worker_id="worker-a")
            if not claimed:
                # Backoff put it in the future; pull it forward rather than sleep.
                await session.execute(
                    text("UPDATE jobs SET run_after = now() WHERE id = :id"), {"id": job_id}
                )
                claimed = await claim(session, worker_id="worker-a")

        async with sessions() as session, session.begin():
            status = await mark_failed(
                session,
                claimed[0],
                error_class="IntegrationError",
                error_message="still failing",
                duration_ms=10,
            )

    assert status is JobStatus.DEAD

    async with sessions() as session:
        row = await get_job(session, job_id)
        runs = await get_runs(session, job_id)

    assert row is not None
    assert row["status"] == JobStatus.DEAD.value
    assert len(runs) == 3, "every attempt must survive in the history"
    assert [run["attempt_number"] for run in runs] == [1, 2, 3]


@pytest.mark.asyncio
async def test_success_releases_the_lease_and_records_the_run(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session, session.begin():
        job_id = await enqueue(session, job_type=_DISPATCH_JOB, payload={"client_id": "abc"})

    async with sessions() as session, session.begin():
        claimed = await claim(session, worker_id="worker-a")

    async with sessions() as session, session.begin():
        await mark_succeeded(session, claimed[0], duration_ms=42)

    async with sessions() as session:
        row = await get_job(session, job_id)
        runs = await get_runs(session, job_id)

    assert row is not None
    assert row["status"] == JobStatus.SUCCEEDED.value
    assert row["lease_expires_at"] is None
    assert runs[0]["outcome"] == "success"
    assert runs[0]["duration_ms"] == 42


# ─── Constraints the database enforces ───────────────────────────────────


@pytest.mark.asyncio
async def test_generation_cannot_be_given_a_retry_budget(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """🔒 Arch §11.2, enforced by `ck_jobs__generation_not_retried`.

    ⚠️ The policy table already sets `max_attempts=1` for generation, so this
    would pass with no constraint at all — which is exactly why it is written
    against raw SQL that bypasses `enqueue`. The check exists for the caller who
    writes their own INSERT, or a future `enqueue` that grows a `max_attempts`
    argument. Each retried attempt is a paid API call.
    """
    with pytest.raises(IntegrityError, match="ck_jobs__generation_not_retried"):
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO jobs (job_type, job_class, payload, max_attempts) "
                    "VALUES (:t, 'generation', '{}'::jsonb, 3)"
                ),
                {"t": _GENERATION_JOB},
            )


@pytest.mark.asyncio
async def test_generation_enqueues_with_a_single_attempt(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The policy and the constraint agree — the row is accepted at 1."""
    async with sessions() as session, session.begin():
        job_id = await enqueue(session, job_type=_GENERATION_JOB, payload={"draft_id": "abc"})

    assert job_id is not None

    async with sessions() as session:
        row = await get_job(session, job_id)

    assert row is not None
    assert row["max_attempts"] == 1


# ─── Idempotent enqueue ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_key_is_suppressed(sessions: async_sessionmaker[AsyncSession]) -> None:
    """🔒 Suppression happens in the index, not in a read-then-write.

    ⚠️ This also proves the `index_where` predicate matches the partial index.
    If it did not, PostgreSQL raises "no unique or exclusion constraint matching
    the ON CONFLICT specification" — a runtime error nothing without a database
    would catch.
    """
    tenant = uuid.uuid4()

    async with sessions() as session, session.begin():
        first = await enqueue(
            session,
            job_type=_DISPATCH_JOB,
            payload={"client_id": "abc"},
            tenant_id=tenant,
            idempotency_key="reminder-2026-08-07",
        )

    async with sessions() as session, session.begin():
        second = await enqueue(
            session,
            job_type=_DISPATCH_JOB,
            payload={"client_id": "abc"},
            tenant_id=tenant,
            idempotency_key="reminder-2026-08-07",
        )

    assert first is not None
    assert second is None, "the second enqueue must be suppressed, not duplicated"


@pytest.mark.asyncio
async def test_platform_jobs_deduplicate_despite_a_null_tenant(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """🔒 `NULLS NOT DISTINCT` — the subtle one.

    ⚠️ Under PostgreSQL's default NULL semantics two rows with a NULL tenant
    never conflict, so this index would silently fail to deduplicate exactly the
    class of job most likely to be enqueued twice: a scheduler restart
    re-enqueueing its maintenance work. Requires PostgreSQL 15+.
    """
    async with sessions() as session, session.begin():
        first = await enqueue(
            session,
            job_type=_DISPATCH_JOB,
            payload={"scope": "platform"},
            tenant_id=None,
            idempotency_key="nightly-purge-2026-08-07",
        )

    async with sessions() as session, session.begin():
        second = await enqueue(
            session,
            job_type=_DISPATCH_JOB,
            payload={"scope": "platform"},
            tenant_id=None,
            idempotency_key="nightly-purge-2026-08-07",
        )

    assert first is not None
    assert second is None, (
        "a platform job (NULL tenant) was enqueued twice under one key. "
        "uq_jobs__idempotency needs NULLS NOT DISTINCT."
    )


@pytest.mark.asyncio
async def test_different_tenants_may_share_an_idempotency_key(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The key is scoped per tenant — two practitioners' reminders are distinct."""
    async with sessions() as session, session.begin():
        first = await enqueue(
            session,
            job_type=_DISPATCH_JOB,
            payload={"client_id": "a"},
            tenant_id=uuid.uuid4(),
            idempotency_key="daily-checkin",
        )
        second = await enqueue(
            session,
            job_type=_DISPATCH_JOB,
            payload={"client_id": "b"},
            tenant_id=uuid.uuid4(),
            idempotency_key="daily-checkin",
        )

    assert first is not None
    assert second is not None
    assert first != second


# ─── Transactional enqueue (Arch §11.1) ──────────────────────────────────


@pytest.mark.asyncio
async def test_rolled_back_transaction_leaves_no_job(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """🔒 The property that made PostgreSQL the right queue (ADR-11).

    A rolled-back approval must not leave a ghost delivery job. With an external
    broker this needs an outbox pattern; here it is a consequence of the job row
    living in the publisher's own transaction.
    """
    job_id: uuid.UUID | None = None

    with pytest.raises(RuntimeError, match="deliberate"):
        async with sessions() as session, session.begin():
            job_id = await enqueue(session, job_type=_DISPATCH_JOB, payload={"client_id": "abc"})
            raise RuntimeError("deliberate rollback")

    assert job_id is not None

    async with sessions() as session:
        assert await get_job(session, job_id) is None, (
            "the job survived a rolled-back transaction — enqueue is not "
            "transactional, and a cancelled action could still send a message"
        )


@pytest.mark.asyncio
async def test_committed_transaction_leaves_a_claimable_job(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The other half: a committed publisher's job is immediately claimable."""
    async with sessions() as session, session.begin():
        job_id = await enqueue(session, job_type=_DISPATCH_JOB, payload={"client_id": "abc"})

    async with sessions() as session, session.begin():
        claimed = await claim(session, worker_id="worker-a")

    assert [job.id for job in claimed] == [job_id]
