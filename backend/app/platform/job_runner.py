"""Job execution — claim, run, record, retry.

The queue (:mod:`app.platform.jobs`) decides *which* job runs next; this decides
*how* it runs. Kept apart because the storage half is asserted against a real
PostgreSQL while this half is about transaction boundaries and failure handling.

🔒 **Three transactions per job, not one** — and the split is the whole design:

1. **Claim.** Commits immediately. The lease must be visible to every other
   worker before this one starts work, or `SKIP LOCKED` protects nothing: a
   second worker polling mid-execution would find the row still `pending`.
2. **Execute.** The handler's own transaction, opened with the job's tenant
   scope. Rolled back on failure, so a handler that fails halfway leaves nothing
   behind.
3. **Record.** Commits the outcome. Separate from (2) because it must be written
   *whether or not* the handler's transaction survived — recording a failure
   inside the transaction that failed would roll the record back with it, and
   the job would look like it had never been attempted.

⚠️ Collapsing these into one transaction is the tempting simplification, and it
breaks the queue in a way that only shows up under concurrency.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.context import RequestContext, context_scope
from app.kernel.jobs import JobContractError, get_handler, policy_for
from app.kernel.models import JobStatus
from app.platform.db import transaction
from app.platform.jobs import (
    ClaimedJob,
    claim,
    mark_failed,
    mark_succeeded,
    recover_expired_leases,
)
from app.platform.logging import get_logger

logger = get_logger(__name__)

#: Opens a transaction. Injected so tests can supply one bound to a test engine
#: without reaching into the process-wide session factory.
TransactionFactory = Callable[..., AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True, slots=True)
class TickResult:
    """What one poll cycle did. Returned for logging and for tests."""

    claimed: int
    succeeded: int
    failed: int
    dead: int
    recovered: int

    @property
    def did_work(self) -> bool:
        """True when the tick found something. A worker that finds work should
        poll again immediately rather than sleeping out its interval — a backlog
        drains at one job per minute otherwise."""
        return self.claimed > 0 or self.recovered > 0


class JobRunner:
    """Claims and executes jobs against a database."""

    def __init__(
        self,
        *,
        worker_id: str | None = None,
        batch_size: int = 1,
        transaction_factory: TransactionFactory = transaction,
    ) -> None:
        #: Identifies this worker in `jobs.claimed_by` and `job_runs.worker_id`.
        #: Random per process: two workers on one host must be distinguishable,
        #: and a hostname is not (containers share it).
        self.worker_id = worker_id or f"worker_{uuid.uuid4().hex[:12]}"
        self._batch_size = batch_size
        self._transaction = transaction_factory

    async def tick(self) -> TickResult:
        """One poll cycle: recover expired leases, then claim and run.

        🔒 Recovery runs *first*. A job whose worker died is already overdue;
        making it wait behind a fresh claim adds the poll interval to a delay
        that has already exceeded the lease.
        """
        recovered = await self._recover()
        claimed = await self._claim_batch()

        succeeded = failed = dead = 0
        for job in claimed:
            outcome = await self._execute(job)
            if outcome is JobStatus.SUCCEEDED:
                succeeded += 1
            elif outcome is JobStatus.DEAD:
                dead += 1
            else:
                failed += 1

        return TickResult(
            claimed=len(claimed),
            succeeded=succeeded,
            failed=failed,
            dead=dead,
            recovered=recovered,
        )

    async def _recover(self) -> int:
        """Return expired-lease jobs to the queue (DB §13.3, NFR-025)."""
        async with self._transaction() as session:
            return await recover_expired_leases(session)

    async def _claim_batch(self) -> list[ClaimedJob]:
        """Claim work in its own committed transaction.

        🔒 Commits before execution begins. Until the claim is committed, another
        worker polling sees the row as `pending` and claims it too — `SKIP
        LOCKED` only protects rows locked by an *open* transaction, and holding
        that transaction open for the job's duration would block the queue for
        as long as the job runs.
        """
        async with self._transaction() as session:
            return await claim(
                session,
                worker_id=self.worker_id,
                limit=self._batch_size,
            )

    async def _execute(self, job: ClaimedJob) -> JobStatus:
        """Run one claimed job and record its outcome.

        🔒 Runs inside :func:`context_scope` with a worker context, so audit
        entries and log lines carry the same shape they would on a request — one
        implementation, not two (NFR-072).

        🔒 The handler's transaction carries the job's ``tenant_id``, so a
        handler queries exactly as it would while serving a request for that
        tenant, and RLS applies to it identically. A handler that ran unscoped
        would see nothing (policies match no rows) or, worse, be written against
        the assumption that it sees everything.
        """
        policy = policy_for(job.job_class)
        started = time.monotonic()

        with context_scope(RequestContext.for_worker(job.job_type, tenant_id=job.tenant_id)):
            try:
                handler = get_handler(job.job_type).handler
            except JobContractError as error:
                # An unknown job type is not retryable: the row was enqueued by a
                # deploy whose handler has since been removed, and every retry
                # would fail identically. Burn the remaining attempts so it
                # dead-letters now rather than at the end of its backoff schedule.
                return await self._record_failure(
                    job,
                    error=error,
                    duration_ms=_elapsed_ms(started),
                    terminal=True,
                )

            try:
                # 🔒 The timeout is enforced here, not by the lease. The lease is
                # a *recovery* mechanism for a worker that died; this is what
                # stops a hung handler from occupying the worker until then.
                async with asyncio.timeout(policy.timeout.total_seconds()):
                    async with self._transaction(tenant_id=job.tenant_id) as session:
                        await handler(job.payload, session)

            except TimeoutError as error:
                return await self._record_failure(
                    job,
                    error=error,
                    duration_ms=_elapsed_ms(started),
                    timed_out=True,
                )

            except Exception as error:
                # 🔒 Catches everything a handler can raise. The loop must
                # survive any single job: one badly-behaved handler must not stop
                # every reminder, plan delivery and rollup in the system.
                return await self._record_failure(
                    job,
                    error=error,
                    duration_ms=_elapsed_ms(started),
                )

            return await self._record_success(job, duration_ms=_elapsed_ms(started))

    async def _record_success(self, job: ClaimedJob, *, duration_ms: int) -> JobStatus:
        async with self._transaction() as session:
            await mark_succeeded(session, job, duration_ms=duration_ms)

        logger.info(
            "Job succeeded",
            extra={
                "job_id": str(job.id),
                "job_type": job.job_type,
                "attempt": job.attempt_count,
                "duration_ms": duration_ms,
            },
        )
        return JobStatus.SUCCEEDED

    async def _record_failure(
        self,
        job: ClaimedJob,
        *,
        error: BaseException,
        duration_ms: int,
        timed_out: bool = False,
        terminal: bool = False,
    ) -> JobStatus:
        """Record a failed attempt in a transaction of its own.

        ⚠️ Separate from the handler's transaction, which has already rolled
        back. Writing the failure there would roll the record back with it, and
        the job would look as though it had never been attempted — retried
        forever with no history explaining why.
        """
        async with self._transaction() as session:
            return await mark_failed(
                session,
                job,
                error_class=type(error).__name__,
                error_message=_safe_message(error),
                duration_ms=duration_ms,
                timed_out=timed_out,
                terminal=terminal,
            )


def _elapsed_ms(started: float) -> int:
    """Monotonic elapsed time. `time.monotonic` rather than wall clock: an NTP
    step mid-job would otherwise record a negative duration."""
    return int((time.monotonic() - started) * 1000)


#: 🔒 What of an exception is safe to store.
#:
#: ⚠️ `str(exception)` on a database error carries the failed statement *and its
#: bound parameters* — which is to say the row's values, in a table operators
#: read and backups retain (NFR-033). A `ValueError` raised by a handler can
#: carry a client's name just as easily.
_MESSAGE_LIMIT = 500


def _safe_message(error: BaseException) -> str:
    """Bound an exception message before it reaches `jobs.last_error`.

    Length only — this cannot tell a parameter value from a description, and
    pretending otherwise would be worse than stating the limit. Handlers that
    raise with row values in the message are the real fix; this stops one
    exception from filling the column.
    """
    message = str(error).strip()
    if not message:
        return type(error).__name__
    if len(message) > _MESSAGE_LIMIT:
        return message[: _MESSAGE_LIMIT - 3] + "..."
    return message
