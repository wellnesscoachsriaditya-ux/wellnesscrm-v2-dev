"""Job contracts — what a job is, how long it may take, and when it retries.

🔒 Arch §11.2 and DB §13. This module holds the *policy*; ``platform/jobs.py``
holds the SQL that enforces it. The split matters: the kernel must not import
platform (R5), and a module enqueuing work should not need to know the queue is
PostgreSQL.

Three things live here, and each exists because the alternative is a bug that
only appears in production:

1. **The class policy table** (:data:`JOB_POLICY`) — timeout, retry ceiling and
   priority per :class:`~app.kernel.models.JobClass`. Declared once, so "how
   long may a PDF render take" has one answer rather than one per call site.
2. **Payload validation** (:func:`validate_payload`) — 🔒 IDs only (DB §13.1,
   NFR-033). A job row is retained, backed up and read by operators; it must not
   become a clinical data store.
3. **The handler registry** — job type → callable, with a startup check that
   every enqueued type can actually run.

⚠️ **Lease duration is derived, never configured per job** (DDR-15 in DB §26.2:
"lease = job timeout × 2"). A lease shorter than the timeout means a job still
running gets re-claimed and executed twice — the failure mode idempotency exists
to survive but should never have to.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.models import JobClass

# ─── Errors ───────────────────────────────────────────────────────────────


class JobContractError(RuntimeError):
    """A job violated a contract that must hold before it reaches the queue.

    🔒 Programmer error — an unknown job type, a payload carrying a value where
    an identifier belongs. Raised at enqueue rather than at execution, so the
    failure surfaces in the request that caused it rather than in a worker log
    an hour later.
    """


# ─── Class policy (Arch §11.2) ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class JobClassPolicy:
    """Timeout, retry ceiling and priority for one job class.

    Frozen: policy is read on every enqueue and every claim. A mutable table
    would let one caller's tweak change every later job's retry budget.
    """

    #: How long one attempt may run before the worker abandons it.
    timeout: timedelta
    #: 🔒 Total attempts, not retries *after* the first. ``1`` means never
    #: retried — see ``GENERATION``.
    max_attempts: int
    #: Lower runs sooner. Matches ``jobs.priority``.
    priority: int
    #: Base for exponential backoff between attempts.
    backoff_base: timedelta = timedelta(seconds=30)

    @property
    def lease(self) -> timedelta:
        """🔒 Lease duration — ``timeout × 2`` (DB §13.3, open question 15).

        ⚠️ Must exceed the timeout, or a job that is still legitimately running
        has its lease expire, gets swept back to ``pending`` and runs a second
        time concurrently with the first. Derived rather than configured so the
        two cannot drift apart.
        """
        return self.timeout * 2


#: 🔒 Arch §11.2, verbatim. The one place these numbers exist.
JOB_POLICY: Mapping[JobClass, JobClassPolicy] = {
    # Reminders and notifications. Retried, because a transient WhatsApp or SMTP
    # failure is the common case and the message is still wanted a minute later.
    JobClass.DISPATCH: JobClassPolicy(
        timeout=timedelta(seconds=30),
        max_attempts=3,
        priority=10,
    ),
    # 🔒 AI drafting. `max_attempts=1` — never auto-retried.
    #
    # Each attempt costs money, and a failure is usually deterministic
    # (malformed output, provider rejection), so a retry buys a second identical
    # failure and a second charge. The practitioner retries explicitly, and
    # FR-M5-010 guarantees they can proceed manually meanwhile. The database
    # carries the same rule as a check constraint (`ck_jobs__generation_not_retried`)
    # so it cannot be overridden by a caller passing its own max_attempts.
    JobClass.GENERATION: JobClassPolicy(
        timeout=timedelta(seconds=60),
        max_attempts=1,
        priority=100,
    ),
    # Plan PDFs. Retried twice: a headless-browser render fails transiently often
    # enough to be worth one more attempt, and the output is deterministic.
    JobClass.RENDERING: JobClassPolicy(
        timeout=timedelta(seconds=60),
        max_attempts=2,
        priority=100,
    ),
    # Check-in scheduling, at-risk recompute. Low priority: nothing waits on
    # them interactively, and they must not delay a reminder.
    JobClass.RECURRING: JobClassPolicy(
        timeout=timedelta(seconds=300),
        max_attempts=3,
        priority=200,
        backoff_base=timedelta(minutes=5),
    ),
    # Retention purge, quota reset. Lowest priority and the longest timeout —
    # they scan whole tables and nothing is waiting.
    JobClass.MAINTENANCE: JobClassPolicy(
        timeout=timedelta(seconds=600),
        max_attempts=3,
        priority=300,
        backoff_base=timedelta(minutes=15),
    ),
}


def policy_for(job_class: JobClass) -> JobClassPolicy:
    """The policy for a class.

    Raises:
        JobContractError: If a class has no policy — which can only happen if
            someone adds a :class:`JobClass` member without deciding its
            timeout and retry budget. Failing here forces that decision rather
            than silently defaulting to something that costs money.
    """
    policy = JOB_POLICY.get(job_class)
    if policy is None:
        raise JobContractError(
            f"No policy declared for job class {job_class.value!r}. Add one to "
            "JOB_POLICY — timeout and retry budget are decisions, not defaults."
        )
    return policy


def backoff_delay(policy: JobClassPolicy, attempt_count: int) -> timedelta:
    """Exponential backoff before the next attempt (FR-M8-004).

    ``attempt_count`` is the number of attempts already made, so the delay after
    the first failure is ``backoff_base``, then double, then quadruple.

    🔒 Capped at one hour. Unbounded doubling means a job that failed five times
    overnight is scheduled days out, which for a reminder is indistinguishable
    from losing it.
    """
    exponent = max(0, attempt_count - 1)
    # Cap the exponent before multiplying: 2**attempt_count on a large
    # attempt_count overflows into a timedelta the database cannot store, and
    # OverflowError inside the retry path would leave the job claimed.
    delay: timedelta = policy.backoff_base * (2 ** min(exponent, 16))
    ceiling = timedelta(hours=1)
    return delay if delay < ceiling else ceiling


# ─── Payload validation (DB §13.1, NFR-033) ───────────────────────────────

#: 🔒 The maximum length of any string in a payload. An identifier, code or slug
#: fits comfortably; a note, message body or AI-generated summary does not.
#: Chosen to be obviously too short for prose, so the failure arrives in
#: development rather than as a leak in a retained, backed-up row.
#:
#: Public because `kernel.events` imports it: an event's fields become a payload
#: verbatim, and two independent limits would let an event pass its own check
#: and then fail at enqueue inside the publisher's transaction.
MAX_PAYLOAD_STRING_LENGTH = 128

#: Keys the queue itself adds. Exempt from the identifier rules because they are
#: written by `kernel.events.to_payload`, not by a caller.
_RESERVED_KEYS = frozenset({"_event", "_request_id"})

#: A payload key. Same shape as an audit field name — anything else is a value
#: being smuggled in as a key.
_KEY_NAME = re.compile(r"^_?[a-z][a-z0-9_]{0,62}$")

#: 🔒 Beyond this many keys, the payload is carrying a record rather than
#: referencing one. A job needs the ids it must load, not the row itself.
_MAX_KEYS = 24


def validate_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """🔒 Assert a job payload holds identifiers only (DB §13.1, NFR-033).

    A job row is retained, included in backups and read by operators diagnosing
    a stuck queue. NFR-033 applies to it exactly as to a log line: the payload
    references the record to work on, it does not carry it.

    Returns the payload as a plain dict, so a caller cannot mutate what was
    validated.

    Raises:
        JobContractError: Naming the offending key and what to do instead. The
            fix is always the same shape — pass the id, let the handler load the
            row under its own tenant scope.
    """
    if len(payload) > _MAX_KEYS:
        raise JobContractError(
            f"Job payload has {len(payload)} keys, more than the {_MAX_KEYS} permitted. "
            "A payload that large is carrying a record rather than referencing one — "
            "pass the identifiers the handler needs and let it load the rest."
        )

    validated: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in _RESERVED_KEYS and not _KEY_NAME.match(key):
            raise JobContractError(
                f"Job payload key {key!r} is not a snake_case identifier. A key carrying "
                "punctuation or spaces is usually a value that has been moved into the "
                "key position, which defeats every check applied to values."
            )
        validated[key] = _validate_value(key, value)
    return validated


def _validate_value(key: str, value: Any) -> Any:
    """Check one payload value. Nested containers are refused, not walked."""
    if value is None or isinstance(value, bool | int):
        return value

    if isinstance(value, str):
        if len(value) > MAX_PAYLOAD_STRING_LENGTH:
            raise JobContractError(
                f"Job payload key {key!r} holds a {len(value)}-character string. Payload "
                f"values are identifiers and codes; anything over {MAX_PAYLOAD_STRING_LENGTH} "
                "characters is prose, and a job row is retained, backed up and read by "
                "operators (DB §13.1, NFR-033). Pass the id and let the handler read the "
                "text under tenant scope."
            )
        return value

    # 🔒 float is refused deliberately. A bare number in a job payload is almost
    # always a measurement — a weight, a dosage, a calorie total — which is the
    # exact class of value NFR-033 keeps out of retained rows. It is also the
    # one type where JSON round-tripping loses precision silently.
    if isinstance(value, float):
        raise JobContractError(
            f"Job payload key {key!r} holds a float. A bare number in a payload is "
            "almost always a measurement, which must not be stored in a job row "
            "(NFR-033) — and JSON float round-tripping loses precision silently. "
            "Pass the id of the record holding the value."
        )

    raise JobContractError(
        f"Job payload key {key!r} holds {type(value).__name__}, which a payload may not "
        "carry. Permitted: str, int, bool and None.\n\n"
        "🔒 A list or dict here is how a whole row — or a list of clinical readings — "
        "reaches a table that is retained and backed up (DB §13.1). Pass identifiers; "
        "the handler loads what it needs under its own tenant scope."
    )


# ─── Handler registry ─────────────────────────────────────────────────────


class JobHandler(Protocol):
    """Executes one job.

    Receives the validated payload and a session. 🔒 The session is
    worker-owned and already carries the job's tenant scope, so a handler
    queries exactly as it would while serving a request for that tenant and RLS
    applies to it identically. A handler that opened its own session would run
    unscoped and see nothing — or, worse, be written on the assumption that it
    sees everything.

    ⚠️ **Do not commit.** The runner owns the transaction and rolls it back on
    failure, which is what makes a handler that fails halfway leave nothing
    behind. A handler that commits mid-way defeats that.

    🔒 **Must be idempotent.** A lease can expire while a job is legitimately
    running — a paused VM, a long GC — and the recovery sweep will hand it to
    another worker (DB §13.3). Re-execution is expected, not exceptional.

    Raises to fail the attempt. The queue records the failure, applies backoff
    and retries up to the class ceiling, then dead-letters.
    """

    async def __call__(self, payload: Mapping[str, Any], session: AsyncSession, /) -> None: ...


@dataclass(frozen=True, slots=True)
class RegisteredJob:
    """A job type and the policy it runs under."""

    job_type: str
    job_class: JobClass
    handler: JobHandler

    @property
    def policy(self) -> JobClassPolicy:
        return policy_for(self.job_class)


_HANDLERS: dict[str, RegisteredJob] = {}


def register_handler(
    job_type: str,
    job_class: JobClass,
    handler: JobHandler,
) -> None:
    """Register a handler for a job type.

    Raises:
        JobContractError: On a duplicate registration with a different handler.
            Two handlers for one type means the queue's behaviour depends on
            import order, which is not something to discover from a support
            ticket.
    """
    if not _KEY_NAME.match(job_type):
        raise JobContractError(
            f"Job type {job_type!r} must be a snake_case identifier. It is stored in "
            "every job row and read by operators; punctuation makes it unqueryable."
        )

    existing = _HANDLERS.get(job_type)
    if existing is not None:
        if existing.handler is handler and existing.job_class is job_class:
            return  # Idempotent re-registration — import order, not a mistake.
        raise JobContractError(
            f"Job type {job_type!r} is already registered to a different handler. "
            "Which one runs would depend on import order."
        )

    _HANDLERS[job_type] = RegisteredJob(
        job_type=job_type,
        job_class=job_class,
        handler=handler,
    )


def get_handler(job_type: str) -> RegisteredJob:
    """Look up a registered job type.

    Raises:
        JobContractError: If the type is unknown. Raised at enqueue so the
            failure lands in the request that caused it; at claim time it means
            a row was enqueued by an older deploy whose handler has since been
            removed, and the job dead-letters rather than retrying forever.
    """
    registered = _HANDLERS.get(job_type)
    if registered is None:
        known = ", ".join(sorted(_HANDLERS)) or "(none registered)"
        raise JobContractError(
            f"No handler registered for job type {job_type!r}. Known types: {known}."
        )
    return registered


def registered_job_types() -> frozenset[str]:
    """Every registered job type — read by the startup completeness check."""
    return frozenset(_HANDLERS)


def verify_handlers_exist(job_types: frozenset[str] | set[str]) -> None:
    """🔒 Assert every job type reachable from an event has a handler.

    Called at startup with :func:`app.kernel.events.deferred_job_types`. A
    deferred subscriber naming a job type nothing can execute would enqueue rows
    that fail on every attempt and dead-letter — discovered in production, at
    the moment the work was actually needed.

    Raises:
        JobContractError: Naming the unhandled types. Startup must fail: a queue
            that cannot run its own work is not a degraded state worth serving.
    """
    missing = sorted(set(job_types) - set(_HANDLERS))
    if missing:
        raise JobContractError(
            f"Event subscribers enqueue job types with no registered handler: "
            f"{', '.join(missing)}.\n\n"
            "Every such job would fail on each attempt and dead-letter. Register a "
            "handler, or remove the subscription."
        )


def reset_handlers() -> None:
    """Clear the registry. 🔒 Tests only — it is process-global."""
    _HANDLERS.clear()
