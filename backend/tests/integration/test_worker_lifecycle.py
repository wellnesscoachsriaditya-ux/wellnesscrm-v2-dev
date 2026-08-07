"""The worker, end to end, against a live PostgreSQL — C6.

🔒 **The S1 Definition of Done bullet**: *"Worker processes a job end to end and
survives a restart mid-job."* The previous file proves the storage layer in
isolation; this one runs :class:`~app.platform.job_runner.JobRunner` against a
real database and asserts the whole path — claim, execute, record — behaves.

Two things here cannot be shown any other way:

1. **Restart survival** (NFR-025). A worker is killed mid-job by abandoning its
   claim with an expired lease; a *different* runner instance then recovers and
   completes it. With a fake queue both halves would be the same in-memory dict
   and the test would prove nothing about what survives a process boundary.
2. **Tenant scope reaches the handler.** The runner opens the handler's
   transaction with the job's ``tenant_id``, which is what makes RLS apply to a
   handler exactly as it does to a request. A handler that ran unscoped would
   see an empty database rather than fail, so this is asserted by having the
   handler read the session variable back.

⚠️ Same setup as the isolation gate — see `test_tenant_isolation.py`. These fail
rather than skip in CI, where `REQUIRE_LIVE_DATABASE` is set.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.kernel.jobs import register_handler, reset_handlers
from app.kernel.models import JobClass, JobStatus
from app.platform.db import set_tenant_scope
from app.platform.job_runner import JobRunner
from app.platform.jobs import claim, enqueue, get_job, get_runs

_JOB_TYPE = "integration_worker_job"


@pytest.fixture(autouse=True)
def _clean_handlers() -> Iterator[None]:
    reset_handlers()
    yield
    reset_handlers()


def enqueued_id(job_id: uuid.UUID | None) -> uuid.UUID:
    """Assert an enqueue produced a job, and narrow the type.

    ``enqueue`` returns ``None`` when an idempotency key suppressed the insert.
    None of these tests pass a key, so ``None`` here means the row was silently
    not created — which would make every later assertion in the test vacuous
    rather than failing. Better to stop at the cause.
    """
    assert job_id is not None, "enqueue returned no id: the job row was not created"
    return job_id


@pytest_asyncio.fixture
async def sessions(app_engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Session factory on the application role — the role RLS constrains."""
    yield async_sessionmaker(bind=app_engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def transaction_factory(sessions: async_sessionmaker[AsyncSession]) -> Any:
    """A transaction factory bound to the test engine.

    Mirrors :func:`app.platform.db.transaction` — including the ``SET LOCAL``
    tenant scope, which is the part under test when a handler queries. Injected
    rather than monkeypatching the process-wide session factory, so this suite
    cannot leak an engine into another.
    """

    @asynccontextmanager
    async def factory(
        *,
        tenant_id: uuid.UUID | None = None,
        actor_id: uuid.UUID | None = None,
        actor_role: str | None = None,
    ) -> AsyncIterator[AsyncSession]:
        async with sessions() as session, session.begin():
            await set_tenant_scope(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                actor_role=actor_role,
            )
            yield session

    return factory


@pytest_asyncio.fixture(autouse=True)
async def _clean_jobs(migrator_engine: AsyncEngine) -> AsyncIterator[None]:
    """Remove this suite's rows before and after — scoped by job type."""
    await _purge(migrator_engine)
    yield
    await _purge(migrator_engine)


async def _purge(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM jobs WHERE job_type = :t"), {"t": _JOB_TYPE})


# ─── End to end ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_worker_processes_a_job_end_to_end(
    sessions: async_sessionmaker[AsyncSession],
    transaction_factory: Any,
) -> None:
    """🔒 The S1 DoD bullet, first half: enqueue → tick → succeeded.

    One `tick()` must claim the pending job, run its handler, and record the
    outcome — with the run history showing a single successful attempt.
    """
    executed: list[dict[str, Any]] = []

    async def handler(payload: Mapping[str, Any], _session: AsyncSession) -> None:
        executed.append(dict(payload))

    register_handler(_JOB_TYPE, JobClass.DISPATCH, handler)

    async with sessions() as session, session.begin():
        job_id = enqueued_id(
            await enqueue(session, job_type=_JOB_TYPE, payload={"client_id": "abc"})
        )

    result = await JobRunner(transaction_factory=transaction_factory).tick()

    assert result.claimed == 1
    assert result.succeeded == 1
    assert len(executed) == 1
    assert executed[0]["client_id"] == "abc"

    async with sessions() as session:
        row = await get_job(session, job_id)
        runs = await get_runs(session, job_id)

    assert row is not None
    assert row["status"] == JobStatus.SUCCEEDED.value
    assert row["lease_expires_at"] is None
    assert len(runs) == 1
    assert runs[0]["outcome"] == "success"
    assert runs[0]["finished_at"] is not None


@pytest.mark.asyncio
async def test_handler_runs_under_the_jobs_tenant_scope(
    sessions: async_sessionmaker[AsyncSession],
    transaction_factory: Any,
) -> None:
    """🔒 RLS applies to a handler exactly as it does to a request.

    ⚠️ The failure this guards against is silent. A handler whose transaction
    carried no tenant would find every policy matching nothing — an empty
    database rather than an error — so "the reminder went out with no recipients"
    would be the first symptom, days later.

    Asserted by reading `app.tenant_id` back inside the handler: the session
    variable is exactly what the policies consult.
    """
    seen: list[str | None] = []

    async def handler(_payload: Mapping[str, Any], session: AsyncSession) -> None:
        seen.append(
            (await session.execute(text("SELECT current_setting('app.tenant_id', true)"))).scalar()
        )

    register_handler(_JOB_TYPE, JobClass.DISPATCH, handler)
    tenant = uuid.uuid4()

    async with sessions() as session, session.begin():
        enqueued_id(
            await enqueue(
                session, job_type=_JOB_TYPE, payload={"client_id": "abc"}, tenant_id=tenant
            )
        )

    result = await JobRunner(transaction_factory=transaction_factory).tick()

    assert result.succeeded == 1
    assert seen == [str(tenant)], (
        "the handler's transaction did not carry the job's tenant. Every RLS "
        "policy would match nothing and the handler would see an empty database."
    )


# ─── Restart survival (NFR-025) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_job_abandoned_mid_flight_is_recovered_and_completed(
    sessions: async_sessionmaker[AsyncSession],
    transaction_factory: Any,
) -> None:
    """🔒 The S1 DoD bullet, second half: survives a restart mid-job (NFR-025).

    The first worker claims with a lease that is already expired and then
    "dies" — never reporting. A *second*, independently-identified runner then
    recovers the job and completes it. That the two runners share no state is
    the point: a deploy replaces the process, and the queue is the only thing
    that persists.
    """
    executed: list[str] = []

    async def handler(payload: Mapping[str, Any], _session: AsyncSession) -> None:
        executed.append(str(payload["client_id"]))

    register_handler(_JOB_TYPE, JobClass.DISPATCH, handler)

    async with sessions() as session, session.begin():
        job_id = enqueued_id(
            await enqueue(session, job_type=_JOB_TYPE, payload={"client_id": "abc"})
        )

    # The doomed worker: claims, takes an already-expired lease, then vanishes
    # without recording an outcome — exactly what SIGKILL mid-job leaves behind.
    async with sessions() as session, session.begin():
        abandoned = await claim(session, worker_id="killed-by-deploy", lease=timedelta(seconds=-1))
    assert len(abandoned) == 1
    assert executed == [], "the doomed worker must not have run the handler"

    # A fresh process. Its tick recovers the expired lease and then claims.
    survivor = JobRunner(worker_id="restarted-worker", transaction_factory=transaction_factory)
    result = await survivor.tick()

    assert result.recovered == 1
    assert result.claimed == 1
    assert result.succeeded == 1
    assert executed == ["abc"], "the recovered job must actually run"

    async with sessions() as session:
        row = await get_job(session, job_id)
        runs = await get_runs(session, job_id)

    assert row is not None
    assert row["status"] == JobStatus.SUCCEEDED.value
    # 🔒 Two attempts recorded: the abandoned one and the successful retry. The
    # first is what tells an operator the job was interrupted rather than slow.
    assert len(runs) == 2
    assert runs[1]["outcome"] == "success"
    assert runs[1]["worker_id"] == "restarted-worker"


