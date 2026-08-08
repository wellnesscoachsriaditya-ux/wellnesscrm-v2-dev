"""The Postgres-backed job queue — where ADR-11 actually happens.

The kernel decides *what* a job is and *when it retries*
(:mod:`app.kernel.jobs`); this module decides *how it is stored and claimed*.
Splitting them is what lets the retry policy be tested without a database and
the storage be swapped without touching a policy.

🔒 **Claiming uses ``SELECT ... FOR UPDATE SKIP LOCKED``** (DB §13.2). Two
workers polling simultaneously never collide and never block: the second skips
the rows the first has locked rather than waiting behind them. Correct with one
worker today (Arch §11.3) and correct with five later — concurrency safety is
built in now because retrofitting it is far harder than including it.

🔒 **Enqueue is transactional** (Arch §11.1). :func:`enqueue` takes the caller's
session and does not commit. A job therefore exists only if the transaction that
created it commits: an approved plan cannot fail to schedule its delivery, and a
rolled-back approval cannot leave a ghost job. This is the property that made
Postgres the right queue; using it with its own connection would throw the
advantage away.

⚠️ **Every timestamp comparison happens in the database**, via ``now()``, never
in Python. The worker's clock and the database's clock are different clocks; a
lease compared against a drifting local clock either expires early — causing
double execution — or never, causing a stuck job that no sweep recovers.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, cast

from sqlalchemy import Table, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.context import get_actor
from app.kernel.jobs import backoff_delay, get_handler, policy_for, validate_payload
from app.kernel.models import Job, JobClass, JobOutcome, JobRun, JobStatus
from app.platform.logging import get_logger

logger = get_logger(__name__)

#: Core constructs rather than ORM classes — same reasoning as
#: ``platform/audit.py``: these writes have no identity map to maintain and must
#: not be reordered by a session flush.
_JOBS: Final[Table] = cast(Table, Job.__table__)
_JOB_RUNS: Final[Table] = cast(Table, JobRun.__table__)


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """A job this worker holds a lease on.

    Frozen: the claim is a fact about what the database granted. Mutating it
    would not extend the lease, and code that believed otherwise would run past
    its expiry.
    """

    id: uuid.UUID
    job_type: str
    job_class: JobClass
    payload: dict[str, Any]
    tenant_id: uuid.UUID | None
    attempt_count: int
    max_attempts: int

    @property
    def attempts_remaining(self) -> int:
        return max(0, self.max_attempts - self.attempt_count)

    @property
    def is_final_attempt(self) -> bool:
        """True when a failure now means dead-letter rather than retry."""
        return self.attempt_count >= self.max_attempts


# ─── Enqueue ──────────────────────────────────────────────────────────────


async def enqueue(
    session: AsyncSession,
    *,
    job_type: str,
    payload: Mapping[str, Any],
    tenant_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
    run_after: datetime | None = None,
    priority: int | None = None,
) -> uuid.UUID | None:
    """Enqueue a job inside the caller's transaction.

    🔒 Does not commit (ADR-04). The job exists only if the caller's transaction
    commits, which is what makes "approve a plan and schedule its delivery"
    atomic without an outbox.

    The class, retry ceiling and default priority come from the handler's
    registration, not from the caller. A caller that could pass its own
    ``max_attempts`` could make an AI generation retryable, which is exactly what
    Arch §11.2 forbids and what costs money.

    Args:
        idempotency_key: When given, a second enqueue with the same
            ``(tenant_id, job_type, key)`` is suppressed and ``None`` is
            returned. 🔒 Suppression is by unique index, not by a read-then-write
            — the read-then-write races, and this path exists precisely because
            two callers may arrive together.

    Returns:
        The job id, or ``None`` if an identical job was already queued.

    Raises:
        JobContractError: Unknown job type, or a payload carrying more than
            identifiers (DB §13.1).
    """
    registered = get_handler(job_type)
    policy = registered.policy
    validated = validate_payload(payload)

    values: dict[str, Any] = {
        "job_type": job_type,
        "job_class": registered.job_class.value,
        "payload": validated,
        "tenant_id": tenant_id,
        "status": JobStatus.PENDING.value,
        "priority": policy.priority if priority is None else priority,
        "max_attempts": policy.max_attempts,
        "attempt_count": 0,
        "idempotency_key": idempotency_key,
    }
    if run_after is not None:
        values["run_after"] = run_after

    base = insert(_JOBS).values(**values)

    # ⚠️ The conflict clause must be attached *before* `.returning()`:
    # `.returning()` narrows the type to a plain `ReturningInsert`, which no
    # longer carries the PostgreSQL-specific `on_conflict_do_nothing`.
    if idempotency_key is not None:
        # 🔒 Suppression happens in the database, via `uq_jobs__idempotency`.
        # An application-level "does it exist already?" check races: two workers
        # or two requests can both read "no" before either writes.
        #
        # ⚠️ `index_where` must match the partial index's predicate exactly, or
        # PostgreSQL cannot prove the index covers the conflict target and
        # raises "no unique or exclusion constraint matching the ON CONFLICT
        # specification" at runtime — a failure no unit test without a database
        # will catch.
        base = base.on_conflict_do_nothing(
            index_elements=["tenant_id", "job_type", "idempotency_key"],
            index_where=text("idempotency_key IS NOT NULL"),
        )

    job_id = (await session.execute(base.returning(_JOBS.c.id))).scalar_one_or_none()

    if job_id is None:
        logger.info(
            "Job suppressed as duplicate",
            extra={"job_type": job_type, "idempotency_key": idempotency_key},
        )
    return job_id


# ─── Claim ────────────────────────────────────────────────────────────────

#: 🔒 The claim. Written as raw SQL rather than assembled through the ORM
#: because every clause is load-bearing and the generated form obscures which:
#:
#: * ``FOR UPDATE SKIP LOCKED`` — the reason this is a queue rather than a table
#:   two workers fight over.
#: * ``run_after <= now()`` — backoff, evaluated by the database's clock.
#: * ``ORDER BY priority, run_after`` — matches ``ix_jobs__claimable`` so the
#:   claim is an index scan rather than a sort of every pending row.
#: * The CTE — selecting and updating in one statement means no window exists
#:   between "this row is mine" and "the row says so".
_CLAIM_SQL = text(
    """
    WITH claimable AS (
        SELECT id
        FROM jobs
        WHERE status = 'pending'
          AND run_after <= now()
        ORDER BY priority, run_after
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    )
    UPDATE jobs
    SET status = 'claimed',
        claimed_at = now(),
        claimed_by = :worker_id,
        lease_expires_at = now() + make_interval(secs => :lease_seconds),
        attempt_count = jobs.attempt_count + 1,
        updated_at = now()
    FROM claimable
    WHERE jobs.id = claimable.id
    RETURNING jobs.id, jobs.job_type, jobs.job_class, jobs.payload,
              jobs.tenant_id, jobs.attempt_count, jobs.max_attempts;
    """
)


async def claim(
    session: AsyncSession,
    *,
    worker_id: str,
    limit: int = 1,
    lease: timedelta | None = None,
) -> list[ClaimedJob]:
    """Claim up to ``limit`` due jobs, taking a lease on each.

    🔒 ``attempt_count`` is incremented **here**, at claim time, not at
    completion. A worker that dies mid-job never reports anything, so counting
    at completion would let a job that crashes the worker be retried forever —
    each attempt killing the process before it could record that it had tried.
    Counting at claim means a crash still consumes an attempt, and the job
    eventually dead-letters instead of becoming a crash loop.

    Args:
        lease: Overrides the per-class lease. Only for tests that need a lease
            short enough to expire within a test run.
    """
    lease_seconds = (
        lease.total_seconds()
        if lease is not None
        # Mixed classes may be claimed in one call, so the lease must cover the
        # longest of them. Taking the shortest would expire a maintenance job's
        # lease while it legitimately ran.
        else max(policy_for(job_class).lease for job_class in JobClass).total_seconds()
    )

    rows = (
        await session.execute(
            _CLAIM_SQL,
            {"limit": limit, "worker_id": worker_id, "lease_seconds": lease_seconds},
        )
    ).all()

    claimed = [
        ClaimedJob(
            id=row.id,
            job_type=row.job_type,
            job_class=JobClass(row.job_class),
            payload=dict(row.payload),
            tenant_id=row.tenant_id,
            attempt_count=row.attempt_count,
            max_attempts=row.max_attempts,
        )
        for row in rows
    ]

    for job in claimed:
        await session.execute(
            insert(_JOB_RUNS).values(
                job_id=job.id,
                attempt_number=job.attempt_count,
                worker_id=worker_id,
            )
        )

    return claimed


# ─── Completion ───────────────────────────────────────────────────────────


async def mark_succeeded(
    session: AsyncSession,
    job: ClaimedJob,
    *,
    duration_ms: int,
) -> None:
    """Record success and release the lease."""
    await session.execute(
        update(_JOBS)
        .where(_JOBS.c.id == job.id)
        .values(
            status=JobStatus.SUCCEEDED.value,
            lease_expires_at=None,
            updated_at=text("now()"),
        )
    )
    await _finish_run(
        session,
        job,
        outcome=JobOutcome.SUCCESS,
        duration_ms=duration_ms,
    )


async def mark_failed(
    session: AsyncSession,
    job: ClaimedJob,
    *,
    error_class: str,
    error_message: str,
    duration_ms: int,
    timed_out: bool = False,
    terminal: bool = False,
) -> JobStatus:
    """Record a failed attempt, then retry with backoff or dead-letter.

    Args:
        terminal: Skip the remaining attempts and dead-letter now. 🔒 For
            failures that cannot succeed on a retry — an unregistered job type,
            a payload the handler cannot parse. Retrying those buys an identical
            failure per attempt and delays the dead-letter that tells an operator
            something is actually wrong.

    🔒 The decision is made from ``attempt_count`` as the database recorded it at
    claim time, so a worker that lost its lease and had the job re-claimed cannot
    resurrect it by reporting late.

    ⚠️ ``error_message`` is truncated and stored as given. It reaches
    ``jobs.last_error``, which operators read — a caller passing a raw exception
    string can leak row values into it (NFR-033). The scrub belongs at the raise
    site; this function bounds the length so one exception cannot fill the table.

    Returns:
        The job's new status — ``PENDING`` if it will be retried, ``DEAD`` if the
        attempt ceiling is reached.
    """
    policy = policy_for(job.job_class)
    dead = terminal or job.is_final_attempt

    if dead:
        await session.execute(
            update(_JOBS)
            .where(_JOBS.c.id == job.id)
            .values(
                status=JobStatus.DEAD.value,
                lease_expires_at=None,
                last_error=_truncate(error_message),
                updated_at=text("now()"),
            )
        )
        # 🔒 AC-M8-007 — never silently discarded. A dead job is visible in the
        # operator console (FR-M11-004), and this is the line that says so in
        # the logs at a severity someone alerts on.
        logger.error(
            "Job dead-lettered",
            extra={
                "job_id": str(job.id),
                "job_type": job.job_type,
                "attempts": job.attempt_count,
                "error_class": error_class,
                # Distinguishes "exhausted its retries" from "could never have
                # succeeded" — the first is a flaky dependency, the second is a
                # deploy that dropped a handler. Different operator responses.
                "terminal": terminal,
            },
        )
    else:
        delay = backoff_delay(policy, job.attempt_count)
        await session.execute(
            update(_JOBS)
            .where(_JOBS.c.id == job.id)
            .values(
                status=JobStatus.PENDING.value,
                claimed_at=None,
                claimed_by=None,
                lease_expires_at=None,
                last_error=_truncate(error_message),
                # 🔒 Computed by the database's clock, like every other time
                # comparison here. `now() + interval` rather than a Python
                # datetime, so a worker with a skewed clock cannot schedule a
                # retry in the past (immediate re-claim, hot loop) or far future.
                #
                # ⚠️ Bound parameter, not an f-string. The value is internal
                # policy rather than user input, so this is not an injection
                # today — but a `text()` with an interpolated value is the
                # pattern a later edit copies to somewhere it does matter.
                run_after=func.now() + func.make_interval(0, 0, 0, 0, 0, 0, delay.total_seconds()),
                updated_at=text("now()"),
            )
        )
        logger.warning(
            "Job failed; retrying",
            extra={
                "job_id": str(job.id),
                "job_type": job.job_type,
                "attempt": job.attempt_count,
                "max_attempts": job.max_attempts,
                "retry_in_seconds": delay.total_seconds(),
                "error_class": error_class,
            },
        )

    await _finish_run(
        session,
        job,
        outcome=JobOutcome.TIMEOUT if timed_out else JobOutcome.FAILURE,
        duration_ms=duration_ms,
        error_class=error_class,
        error_message=_truncate(error_message),
    )

    return JobStatus.DEAD if dead else JobStatus.PENDING


async def _finish_run(
    session: AsyncSession,
    job: ClaimedJob,
    *,
    outcome: JobOutcome,
    duration_ms: int,
    error_class: str | None = None,
    error_message: str | None = None,
) -> None:
    """Close the ``job_runs`` row this attempt opened."""
    await session.execute(
        update(_JOB_RUNS)
        .where(
            _JOB_RUNS.c.job_id == job.id,
            _JOB_RUNS.c.attempt_number == job.attempt_count,
        )
        .values(
            finished_at=text("now()"),
            outcome=outcome.value,
            duration_ms=duration_ms,
            error_class=error_class,
            error_message=error_message,
        )
    )


#: 🔒 Longest stored error text. An exception carrying a full SQL statement with
#: bound parameters is both a leak (NFR-033) and a way to fill the table.
_MAX_ERROR_LENGTH = 1000


def _truncate(message: str) -> str:
    if len(message) <= _MAX_ERROR_LENGTH:
        return message
    return message[: _MAX_ERROR_LENGTH - 3] + "..."


# ─── Lease recovery (DB §13.3) ────────────────────────────────────────────

#: 🔒 NFR-025 — jobs are not lost on deploy.
#:
#: A worker killed mid-job leaves ``status='claimed'`` and a lease that stops
#: being renewed. This returns those rows to ``pending`` so another worker picks
#: them up. Combined with handler idempotency, re-execution is safe.
#:
#: ⚠️ ``attempt_count`` is deliberately **not** reset. The attempt was made; the
#: worker died during it. Resetting would make a job that reliably kills its
#: worker retry forever, which is the failure mode this whole mechanism exists
#: to bound.
#:
#: ⚠️ 🔒 ``::job_status`` is load-bearing, not decoration. A plain literal in a
#: ``SET`` clause is coerced to the column's enum type, but the result of a
#: ``CASE`` is resolved on its own — all branches are ``unknown`` literals, so
#: PostgreSQL types the whole expression as ``text`` and refuses to assign it to
#: the enum column. Without the cast this statement fails with
#: ``DatatypeMismatch`` the first time a lease actually expires, which is
#: exactly when it must work. (The ``WHERE status IN ('claimed', 'running')``
#: literals need no cast: there they are compared against the column, so the
#: comparison coerces them.)
_RECOVER_SQL = text(
    """
    UPDATE jobs
    SET status = (
            CASE WHEN attempt_count >= max_attempts THEN 'dead' ELSE 'pending' END
        )::job_status,
        claimed_at = NULL,
        claimed_by = NULL,
        lease_expires_at = NULL,
        last_error = 'Lease expired — worker died or stalled mid-job',
        updated_at = now()
    WHERE status IN ('claimed', 'running')
      AND lease_expires_at < now()
    RETURNING id, job_type, attempt_count, max_attempts;
    """
)


async def recover_expired_leases(session: AsyncSession) -> int:
    """Return jobs with expired leases to the queue (DB §13.3).

    A job whose lease expired on its final attempt goes straight to ``dead``
    rather than back to ``pending``: it has already consumed its budget, and
    re-queueing it would let it be claimed once more than its ceiling allows.

    Returns:
        How many jobs were recovered.
    """
    rows = (await session.execute(_RECOVER_SQL)).all()

    for row in rows:
        logger.warning(
            "Recovered job with expired lease",
            extra={
                "job_id": str(row.id),
                "job_type": row.job_type,
                "attempt": row.attempt_count,
                "dead": row.attempt_count >= row.max_attempts,
            },
        )

    return len(rows)


# ─── Inspection ───────────────────────────────────────────────────────────


async def get_job(session: AsyncSession, job_id: uuid.UUID) -> Mapping[str, Any] | None:
    """Read one job row. For the operator console and for tests."""
    row = (
        (await session.execute(select(_JOBS).where(_JOBS.c.id == job_id))).mappings().one_or_none()
    )
    return dict(row) if row is not None else None


async def get_runs(session: AsyncSession, job_id: uuid.UUID) -> Sequence[Mapping[str, Any]]:
    """Read a job's attempt history, oldest first (FR-M11-004)."""
    rows = (
        await session.execute(
            select(_JOB_RUNS)
            .where(_JOB_RUNS.c.job_id == job_id)
            .order_by(_JOB_RUNS.c.attempt_number)
        )
    ).mappings()
    return [dict(row) for row in rows]


