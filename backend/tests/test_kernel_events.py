"""The event bus must hold its contracts — C1.

🔒 What this protects:

1. **No double execution.** A handler appended twice by a re-run subscription
   block would fire twice per publish — for "recount entitlements" or "write a
   timeline entry" that is a data bug, not a performance one. Note the Arch §3.4
   rule is per *handler*: one event legitimately has both a transactional and a
   deferred subscriber, and `test_one_event_may_have_both_kinds_of_subscriber`
   pins that down so a future "tidy-up" does not forbid it.
2. **NFR-033 enforced at the boundary.** A long string, a collection or a nested
   object in an event is how a clinical value reaches a job payload and a log
   line. Checked in `to_payload`, the single place a payload is built.
3. **The enqueue seam fails loudly.** No queue wired means an exception, not a
   silently dropped job — the difference between a visible startup bug and a
   plan that is approved and never delivered.

⚠️ These are the event side only. That the queue actually consumes what
`to_payload` produces is asserted in the C6 integration suite, against a real
PostgreSQL — no fake here can prove it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

import pytest

from app.kernel.context import RequestContext, context_scope
from app.kernel.events import (
    DomainEvent,
    EventContractError,
    QueueNotConfiguredError,
    configure_deferred_enqueuer,
    deferred_job_types,
    publish,
    register_event,
    registered_events,
    reset_subscriptions,
    subscribe,
    to_payload,
)


class ClientStage(StrEnum):
    ENQUIRY = "enquiry"
    ACTIVE = "active"


@register_event("example.stage_changed")
@dataclass(frozen=True, slots=True)
class StageChanged(DomainEvent):
    """A client's stage was updated — the canonical event for timeline tests."""

    client_id: uuid.UUID
    from_stage: ClientStage
    to_stage: ClientStage
    changed_by: uuid.UUID
    occurred_at: datetime


@register_event("example.created")
@dataclass(frozen=True, slots=True)
class ClientCreated(DomainEvent):
    client_id: uuid.UUID


@pytest.fixture(autouse=True)
def _clean_subscriptions() -> Iterator[None]:
    """Clear handlers before *and* after each test. 🔒 The registry is process-global.

    ⚠️ The teardown half is not symmetry for its own sake. Any test here that
    leaves a `deferred_job_type` subscribed leaks it into every later test in the
    session — and `create_app()` calls `verify_handlers_exist(deferred_job_types())`
    at startup, so the next test that builds an app fails with an error naming a
    job type it has never heard of. Cleaning only on entry made this suite's
    subscriptions a global side effect.
    """
    reset_subscriptions()
    yield
    reset_subscriptions()


@pytest.fixture
def session() -> object:
    """A fake session — handlers never inspect it.

    The real session comes from the HTTP pipeline's transaction, which carries
    the tenant scope for RLS and is committed after the response. For these
    contract tests the handlers only record that they were called; the argument
    exists so the signatures match.
    """
    return object()


# ─── Registration ─────────────────────────────────────────────────────────


def test_event_name_must_be_qualified() -> None:
    """🔒 Unqualified names collide across modules."""
    with pytest.raises(EventContractError, match="'created' must be '<resource>.<verb>'"):

        @register_event("created")
        @dataclass(frozen=True, slots=True)
        class UnqualifiedEvent(DomainEvent):
            pass


def test_duplicate_name_refused() -> None:
    """🔒 Two events sharing a name are indistinguishable in a job payload."""

    @register_event("duplicate.event")
    @dataclass(frozen=True, slots=True)
    class First(DomainEvent):
        pass

    with pytest.raises(EventContractError, match="already registered"):

        @register_event("duplicate.event")
        @dataclass(frozen=True, slots=True)
        class Second(DomainEvent):
            pass


def test_forgotten_dataclass_decorator_is_caught() -> None:
    """🔒 The silent-failure mode: annotations that never become fields.

    Without `@dataclass`, `client_id` below is an annotation and nothing more.
    `to_payload` would emit an event carrying none of its data, every subscriber
    would receive a hollow object, and nothing would raise — the notification is
    simply never sent, discovered days later.

    ⚠️ There is deliberately no test for `frozen=False` or for a non-dataclass
    base: `DomainEvent` is frozen, so Python raises `TypeError: cannot inherit
    non-frozen dataclass from a frozen one` at class creation, and
    `is_dataclass()` is true for any subclass of a dataclass whether decorated or
    not. Both guarantees hold one layer below this suite.
    """
    with pytest.raises(EventContractError, match="not decorated with @dataclass"):

        @register_event("forgotten.decorator")
        class ForgottenDecorator(DomainEvent):
            client_id: uuid.UUID


def test_registered_events_visible_for_startup_checks() -> None:
    """🔒 The entry point asserts every deferred job type has a handler."""
    events = registered_events()
    assert "example.stage_changed" in events
    assert events["example.stage_changed"] is StageChanged


