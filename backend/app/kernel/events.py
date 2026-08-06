"""In-process event bus — Arch §3.4.

🔒 **The mechanism that makes R3 possible.** Modules must not import one another
at all; cross-module needs go through here. The publisher names an event, not a
subscriber, so adding a consumer requires no change to the producer — which is
what AC-M8-008 asserts.

Two dispositions, and the choice between them is a correctness decision rather
than a performance one:

* **Transactional** — runs inside the publisher's transaction. For work that
  must be consistent with the change that triggered it: a stage transition and
  its timeline entry either both commit or neither does (DDR-06).
* **Deferred** — enqueued as a job. For work that may fail on its own without
  invalidating the original action: message dispatch, AI generation, PDF
  rendering. Retried, leased and dead-lettered by the queue (Arch §11.4).

⚠️ **A given handler is one or the other, never both** (Arch §3.4). A handler
that ran inline *and* from the queue would execute twice per publish, and no use
case wants that. Note this is a rule about handlers, not events: one event
routinely has subscribers of both kinds — §3.4's own example has `PlanApproved`
writing a timeline entry transactionally (DDR-06) while `messaging` sends the
delivery notification from a job.

🔒 **Events carry identifiers only** (NFR-033). An event reaches every
subscriber, is serialised into a job payload, and is summarised into log lines;
each of those has a different retention policy from the record the value came
from. :func:`publish` validates this rather than trusting it.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields
from datetime import date, datetime
from enum import Enum
from typing import Any, ClassVar, Protocol, runtime_checkable

from app.kernel.context import get_context

# ─── Events ───────────────────────────────────────────────────────────────

#: Scalar types an event field may hold. 🔒 Deliberately narrow: a `str` is
#: permitted because codes and slugs are strings, but see `_TEXT_FIELD_LIMIT` —
#: a long string is prose, and prose in an event is a clinical note in a job
#: payload. `float`/`Decimal` are absent because a bare number in an event is
#: almost always a measurement, which is the exact class of value NFR-033 keeps
#: out of logs and payloads.
_ALLOWED_SCALARS: tuple[type, ...] = (str, int, bool, uuid.UUID, datetime, date, Enum)

#: 🔒 Longest permitted string in an event field. An identifier, code, slug or
#: enum value fits comfortably; a note, message body or AI-generated summary does
#: not. Chosen to be obviously too short for prose, so the failure arrives in
#: development rather than as a leak in production.
_TEXT_FIELD_LIMIT = 128


class EventContractError(RuntimeError):
    """An event violated the contract that makes it safe to publish.

    🔒 Raised for programmer error — a value where an identifier belongs, a
    mutable event, an unregistered name. Loud rather than best-effort: an event
    bus that silently drops malformed events is one where a missing timeline
    entry has no explanation.
    """


class QueueNotConfiguredError(RuntimeError):
    """A deferred subscriber fired with no job queue installed.

    🔒 A deployment error, not a request error, so it is a plain ``RuntimeError``
    rather than an :class:`~app.kernel.errors.AppError` — there is no status code
    that would make it a sensible thing to tell an API caller. It surfaces at the
    first publish, which the startup check in
    :func:`~app.kernel.events.deferred_job_types` is meant to pre-empt.
    """


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """Base for every domain event.

    🔒 Frozen. An event is a statement about something that already happened;
    the past does not get edited, so neither does the object. Frozen also means a
    transactional handler cannot mutate what the next handler receives, which
    would make handler order load-bearing and the bus untestable.

    Subclasses declare their own fields. Keep them to identifiers, enums and
    timestamps — :func:`publish` enforces that, and the enforcement is the reason
    a job payload built from an event is safe by construction.
    """

    #: Stable wire name, used as the job payload's discriminator. Set by
    #: :func:`register_event`. A class name would work until the first rename,
    #: at which point every queued job referencing it becomes undeliverable.
    event_name: ClassVar[str] = ""


@runtime_checkable
class _Publishable(Protocol):
    event_name: ClassVar[str]


# ─── Registry ─────────────────────────────────────────────────────────────

#: Wire name → event class. Populated by :func:`register_event`.
_EVENTS: dict[str, type[DomainEvent]] = {}

#: Event class → handlers running inside the publisher's transaction.
_TRANSACTIONAL: dict[type[DomainEvent], list[TransactionalHandler]] = {}

#: Event class → job types enqueued when the event fires.
_DEFERRED: dict[type[DomainEvent], list[str]] = {}


class TransactionalHandler(Protocol):
    """A handler running inside the publisher's transaction.

    May raise: the exception propagates to the publisher and rolls the
    transaction back, which is the entire point of choosing this disposition.

    The event parameter is positional-only (``/``) so a handler may name it
    whatever reads best — ``event``, ``_event`` when unused, or the concrete
    event type. Without that, mypy requires every handler to use the same
    parameter name as this protocol, which is friction for no benefit.
    """

    async def __call__(self, event: Any, /) -> None: ...


def register_event(name: str) -> Callable[[type[DomainEvent]], type[DomainEvent]]:
    """Class decorator giving an event its stable wire name.

    The name is what a queued job stores, so it must outlive class renames::

        @register_event("client.stage_changed")
        @dataclass(frozen=True, slots=True)
        class StageChanged(DomainEvent):
            client_id: uuid.UUID

    Raises:
        EventContractError: On a duplicate name, or a name that is not
            ``resource.verb``. Grouping by resource is what keeps the registry
            readable once there are forty of them, and an unqualified verb
            collides across modules — the same rule authz actions follow.
    """

    def decorate(event_type: type[DomainEvent]) -> type[DomainEvent]:
        if "." not in name:
            raise EventContractError(
                f"Event name {name!r} must be '<resource>.<verb>' — the registry is "
                "grouped by resource, and an unqualified verb collides across modules."
            )
        existing = _EVENTS.get(name)
        if existing is not None and existing is not event_type:
            raise EventContractError(
                f"Event name {name!r} is already registered to {existing.__name__}. "
                "Two events sharing a name would be indistinguishable in a job payload."
            )
        _validate_event_shape(event_type)
        event_type.event_name = name
        _EVENTS[name] = event_type
        return event_type

    return decorate


def _validate_event_shape(event_type: type[DomainEvent]) -> None:
    """🔒 Reject an event class whose fields would not reach the payload.

    Checked once at registration — i.e. at import time — so a violation fails the
    test suite immediately and costs nothing per publish.

    ⚠️ Two properties that look checkable here are not, and both are already
    guaranteed one layer down:

    * **Frozen.** :class:`DomainEvent` is frozen and Python refuses to derive a
      non-frozen dataclass from a frozen one, raising ``TypeError`` before this
      function runs.
    * **Is a dataclass.** ``is_dataclass()`` is true for any subclass of a
      dataclass, decorated or not, because ``__dataclass_fields__`` is inherited.

    What *is* checkable, and is a genuine silent-failure mode: a class that
    declares annotations but forgets ``@dataclass``. Its annotations never become
    fields, so :func:`to_payload` emits an event with none of its data and every
    subscriber receives a hollow object. Nothing raises — the message is simply
    empty, which surfaces as a missing notification days later.
    """
    declared = set(getattr(event_type, "__annotations__", {}))
    captured = {field_def.name for field_def in fields(event_type)}
    missing = sorted(declared - captured - {"event_name"})
    if missing:
        raise EventContractError(
            f"{event_type.__name__} declares {', '.join(missing)} but is not decorated "
            "with @dataclass, so those annotations never become fields. The event would "
            "publish with an empty payload and every subscriber would receive nothing — "
            "silently. Add:\n\n"
            "    @register_event('<resource>.<verb>')\n"
            "    @dataclass(frozen=True, slots=True)\n"
            f"    class {event_type.__name__}(DomainEvent):"
        )


def subscribe(
    event_type: type[DomainEvent],
    *,
    transactional: TransactionalHandler | None = None,
    deferred_job_type: str | None = None,
) -> None:
    """Register a subscriber for an event.

    Exactly one disposition per call:

    * ``transactional`` — an async callable run inside the publisher's
      transaction. Choose when the handler's write must commit with the
      publisher's, and when its failure *should* undo the original action.
    * ``deferred_job_type`` — the job type to enqueue. Choose when the work can
      fail and be retried without invalidating what triggered it.

    Raises:
        EventContractError: If neither or both arguments are given, or if the
            event is not registered.

    ⚠️ Registering the *same* handler twice for one event is a no-op rather than
    an error — see the comment in the body. Registering both kinds for one event
    is legitimate and expected (Arch §3.4).
    """
    if (transactional is None) == (deferred_job_type is None):
        raise EventContractError(
            "subscribe() takes exactly one of `transactional=` or `deferred_job_type=`. "
            "A handler registered both ways runs twice — once inline and once from the "
            "queue — which Arch §3.4 forbids and no caller wants."
        )

    if event_type not in _EVENTS.values():
        raise EventContractError(
            f"{event_type.__name__} is not registered. Decorate it with "
            "@register_event('<resource>.<verb>') — an unregistered event cannot be "
            "named in a job payload, so a deferred subscriber could never fire."
        )

    # 🔒 Arch §3.4's "never both" is per *handler*, not per event. One event
    # legitimately has subscribers of both kinds — the §3.4 example has
    # `PlanApproved` writing a timeline entry transactionally (DDR-06) while
    # `messaging` sends the delivery notification from a job. What must never
    # happen is one *handler* running twice for a single publish, which is what
    # the identity dedupe below prevents.
    if transactional is not None:
        handlers = _TRANSACTIONAL.setdefault(event_type, [])
        identity = _handler_identity(transactional)
        if any(_handler_identity(existing) == identity for existing in handlers):
            # ⚠️ Re-registration is silently ignored rather than raising: module
            # import order can legitimately run a subscription block twice (a
            # reload, a test importing a module the app already imported), and
            # appending would double-execute the handler on every publish — the
            # exact failure this whole section exists to prevent.
            return
        handlers.append(transactional)
        return

    assert deferred_job_type is not None  # narrowed by the check above
    job_types = _DEFERRED.setdefault(event_type, [])
    if deferred_job_type not in job_types:
        job_types.append(deferred_job_type)


def _handler_identity(handler: TransactionalHandler) -> str:
    """A stable name for a handler, used to dedupe re-registration.

    ``module.qualname`` rather than ``id()``: a decorated or re-imported handler
    is a different object with the same identity, and it is the *logical*
    handler that must not run twice.
    """
    module = getattr(handler, "__module__", "?")
    qualname = getattr(handler, "__qualname__", None)
    return f"{module}.{qualname}" if qualname else repr(handler)


# ─── The enqueue seam ─────────────────────────────────────────────────────
#
# 🔒 Arch §3.1 R5 — the kernel does not import platform/. The queue lives in
# platform/ because it owns the engine and the session; the kernel names the
# capability it needs and the entry point installs the implementation. Same
# shape as the pipeline's actor-resolver and transaction-provider seams.

#: Enqueues a deferred job. Takes the job type, the event's wire name and its
#: payload; returns nothing. Raises to fail the publisher's transaction — a
#: deferred handler that silently fails to enqueue is a message never sent.
DeferredEnqueuer = Callable[[str, str, dict[str, Any]], Awaitable[None]]


async def _unconfigured_enqueuer(job_type: str, event_name: str, _payload: dict[str, Any]) -> None:
    """🔒 The default: refuse, loudly.

    Not a no-op. A silently-dropped enqueue means a plan is approved and never
    delivered, with nothing in any log to say why. If a deferred subscriber
    exists and no queue is wired, that is a wiring bug at startup, and it should
    look like one.
    """
    raise QueueNotConfiguredError(
        f"Event {event_name!r} has a deferred subscriber for job type {job_type!r}, "
        "but no job queue is configured. Call `configure_deferred_enqueuer()` during "
        "startup. Dropping the job silently would mean the work never happens and "
        "nothing records that it did not."
    )


_enqueuer: DeferredEnqueuer = _unconfigured_enqueuer


def configure_deferred_enqueuer(enqueuer: DeferredEnqueuer) -> None:
    """Install the queue-backed enqueuer. Called by the entry points."""
    global _enqueuer
    _enqueuer = enqueuer


# ─── Publishing ───────────────────────────────────────────────────────────


async def publish(event: DomainEvent) -> None:
    """Publish an event to its subscribers.

    Transactional handlers run first, in registration order, inside the caller's
    transaction. A raise propagates and rolls that transaction back.

    Deferred handlers are then enqueued. The enqueue writes a row in the *same*
    transaction, so the job exists only if the publisher commits (Arch §11.1) —
    an approved plan cannot fail to schedule its delivery, and a rolled-back
    approval cannot leave a ghost job. That is the property an external broker
    would need an outbox pattern to reproduce.

    Raises:
        EventContractError: If the event is unregistered, or carries a field
            that is not an identifier (NFR-033).
    """
    event_type = type(event)
    name = getattr(event_type, "event_name", "")
    if not name:
        raise EventContractError(
            f"{event_type.__name__} was published without a wire name. Decorate it "
            "with @register_event('<resource>.<verb>')."
        )

    payload = to_payload(event)

    for handler in tuple(_TRANSACTIONAL.get(event_type, ())):
        await handler(event)

    for job_type in tuple(_DEFERRED.get(event_type, ())):
        await _enqueuer(job_type, name, payload)


def to_payload(event: DomainEvent) -> dict[str, Any]:
    """Serialise an event to a JSON-safe job payload.

    🔒 **Where NFR-033 is enforced for the queue.** DB §13.1 requires job
    payloads to hold IDs only; this is the single function that builds one from
    an event, so the rule is checked in one place rather than trusted at every
    call site.

    The request id rides along so a job's logs correlate with the request that
    caused it — the one thing that makes an asynchronous failure traceable back
    to a user's action.

    Raises:
        EventContractError: On a field whose type or length says it is a value
            rather than an identifier.
    """
    payload: dict[str, Any] = {}
    for field_def in fields(event):
        value = getattr(event, field_def.name)
        payload[field_def.name] = _encode(event, field_def.name, value)

    payload["_event"] = type(event).event_name
    payload["_request_id"] = get_context().request_id
    return payload


def _encode(event: DomainEvent, field_name: str, value: Any) -> Any:
    """Convert one event field to a JSON-safe scalar, or refuse it."""
    if value is None:
        return None

    if isinstance(value, bool | int):
        return value

    if isinstance(value, Enum):
        return value.value

    if isinstance(value, uuid.UUID):
        return str(value)

    if isinstance(value, datetime | date):
        return value.isoformat()

    if isinstance(value, str):
        if len(value) > _TEXT_FIELD_LIMIT:
            raise EventContractError(
                f"{type(event).__name__}.{field_name} is {len(value)} characters long. "
                f"Event fields are identifiers, codes and enum values — anything over "
                f"{_TEXT_FIELD_LIMIT} characters is prose, and prose in an event ends up "
                "in a job payload and a log line, which have different retention rules "
                "from the record it came from (NFR-033). Publish the id and let the "
                "handler read the text."
            )
        return value

    raise EventContractError(
        f"{type(event).__name__}.{field_name} has type {type(value).__name__}, which an "
        f"event may not carry. Permitted: {', '.join(t.__name__ for t in _ALLOWED_SCALARS)} "
        "and None.\n\n"
        "🔒 A collection or a nested object in an event is how a measurement, a note or "
        "a whole clinical record reaches a job payload (NFR-033, DB §13.1). Publish the "
        "identifier; the handler loads what it needs under its own tenant scope."
    )


# ─── Test and startup support ─────────────────────────────────────────────


def registered_events() -> dict[str, type[DomainEvent]]:
    """The event registry, for startup checks and diagnostics."""
    return dict(_EVENTS)


def deferred_job_types() -> set[str]:
    """Every job type reachable from a deferred subscription.

    🔒 Read at startup to assert each has a registered handler. A deferred
    subscriber naming a job type nothing can execute would enqueue rows that
    fail forever and dead-letter — discovered in production, at the worst moment.
    """
    return {job_type for handlers in _DEFERRED.values() for job_type in handlers}


def reset_subscriptions() -> None:
    """Clear every subscription. 🔒 Tests only.

    The registry is process-global, so a test that subscribes without clearing up
    leaks a handler into every later test in the session. Event *registrations*
    survive: those come from import-time decorators and re-registering them would
    fail the duplicate-name check.
    """
    _TRANSACTIONAL.clear()
    _DEFERRED.clear()