# ─── The event-bus enqueuer (Arch §3.4b) ──────────────────────────────────


async def enqueue_for_event(
    session: AsyncSession,
    job_type: str,
    event_name: str,
    payload: dict[str, Any],
) -> None:
    """Enqueue a deferred subscriber's job, on the publisher's session.

    Installed into :mod:`app.kernel.events` at startup — the kernel names the
    capability, the entry point supplies this (R5).

    🔒 **The tenant comes from the request context, not the payload.** A payload
    is caller-supplied data; reading a tenant id out of it would let whoever
    constructed the event choose which tenant the job runs against. The context's
    tenant was established by authentication.

    🔒 **The idempotency key is derived from the event, not generated.** Key parts:
    the event's wire name, the job type, and the publisher's request id. That
    makes a retried HTTP request — same request id, same event — collapse to one
    job, which is what stops a client's double-tap from sending two WhatsApp
    messages. It also means a *different* request publishing the same event still
    enqueues its own job, which is correct: two genuine stage changes deserve two
    notifications.
    """
    request_id = payload.get("_request_id")
    tenant_id = get_actor().tenant_id

    await enqueue(
        session,
        job_type=job_type,
        payload=payload,
        tenant_id=tenant_id,
        # 🔒 `None` when there is no request id to key on — an absent key means
        # "do not deduplicate", which is the safe direction. A constant fallback
        # would collapse every job of this type into one row and silently drop
        # work that should have run.
        idempotency_key=(f"{event_name}:{job_type}:{request_id}" if request_id else None),
    )
