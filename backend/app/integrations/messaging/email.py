"""The email transport — SMTP behind the notification port.

🔒 **The fallback that keeps S5 shippable and the product sellable outside
India.** Approved proposal #7 defers SMS; WhatsApp waits on Meta. Email is the
one channel that needs nobody's approval, and the sprint plan names it as what
S5 ships on until verification lands.

🔒 **Credentials arrive as constructor arguments, never read from anywhere.**
R5 forbids an integration importing ``app.platform``, where settings live, so the
entry point builds this from configuration. Nothing here reads an environment
variable and nothing here has a default that would work — an adapter with a
built-in host is an adapter that silently sends somewhere in a misconfigured
deployment.

⚠️ **`smtplib` on a worker thread, not an async client.** The alternative is a
new runtime dependency (NFR-078 requires each to be justified), and the volume
this carries — a few thousand messages a month at the sizing in Arch §14 — does
not justify one. 🔒 The revisit trigger is concrete: if email volume makes the
thread pool a bottleneck, or if we need DKIM signing or provider-side
suppression lists, take the dependency then.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage

from app.kernel.models import TransportType
from app.kernel.notifications import DeliveryRecord, DeliveryStatus, Notification

logger = logging.getLogger(__name__)

#: 🔒 How long one SMTP conversation may take. Bounded because this runs inside a
#: job whose own timeout is 30 seconds (`JobClass.DISPATCH`); an unbounded socket
#: would let the job's timeout fire instead, which loses the failure reason.
_TIMEOUT_SECONDS = 15


@dataclass(frozen=True, slots=True)
class SmtpSettings:
    """Everything the adapter needs to reach a mail server.

    Frozen, and built by the entry point from settings. ⚠️ ``password`` is a
    plain ``str`` here rather than a ``SecretStr``: pydantic lives in
    ``platform``, and an integration that imported it to hold a secret would
    breach R5 to gain a repr guard. The value must therefore never be logged, and
    is not — see :meth:`EmailTransport.send`.
    """

    host: str
    port: int
    from_address: str
    username: str | None = None
    password: str | None = None
    use_tls: bool = True


class EmailTransport:
    """Satisfies ``kernel.notifications.NotificationTransport`` over SMTP."""

    def __init__(self, settings: SmtpSettings) -> None:
        self._settings = settings

    @property
    def transport(self) -> TransportType:
        return TransportType.EMAIL

    async def send(self, notification: Notification) -> DeliveryRecord:
        """Send one message, and report what happened.

        🔒 Returns a failed record rather than raising (the port's contract). A
        mail server being down is an expected operational state, and the dispatch
        engine needs the outcome recorded in the delivery log before it decides
        whether to retry.
        """
        body = notification.body
        if not body:
            # 🔒 An email with no body is not a message. This is a template
            # configuration fault, and it must be visible as one rather than
            # arriving at a client as a blank email under their practitioner's
            # name.
            return self._record(
                notification,
                status=DeliveryStatus.FAILED,
                failure_reason="The template rendered an empty body.",
            )

        message = EmailMessage()
        message["From"] = self._settings.from_address
        message["To"] = notification.recipient.address
        message["Subject"] = _subject_for(notification.template_code)
        # 🔒 A `Message-ID` we generate, so the delivery log can correlate with
        # the mail server's own logs. SMTP has no delivery receipt to key on, so
        # this is the only provider id an email send can have.
        message_id = f"<{uuid.uuid4()}@{_domain_of(self._settings.from_address)}>"
        message["Message-ID"] = message_id
        message.set_content(body)

        try:
            await asyncio.to_thread(self._deliver, message)
        except (smtplib.SMTPException, OSError, ssl.SSLError) as error:
            # ⚠️ `type(error).__name__` and `str(error)` only. An SMTP exception
            # can carry the server's response verbatim, which is operator-facing
            # detail; it must not gain the recipient address or the body on the
            # way into the log (NFR-033).
            logger.warning(
                "Email transport failed",
                extra={
                    "tenant_id": str(notification.tenant_id),
                    "template_code": notification.template_code,
                    "error_class": type(error).__name__,
                },
            )
            return self._record(
                notification,
                status=DeliveryStatus.FAILED,
                failure_reason=f"{type(error).__name__}: {error}",
            )

        return self._record(
            notification, status=DeliveryStatus.SENT, provider_message_id=message_id
        )

    def _deliver(self, message: EmailMessage) -> None:
        """The blocking half, run on a worker thread.

        🔒 ``SMTP_SSL`` for an implicit-TLS port and ``starttls()`` otherwise.
        There is no plaintext path: credentials and a client's address would
        otherwise cross the network in the clear, and a "TLS optional" flag is
        one somebody eventually sets wrong.
        """
        settings = self._settings
        context = ssl.create_default_context()

        if settings.use_tls and settings.port == 465:
            with smtplib.SMTP_SSL(
                settings.host, settings.port, timeout=_TIMEOUT_SECONDS, context=context
            ) as client:
                self._authenticate(client)
                client.send_message(message)
            return

        with smtplib.SMTP(settings.host, settings.port, timeout=_TIMEOUT_SECONDS) as client:
            if settings.use_tls:
                client.starttls(context=context)
            self._authenticate(client)
            client.send_message(message)

    def _authenticate(self, client: smtplib.SMTP) -> None:
        """Log in when credentials were supplied.

        ⚠️ Both are optional: a local relay (MailHog in development, a sidecar in
        a private network) legitimately takes no credentials, and requiring them
        would mean the only way to run the engine locally is to point it at a
        real mail provider.
        """
        if self._settings.username and self._settings.password:
            client.login(self._settings.username, self._settings.password)

    def _record(
        self,
        notification: Notification,
        *,
        status: DeliveryStatus,
        provider_message_id: str | None = None,
        failure_reason: str | None = None,
    ) -> DeliveryRecord:
        return DeliveryRecord(
            tenant_id=notification.tenant_id,
            transport=self.transport,
            template_code=notification.template_code,
            recipient_address=notification.recipient.address,
            status=status,
            category=notification.category,
            occurred_at=datetime.now(UTC),
            client_id=notification.client_id,
            provider_message_id=provider_message_id,
            failure_reason=failure_reason,
        )


#: 🔒 Subject lines, per template code. Deliberately **not** derived from the
#: body: a subject assembled from the first line of a rendered message would put
#: a client's name and their practitioner's into a mail header, which is copied
#: into every intermediate server's logs.
#:
#: ⚠️ Neutral by design. "Your nutrition plan is ready" says enough to be opened
#: and nothing about the person's health.
_SUBJECTS: dict[str, str] = {
    "plan_delivered": "Your nutrition plan is ready",
    "appointment_confirmed": "Your appointment is confirmed",
    "appointment_reminder": "A reminder about your appointment",
    "checkin_nudge": "Time for your check-in",
    "assessment_invitation": "A short form before your consultation",
    "lead_acknowledgement": "We have received your enquiry",
    "lead_notification": "A new enquiry has arrived",
    "magic_link": "Your secure link",
}

#: The fallback subject. ⚠️ Deliberately generic rather than the template code:
#: a code such as `assessment_invitation` in a subject line discloses what stage
#: of care the recipient is at, to anyone who sees their inbox.
_DEFAULT_SUBJECT = "A message from your practitioner"


def _subject_for(template_code: str) -> str:
    return _SUBJECTS.get(template_code, _DEFAULT_SUBJECT)


def _domain_of(address: str) -> str:
    """The domain half of the from-address, for the generated ``Message-ID``."""
    _, _, domain = address.rpartition("@")
    return domain or "wellnesscrm.local"
