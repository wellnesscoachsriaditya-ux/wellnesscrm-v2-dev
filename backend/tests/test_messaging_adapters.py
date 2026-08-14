"""The three transports, against fixtures — no network, no mail server.

⚠️ 🔒 **These are contract tests, not evidence of delivery.** They assert that
the adapter translates our vocabulary into the provider's correctly and reports
what came back. What they cannot show — and the S5 report says so plainly — is
that Meta accepts the payload, because Meta Business Verification has not been
granted and no message has been sent to a real number.

That limitation is the reason the fixtures are recorded shapes from Meta's
documented Cloud API responses rather than invented ones: a fixture I made up
would test only my assumptions, which is what deferring the adapter in S1 was
meant to avoid.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from app.integrations.messaging import (
    EmailTransport,
    LoggedTransport,
    SmtpSettings,
    WhatsAppSettings,
    WhatsAppTransport,
    parse_status_events,
    verify_signature,
)
from app.integrations.messaging.email import _subject_for
from app.kernel.models import TransportType
from app.kernel.notifications import (
    DeliveryStatus,
    MessageCategory,
    Notification,
    Recipient,
)

TENANT = uuid.uuid4()

#: 🔒 The documented Cloud API success shape. The `wamid.` prefix is Meta's own;
#: `uq_message_dispatches__provider_id` and the webhook both key on this value.
_SEND_RESPONSE: dict[str, Any] = {
    "messaging_product": "whatsapp",
    "contacts": [{"input": "919876543210", "wa_id": "919876543210"}],
    "messages": [{"id": "wamid.HBgMOTE5ODc2NTQzMjEwFQIAERgSMEE="}],
}

#: A recorded status callback, with the two shapes that matter: a `delivered`
#: for one message and a `failed` carrying an error for another.
_STATUS_PAYLOAD: dict[str, Any] = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "102290129340398",
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"phone_number_id": "106540352242922"},
                        "statuses": [
                            {
                                "id": "wamid.delivered",
                                "status": "delivered",
                                "timestamp": "1755000000",
                                "recipient_id": "919876543210",
                            },
                            {
                                "id": "wamid.failed",
                                "status": "failed",
                                "timestamp": "1755000060",
                                "recipient_id": "919876543211",
                                "errors": [
                                    {
                                        "code": 131026,
                                        "title": "Message undeliverable",
                                        "error_data": {
                                            "details": "Receiver is incapable of receiving"
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                }
            ],
        }
    ],
}

#: 🔒 The reply shape. EC-M8-07 — MVP does not process these.
_REPLY_PAYLOAD: dict[str, Any] = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "102290129340398",
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "messages": [
                            {
                                "from": "919876543210",
                                "id": "wamid.inbound",
                                "timestamp": "1755000120",
                                "text": {"body": "thank you!"},
                                "type": "text",
                            }
                        ],
                    },
                }
            ],
        }
    ],
}


def _notification(**overrides: Any) -> Notification:
    defaults: dict[str, Any] = {
        "tenant_id": TENANT,
        "recipient": Recipient(address="+919876543210"),
        "template_code": "plan_delivered",
        "category": MessageCategory.TRANSACTIONAL,
        "transports": (TransportType.WHATSAPP,),
        "variables": {"client_name": "Anjali", "plan_url": "https://example.invalid/p/1"},
        "provider_template_name": "plan_delivered_v1",
        "body": "Hi Anjali, your plan is at https://example.invalid/p/1",
        "dispatch_id": uuid.uuid4(),
    }
    defaults.update(overrides)
    return Notification(**defaults)


def _whatsapp(response: Mapping[str, Any] | Exception) -> WhatsAppTransport:
    async def post(
        _url: str, _payload: Mapping[str, Any], _headers: Mapping[str, str], _timeout: int
    ) -> Mapping[str, Any]:
        if isinstance(response, Exception):
            raise response
        return response

    return WhatsAppTransport(
        WhatsAppSettings(phone_number_id="106540352242922", access_token="token"), post=post
    )


# ─── The logged transport ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_logged_transport_accepts_and_sends_nothing() -> None:
    record = await LoggedTransport().send(_notification())
    assert record.status is DeliveryStatus.SENT
    assert record.transport is TransportType.LOGGED


@pytest.mark.asyncio
async def test_the_logged_transport_issues_no_provider_id() -> None:
    """🔒 So no delivery receipt can ever arrive for it, and nothing in the
    delivery log can be mistaken for a real WhatsApp delivery."""
    record = await LoggedTransport().send(_notification())
    assert record.provider_message_id is None


# ─── The email transport ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_email_sends_the_rendered_body(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Any] = []
    transport = EmailTransport(
        SmtpSettings(host="localhost", port=587, from_address="practice@example.test")
    )
    monkeypatch.setattr(transport, "_deliver", sent.append)

    record = await transport.send(_notification())

    assert record.status is DeliveryStatus.SENT
    assert record.provider_message_id is not None
    assert sent[0]["To"] == "+919876543210"
    assert sent[0].get_content().strip() == _notification().body


@pytest.mark.asyncio
async def test_email_refuses_an_empty_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔒 A template configuration fault, visible as one — rather than a blank
    email arriving at a client under their practitioner's name."""
    transport = EmailTransport(
        SmtpSettings(host="localhost", port=587, from_address="practice@example.test")
    )
    monkeypatch.setattr(transport, "_deliver", lambda _message: None)

    record = await transport.send(_notification(body=None))
    assert record.status is DeliveryStatus.FAILED
    assert record.failure_reason is not None


