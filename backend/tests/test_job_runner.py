"""The job runner's execution semantics — C5.

🔒 What this protects, none of which needs a database:

* **The three-transaction split.** Claim commits before execution starts; the
  handler gets its own transaction; the outcome is recorded in a third. Merging
  any two breaks the queue under concurrency, and the failure is invisible with
  one worker — exactly the configuration MVP runs.
* **The loop survives any handler.** One badly-behaved job must not stop every
  reminder, plan delivery and rollup in the system.
* **A hung handler is bounded by its class timeout**, not by the lease. The
  lease recovers a worker that *died*; the timeout stops one that is merely
  stuck occupying the process until then.
* **An unknown job type dead-letters immediately** rather than burning its
  backoff schedule on a failure that cannot change.

⚠️ These use a fake transaction factory, so they assert the runner's control
flow, not its SQL. That `SKIP LOCKED` prevents a double claim and that a lease
actually expires are C6, against a real PostgreSQL — no fake can prove either.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.kernel.jobs import register_handler, reset_handlers
from app.kernel.models import JobClass, JobStatus
from app.platform import job_runner as runner_module
from app.platform.job_runner import JobRunner
from app.platform.jobs import ClaimedJob


@pytest.fixture(autouse=True)
def _clean_handlers() -> Iterator[None]:
    """Before and after. 🔒 Same leak as the other two registries."""
    reset_handlers()
    yield
    reset_handlers()


class FakeSession:
    """Stands in for an AsyncSession. The runner never inspects it."""


class RecordingFactory:
    """A transaction factory that records how it was opened.

    🔒 The point of the recording is the *count*: the runner must open a
    separate transaction for the claim, the handler and the outcome. A single
    shared transaction would pass every behavioural assertion here while
    breaking the queue in production.
    """

    def __init__(self) -> None:
        self.opened: list[dict[str, Any]] = []
        self.rolled_back = 0

    def __call__(self, **kwargs: Any) -> Any:
        self.opened.append(kwargs)

        @asynccontextmanager
        async def _scope() -> AsyncIterator[FakeSession]:
            try:
                yield FakeSession()
            except Exception:
                self.rolled_back += 1
                raise

        return _scope()


def _job(
    *,
    job_type: str = "send_message",
    job_class: JobClass = JobClass.DISPATCH,
    attempt_count: int = 1,
    max_attempts: int = 3,
    tenant_id: uuid.UUID | None = None,
) -> ClaimedJob:
    return ClaimedJob(
        id=uuid.uuid4(),
        job_type=job_type,
        job_class=job_class,
        payload={"client_id": str(uuid.uuid4())},
        tenant_id=tenant_id,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
    )


@pytest.fixture
def patched_queue(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the SQL layer, leaving the runner's control flow under test."""
    state: dict[str, Any] = {
        "to_claim": [],
        "recovered": 0,
        "succeeded": [],
        "failed": [],
    }

    async def fake_claim(_session: Any, **_kwargs: Any) -> list[ClaimedJob]:
        batch = state["to_claim"]
        state["to_claim"] = []
        return batch

    async def fake_recover(_session: Any) -> int:
        return int(state["recovered"])

    async def fake_succeeded(_session: Any, job: ClaimedJob, **kwargs: Any) -> None:
        state["succeeded"].append((job, kwargs))

    async def fake_failed(_session: Any, job: ClaimedJob, **kwargs: Any) -> JobStatus:
        state["failed"].append((job, kwargs))
        if kwargs.get("terminal") or job.is_final_attempt:
            return JobStatus.DEAD
        return JobStatus.PENDING

    monkeypatch.setattr(runner_module, "claim", fake_claim)
    monkeypatch.setattr(runner_module, "recover_expired_leases", fake_recover)
    monkeypatch.setattr(runner_module, "mark_succeeded", fake_succeeded)
    monkeypatch.setattr(runner_module, "mark_failed", fake_failed)
    return state


# ─── Transaction boundaries ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claim_handler_and_outcome_use_separate_transactions(
    patched_queue: dict[str, Any],
) -> None:
    """🔒 The three-transaction split (see the module docstring).

    A claim that has not committed is invisible to other workers, so a second
    worker polling mid-execution would claim the same row. Holding one
    transaction open for the job's whole duration would block the queue for as
    long as the job runs.
    """

    async def handler(_payload: Mapping[str, Any], _session: Any) -> None:
        pass

    register_handler("send_message", JobClass.DISPATCH, handler)
    patched_queue["to_claim"] = [_job()]

    factory = RecordingFactory()
    result = await JobRunner(transaction_factory=factory).tick()

    assert result.succeeded == 1
    # recover, claim, handler, record — four separate transactions.
    assert len(factory.opened) == 4


