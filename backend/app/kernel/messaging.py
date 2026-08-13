"""Messaging Kernel — the rules every message passes through, as pure functions.

🔒 **One engine, one dispatch path (FR-M8-001).** No module schedules or sends its
own messages; they create ``scheduled_messages`` rows and this decides what
happens to them. The rules live here, without a database or a session, because
they are the part that must be provably right: a suppression rule that fires in
one caller and not another is indistinguishable from no rule at all.

🔒 **Suppression is evaluated at dispatch, never at scheduling** (DB §11.5). A
client's stage, consent or tenant status can change between the two. A check-in
scheduled on Monday for Friday must be suppressed if the client is paused on
Wednesday — checking only at scheduling would send it, which is exactly the
failure that erodes practitioner trust.

🔒 **Quiet hours defer, they never drop** (AC-M8-006). A message due at 02:00
moves to the start of the next allowed window and keeps its ``deferred_from``
audit trail. Dropping it would lose a plan a practitioner had already approved.

⚠️ Nothing here talks to a provider. The WhatsApp/email adapters live behind a
port in ``integrations/``; this module cannot tell which transport will carry a
message, and must not learn.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Final
from uuid import UUID


class MessageCategory(enum.StrEnum):
    """DB §11.1 — what kind of message this is, for reporting and policy."""

    TRANSACTIONAL = "transactional"
    REMINDER = "reminder"
    NUDGE = "nudge"
    NOTIFICATION = "notification"


class TransportType(enum.StrEnum):
    """🔒 SMS is deliberately absent at MVP — approved proposal #7 keeps TRAI DLT
    off the critical path. ``LOGGED`` is the no-op transport the engine runs on
    before a provider is verified, and is what makes S5 shippable without Meta.
    """

    WHATSAPP = "whatsapp"
    EMAIL = "email"
    LOGGED = "logged"


class ProviderTemplateStatus(enum.StrEnum):
    """🔒 EC-M8-03 — per template, so one revoked WhatsApp template pauses only
    its own message type rather than looking like a total outage."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAUSED = "paused"


class ScheduledState(enum.StrEnum):
    """DB §11.2. 🔒 ``suppressed`` and ``expired`` are retained, never deleted
    (AC-M8-004) — the reason a message did not arrive is the answer to the
    support question that follows."""

    PENDING = "pending"
    DISPATCHED = "dispatched"
    SUPPRESSED = "suppressed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class SuppressionReason(enum.StrEnum):
    """🔒 DB §11.5 — the seven reasons a due message is not sent.

    ⚠️ The spec calls these "the six rules" and then lists seven values;
    ``client_unsubscribed`` and ``consent_withdrawn`` are distinct facts —
    withdrawing DPDP consent is not the same act as muting one message type, and
    conflating them would either over-suppress or lose a lawful-basis record.
    """

    CLIENT_STAGE_INACTIVE = "client_stage_inactive"
    CONSENT_WITHDRAWN = "consent_withdrawn"
    TENANT_SUSPENDED = "tenant_suspended"
    QUOTA_EXCEEDED = "quota_exceeded"
    FREQUENCY_CAPPED = "frequency_capped"
    TEMPLATE_PAUSED = "template_paused"
    CLIENT_UNSUBSCRIBED = "client_unsubscribed"


class DispatchStatus(enum.StrEnum):
    """DB §11.3 — the provider-reported lifecycle of one attempt."""

    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"
    REJECTED = "rejected"


class CheckinFrequency(enum.StrEnum):
    """DB §11.7 — FR-M8-022."""

    WEEKLY = "weekly"
    FORTNIGHTLY = "fortnightly"
    MONTHLY = "monthly"


#: 🟡 FR-M8-009 — the default window a client may be messaged in, local time.
DEFAULT_QUIET_HOURS_START: Final[time] = time(21, 0)
DEFAULT_QUIET_HOURS_END: Final[time] = time(8, 0)

#: FR-M8-008 — the default weekly cap across *all* non-essential message types.
DEFAULT_MAX_MESSAGES_PER_WEEK: Final[int] = 7


@dataclass(frozen=True, slots=True)
class TemplatePolicy:
    """The policy half of a ``message_templates`` row — DB §11.1.

    🔒 ``is_essential`` is a property of the *template*, declared once, never a
    runtime special case (DB §11.6). A client locked out of their portal because
    their practitioner hit a nudge quota is unacceptable, and the way to
    guarantee that is to make the exemption structural.
    """

    code: str
    is_essential: bool
    priority: int
    staleness_tolerance_minutes: int | None
    provider_template_status: ProviderTemplateStatus
    category: MessageCategory


@dataclass(frozen=True, slots=True)
class RecipientPolicy:
    """Everything about the recipient that can suppress a message at dispatch.

    ⚠️ Assembled by the caller *at dispatch time* from live rows — not carried
    on the ``scheduled_messages`` row. That is the whole point of DB §11.5: a
    snapshot taken at scheduling is exactly the stale answer the rule exists to
    avoid.
    """

    client_is_active: bool
    consent_granted: bool
    tenant_is_active: bool
    type_is_muted: bool
    messages_sent_this_week: int
    max_messages_per_week: int
    quota_remaining: int | None