@pytest.mark.asyncio
async def test_email_reports_a_server_failure_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔒 The port's contract: a mail server being down is an operational state,
    and the dispatch engine needs the outcome in the delivery log before it
    decides whether to retry."""

    def explode(_message: Any) -> None:
        raise OSError("connection refused")

    transport = EmailTransport(
        SmtpSettings(host="localhost", port=587, from_address="practice@example.test")
    )
    monkeypatch.setattr(transport, "_deliver", explode)

    record = await transport.send(_notification())
    assert record.status is DeliveryStatus.FAILED
    assert "connection refused" in (record.failure_reason or "")


def test_every_mvp_template_has_a_neutral_subject() -> None:
    """⚠️ A subject line is copied into every intermediate mail server's logs, so
    it must say enough to be opened and nothing about the person's health."""
    for code in (
        "plan_delivered",
        "appointment_confirmed",
        "appointment_reminder",
        "checkin_nudge",
        "assessment_invitation",
        "lead_acknowledgement",
        "lead_notification",
        "magic_link",
    ):
        subject = _subject_for(code)
        assert subject and code not in subject


def test_an_unknown_template_gets_a_generic_subject() -> None:
    """⚠️ Deliberately not the template code: `assessment_invitation` in a
    subject line discloses what stage of care the recipient is at."""
    assert "unmapped_code" not in _subject_for("unmapped_code")


# ─── The WhatsApp transport ──────────────────────────────────────────────


def test_the_endpoint_is_the_configured_phone_number() -> None:
    assert _whatsapp(_SEND_RESPONSE).endpoint() == (
        "https://graph.facebook.com/v21.0/106540352242922/messages"
    )


def test_the_payload_is_a_template_message() -> None:
    """🔒 Template messages only. Free text is rejected per recipient at send
    time, which is a failure discovered in production one client at a time."""
    payload = _whatsapp(_SEND_RESPONSE).build_payload(_notification())

    assert payload["type"] == "template"
    assert payload["template"]["name"] == "plan_delivered_v1"
    assert payload["template"]["language"] == {"code": "en"}
    # ⚠️ Positional, in sorted variable order — the coupling the adapter's
    # docstring names, asserted so a change to it fails here rather than at a
    # client's handset.
    assert [p["text"] for p in payload["template"]["components"][0]["parameters"]] == [
        "Anjali",
        "https://example.invalid/p/1",
    ]


def test_the_recipient_loses_its_plus() -> None:
    """Our storage is E.164 with the ``+``; Meta expects a bare number."""
    assert _whatsapp(_SEND_RESPONSE).build_payload(_notification())["to"] == "919876543210"


@pytest.mark.asyncio
async def test_a_successful_send_captures_the_provider_message_id() -> None:
    record = await _whatsapp(_SEND_RESPONSE).send(_notification())
    assert record.status is DeliveryStatus.SENT
    assert record.provider_message_id == _SEND_RESPONSE["messages"][0]["id"]


@pytest.mark.asyncio
async def test_a_response_without_a_message_id_is_a_failure() -> None:
    """⚠️ The webhook correlation depends on this value, so a shape change must
    be a recorded failure rather than a `KeyError` inside a job."""
    record = await _whatsapp({"messaging_product": "whatsapp"}).send(_notification())
    assert record.status is DeliveryStatus.FAILED


@pytest.mark.asyncio
async def test_a_template_with_no_provider_name_is_refused_before_the_call() -> None:
    record = await _whatsapp(_SEND_RESPONSE).send(_notification(provider_template_name=None))
    assert record.status is DeliveryStatus.FAILED
    assert "WhatsApp" in (record.failure_reason or "")


@pytest.mark.asyncio
async def test_a_4xx_is_reported_as_rejected() -> None:
    """🔒 The wording is load-bearing: `dispatch._failure_code` classifies a
    "reject" as non-retryable, and retrying a rejected template buys an identical
    failure per attempt."""
    error = urllib.error.HTTPError(
        url="https://graph.facebook.com",
        code=400,
        msg="Bad Request",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )
    record = await _whatsapp(error).send(_notification())
    assert record.status is DeliveryStatus.FAILED
    assert "rejected" in (record.failure_reason or "")


