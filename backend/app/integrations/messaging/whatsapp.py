"""The WhatsApp transport — Meta Cloud API behind the notification port.

⚠️ 🔒 **No real delivery has been verified.** Meta Business Verification is not
granted for this account, so this adapter has never sent a message to a real
number. What exists is the translation of one call, contract-tested against
recorded fixtures (`tests/test_whatsapp_adapter.py`). The S5 report states this
plainly, and nothing in the product claims otherwise: a deployment without
credentials does not register this adapter at all, and every message it would
have carried is recorded against the transport that actually carried it.

🔒 **Template messages only.** Meta delivers pre-approved templates outside the
24-hour customer-service window, and free text is rejected per recipient at send
time — a failure discovered in production, one client at a time. So this refuses
to send without a ``provider_template_name`` rather than improvising a body.

🔒 **Credentials arrive as constructor arguments.** R5 forbids an integration
importing ``app.platform``; the entry point builds this from settings. There is
no default that would work, and no environment variable is read here.

⚠️ **`urllib.request` on a worker thread, not an HTTP client dependency.** One
POST does not justify a runtime dependency under NFR-078, and `httpx` is
currently a dev-only dependency — adding it to the runtime set for this would be
a decision to make deliberately rather than in passing. 🔒 The revisit trigger:
media upload, connection pooling across a burst, or the first need for
provider-side retry semantics.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.kernel.models import TransportType
from app.kernel.notifications import DeliveryRecord, DeliveryStatus, Notification

logger = logging.getLogger(__name__)

#: Bounded for the same reason the SMTP timeout is: the dispatch job's own
#: timeout is 30 seconds, and letting that fire instead loses the failure reason.
_TIMEOUT_SECONDS = 15

#: The Cloud API version this adapter speaks. 🔒 Pinned rather than "latest":
#: Meta deprecates versions on a schedule, and a floating version means the
#: payload shape can change under a deployment nobody touched.
DEFAULT_API_VERSION = "v21.0"

DEFAULT_BASE_URL = "https://graph.facebook.com"

#: The POST this adapter makes, as an injectable seam for the contract tests.
PostCallable = Callable[
    [str, Mapping[str, Any], Mapping[str, str], int], Awaitable[Mapping[str, Any]]
]


@dataclass(frozen=True, slots=True)
class WhatsAppSettings:
    """Everything the adapter needs to reach Meta's Cloud API.

    ⚠️ ``access_token`` is a plain ``str`` for the same reason the SMTP password
    is: pydantic lives in ``platform``. It must never be logged, and is not.
    """

    phone_number_id: str
    access_token: str
    base_url: str = DEFAULT_BASE_URL
    api_version: str = DEFAULT_API_VERSION
    #: 🔒 The language the approved templates were registered under. A template
    #: sent with a language code Meta has not approved for it is rejected, so
    #: this is configuration rather than a per-message choice.
    language_code: str = "en"


class WhatsAppTransport:
    """Satisfies ``kernel.notifications.NotificationTransport`` over the Cloud API."""

    def __init__(
        self,
        settings: WhatsAppSettings,
        *,
        post: PostCallable | None = None,
    ) -> None:
        self._settings = settings
        # ⚠️ Injected so the contract tests can drive this against recorded
        # fixtures without a network. 🔒 It is *not* a way to disable the real
        # client in production: the default is the real one, and a deployment
        # that wants no WhatsApp simply does not register this adapter.
        self._post = post or _post_json

    @property
    def transport(self) -> TransportType:
        return TransportType.WHATSAPP

    def endpoint(self) -> str:
        """The messages endpoint for the configured phone number."""
        base = self._settings.base_url.rstrip("/")
        return f"{base}/{self._settings.api_version}/{self._settings.phone_number_id}/messages"

    def build_payload(self, notification: Notification) -> dict[str, Any]:
        """Translate a :class:`Notification` into Meta's template message shape.

        🔒 **Positional parameters, in declaration order.** Meta's template
        parameters are positional (`{{1}}`, `{{2}}`), while our templates declare
        named variables. The order is the order the template's ``variables``
        mapping declares — which is why `message_templates.variables` is stored
        as an object whose key order is preserved by `jsonb`… ⚠️ except that
        `jsonb` does **not** preserve key order. So the order is taken from the
        sorted variable names, and the approved Meta template must be registered
        with its parameters in that same order.

        That coupling is real and is the price of positional parameters; it is
        stated here because the failure mode — a client's name appearing where
        the plan link should be — is silent and reaches a real person.
        """
        variables = notification.variables or {}
        parameters = [{"type": "text", "text": str(variables[name])} for name in sorted(variables)]

        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": _to_msisdn(notification.recipient.address),
            "type": "template",
            "template": {
                "name": notification.provider_template_name,
                "language": {"code": self._settings.language_code},
                "components": ([{"type": "body", "parameters": parameters}] if parameters else []),
            },
        }

    async def send(self, notification: Notification) -> DeliveryRecord:
        """Attempt one send and report the outcome.

        🔒 Returns a record rather than raising, per the port's contract. Meta
        reports failure as a response body, and the dispatch engine needs the
        outcome in the delivery log before deciding whether a retry is warranted.
        """
        if not notification.provider_template_name:
            # 🔒 See the module docstring: free text is rejected per recipient.
            # Refusing here turns a production failure into a recorded,
            # non-retryable one with a reason that names the fix.
            return self._record(
                notification,
                status=DeliveryStatus.FAILED,
                failure_reason=(
                    "This template has no approved WhatsApp template name, so it cannot "
                    "be sent on WhatsApp."
                ),
            )

        try:
            response = await self._post(
                self.endpoint(),
                self.build_payload(notification),
                {
                    "Authorization": f"Bearer {self._settings.access_token}",
                    "Content-Type": "application/json",
                },
                _TIMEOUT_SECONDS,
            )
        except urllib.error.HTTPError as error:
            return self._record(
                notification,
                status=DeliveryStatus.FAILED,
                failure_reason=_describe_http_error(error),
            )
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            logger.warning(
                "WhatsApp transport failed",
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

        message_id = _extract_message_id(response)
        if message_id is None:
            return self._record(
                notification,
                status=DeliveryStatus.FAILED,
                failure_reason="The provider accepted the request but returned no message id.",
            )

        return self._record(
            notification, status=DeliveryStatus.SENT, provider_message_id=message_id
        )

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


# ─── The HTTP call ───────────────────────────────────────────────────────


async def _post_json(
    url: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    timeout_seconds: int,
) -> Mapping[str, Any]:
    """POST JSON and decode the response, on a worker thread."""

    def call() -> Mapping[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=dict(headers),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
        decoded: Mapping[str, Any] = json.loads(body) if body else {}
        return decoded

    return await asyncio.to_thread(call)


def _extract_message_id(response: Mapping[str, Any]) -> str | None:
    """Pull the provider's message id out of a Cloud API response.

    Meta returns ``{"messages": [{"id": "wamid...."}]}``. ⚠️ Read defensively:
    the delivery log's webhook correlation depends on this value, and a shape
    change that produced ``None`` must be a recorded failure rather than a
    ``KeyError`` inside a job.
    """
    messages = response.get("messages")
    if isinstance(messages, list) and messages:
        first = messages[0]
        if isinstance(first, dict):
            identifier = first.get("id")
            if isinstance(identifier, str) and identifier:
                return identifier
    return None


def _describe_http_error(error: urllib.error.HTTPError) -> str:
    """A bounded, operator-facing description of a rejected request.

    🔒 The body is read and truncated rather than dropped: Meta's error payload
    names the actual problem ("template does not exist", "recipient not on
    WhatsApp"), and EC-M8-01/EC-M8-03 both depend on that reaching the
    practitioner-facing failure reason.

    ⚠️ The word "reject" in the text is what `dispatch._failure_code` classifies
    as non-retryable, so this wording is load-bearing for a 4xx.
    """
    try:
        detail = error.read().decode("utf-8", errors="replace")[:500]
    except OSError:  # pragma: no cover — the body is already consumed
        detail = ""

    verb = "rejected" if 400 <= error.code < 500 else "failed"
    return f"The provider {verb} the message (HTTP {error.code}). {detail}".strip()


def _to_msisdn(address: str) -> str:
    """Meta expects a bare international number, without the leading ``+``.

    ⚠️ Our own storage is E.164 with the ``+`` (``ck_clients__mobile_e164``), so
    this is a translation rather than a normalisation — nothing here decides what
    a valid number is.
    """
    return address.removeprefix("+")


# ─── The delivery-receipt webhook (API §11.3) ────────────────────────────


def verify_signature(*, secret: str, body: bytes, header: str | None) -> bool:
    """🔒 Verify Meta's ``X-Hub-Signature-256`` before the body is parsed.

    API §11.3, rule 1: "Verify the signature before parsing the body. An
    unverified webhook is an unauthenticated write endpoint." This endpoint can
    move a delivery status for any tenant, so an unsigned caller must not reach
    the parser, let alone the database.

    ⚠️ ``hmac.compare_digest``, never ``==``. A byte-by-byte comparison leaks the
    correct prefix through timing, and a webhook signature is exactly the kind of
    secret an attacker can probe at leisure.

    ⚠️ Computed over the **raw request body**, not a re-serialised object. JSON
    round-tripping changes key order and whitespace, and the signature is over
    the bytes Meta sent.

    Returns ``False`` for a missing or malformed header rather than raising: the
    caller's response to "no valid signature" is 401 either way, and an exception
    here would make a malformed header a 500.
    """
    if not header or not secret:
        return False

    algorithm, _, digest = header.partition("=")
    if algorithm != "sha256" or not digest:
        return False

    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, digest)


#: 🔒 Meta's status vocabulary → ours (DB §11.3). ``failed`` is the only one that
#: is not a straight rename, and it is deliberately mapped to ``failed`` rather
#: than ``rejected``: Meta uses "failed" for both a rejected template and an
#: undeliverable number, and inventing the distinction here would put a guess in
#: a column a practitioner reads.
_STATUS_MAP: dict[str, str] = {
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
}


@dataclass(frozen=True, slots=True)
class ProviderStatusEvent:
    """One delivery-status fact, as this provider expresses it.

    🔒 Provider-shaped on purpose, and translated by the caller. R4 forbids an
    integration importing a module, so this cannot produce
    ``modules.messaging.webhooks.StatusUpdate`` — and it should not: the whole
    point of the port is that the engine's vocabulary does not learn Meta's.
    """

    provider_message_id: str
    status: str
    occurred_at: datetime
    error_code: str | None = None
    error_detail: str | None = None


def parse_status_events(payload: Mapping[str, Any]) -> Sequence[ProviderStatusEvent]:
    """Extract delivery statuses from a Cloud API webhook body.

    🔒 **Statuses only** (EC-M8-07, API §11.3). A payload carrying inbound
    *messages* — a client replying — yields nothing here and is accepted and
    discarded, which is what stops Meta marking the webhook unhealthy while
    keeping the two-way inbox firmly in Phase 2 (FR-M8-030).

    ⚠️ Read defensively at every level. This parses attacker-reachable JSON that
    has already passed a signature check; a malformed shape must produce no
    events rather than an exception inside a request handler.
    """
    events: list[ProviderStatusEvent] = []

    for entry in _sequence(payload.get("entry")):
        for change in _sequence(_mapping(entry).get("changes")):
            value = _mapping(_mapping(change).get("value"))
            for status in _sequence(value.get("statuses")):
                parsed = _parse_status(_mapping(status))
                if parsed is not None:
                    events.append(parsed)

    return events


def _parse_status(status: Mapping[str, Any]) -> ProviderStatusEvent | None:
    identifier = status.get("id")
    raw_status = status.get("status")
    if not isinstance(identifier, str) or not isinstance(raw_status, str):
        return None

    mapped = _STATUS_MAP.get(raw_status)
    if mapped is None:
        # An unknown status is not an error — Meta adds them — and guessing where
        # it sits in the lifecycle could walk a row backwards.
        return None

    errors = _sequence(status.get("errors"))
    first_error = _mapping(errors[0]) if errors else {}

    return ProviderStatusEvent(
        provider_message_id=identifier,
        status=mapped,
        occurred_at=_timestamp(status.get("timestamp")),
        error_code=str(first_error["code"]) if "code" in first_error else None,
        error_detail=_error_detail(first_error),
    )


def _error_detail(error: Mapping[str, Any]) -> str | None:
    """The provider's own explanation, bounded.

    ⚠️ Reaches ``message_dispatches.failure_reason``, which operators read. It is
    truncated here rather than trusted to be short.
    """
    for key in ("error_data", "title", "message"):
        value = error.get(key)
        if isinstance(value, Mapping):
            nested = value.get("details")
            if isinstance(nested, str) and nested:
                return nested[:500]
        elif isinstance(value, str) and value:
            return value[:500]
    return None


def _timestamp(raw: Any) -> datetime:
    """Meta sends seconds since the epoch, as a string.

    ⚠️ Falls back to *now* rather than raising. A receipt with an unreadable
    timestamp is still a real status change, and losing it to a parse error would
    leave the delivery log claiming a message was never delivered.
    """
    try:
        return datetime.fromtimestamp(int(str(raw)), tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, list) else []


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}