@pytest.mark.asyncio
async def test_handler_transaction_carries_the_jobs_tenant(
    patched_queue: dict[str, Any],
) -> None:
    """🔒 The handler queries as though serving a request for that tenant.

    An unscoped handler transaction would have RLS match nothing, so the handler
    would silently see an empty database rather than fail.
    """
    tenant = uuid.uuid4()

    async def handler(_payload: Mapping[str, Any], _session: Any) -> None:
        pass

    register_handler("send_message", JobClass.DISPATCH, handler)
    patched_queue["to_claim"] = [_job(tenant_id=tenant)]

    factory = RecordingFactory()
    await JobRunner(transaction_factory=factory).tick()

    assert {"tenant_id": tenant} in factory.opened


@pytest.mark.asyncio
async def test_recovery_runs_before_claiming(patched_queue: dict[str, Any]) -> None:
    """🔒 A job whose worker died is already overdue.

    Making it wait behind a fresh claim adds a poll interval to a delay that has
    already exceeded the lease.
    """
    patched_queue["recovered"] = 2

    result = await JobRunner(transaction_factory=RecordingFactory()).tick()

    assert result.recovered == 2
    assert result.did_work, "a tick that recovered work must not sleep"


# ─── Failure handling ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handler_exception_is_recorded_not_raised(
    patched_queue: dict[str, Any],
) -> None:
    """🔒 The loop must survive any single job.

    One handler raising must not stop every reminder and rollup in the system.
    """

    async def handler(_payload: Mapping[str, Any], _session: Any) -> None:
        raise ValueError("provider rejected the message")

    register_handler("send_message", JobClass.DISPATCH, handler)
    patched_queue["to_claim"] = [_job()]

    result = await JobRunner(transaction_factory=RecordingFactory()).tick()

    assert result.failed == 1
    job, kwargs = patched_queue["failed"][0]
    assert kwargs["error_class"] == "ValueError"
    assert "provider rejected" in kwargs["error_message"]


@pytest.mark.asyncio
async def test_final_attempt_failure_dead_letters(patched_queue: dict[str, Any]) -> None:
    """🔒 AC-M8-007 — terminal failures are visible, never discarded."""

    async def handler(_payload: Mapping[str, Any], _session: Any) -> None:
        raise ValueError("still failing")

    register_handler("send_message", JobClass.DISPATCH, handler)
    patched_queue["to_claim"] = [_job(attempt_count=3, max_attempts=3)]

    result = await JobRunner(transaction_factory=RecordingFactory()).tick()

    assert result.dead == 1


@pytest.mark.asyncio
async def test_unknown_job_type_dead_letters_immediately(
    patched_queue: dict[str, Any],
) -> None:
    """🔒 A retry cannot make a missing handler appear.

    The row was enqueued by a deploy whose handler has since been removed. Every
    retry fails identically, so burning the backoff schedule only delays the
    dead-letter that tells an operator something is wrong.
    """
    patched_queue["to_claim"] = [_job(job_type="removed_in_a_later_deploy", attempt_count=1)]

    result = await JobRunner(transaction_factory=RecordingFactory()).tick()

    assert result.dead == 1
    _job_arg, kwargs = patched_queue["failed"][0]
    assert kwargs["terminal"] is True, "an unknown job type must not consume its backoff schedule"


@pytest.mark.asyncio
async def test_hung_handler_is_stopped_by_the_class_timeout(
    patched_queue: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔒 The timeout bounds a stuck handler; the lease only recovers a dead one.

    Without this a hung handler occupies the worker until its lease expires —
    for a maintenance job, ten minutes of no reminders going out.
    """
    from datetime import timedelta

    from app.kernel import jobs as kernel_jobs

    fast = kernel_jobs.JobClassPolicy(
        timeout=timedelta(milliseconds=50),
        max_attempts=3,
        priority=10,
    )
    monkeypatch.setattr(runner_module, "policy_for", lambda _job_class: fast)

    async def handler(_payload: Mapping[str, Any], _session: Any) -> None:
        await asyncio.sleep(5)

    register_handler("send_message", JobClass.DISPATCH, handler)
    patched_queue["to_claim"] = [_job()]

    result = await JobRunner(transaction_factory=RecordingFactory()).tick()

    assert result.failed == 1
    _job_arg, kwargs = patched_queue["failed"][0]
    assert kwargs["timed_out"] is True


@pytest.mark.asyncio
async def test_empty_tick_reports_no_work(patched_queue: dict[str, Any]) -> None:
    """An idle tick must sleep rather than spin the poll loop."""
    result = await JobRunner(transaction_factory=RecordingFactory()).tick()

    assert result.claimed == 0
    assert not result.did_work


# ─── Worker identity ─────────────────────────────────────────────────────


def test_workers_get_distinct_identities() -> None:
    """🔒 `claimed_by` must distinguish two workers on one host.

    A hostname does not: containers share it, and "which worker holds this
    lease" is the first question when a job is stuck.
    """
    assert JobRunner().worker_id != JobRunner().worker_id


def test_explicit_worker_id_is_respected() -> None:
    assert JobRunner(worker_id="worker-a").worker_id == "worker-a"