@pytest.mark.asyncio
async def test_recovery_does_not_exceed_the_attempt_ceiling(
    sessions: async_sessionmaker[AsyncSession],
    transaction_factory: Any,
) -> None:
    """A job abandoned on its last attempt dead-letters instead of re-running.

    Otherwise repeated crashes would loop forever: each expiry hands back a job
    that has already spent every attempt it was allowed.
    """

    async def handler(_payload: Mapping[str, Any], _session: AsyncSession) -> None:
        pass

    register_handler(_JOB_TYPE, JobClass.GENERATION, handler)

    # `generation` has max_attempts=1 (Arch §11.2), so one claim exhausts it.
    async with sessions() as session, session.begin():
        job_id = enqueued_id(
            await enqueue(session, job_type=_JOB_TYPE, payload={"draft_id": "abc"})
        )

    async with sessions() as session, session.begin():
        await claim(session, worker_id="killed-by-deploy", lease=timedelta(seconds=-1))

    result = await JobRunner(transaction_factory=transaction_factory).tick()

    assert result.recovered == 1
    assert result.claimed == 0, "a job past its ceiling must not be claimable again"

    async with sessions() as session:
        row = await get_job(session, job_id)

    assert row is not None
    assert row["status"] == JobStatus.DEAD.value


# ─── Failure paths ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failing_handler_retries_then_succeeds(
    sessions: async_sessionmaker[AsyncSession],
    transaction_factory: Any,
) -> None:
    """Transient failure → backoff → success, with both attempts in the history."""
    attempts: list[int] = []

    async def flaky(_payload: Mapping[str, Any], _session: AsyncSession) -> None:
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise RuntimeError("provider unavailable")

    register_handler(_JOB_TYPE, JobClass.DISPATCH, flaky)

    async with sessions() as session, session.begin():
        job_id = enqueued_id(
            await enqueue(session, job_type=_JOB_TYPE, payload={"client_id": "abc"})
        )

    runner = JobRunner(transaction_factory=transaction_factory)

    first = await runner.tick()
    assert first.failed == 1

    # Backoff put the retry in the future; pull it forward rather than sleeping
    # out a 30-second base delay in a test.
    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE jobs SET run_after = now() WHERE id = :id"), {"id": job_id}
        )

    second = await runner.tick()
    assert second.succeeded == 1
    assert attempts == [1, 2]

    async with sessions() as session:
        row = await get_job(session, job_id)
        runs = await get_runs(session, job_id)

    assert row is not None
    assert row["status"] == JobStatus.SUCCEEDED.value
    assert len(runs) == 2
    assert runs[0]["outcome"] == "failure"
    assert runs[0]["error_class"] == "RuntimeError"
    assert runs[1]["outcome"] == "success"