@pytest.mark.asyncio
async def test_a_network_failure_is_reported_not_raised() -> None:
    record = await _whatsapp(urllib.error.URLError("no route to host")).send(_notification())
    assert record.status is DeliveryStatus.FAILED
    assert "URLError" in (record.failure_reason or "")


@pytest.mark.asyncio
async def test_missing_credentials_are_a_deployment_decision_not_a_send_failure() -> None:
    """🔒 An adapter is registered only when its credentials are present, so a
    process with no token has no WhatsApp transport at all — rather than one that
    fails every message. Asserted through the wiring, which is where the decision
    lives."""
    from app.platform.config import Settings
    from app.platform.messaging_wiring import build_transports

    transports = build_transports(Settings(whatsapp_phone_number_id=None))
    assert TransportType.WHATSAPP not in transports
    assert TransportType.LOGGED in transports


# ─── Webhook signature (API §11.3 rule 1) ────────────────────────────────


def _signed(body: bytes, secret: str = "app-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_a_valid_signature_is_accepted() -> None:
    body = json.dumps(_STATUS_PAYLOAD).encode()
    assert verify_signature(secret="app-secret", body=body, header=_signed(body))


def test_a_signature_over_different_bytes_is_refused() -> None:
    """🔒 Computed over the raw body: JSON round-tripping changes key order and
    whitespace, and the signature is over the bytes the provider sent."""
    body = json.dumps(_STATUS_PAYLOAD).encode()
    assert not verify_signature(secret="app-secret", body=body + b" ", header=_signed(body))


def test_a_wrong_secret_is_refused() -> None:
    body = b"{}"
    assert not verify_signature(secret="app-secret", body=body, header=_signed(body, "other"))


@pytest.mark.parametrize("header", [None, "", "sha1=abc", "abc", "sha256="])
def test_a_missing_or_malformed_signature_is_refused(header: str | None) -> None:
    """⚠️ Returns False rather than raising: the response is 401 either way, and
    an exception would make a malformed header a 500."""
    assert not verify_signature(secret="app-secret", body=b"{}", header=header)


def test_no_configured_secret_refuses_everything() -> None:
    """🔒 Fails closed. An unverified webhook is an unauthenticated write
    endpoint, and this one can move a delivery status."""
    assert not verify_signature(secret="", body=b"{}", header=_signed(b"{}"))


# ─── Webhook parsing (EC-M8-07) ──────────────────────────────────────────


def test_every_delivery_status_is_parsed() -> None:
    events = parse_status_events(_STATUS_PAYLOAD)
    assert [(e.provider_message_id, e.status) for e in events] == [
        ("wamid.delivered", "delivered"),
        ("wamid.failed", "failed"),
    ]


def test_a_failure_carries_the_provider_code_and_detail() -> None:
    """EC-M8-01/EC-M8-03 both depend on this reaching the practitioner-facing
    failure reason."""
    failed = parse_status_events(_STATUS_PAYLOAD)[1]
    assert failed.error_code == "131026"
    assert failed.error_detail == "Receiver is incapable of receiving"


def test_the_timestamp_is_the_provider_s() -> None:
    delivered = parse_status_events(_STATUS_PAYLOAD)[0]
    assert delivered.occurred_at == datetime.fromtimestamp(1755000000, tz=UTC)


def test_an_inbound_reply_yields_nothing() -> None:
    """🔒 EC-M8-07 — replies reach the practitioner's own WhatsApp. Accepted and
    discarded, so Meta does not mark the webhook unhealthy; the two-way inbox is
    Phase 2 (FR-M8-030)."""
    assert parse_status_events(_REPLY_PAYLOAD) == []


def test_an_unknown_status_is_skipped_rather_than_guessed() -> None:
    """⚠️ Guessing where it sits in the lifecycle could walk a row backwards."""
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [{"id": "wamid.x", "status": "warping", "timestamp": "1"}]
                        }
                    }
                ]
            }
        ]
    }
    assert parse_status_events(payload) == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"entry": "not-a-list"},
        {"entry": [{"changes": [{"value": {"statuses": [{"id": 5, "status": "sent"}]}}]}]},
        {"entry": [{"changes": [{"value": {"statuses": ["nonsense"]}}]}]},
    ],
)
def test_a_malformed_payload_produces_no_events(payload: dict[str, Any]) -> None:
    """⚠️ This parses attacker-reachable JSON that has passed a signature check;
    a malformed shape must produce nothing rather than an exception inside a
    request handler."""
    assert parse_status_events(payload) == []