# ─── Subscription ─────────────────────────────────────────────────────────


def test_subscribe_requires_exactly_one_disposition() -> None:
    """🔒 Both means double-execution; neither is a programming error."""
    with pytest.raises(EventContractError, match="exactly one"):
        subscribe(StageChanged)

    async def handler(_event: DomainEvent) -> None:
        pass

    with pytest.raises(EventContractError, match="exactly one"):
        subscribe(StageChanged, transactional=handler, deferred_job_type="job")


def test_reregistering_same_handler_does_not_double_execute() -> None:
    """🔒 The real double-execution guard.

    Import order can legitimately run a subscription block twice. If that
    appended the handler again, every publish would run it twice — which for a
    "recount entitlements" or "write a timeline entry" handler is a data bug, not
    a performance one.
    """

    async def handler(_event: DomainEvent) -> None:
        pass

    subscribe(StageChanged, transactional=handler)
    subscribe(StageChanged, transactional=handler)

    from app.kernel.events import _TRANSACTIONAL

    assert len(_TRANSACTIONAL[StageChanged]) == 1


def test_same_deferred_job_type_registered_once() -> None:
    """The deferred equivalent: one job per event, not one per import."""
    subscribe(StageChanged, deferred_job_type="notify")
    subscribe(StageChanged, deferred_job_type="notify")

    from app.kernel.events import _DEFERRED

    assert _DEFERRED[StageChanged] == ["notify"]


def test_one_event_may_have_both_kinds_of_subscriber() -> None:
    """Arch §3.4's own example — `PlanApproved` is transactional *and* deferred.

    A timeline entry must commit with the approval (DDR-06); the delivery
    notification must survive a deploy and be retried. Different handlers,
    different dispositions, one event.
    """

    async def timeline_handler(_event: DomainEvent) -> None:
        pass

    subscribe(StageChanged, transactional=timeline_handler)
    subscribe(StageChanged, deferred_job_type="send_notification")

    from app.kernel.events import _DEFERRED, _TRANSACTIONAL

    assert len(_TRANSACTIONAL[StageChanged]) == 1
    assert _DEFERRED[StageChanged] == ["send_notification"]


def test_deferred_job_types_collected_for_startup_check() -> None:
    """🔒 Every job type reachable from events must have a registered handler."""
    subscribe(StageChanged, deferred_job_type="notify_stage_change")
    subscribe(ClientCreated, deferred_job_type="send_welcome")
    jobs = deferred_job_types()
    assert "notify_stage_change" in jobs
    assert "send_welcome" in jobs


# ─── Payload encoding ─────────────────────────────────────────────────────


def test_to_payload_encodes_identifiers_and_enums() -> None:
    """The happy path: IDs, enums, timestamps."""
    with context_scope(RequestContext.for_worker("test")):
        event = StageChanged(
            client_id=uuid.uuid4(),
            from_stage=ClientStage.ENQUIRY,
            to_stage=ClientStage.ACTIVE,
            changed_by=uuid.uuid4(),
            occurred_at=datetime(2026, 8, 6, 10, 30, 0),
        )
        payload = to_payload(event)

    assert payload["_event"] == "example.stage_changed"
    assert "_request_id" in payload
    assert uuid.UUID(payload["client_id"]) == event.client_id
    assert payload["from_stage"] == "enquiry"
    assert payload["to_stage"] == "active"
    assert payload["occurred_at"] == "2026-08-06T10:30:00"


def test_to_payload_refuses_long_strings() -> None:
    """🔒 NFR-033: a 200-character string is prose, not a code."""

    @register_event("leak.long_text")
    @dataclass(frozen=True, slots=True)
    class LongText(DomainEvent):
        note: str

    event = LongText(note="x" * 200)
    with pytest.raises(EventContractError, match="200 characters long"):
        to_payload(event)


def test_to_payload_refuses_collections() -> None:
    """🔒 DB §13.1: a list in an event is a list of measurements in a payload."""

    @register_event("leak.collection")
    @dataclass(frozen=True, slots=True)
    class CollectionEvent(DomainEvent):
        values: list[int]

    event = CollectionEvent(values=[1, 2, 3])
    with pytest.raises(EventContractError, match="list.*may not carry"):
        to_payload(event)


def test_to_payload_refuses_nested_objects() -> None:
    """🔒 A nested object in an event is a whole clinical record in a payload."""

    @dataclass(frozen=True)
    class Detail:
        field: str

    @register_event("leak.nested")
    @dataclass(frozen=True, slots=True)
    class NestedEvent(DomainEvent):
        detail: Detail

    event = NestedEvent(detail=Detail(field="value"))
    with pytest.raises(EventContractError, match="Detail.*may not carry"):
        to_payload(event)


