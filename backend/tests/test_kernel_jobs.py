"""Job contracts must hold before a row reaches the queue — C3.

🔒 What this protects:

* **`generation` is never retried** (Arch §11.2). Each attempt costs money and
  the failures are usually deterministic, so a retry buys a second identical
  failure and a second charge. Asserted here *and* as a database check
  constraint, because the cost of getting it wrong is billed.
* **The lease always exceeds the timeout** (DB §13.3). A shorter lease means a
  job that is still legitimately running gets swept back to `pending` and
  executed a second time, concurrently with the first.
* **Payloads carry identifiers only** (DB §13.1, NFR-033). A job row is
  retained, backed up and read by operators — the same rules a log line follows.
* **Backoff is bounded.** Unbounded doubling schedules a failed reminder days
  out, which is indistinguishable from losing it.

⚠️ These are contract tests, not queue tests. That `SKIP LOCKED` prevents a
double claim and that a lease actually expires need a live PostgreSQL — C6.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from app.kernel.jobs import (
    JOB_POLICY,
    MAX_PAYLOAD_STRING_LENGTH,
    JobContractError,
    backoff_delay,
    get_handler,
    policy_for,
    register_handler,
    registered_job_types,
    reset_handlers,
    validate_payload,
    verify_handlers_exist,
)
from app.kernel.models import JobClass


@pytest.fixture(autouse=True)
def _clean_handlers() -> None:
    """The registry is process-global."""
    reset_handlers()


async def _noop(_payload: dict[str, Any]) -> None:
    pass


# ─── Class policy ────────────────────────────────────────────────────────


def test_every_job_class_has_a_policy() -> None:
    """🔒 A class without a policy would have no timeout and no retry ceiling.

    Parametrised over the enum rather than the table, so adding a `JobClass`
    member without deciding its cost fails here instead of at the first enqueue.
    """
    for job_class in JobClass:
        assert job_class in JOB_POLICY, (
            f"JobClass.{job_class.name} has no policy. Timeout and retry budget "
            "are decisions, not defaults."
        )


def test_generation_is_never_retried() -> None:
    """🔒 Arch §11.2 — the one that costs money if it regresses.

    Also enforced by `ck_jobs__generation_not_retried` in migration 0005, so a
    caller passing its own `max_attempts` is refused by the database. Two checks
    on one rule is proportionate when each extra attempt is a real charge.
    """
    assert policy_for(JobClass.GENERATION).max_attempts == 1


def test_lease_always_exceeds_timeout() -> None:
    """🔒 DB §13.3 — a lease shorter than the timeout causes double execution.

    The job is still running, its lease expires, the recovery sweep returns it
    to `pending`, and a second worker starts it while the first is mid-flight.
    Checked for every class because the relationship is derived, and a future
    edit making `lease` configurable would break it silently.
    """
    for job_class, policy in JOB_POLICY.items():
        assert policy.lease > policy.timeout, (
            f"{job_class.value}: lease ({policy.lease}) must exceed timeout "
            f"({policy.timeout}), or a running job is re-claimed while running."
        )


def test_dispatch_runs_before_maintenance() -> None:
    """Priority ordering: a reminder must not queue behind a retention purge.

    Lower is sooner, matching `jobs.priority` and the claim query's ORDER BY.
    """
    assert policy_for(JobClass.DISPATCH).priority < policy_for(JobClass.MAINTENANCE).priority
    assert policy_for(JobClass.DISPATCH).priority < policy_for(JobClass.RECURRING).priority


def test_unknown_job_class_is_refused() -> None:
    """Reaching for a policy that does not exist must fail loudly."""

    class FakeClass:
        value = "fabricated"

    with pytest.raises(JobContractError, match="No policy declared"):
        policy_for(FakeClass)  # type: ignore[arg-type]


# ─── Backoff ─────────────────────────────────────────────────────────────


def test_backoff_grows_exponentially() -> None:
    policy = policy_for(JobClass.DISPATCH)
    first = backoff_delay(policy, attempt_count=1)
    second = backoff_delay(policy, attempt_count=2)
    third = backoff_delay(policy, attempt_count=3)

    assert first == policy.backoff_base
    assert second == policy.backoff_base * 2
    assert third == policy.backoff_base * 4


def test_backoff_is_capped() -> None:
    """🔒 Unbounded doubling schedules a failed reminder days out.

    For a message the user is waiting on, "retried in four days" and "lost" are
    the same outcome.
    """
    policy = policy_for(JobClass.DISPATCH)
    assert backoff_delay(policy, attempt_count=50) == timedelta(hours=1)


def test_backoff_does_not_overflow() -> None:
    """⚠️ `base * 2**attempt_count` raises OverflowError past ~1000.

    The retry path runs inside the failure handler; an exception there would
    leave the job `claimed` with no lease renewal, i.e. stuck until the sweep.
    """
    policy = policy_for(JobClass.MAINTENANCE)
    assert backoff_delay(policy, attempt_count=100_000) == timedelta(hours=1)


def test_backoff_on_first_attempt_is_the_base() -> None:
    """attempt_count=0 should not produce a negative exponent."""
    policy = policy_for(JobClass.DISPATCH)
    assert backoff_delay(policy, attempt_count=0) == policy.backoff_base


# ─── Payload validation ──────────────────────────────────────────────────


def test_identifier_payload_is_accepted() -> None:
    payload = {"client_id": "5c1a...", "attempt": 2, "notify": True, "note_id": None}
    assert validate_payload(payload) == payload


def test_reserved_keys_are_permitted() -> None:
    """`kernel.events.to_payload` adds these; they are not caller input."""
    payload = {"_event": "client.stage_changed", "_request_id": "req_abc"}
    assert validate_payload(payload) == payload


def test_long_string_is_refused() -> None:
    """🔒 NFR-033 — prose in a payload is a clinical note in a retained row."""
    payload = {"note": "x" * (MAX_PAYLOAD_STRING_LENGTH + 1)}
    with pytest.raises(JobContractError, match="character string"):
        validate_payload(payload)


def test_nested_structures_are_refused() -> None:
    """🔒 DB §13.1 — a dict here is how a whole row reaches the queue."""
    with pytest.raises(JobContractError, match="may not\ncarry|may not carry"):
        validate_payload({"client": {"name": "A", "weight_kg": 78}})

    with pytest.raises(JobContractError, match="may not\ncarry|may not carry"):
        validate_payload({"readings": [78, 79, 80]})


def test_float_is_refused() -> None:
    """🔒 A bare number in a payload is almost always a measurement.

    Also the one type where JSON round-tripping loses precision silently, so
    even a legitimate float would not survive the trip intact.
    """
    with pytest.raises(JobContractError, match="holds a float"):
        validate_payload({"weight_kg": 78.4})


def test_non_identifier_key_is_refused() -> None:
    """A key carrying punctuation is usually a value moved into key position,
    which defeats every check applied to values."""
    with pytest.raises(JobContractError, match="not a snake_case identifier"):
        validate_payload({"weight (kg)": 78})


def test_oversized_payload_is_refused() -> None:
    """🔒 A payload with dozens of keys is carrying a record, not referencing one."""
    with pytest.raises(JobContractError, match="more than the"):
        validate_payload({f"key_{index}": index for index in range(100)})


def test_validate_returns_a_copy() -> None:
    """The caller must not be able to mutate what was validated."""
    original = {"client_id": "abc"}
    validated = validate_payload(original)
    validated["injected"] = "value"
    assert "injected" not in original


# ─── Handler registry ────────────────────────────────────────────────────


def test_register_and_look_up_a_handler() -> None:
    register_handler("send_message", JobClass.DISPATCH, _noop)
    registered = get_handler("send_message")

    assert registered.job_type == "send_message"
    assert registered.job_class is JobClass.DISPATCH
    assert registered.policy.max_attempts == 3


def test_reregistering_the_same_handler_is_idempotent() -> None:
    """Import order can run a registration block twice."""
    register_handler("send_message", JobClass.DISPATCH, _noop)
    register_handler("send_message", JobClass.DISPATCH, _noop)
    assert registered_job_types() == {"send_message"}


def test_conflicting_registration_is_refused() -> None:
    """🔒 Two handlers for one type means behaviour depends on import order."""

    async def other(_payload: dict[str, Any]) -> None:
        pass

    register_handler("send_message", JobClass.DISPATCH, _noop)
    with pytest.raises(JobContractError, match="already registered to a different handler"):
        register_handler("send_message", JobClass.DISPATCH, other)


def test_job_type_must_be_an_identifier() -> None:
    """It is stored in every job row and queried by operators."""
    with pytest.raises(JobContractError, match="snake_case identifier"):
        register_handler("send message!", JobClass.DISPATCH, _noop)


def test_unknown_job_type_names_what_is_registered() -> None:
    """The error is read by someone who just misspelled a job type."""
    register_handler("send_message", JobClass.DISPATCH, _noop)
    with pytest.raises(JobContractError, match="send_message"):
        get_handler("send_mesage")


# ─── Startup completeness ────────────────────────────────────────────────


def test_verify_handlers_exist_passes_when_complete() -> None:
    register_handler("send_message", JobClass.DISPATCH, _noop)
    verify_handlers_exist({"send_message"})


def test_verify_handlers_exist_names_the_gap() -> None:
    """🔒 A deferred subscriber naming an unhandled job type would enqueue rows
    that fail on every attempt and dead-letter — found in production, at the
    moment the work was actually needed."""
    register_handler("send_message", JobClass.DISPATCH, _noop)

    with pytest.raises(JobContractError, match="notify_practitioner"):
        verify_handlers_exist({"send_message", "notify_practitioner"})


def test_verify_handlers_exist_accepts_an_empty_set() -> None:
    """No deferred subscribers is a valid state, not an error."""
    verify_handlers_exist(set())