def build_idempotency_key(
    *,
    tenant_id: UUID,
    recipient: str,
    template_code: str,
    occasion: str,
) -> str:
    """🔒 EC-M8-06 — the key a unique constraint enforces, so a retry cannot double-send.

    A deterministic composition of tenant, recipient, template and the *logical
    occasion* (DB §11.4) — "the check-in for week 32", not "now". Two attempts to
    schedule the same occasion produce the same key, and
    ``uq_scheduled_messages__idempotency`` refuses the second.

    🔒 **The constraint is the enforcement, not this function.** A ``SELECT``
    then ``INSERT`` can lose a race; a unique index cannot. This only has to be
    *stable* — same inputs, same key, forever — which is why it is hashed rather
    than concatenated: a recipient address containing the separator would
    otherwise collide with a different tuple.

    Args:
        tenant_id: The owning tenant.
        recipient: The address the message is bound for — number or email.
        template_code: Which message type.
        occasion: What this message is *for*, stable across retries.

    Returns:
        A 64-character hex digest.
    """
    material = "\x1f".join((str(tenant_id), recipient, template_code, occasion))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def evaluate_suppression(
    template: TemplatePolicy, recipient: RecipientPolicy
) -> SuppressionReason | None:
    """🔒 DB §11.5 — the one place a due message is refused, called at dispatch.

    Order matters and is deliberate: the reasons that are *about the client or
    the tenant* come before the ones that are about volume, because "this client
    withdrew consent" is a truer answer to "why didn't it send?" than "the weekly
    cap was reached". A support conversation turns on which reason is recorded.

    🔒 **Essential templates bypass quota and frequency only** (DB §11.6). They
    do not bypass consent withdrawal, an inactive client or a suspended tenant —
    a magic link to someone who withdrew consent is still a message they said
    they did not want.

    Args:
        template: The template's own policy.
        recipient: Live facts, read at dispatch.

    Returns:
        The reason to suppress, or ``None`` to proceed.
    """
    if not recipient.tenant_is_active:
        return SuppressionReason.TENANT_SUSPENDED
    if not recipient.consent_granted:
        return SuppressionReason.CONSENT_WITHDRAWN
    if not recipient.client_is_active:
        return SuppressionReason.CLIENT_STAGE_INACTIVE
    if template.provider_template_status is not ProviderTemplateStatus.APPROVED:
        return SuppressionReason.TEMPLATE_PAUSED
    if recipient.type_is_muted:
        return SuppressionReason.CLIENT_UNSUBSCRIBED

    # 🔒 Below this line, and only below it, an essential template is exempt.
    if template.is_essential:
        return None

    if recipient.quota_remaining is not None and recipient.quota_remaining <= 0:
        return SuppressionReason.QUOTA_EXCEEDED
    if recipient.messages_sent_this_week >= recipient.max_messages_per_week:
        return SuppressionReason.FREQUENCY_CAPPED

    return None


def is_within_quiet_hours(moment: time, *, start: time, end: time) -> bool:
    """Whether a local time falls inside the client's quiet window.

    ⚠️ The window normally wraps midnight (21:00 → 08:00), so the comparison is
    not a simple ``start <= t < end``. A non-wrapping window (e.g. 13:00 → 14:00)
    is also legal and handled.
    """
    if start == end:
        return False
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end


def defer_past_quiet_hours(
    scheduled_for: datetime,
    *,
    start: time = DEFAULT_QUIET_HOURS_START,
    end: time = DEFAULT_QUIET_HOURS_END,
) -> datetime:
    """🔒 AC-M8-006 — a message due in quiet hours is **moved, never dropped**.

    Returns the same instant when it is already outside the window, so a caller
    can compare identity to decide whether to record ``deferred_from``.

    ⚠️ Naive datetimes are rejected rather than assumed UTC. "02:00" is only
    meaningful against a zone, and silently guessing one would move a message to
    the wrong side of a client's night.

    Args:
        scheduled_for: When the message is due, timezone-aware.
        start: Quiet-hours start, in the same zone.
        end: Quiet-hours end.

    Returns:
        The original instant, or the next moment the window allows.

    Raises:
        ValueError: If ``scheduled_for`` is naive.
    """
    if scheduled_for.tzinfo is None:
        raise ValueError(
            "defer_past_quiet_hours needs a timezone-aware datetime: a quiet-hours "
            "window is meaningless without one."
        )

    if not is_within_quiet_hours(scheduled_for.timetz().replace(tzinfo=None), start=start, end=end):
        return scheduled_for

    candidate = scheduled_for.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if candidate <= scheduled_for:
        candidate += timedelta(days=1)
    return candidate


def is_stale(scheduled_for: datetime, *, now: datetime, tolerance_minutes: int | None) -> bool:
    """🔒 EC-M8-05 — a message too old to be worth sending expires instead.

    A check-in reminder for Friday morning, dispatched on Sunday because the
    worker was down, is worse than no reminder: it tells the client the practice
    is not paying attention. ``None`` means the message never goes stale —
    correct for a plan delivery, which is still wanted late.

    Args:
        scheduled_for: When it was due.
        now: The dispatch moment.
        tolerance_minutes: Grace period, or ``None`` for never stale.

    Returns:
        Whether the message should be expired rather than sent.
    """
    if tolerance_minutes is None:
        return False
    return now - scheduled_for > timedelta(minutes=tolerance_minutes)


def next_checkin_due(last: datetime, *, frequency: CheckinFrequency) -> datetime:
    """The next occurrence of a recurring check-in — DB §11.7, FR-M8-022.

    ⚠️ Monthly advances by 28 days rather than a calendar month. A check-in is a
    cadence, not a billing date: "the 31st" has no meaning in February, and a
    practitioner asking for monthly contact means roughly four weeks, not a date
    that silently skips a month.
    """
    steps = {
        CheckinFrequency.WEEKLY: 7,
        CheckinFrequency.FORTNIGHTLY: 14,
        CheckinFrequency.MONTHLY: 28,
    }
    return last + timedelta(days=steps[frequency])


def utc_now() -> datetime:
    """The dispatch clock, in one place so tests can reason about it."""
    return datetime.now(UTC)
