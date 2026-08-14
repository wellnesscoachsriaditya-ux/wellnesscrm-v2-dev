"""Transport selection and the check-in cadence — pure decisions, no database.

🔒 Two rules live here and both are the kind that must be provably right:

* which transport carries a message when a deployment cannot serve the one the
  template asked for (the substitution that makes S5 shippable without Meta);
* when the next check-in falls (FR-M8-022/023).
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.kernel.clients import ContactDetails
from app.kernel.messaging import CheckinFrequency, MessageCategory, ProviderTemplateStatus
from app.kernel.models import TransportType
from app.modules.messaging import Template, next_occurrence
from app.modules.messaging.transports import address_for, choose

ALL = frozenset({TransportType.WHATSAPP, TransportType.EMAIL, TransportType.LOGGED})
NO_WHATSAPP = frozenset({TransportType.EMAIL, TransportType.LOGGED})
LOGGED_ONLY = frozenset({TransportType.LOGGED})


def _template(transport: TransportType = TransportType.WHATSAPP) -> Template:
    return Template(
        id=uuid.uuid4(),
        code="plan_delivered",
        version=1,
        category=MessageCategory.TRANSACTIONAL,
        is_essential=False,
        priority=10,
        staleness_tolerance_minutes=None,
        variables={},
        body_template="body",
        default_transport=transport,
        provider_template_name="plan_delivered_v1",
        provider_template_status=ProviderTemplateStatus.APPROVED,
        is_practitioner_disableable=False,
    )


BOTH = ContactDetails(mobile="+919876543210", email="anjali@example.test")
MOBILE_ONLY = ContactDetails(mobile="+919876543210")
EMAIL_ONLY = ContactDetails(email="anjali@example.test")


# ─── Transport selection ─────────────────────────────────────────────────


def test_the_template_transport_wins_when_it_is_configured() -> None:
    choice = choose(template=_template(), contact=BOTH, available=ALL)
    assert choice.transport is TransportType.WHATSAPP
    assert choice.address == BOTH.mobile
    assert choice.substituted is False


def test_an_unconfigured_transport_substitutes_email() -> None:
    """🔒 The state S5 ships in. Meta verification is pending, so a WhatsApp
    template runs on whatever this deployment does have — recorded on the
    dispatch row, so the delivery log never claims a WhatsApp delivery."""
    choice = choose(template=_template(), contact=BOTH, available=NO_WHATSAPP)
    assert choice.transport is TransportType.EMAIL
    assert choice.address == BOTH.email
    assert choice.substituted is True


def test_substitution_falls_through_to_logged() -> None:
    choice = choose(template=_template(), contact=BOTH, available=LOGGED_ONLY)
    assert choice.transport is TransportType.LOGGED
    assert choice.substituted is True


def test_substitution_never_picks_a_transport_the_client_cannot_receive_on() -> None:
    """EC-M1-08 — a client with only a mobile is not reachable by email."""
    choice = choose(template=_template(), contact=MOBILE_ONLY, available=NO_WHATSAPP)
    assert choice.transport is TransportType.LOGGED
    assert choice.address == MOBILE_ONLY.mobile


def test_no_usable_address_reports_the_transport_that_was_attempted() -> None:
    """🔒 A failure row naming `logged` because that happened to be configured
    would misreport the outage."""
    choice = choose(
        template=_template(), contact=EMAIL_ONLY, available=frozenset({TransportType.WHATSAPP})
    )
    assert choice.transport is TransportType.WHATSAPP
    assert choice.address == ""


def test_a_preference_overrides_the_template_default() -> None:
    choice = choose(
        template=_template(), contact=BOTH, available=ALL, preferred=TransportType.EMAIL
    )
    assert choice.transport is TransportType.EMAIL
    assert choice.substituted is False


def test_a_preference_for_an_unconfigured_transport_is_a_stale_setting() -> None:
    """⚠️ Not an instruction to fail: the template's own transport is used."""
    choice = choose(
        template=_template(TransportType.EMAIL),
        contact=BOTH,
        available=NO_WHATSAPP,
        preferred=TransportType.WHATSAPP,
    )
    assert choice.transport is TransportType.EMAIL


def test_the_logged_transport_still_records_a_real_address() -> None:
    """🔒 A logged row with an empty address proves nothing."""
    assert address_for(TransportType.LOGGED, EMAIL_ONLY) == EMAIL_ONLY.email
    assert address_for(TransportType.LOGGED, MOBILE_ONLY) == MOBILE_ONLY.mobile


def test_sms_is_absent_from_the_substitution_order() -> None:
    """🔒 Approved proposal #7 — TRAI DLT registration is off the critical path,
    so there is no SMS adapter to substitute to."""
    choice = choose(template=_template(), contact=BOTH, available=frozenset({TransportType.SMS}))
    assert choice.transport is TransportType.WHATSAPP
    assert choice.address == ""


# ─── Check-in cadence (FR-M8-022/023) ────────────────────────────────────


@pytest.mark.parametrize(
    ("frequency", "expected"),
    [
        (CheckinFrequency.WEEKLY, date(2026, 8, 21)),
        (CheckinFrequency.FORTNIGHTLY, date(2026, 8, 28)),
        (CheckinFrequency.MONTHLY, date(2026, 9, 11)),
    ],
)
def test_the_cadence_steps_by_its_own_interval(frequency: CheckinFrequency, expected: date) -> None:
    """⚠️ Monthly is 28 days: a check-in is a cadence, not a billing date, and
    "the 31st" has no meaning in February."""
    assert (
        next_occurrence(after=date(2026, 8, 14), frequency=frequency, day_of_week=None) == expected
    )


def test_a_weekly_cadence_lands_on_the_chosen_weekday() -> None:
    """FR-M8-023 — the weekday the client became active. 14 August 2026 is a
    Friday; asking for Monday (1) moves the next occurrence to the 24th."""
    assert next_occurrence(
        after=date(2026, 8, 14), frequency=CheckinFrequency.WEEKLY, day_of_week=1
    ) == date(2026, 8, 24)


def test_a_weekly_cadence_on_the_same_weekday_stays_seven_days_out() -> None:
    assert next_occurrence(
        after=date(2026, 8, 14), frequency=CheckinFrequency.WEEKLY, day_of_week=5
    ) == date(2026, 8, 21)


def test_the_weekday_is_ignored_for_longer_cadences() -> None:
    """⚠️ Honouring it would mean "every 14 days, but on a Tuesday" — either 14
    days or 21 depending on where the arithmetic lands, which silently skips a
    fortnight twice a year."""
    assert next_occurrence(
        after=date(2026, 8, 14), frequency=CheckinFrequency.FORTNIGHTLY, day_of_week=1
    ) == date(2026, 8, 28)