# ─── Publishing ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transactional_handlers_run_inline(session: object) -> None:
    """Transactional handlers execute immediately, in registration order."""
    calls: list[str] = []

    async def first(_event: DomainEvent, _session: object) -> None:
        calls.append("first")

    async def second(_event: DomainEvent, _session: object) -> None:
        calls.append("second")

    subscribe(StageChanged, transactional=first)
    subscribe(StageChanged, transactional=second)

    event = StageChanged(
        client_id=uuid.uuid4(),
        from_stage=ClientStage.ENQUIRY,
        to_stage=ClientStage.ACTIVE,
        changed_by=uuid.uuid4(),
        occurred_at=datetime.now(),
    )
    await publish(event, session)

    assert calls == ["first", "second"]


@pytest.mark.asyncio
async def test_transactional_handler_raise_propagates(session: object) -> None:
    """A transactional handler's exception rolls back the publisher's transaction."""

    async def failing(_event: DomainEvent, _session: object) -> None:
        raise ValueError("handler failed")

    subscribe(StageChanged, transactional=failing)

    event = StageChanged(
        client_id=uuid.uuid4(),
        from_stage=ClientStage.ENQUIRY,
        to_stage=ClientStage.ACTIVE,
        changed_by=uuid.uuid4(),
        occurred_at=datetime.now(),
    )

    with pytest.raises(ValueError, match="handler failed"):
        await publish(event, session)


@pytest.mark.asyncio
async def test_deferred_handlers_enqueue_via_configured_seam(session: object) -> None:
    """🔒 Deferred subscribers call the configured enqueuer, which writes a job."""
    enqueued: list[tuple[str, str, dict[str, Any]]] = []

    async def fake_enqueuer(
        _session: object, job_type: str, event_name: str, payload: dict[str, Any]
    ) -> None:
        enqueued.append((job_type, event_name, payload))

    configure_deferred_enqueuer(fake_enqueuer)

    subscribe(StageChanged, deferred_job_type="notify_stage_change")

    with context_scope(RequestContext.for_worker("test")):
        event = StageChanged(
            client_id=uuid.uuid4(),
            from_stage=ClientStage.ENQUIRY,
            to_stage=ClientStage.ACTIVE,
            changed_by=uuid.uuid4(),
            occurred_at=datetime.now(),
        )
        await publish(event, session)

    assert len(enqueued) == 1
    job_type, event_name, payload = enqueued[0]
    assert job_type == "notify_stage_change"
    assert event_name == "example.stage_changed"
    assert payload["_event"] == "example.stage_changed"
    assert uuid.UUID(payload["client_id"]) == event.client_id


@pytest.mark.asyncio
async def test_unconfigured_enqueuer_fails_loudly(session: object) -> None:
    """🔒 A deferred subscriber with no queue wired is a bug, not a skip."""
    reset_subscriptions()
    subscribe(StageChanged, deferred_job_type="job")

    # Re-install the default unconfigured enqueuer.
    from app.kernel.events import _unconfigured_enqueuer

    configure_deferred_enqueuer(_unconfigured_enqueuer)

    event = StageChanged(
        client_id=uuid.uuid4(),
        from_stage=ClientStage.ENQUIRY,
        to_stage=ClientStage.ACTIVE,
        changed_by=uuid.uuid4(),
        occurred_at=datetime.now(),
    )

    with pytest.raises(QueueNotConfiguredError, match="no job queue is configured"):
        await publish(event, session)


@pytest.mark.asyncio
async def test_publish_unregistered_event_refused(session: object) -> None:
    """An unregistered event has no wire name and cannot be queued."""

    @dataclass(frozen=True, slots=True)
    class UnregisteredEvent(DomainEvent):
        field: str

    event = UnregisteredEvent(field="value")
    with pytest.raises(EventContractError, match="published without a wire name"):
        await publish(event, session)


@pytest.mark.asyncio
async def test_both_dispositions_fire_for_same_event(session: object) -> None:
    """Transactional and deferred subscribers for one event both execute."""
    inline_fired = False
    enqueued: list[str] = []

    async def inline_handler(_event: DomainEvent, _session: object) -> None:
        nonlocal inline_fired
        inline_fired = True

    async def fake_enqueuer(
        _session: object, job_type: str, _event_name: str, _payload: dict[str, Any]
    ) -> None:
        enqueued.append(job_type)

    configure_deferred_enqueuer(fake_enqueuer)

    subscribe(StageChanged, transactional=inline_handler)
    subscribe(StageChanged, deferred_job_type="deferred_job")

    event = StageChanged(
        client_id=uuid.uuid4(),
        from_stage=ClientStage.ENQUIRY,
        to_stage=ClientStage.ACTIVE,
        changed_by=uuid.uuid4(),
        occurred_at=datetime.now(),
    )
    await publish(event, session)

    assert inline_fired
    assert "deferred_job" in enqueued
