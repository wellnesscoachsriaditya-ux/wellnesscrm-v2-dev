"""Choosing which transport carries a message, and to what address.

🔒 **This is deployment substitution, not FR-M8-012 transport fallback.**
The distinction matters and is easy to lose:

* **Fallback (Phase 2, FR-M8-012)** is per message type and per attempt: "try
  WhatsApp, and if *this send* fails, try SMS." Not built, and not built here.
* **Substitution (MVP, and the reason S5 ships)** is per deployment: a process
  with no WhatsApp credentials cannot send on WhatsApp *at all*, so every
  WhatsApp template runs on whatever this deployment does have. It is decided
  once from the configured adapter set, applies uniformly, and is recorded on
  every `message_dispatches` row — so nobody reading the delivery log can
  mistake a logged attempt for a WhatsApp delivery.

The implementation plan makes this explicit: "email and logged-only transports
work, and the WhatsApp adapter is swapped in when approval arrives."

⚠️ Substitution never invents a channel the recipient cannot receive on. A
client with an email and no mobile (EC-M1-08 permits exactly that) is reached by
email or not at all, and "not at all" is recorded as a failed dispatch with a
reason rather than silently skipped.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.kernel.clients import ContactDetails
from app.kernel.models import TransportType
from app.modules.messaging.templates import Template

#: 🔒 The substitution order when a template's transport is not configured.
#: Email first because it reaches a real person; `logged` last because it
#: reaches nobody and exists so the engine can run end to end before a provider
#: is verified. ⚠️ SMS is absent — approved proposal #7, no adapter at MVP.
_SUBSTITUTION_ORDER: tuple[TransportType, ...] = (TransportType.EMAIL, TransportType.LOGGED)


@dataclass(frozen=True, slots=True)
class TransportChoice:
    """Which transport will carry this message, and where to.

    ``address`` is empty only when the recipient cannot be reached on the chosen
    transport at all, which the dispatch engine records as a failed attempt
    rather than treating as a suppression — nothing about the client's wishes or
    the tenant's status caused it.
    """

    transport: TransportType
    address: str
    #: True when this is not the transport the template asked for. Carried so
    #: the caller can log it once per send rather than inferring it later.
    substituted: bool


def address_for(transport: TransportType, contact: ContactDetails) -> str | None:
    """The address a given transport would use, or ``None`` if there is none."""
    match transport:
        case TransportType.WHATSAPP | TransportType.SMS:
            return contact.mobile
        case TransportType.EMAIL:
            return contact.email
        case TransportType.LOGGED:
            # 🔒 The no-op transport still records a real address, because the
            # delivery log's value is that it says where a message *would* have
            # gone. A logged row with an empty address proves nothing.
            return contact.mobile or contact.email


def choose(
    *,
    template: Template,
    contact: ContactDetails,
    available: frozenset[TransportType],
    preferred: TransportType | None = None,
) -> TransportChoice:
    """Pick the transport for one send.

    Args:
        template: Supplies ``default_transport``.
        contact: The recipient's live addresses, read at dispatch (EC-M8-08).
        available: What this process has adapters for — from
            ``kernel.notifications.available_transports``.
        preferred: A per-tenant or per-client override from
            ``notification_preferences``. Applied only when this deployment can
            actually serve it; a preference for a transport nobody configured is
            a stale setting, not an instruction to fail.

    Returns:
        The transport, the address, and whether a substitution happened. The
        address may be empty — see :class:`TransportChoice`.
    """
    desired = preferred if preferred in available else template.default_transport

    candidates: list[tuple[TransportType, bool]] = [(desired, False)]
    candidates += [(transport, True) for transport in _SUBSTITUTION_ORDER if transport != desired]

    for transport, substituted in candidates:
        if transport not in available:
            continue
        address = address_for(transport, contact)
        if address:
            return TransportChoice(transport=transport, address=address, substituted=substituted)

    # 🔒 Nothing usable. Return the desired transport with no address so the
    # caller records *what was attempted* — a failure row naming `logged`
    # because that happened to be configured would misreport the outage.
    return TransportChoice(transport=desired, address="", substituted=False)
