"""The notifications port — FR-M0-041/042/043.

Interface only at S1: no adapters, no transports, no message tables. These tests
assert the *contract* every future transport must fit, which is the whole
deliverable of this half of the slice.

🔒 The consent tests are the ones with teeth. A transport is bound to a consent
purpose, and getting that mapping wrong sends a marketing message on the basis of
a permission the client gave for appointment reminders — a compliance finding
rather than a UX liberty.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.kernel.errors import ValidationError
from app.kernel.models import TransportType
from app.kernel.notifications import (
    DeliveryRecord,
    DeliveryStatus,
    MessageCategory,
    Notification,
    NotificationTransport,
    Recipient,
    purpose_for,
)

_TENANT = uuid.uuid4()


def _notification(
    *,
    category: MessageCategory = MessageCategory.TRANSACTIONAL,
    transports: tuple[TransportType, ...] = (TransportType.WHATSAPP,),
    template_code: str = "plan_ready",
) -> Notification:
    return Notification(
        tenant_id=_TENANT,
        recipient=Recipient(address="+919876543210"),
        template_code=template_code,
        category=category,
        transports=transports,
    )


# ─── Consent binding (FR-M0-022, DPDP) ───────────────────────────────────


def test_whatsapp_requires_whatsapp_consent() -> None:
    """A channel the client has not consented to is not available."""
    assert (
        purpose_for(TransportType.WHATSAPP, MessageCategory.TRANSACTIONAL)
        == "whatsapp_communication"
    )


def test_marketing_requires_marketing_consent_on_every_transport() -> None:
    """🔒 The case that matters.

    Consent to be contacted on WhatsApp about your plan is not consent to be
    advertised to. Treating the channel permission as sufficient is precisely the
    dark pattern the DPDP consent model exists to prevent.
    """
    for transport in TransportType:
        assert purpose_for(transport, MessageCategory.MARKETING) == "marketing"


def test_reminders_require_reminder_consent() -> None:
    """FR-M0-029 — a distinct purpose, separately withdrawable."""
    for transport in TransportType:
        assert purpose_for(transport, MessageCategory.REMINDER) == "appointment_reminders"


def test_every_transport_has_a_consent_purpose() -> None:
    """🔒 A transport with no purpose would be consent-free by omission — the one
    way a channel escapes the consent model entirely."""
    for transport in TransportType:
        assert purpose_for(transport, MessageCategory.TRANSACTIONAL)


def test_marketing_purpose_matches_the_seeded_catalogue() -> None:
    """The purpose codes must exist in migration 0006, or the consent lookup at
    send time finds nothing and the send is refused for the wrong reason."""
    seeded = {
        "service_delivery",
        "clinical_records",
        "plan_delivery",
        "progress_tracking",
        "whatsapp_communication",
        "appointment_reminders",
        "marketing",
    }
    for transport in TransportType:
        for category in MessageCategory:
            assert purpose_for(transport, category) in seeded


# ─── The notification ────────────────────────────────────────────────────


def test_a_notification_carries_a_template_not_a_body() -> None:
    """🔒 WhatsApp delivers pre-approved templates only.

    A composed body would be rejected by the transport per-recipient at send
    time, which surfaces in production one client at a time. The dataclass has no
    body field at all — the strongest form of this guarantee.
    """
    notification = _notification()
    assert notification.template_code == "plan_ready"
    assert not hasattr(notification, "body")


def test_a_notification_without_a_template_is_refused() -> None:
    with pytest.raises(ValidationError):
        _notification(template_code="")


def test_a_notification_without_a_transport_is_refused() -> None:
    """🔒 An empty preference list is silently "never delivered"."""
    with pytest.raises(ValidationError):
        _notification(transports=())


def test_a_recipient_without_an_address_is_refused() -> None:
    for address in ("", "   "):
        with pytest.raises(ValidationError):
            Recipient(address=address)


def test_primary_transport_is_the_first_preference() -> None:
    """FR-M0-043 fallback is Phase 2, so only the first is attempted at MVP."""
    notification = _notification(transports=(TransportType.WHATSAPP, TransportType.SMS))
    assert notification.primary_transport is TransportType.WHATSAPP


def test_required_purpose_follows_the_primary_transport_and_category() -> None:
    notification = _notification(
        category=MessageCategory.MARKETING, transports=(TransportType.EMAIL,)
    )
    assert notification.required_purpose() == "marketing"


def test_transports_are_ordered_so_fallback_can_be_added_without_churn() -> None:
    """⚠️ A tuple, not a single field. Retrofitting fallback onto a scalar would
    touch every call site in the codebase."""
    notification = _notification(transports=(TransportType.WHATSAPP, TransportType.SMS))
    assert notification.transports == (TransportType.WHATSAPP, TransportType.SMS)


# ─── The delivery record (FR-M0-042) ─────────────────────────────────────


def _record(**overrides: object) -> DeliveryRecord:
    base: dict[str, object] = {
        "tenant_id": _TENANT,
        "transport": TransportType.WHATSAPP,
        "template_code": "plan_ready",
        "recipient_address": "+919876543210",
        "status": DeliveryStatus.SENT,
        "category": MessageCategory.TRANSACTIONAL,
        "occurred_at": datetime(2026, 8, 8, tzinfo=UTC),
    }
    base.update(overrides)
    return DeliveryRecord(**base)  # type: ignore[arg-type]


def test_a_record_captures_everything_fr_m0_042_requires() -> None:
    """Transport, template, recipient, status and failure reason."""
    record = _record(status=DeliveryStatus.FAILED, failure_reason="rate limited")
    assert record.transport is TransportType.WHATSAPP
    assert record.template_code
    assert record.recipient_address
    assert record.status is DeliveryStatus.FAILED
    assert record.failure_reason


def test_a_failed_delivery_must_say_why() -> None:
    """🔒 A failure with no reason cannot be acted on, and the reason is the
    entire point of recording the failure."""
    with pytest.raises(ValidationError):
        _record(status=DeliveryStatus.FAILED, failure_reason=None)


def test_suppressed_is_distinct_from_failed() -> None:
    """🔒 Refused before dispatch — no consent, or opted out.

    Nothing went wrong, and retrying is exactly the wrong response. A record that
    collapsed the two would have a retry loop hammering a client who withdrew
    consent.
    """
    record = _record(status=DeliveryStatus.SUPPRESSED)
    assert record.status is DeliveryStatus.SUPPRESSED
    assert record.failure_reason is None


def test_queued_is_distinct_from_sent() -> None:
    """The only window in which a stuck queue is visible."""
    assert DeliveryStatus.QUEUED is not DeliveryStatus.SENT


# ─── The port ────────────────────────────────────────────────────────────


def test_the_transport_protocol_is_the_only_send_surface() -> None:
    """🔒 FR-M0-041 — one abstraction. A conforming stub satisfies it without
    importing any provider SDK, which is what makes the protocol substitutable."""

    class _Stub:
        @property
        def transport(self) -> TransportType:
            return TransportType.SMS

        async def send(self, notification: Notification) -> DeliveryRecord:
            return _record(transport=TransportType.SMS)

    stub: NotificationTransport = _Stub()
    assert stub.transport is TransportType.SMS


def test_no_adapter_ships_in_s1() -> None:
    """🔒 The sprint plan says "Interface only; no adapters yet".

    An HTTP client against an unprovisioned provider would assert only my
    assumptions about its contract. This test is what notices if one appears
    without the contract test that must accompany it.
    """
    from pathlib import Path

    integrations = Path(__file__).resolve().parents[1] / "app" / "integrations"
    modules = [p.name for p in integrations.glob("*.py") if p.name != "__init__.py"]
    assert modules == [], (
        f"an integration adapter appeared in S1: {modules}. FR-M0-041 allows this, "
        "but it must arrive with a contract test against a provisioned provider."
    )
