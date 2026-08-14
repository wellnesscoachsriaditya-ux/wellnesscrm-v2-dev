"""Message transports — the adapters behind ``kernel.notifications``.

🔒 Arch §3.1 R4/R5: an integration knows nothing about the modules that use it
and nothing about ``platform``. Credentials arrive as constructor arguments,
built by the entry point from settings, so no adapter reads configuration and
none can be constructed into a state that silently sends somewhere unintended.

Three adapters at MVP:

* :class:`~app.integrations.messaging.logged.LoggedTransport` — records and sends
  nothing. 🔒 The transport S5 runs on before Meta Business Verification, and a
  real deployment mode rather than a test double.
* :class:`~app.integrations.messaging.email.EmailTransport` — SMTP.
* :class:`~app.integrations.messaging.whatsapp.WhatsAppTransport` — Meta Cloud
  API. ⚠️ Contract-tested against fixtures; no real delivery has been verified.

🔒 SMS is deliberately absent (approved proposal #7 — TRAI DLT registration off
the critical path).
"""

from app.integrations.messaging.email import EmailTransport, SmtpSettings
from app.integrations.messaging.logged import LoggedTransport
from app.integrations.messaging.whatsapp import (
    ProviderStatusEvent,
    WhatsAppSettings,
    WhatsAppTransport,
    parse_status_events,
    verify_signature,
)

__all__ = [
    "EmailTransport",
    "LoggedTransport",
    "ProviderStatusEvent",
    "SmtpSettings",
    "WhatsAppSettings",
    "WhatsAppTransport",
    "parse_status_events",
    "verify_signature",
]