@pytest.mark.asyncio
async def test_a_handlers_writes_roll_back_when_it_fails(
    sessions: async_sessionmaker[AsyncSession],
    transaction_factory: Any,
) -> None:
    """🔒 A handler that fails halfway leaves nothing behind.

    ⚠️ This is why the handler gets its own transaction rather than sharing the
    one that records the outcome: the write must vanish, while the *record of
    the failure* must survive. Both are asserted here, because an implementation
    that collapsed them into one transaction would lose the second.
    """
    marker = f"rollback-probe-{uuid.uuid4()}"

    async def writes_then_fails(_payload: Mapping[str, Any], session: AsyncSession) -> None:
        await session.execute(
            text(
                "INSERT INTO jobs (job_type, job_class, payload, max_attempts) "
                "VALUES (:t, 'dispatch', '{}'::jsonb, 3)"
            ),
            {"t": marker},
        )
        raise RuntimeError("failed after writing")

    register_handler(_JOB_TYPE, JobClass.DISPATCH, writes_then_fails)

    async with sessions() as session, session.begin():
        job_id = enqueued_id(
            await enqueue(session, job_type=_JOB_TYPE, payload={"client_id": "abc"})
        )

    result = await JobRunner(transaction_factory=transaction_factory).tick()
    assert result.failed == 1

    async with sessions() as session:
        orphans = (
            await session.execute(
                text("SELECT count(*) FROM jobs WHERE job_type = :t"), {"t": marker}
            )
        ).scalar_one()
        runs = await get_runs(session, job_id)

    assert orphans == 0, "the handler's write survived its own failure"
    # The failure record must NOT have rolled back with it.
    assert len(runs) == 1
    assert runs[0]["outcome"] == "failure"


@pytest.mark.asyncio
async def test_an_empty_queue_is_a_no_op(transaction_factory: Any) -> None:
    """A tick with nothing to do reports no work, so the loop sleeps."""
    result = await JobRunner(transaction_factory=transaction_factory).tick()

    assert result.claimed == 0
    assert result.recovered == 0
    assert not result.did_work
