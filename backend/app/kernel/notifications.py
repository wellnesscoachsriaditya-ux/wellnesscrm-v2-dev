"""Notifications — one way to reach a person, whatever the transport.

🔒 FR-M0-041/042/043, Arch §3.4. **Interface only at S1** — there are no adapters
in this sprint and no message tables yet (those arrive with messaging in S4/S5).
What exists here is the contract every future transport must fit, defined before
any of them exist so that the first one is written *to* a shape rather than
becoming the shape.

Four properties, each answering a specific failure:

1. 🔒 **One abstraction, and modules never touch a transport** (FR-M0-041). A
   module asks for a :class:`Notification` to be sent; it does not know whether
   that becomes WhatsApp, SMS or email. The moment a module imports a WhatsApp
   client directly, changing provider becomes a search-and-replace across the
   codebase and the fallback in FR-M0-043 becomes unimplementable.
2. 🔒 **Every send is recorded** (FR-M0-042) — transport, template, recipient,
   status and failure reason. :class:`DeliveryRecord` is that shape. ⚠️ Nothing
   persists it in S1: the table lands with messaging. Defining it now is what
   stops the first adapter inventing its own log.
3. 🔒 **Templates are referenced, never composed here.** A notification carries a
   template code and its variables, not a rendered string. WhatsApp requires
   pre-approved templates, so a module that built its own message body would
   produce something the transport rejects at send time — a failure discovered in
   production, per recipient.
4. 🔒 **Consent is a precondition, not a detail** (FR-M0-022, DPDP). A transport
   is bound to a consent purpose, and :func:`purpose_for` is the mapping. A
   marketing message sent over a channel the client consented to for
   appointment reminders is a compliance finding, not a UX liberty.

⚠️ **This module sends nothing and cannot.** There is no adapter, and
:func:`send` does not exist. That is deliberate: writing an HTTP client against
a provider that is not provisioned produces code whose tests assert only my
assumptions about its contract — green, and evidence of nothing. The same
argument the identity slice makes for deferring the GoTrue adapter
(``platform/identity/credentials.py``).

⚠️ **Payloads carry identifiers, never clinical values** (NFR-033). The same rule
as ``jobs.payload``: a notification is queued, logged and retried, so anything in
it is copied into places with different retention and access rules.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.kernel.errors import ValidationError
from app.kernel.models import TransportType

#: 🔒 FR-M0-022 — which consent purpose each transport requires. A channel the
#: client has not consented to is not available, whatever the message.
#:
#: ⚠️ Keyed by transport, not by message. The *purpose* of the message narrows it
#: further — see :func:`purpose_for` — but no transport is ever consent-free.
_TRANSPORT_PURPOSE: dict[TransportType, str] = {
    TransportType.WHATSAPP: "whatsapp_communication",
    # 🟡 SMS and email have no dedicated purpose in the 0006 seed. They fall back
    # to service delivery, which is essential and therefore always present for an
    # active engagement. ⚠️ That is correct for transactional messages only;
    # a marketing send must use `purpose_for` with its own category, not this.
    TransportType.SMS: "service_delivery",
    TransportType.EMAIL: "service_delivery",
}


class MessageCategory(str, enum.Enum):
    """Why a message is being sent — which decides the consent purpose.

    🔒 The distinction DPDP cares about. A reminder and an advertisement travel
    the same wire and require different permission, so the category is carried
    explicitly rather than inferred from the template name.
    """

    #: Something the client asked for or is required for the service.
    TRANSACTIONAL = "transactional"
    #: A scheduled nudge about an appointment.
    REMINDER = "reminder"
    #: 🔒 Requires its own consent, always withdrawable (FR-M0-024).
    MARKETING = "marketing"


#: 🔒 Category overrides the transport default where DPDP requires a narrower
#: basis. Marketing is the case that matters: it is never covered by an essential
#: purpose, on any channel.
_CATEGORY_PURPOSE: dict[MessageCategory, str] = {
    MessageCategory.MARKETING: "marketing",
    MessageCategory.REMINDER: "appointment_reminders",
}


def purpose_for(transport: TransportType, category: MessageCategory) -> str:
    """The consent purpose code this send requires.

    🔒 Category first, transport second. A marketing message over WhatsApp needs
    *marketing* consent, not merely consent to be contacted on WhatsApp —
    treating the channel permission as sufficient is precisely the dark pattern
    FR-M0-024 and the DPDP consent model exist to prevent.
    """
    return _CATEGORY_PURPOSE.get(category, _TRANSPORT_PURPOSE[transport])


# ─── The recipient ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Recipient:
    """Who to reach, and on what address.

    ⚠️ The address is carried rather than looked up here, because resolving a
    client id to a mobile number is a ``clients`` read and the kernel must not
    reach into a module's tables (Arch R6). The caller resolves; this records.

    🔒 ``subject_id`` is retained alongside the address so the delivery record
    can be tied back to a person after the address changes — a client who
    switches number would otherwise orphan their own message history.
    """

    address: str
    subject_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if not self.address or not self.address.strip():
            raise ValidationError(
                "A notification needs somewhere to go.",
                action="Add a mobile number or email address for this client.",
            )


# ─── The notification ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Notification:
    """One message to send, described in transport-independent terms.

    🔒 **A template code and variables, never a rendered body** (FR-M0-041).
    WhatsApp only delivers pre-approved templates, so a caller that composed its
    own text would produce something the transport rejects per-recipient at send
    time — a failure that appears in production, one client at a time, long after
    the code that caused it shipped.

    ⚠️ ``variables`` carries identifiers and short display strings only, never
    clinical values (NFR-033). A notification is queued, retried and logged, so
    anything inside it is copied into stores with different retention rules — the
    same constraint that governs ``jobs.payload``.
    """

    tenant_id: uuid.UUID
    recipient: Recipient
    template_code: str
    category: MessageCategory
    #: Preference order. 🔒 FR-M0-043 (fallback) is Phase 2, so at MVP only the
    #: first entry is attempted — but the shape is a sequence now, because
    #: retrofitting fallback onto a single-transport field would touch every
    #: call site.
    transports: tuple[TransportType, ...]
    variables: Mapping[str, str] | None = None
    #: Set when the send belongs to a client engagement, so the delivery record
    #: can be shown on that client's timeline once messaging ships.
    client_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if not self.template_code:
            raise ValidationError(
                "A notification needs a template.",
                action="Name the message template to send.",
            )
        if not self.transports:
            # 🔒 An empty preference list is silently "never delivered". Failing
            # here makes it a caller bug rather than a message nobody receives.
            raise ValidationError(
                "A notification needs at least one transport.",
                action="Choose how this message should be sent.",
            )

    @property
    def primary_transport(self) -> TransportType:
        """The transport to attempt first."""
        return self.transports[0]

    def required_purpose(self) -> str:
        """The consent purpose this send needs — see :func:`purpose_for`."""
        return purpose_for(self.primary_transport, self.category)


# ─── The delivery record (FR-M0-042) ─────────────────────────────────────


class DeliveryStatus(str, enum.Enum):
    """What became of one send attempt.

    🔒 ``queued`` and ``sent`` are different states, and collapsing them loses
    the only window in which a stuck queue is visible: a message that is
    ``queued`` for an hour is an incident, while one that is ``sent`` and
    undelivered is the transport's problem.
    """

    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    #: 🔒 Refused before dispatch — no consent, or the client opted out. Distinct
    #: from ``failed``: nothing went wrong, and retrying is exactly the wrong
    #: response.
    SUPPRESSED = "suppressed"


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    """🔒 FR-M0-042 — what must be recorded for every outbound message.

    ⚠️ **Nothing persists this in S1.** The table arrives with messaging (S4/S5).
    It is defined now so the first adapter is written against an agreed shape
    rather than inventing a log of its own — at which point a second adapter
    invents a different one and "every outbound message is recorded" becomes true
    of two incompatible records.

    ⚠️ ``failure_reason`` is the provider's message, which is operator-facing.
    It must never be shown to a client and must never carry a clinical value: it
    is the one field here most likely to be echoed into a log wholesale.
    """

    tenant_id: uuid.UUID
    transport: TransportType
    template_code: str
    #: 🔒 The address as sent to. Stored because "we sent it" is only evidence if
    #: it says where — but see the retention note in FR-M0-042's implementation
    #: when the table lands: this is contact data.
    recipient_address: str
    status: DeliveryStatus
    category: MessageCategory
    occurred_at: datetime
    client_id: uuid.UUID | None = None
    provider_message_id: str | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        # 🔒 A failure with no reason is a record that cannot be acted on, and
        # the reason is the whole point of recording the failure.
        if self.status is DeliveryStatus.FAILED and not self.failure_reason:
            raise ValidationError(
                "A failed delivery must record why it failed.",
                action="Pass the transport's error through as failure_reason.",
            )


# ─── The port ────────────────────────────────────────────────────────────


class NotificationTransport(Protocol):
    """One transport, as the rest of the system is allowed to see it.

    🔒 FR-M0-041 — this is the *only* surface a module may reach a person
    through. An implementation lives in ``app/integrations/``, behind this
    protocol, and nothing outside that package imports a provider SDK.

    ⚠️ **No implementation exists in S1, deliberately.** See the note at the foot
    of this module.
    """

    @property
    def transport(self) -> TransportType:
        """Which channel this implementation speaks."""
        ...

    async def send(self, notification: Notification) -> DeliveryRecord:
        """Attempt one send and return what happened.

        🔒 Returns a record rather than raising on provider failure. A transport
        being down is an expected operational state, not an exception — the
        caller records the outcome and, once FR-M0-043 lands, tries the next
        transport in the preference list.

        ⚠️ Implementations must not check consent. That decision belongs to the
        caller, which has the consent state loaded; a transport that re-derived
        it would be a second, weaker copy of the rule.
        """
        ...


# ─── 🔒 Remaining work: the adapters ─────────────────────────────────────
#
# S1 ships the port and no adapters, which the sprint plan asks for explicitly
# ("`notifications` port — Interface only; no adapters yet", FR-M0-041).
#
# They are deliberately NOT written here. An HTTP client against a WhatsApp
# Business account that has not been provisioned would produce code whose tests
# assert only my assumptions about the provider's contract — green, and evidence
# of nothing. That is the same reasoning that defers the GoTrue adapter in
# `app/platform/identity/credentials.py`.
#
# The shape above is drawn from the constraints that are already known and are
# not going to change:
#
#   * WhatsApp delivers pre-approved templates only, so `Notification` carries a
#     template code and variables rather than a body.
#   * Providers report failure as a response, not an exception, so `send`
#     returns a `DeliveryRecord` rather than raising.
#   * Every provider issues its own message id, so `provider_message_id` exists
#     for the delivery-receipt webhook to correlate against.
#
# When messaging is built (S4/S5), the adapter is a translation of one call, and
# `DeliveryRecord` gains the table that FR-M0-042 requires.
