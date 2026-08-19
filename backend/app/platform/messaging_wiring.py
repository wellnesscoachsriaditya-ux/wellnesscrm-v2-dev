"""Building the message transports from configuration — the entry-point seam.

🔒 **The one place that knows both settings and adapters.** R5 forbids a module
importing ``platform``; R4 forbids an integration importing a module. So the
adapters take plain constructor arguments and something has to turn settings into
them. That something is here, called by ``main`` and ``worker``, and it is the
only file in the codebase that imports both ``app.platform.config`` and
``app.integrations.messaging``.

🔒 **An adapter is registered only when its credentials are present.** The
registry is a claim about what this process can actually do: the dispatch engine
picks its transport from what is registered, so a WhatsApp adapter with no access
token would turn every plan delivery into a failed attempt with a retry schedule
rather than a message that went out by email.

⚠️ ``logged`` is always registered, and that is deliberate. It is the floor: with
nothing else configured the engine still runs end to end, records every attempt
and sends nothing — which is exactly the state S5 ships in until Meta Business
Verification lands. A process with *no* transport at all could not dispatch, and
every message would fail for a configuration reason rather than being visibly
unsent.
"""

from __future__ import annotations

from app.integrations.messaging import (
    EmailTransport,
    LoggedTransport,
    SmtpSettings,
    WhatsAppSettings,
    WhatsAppTransport,
)
from app.kernel.models import TransportType
from app.kernel.notifications import NotificationTransport, configure_transports
from app.platform.config import Settings
from app.platform.logging import get_logger

logger = get_logger(__name__)


def build_transports(settings: Settings) -> dict[TransportType, NotificationTransport]:
    """The adapters this configuration supports.

    ⚠️ Returns a mapping rather than installing it, so the decision is testable
    without touching process-global state.
    """
    transports: dict[TransportType, NotificationTransport] = {
        TransportType.LOGGED: LoggedTransport()
    }

    if settings.smtp_host and settings.smtp_from_address:
        transports[TransportType.EMAIL] = EmailTransport(
            SmtpSettings(
                host=settings.smtp_host,
                port=settings.smtp_port,
                from_address=settings.smtp_from_address,
                username=settings.smtp_username,
                password=(
                    settings.smtp_password.get_secret_value() if settings.smtp_password else None
                ),
                use_tls=settings.smtp_use_tls,
            )
        )

    if settings.whatsapp_phone_number_id and settings.whatsapp_access_token:
        transports[TransportType.WHATSAPP] = WhatsAppTransport(
            WhatsAppSettings(
                phone_number_id=settings.whatsapp_phone_number_id,
                access_token=settings.whatsapp_access_token.get_secret_value(),
                base_url=settings.whatsapp_api_base_url,
                api_version=settings.whatsapp_api_version,
                language_code=settings.whatsapp_template_language,
            )
        )

    return transports


def configure_messaging(settings: Settings) -> frozenset[TransportType]:
    """Install the transports and say, once, what this process can send on.

    🔒 The log line is the operational answer to "why did that arrive by email?"
    and "why is nothing reaching WhatsApp?". Both questions are otherwise
    answered by reading configuration on a running host.
    """
    transports = build_transports(settings)
    configure_transports(transports)

    available = frozenset(transports)
    logger.info(
        "Message transports configured",
        extra={
            "transports": sorted(transport.value for transport in available),
            # ⚠️ Stated explicitly rather than left to be inferred from the list.
            # "WhatsApp is not configured" is the single most consequential fact
            # about a deployment of this product in India (M8.4).
            "whatsapp_enabled": TransportType.WHATSAPP in available,
        },
    )
    return available
