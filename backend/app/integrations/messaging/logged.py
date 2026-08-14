"""The logged transport — a real no-op channel (FR-M0-041, S5's shipping gate).

🔒 **This is not a test double.** It runs in local and staging deployments and is
what makes the implementation plan's promise true: "If Meta verification has not
landed, this sprint still ships — email and logged-only transports work, and the
WhatsApp adapter is swapped in when approval arrives."

🔒 **It never claims a delivery.** It returns ``DeliveryStatus.SENT`` — meaning
*this transport accepted the message*, which is exactly what happened — and it
issues no provider message id, so no delivery receipt can ever arrive for it and
nothing in the delivery log can be mistaken for a WhatsApp delivery. The row
records ``transport = 'logged'``, and the practitioner-facing history says so.

⚠️ The log line carries **no message body and no recipient address**. A body is
rendered content about a person's care, and an address is contact PII; both would
be copied into a log store with different retention rules from the delivery log
they came from (NFR-033). What is logged is what an operator needs to confirm the
engine ran: tenant, template, transport and dispatch id.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.kernel.models import TransportType
from app.kernel.notifications import DeliveryRecord, DeliveryStatus, Notification

# ⚠️ `logging.getLogger`, not `app.platform.logging.get_logger`. R5 forbids an
# integration importing platform; the process-wide logging configuration applies
# to this logger either way, because it is configured on the root.
logger = logging.getLogger(__name__)


class LoggedTransport:
    """Satisfies ``kernel.notifications.NotificationTransport`` by recording only."""

    @property
    def transport(self) -> TransportType:
        return TransportType.LOGGED

    async def send(self, notification: Notification) -> DeliveryRecord:
        """Record that a message would have gone out, and send nothing.

        Returns a ``SENT`` record with no ``provider_message_id`` — see the
        module docstring for why both halves of that are deliberate.
        """
        logger.info(
            "Message accepted by the logged transport (nothing was sent)",
            extra={
                "tenant_id": str(notification.tenant_id),
                "template_code": notification.template_code,
                "transport": self.transport.value,
                "dispatch_id": str(notification.dispatch_id) if notification.dispatch_id else None,
                "client_id": str(notification.client_id) if notification.client_id else None,
            },
        )
        return DeliveryRecord(
            tenant_id=notification.tenant_id,
            transport=self.transport,
            template_code=notification.template_code,
            recipient_address=notification.recipient.address,
            status=DeliveryStatus.SENT,
            category=notification.category,
            occurred_at=datetime.now(UTC),
            client_id=notification.client_id,
            provider_message_id=None,
        )
