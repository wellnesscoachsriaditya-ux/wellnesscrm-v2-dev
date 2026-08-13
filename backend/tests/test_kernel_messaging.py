"""Tests for the messaging kernel — S5, M8.

🔒 The suppression rules, quiet-hours deferral and idempotency key are the parts
of the messaging engine that must be provably right, and they are pure functions,
so they are proved here without a database. An integration test can show a rule
fired once; these show it fires by construction.
"""

from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

import pytest

from app.kernel.messaging import (
    DEFAULT_MAX_MESSAGES_PER_WEEK,
    DEFAULT_QUIET_HOURS_END,
    DEFAULT_QUIET_HOURS_START,
    CheckinFrequency,
    MessageCategory,
    ProviderTemplateStatus,
    RecipientPolicy,
    SuppressionReason,
    TemplatePolicy,
    TransportType,
    build_idempotency_key,
    defer_past_quiet_hours,
    evaluate_suppression,
    is_stale,
    is_within_quiet_hours,
    next_checkin_due,
)


def _template(
    *,
    essential: bool = False,
    status: ProviderTemplateStatus = ProviderTemplateStatus.APPROVED,
    staleness: int | None = None,
) -> TemplatePolicy:
    return TemplatePolicy(
        code="checkin_due",
        is_essential=essential,
        priority=50,
        staleness_tolerance_minutes=staleness,
        provider_template_status=status,
        category=MessageCategory.REMINDER,
    )


def _recipient(
    *,
    client_active: bool = True,
    consent: bool = True,
    tenant_active: bool = True,
    muted: bool = False,
    sent_this_week: int = 0,
    cap: int = DEFAULT_MAX_MESSAGES_PER_WEEK,
    quota: int | None = None,
) -> RecipientPolicy:
    return RecipientPolicy(
        client_is_active=client_active,
        consent_granted=consent,
        tenant_is_active=tenant_active,
        type_is_muted=muted,
        messages_sent_this_week=sent_this_week,
        max_messages_per_week=cap,
        quota_remaining=quota,
    )


# ─── Idempotency — EC-M8-06 ──────────────────────────────────────────────


def test_the_same_occasion_produces_the_same_key():
    """🔒 A retry must resolve to the row the first attempt created."""
    tenant = uuid4()
    args = {
        "tenant_id": tenant,
        "recipient": "+919876543210",
        "template_code": "checkin_due",
        "occasion": "2026-W32",
    }

    assert build_idempotency_key(**args) == build_idempotency_key(**args)


@pytest.mark.parametrize(
    "field,value",
    [
        ("recipient", "+919999999999"),
        ("template_code", "plan_delivered"),
        ("occasion", "2026-W33"),
    ],
)
def test_a_different_occasion_produces_a_different_key(field: str, value: str):
    """Two genuinely different messages must not collide into one row."""
    base = {
        "tenant_id": uuid4(),
        "recipient": "+919876543210",
        "template_code": "checkin_due",
        "occasion": "2026-W32",
    }

    assert build_idempotency_key(**base) != build_idempotency_key(**{**base, field: value})


def test_the_key_is_tenant_scoped():
    """Two tenants messaging the same number on the same occasion are distinct."""
    args = {
        "recipient": "+919876543210",
        "template_code": "checkin_due",
        "occasion": "2026-W32",
    }

    assert build_idempotency_key(tenant_id=uuid4(), **args) != build_idempotency_key(
        tenant_id=uuid4(), **args
    )


def test_a_separator_in_the_recipient_cannot_forge_a_collision():
    """🔒 Hashed rather than concatenated, so no input can impersonate a tuple."""
    tenant = uuid4()

    first = build_idempotency_key(tenant_id=tenant, recipient="a", template_code="b", occasion="c")
    second = build_idempotency_key(
        tenant_id=tenant, recipient="a\x1fb", template_code="", occasion="c"
    )

    assert first != second


# ─── Suppression — the seven rules, each independently ───────────────────


def test_nothing_is_suppressed_when_every_rule_is_satisfied():
    """The control. Without it, every assertion below could be a false pass."""
    assert evaluate_suppression(_template(), _recipient()) is None


def test_a_suspended_tenant_suppresses():
    assert (
        evaluate_suppression(_template(), _recipient(tenant_active=False))
        is SuppressionReason.TENANT_SUSPENDED
    )


def test_withdrawn_consent_suppresses():
    assert (
        evaluate_suppression(_template(), _recipient(consent=False))
        is SuppressionReason.CONSENT_WITHDRAWN
    )


def test_an_inactive_client_suppresses():
    """🔒 FR-M8-005 — a paused client stops receiving nudges."""
    assert (
        evaluate_suppression(_template(), _recipient(client_active=False))
        is SuppressionReason.CLIENT_STAGE_INACTIVE
    )


@pytest.mark.parametrize(
    "status",
    [
        ProviderTemplateStatus.PENDING,
        ProviderTemplateStatus.REJECTED,
        ProviderTemplateStatus.PAUSED,
    ],
)
def test_an_unapproved_template_suppresses(status: ProviderTemplateStatus):
    """🔒 EC-M8-03 — per template, so one rejection is not a total outage."""
    assert (
        evaluate_suppression(_template(status=status), _recipient())
        is SuppressionReason.TEMPLATE_PAUSED
    )


def test_a_muted_message_type_suppresses():
    assert (
        evaluate_suppression(_template(), _recipient(muted=True))
        is SuppressionReason.CLIENT_UNSUBSCRIBED
    )


def test_an_exhausted_quota_suppresses():
    assert (
        evaluate_suppression(_template(), _recipient(quota=0)) is SuppressionReason.QUOTA_EXCEEDED
    )


def test_the_weekly_frequency_cap_suppresses():
    """EC-M8-09 — the cap counts across all message types, not per type."""
    assert (
        evaluate_suppression(_template(), _recipient(sent_this_week=7, cap=7))
        is SuppressionReason.FREQUENCY_CAPPED
    )


def test_one_message_below_the_cap_is_allowed():
    assert evaluate_suppression(_template(), _recipient(sent_this_week=6, cap=7)) is None


# ─── Essential messages — DB §11.6, EC-M10-04 ────────────────────────────


def test_an_essential_template_bypasses_quota_and_frequency():
    """🔒 A client must never be locked out of their portal by a nudge quota."""
    exhausted = _recipient(quota=0, sent_this_week=99, cap=7)

    assert evaluate_suppression(_template(essential=True), exhausted) is None


@pytest.mark.parametrize(
    "recipient,expected",
    [
        (_recipient(consent=False), SuppressionReason.CONSENT_WITHDRAWN),
        (_recipient(client_active=False), SuppressionReason.CLIENT_STAGE_INACTIVE),
        (_recipient(tenant_active=False), SuppressionReason.TENANT_SUSPENDED),
        (_recipient(muted=True), SuppressionReason.CLIENT_UNSUBSCRIBED),
    ],
)
def test_an_essential_template_does_not_bypass_consent_or_stage(
    recipient: RecipientPolicy, expected: SuppressionReason
):
    """🔒 The exemption is quota and frequency **only**.

    A magic link to someone who withdrew consent is still a message they said
    they did not want, and sending it would be a lawful-basis failure rather
    than a convenience.
    """
    assert evaluate_suppression(_template(essential=True), recipient) is expected


def test_the_most_specific_reason_wins():
    """Support asks "why didn't it send?" — "consent withdrawn" beats "capped"."""
    everything_wrong = _recipient(
        tenant_active=False, consent=False, client_active=False, sent_this_week=99
    )

    assert evaluate_suppression(_template(), everything_wrong) is SuppressionReason.TENANT_SUSPENDED


# ─── Quiet hours — AC-M8-006 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "moment,inside",
    [
        (time(2, 0), True),
        (time(22, 30), True),
        (time(21, 0), True),
        (time(7, 59), True),
        (time(8, 0), False),
        (time(13, 0), False),
        (time(20, 59), False),
    ],
)
def test_the_default_window_wraps_midnight(moment: time, inside: bool):
    assert (
        is_within_quiet_hours(moment, start=DEFAULT_QUIET_HOURS_START, end=DEFAULT_QUIET_HOURS_END)
        is inside
    )


def test_a_non_wrapping_window_is_handled():
    assert is_within_quiet_hours(time(13, 30), start=time(13, 0), end=time(14, 0)) is True
    assert is_within_quiet_hours(time(15, 0), start=time(13, 0), end=time(14, 0)) is False


def test_an_empty_window_never_suppresses():
    assert is_within_quiet_hours(time(3, 0), start=time(8, 0), end=time(8, 0)) is False


def test_a_message_due_at_two_am_defers_to_eight():
    """🔒 AC-M8-006 — deferred, not dropped."""
    due = datetime(2026, 8, 14, 2, 0, tzinfo=UTC)

    assert defer_past_quiet_hours(due) == datetime(2026, 8, 14, 8, 0, tzinfo=UTC)


def test_a_message_due_late_at_night_defers_to_the_next_morning():
    """22:30 is *before* midnight, so the window ends on the following day."""
    due = datetime(2026, 8, 14, 22, 30, tzinfo=UTC)

    assert defer_past_quiet_hours(due) == datetime(2026, 8, 15, 8, 0, tzinfo=UTC)


def test_a_message_outside_quiet_hours_is_returned_unchanged():
    """Identity, so a caller can tell whether to record `deferred_from`."""
    due = datetime(2026, 8, 14, 13, 0, tzinfo=UTC)

    assert defer_past_quiet_hours(due) is due


def test_a_naive_datetime_is_refused():
    """⚠️ Guessing a zone would move a message to the wrong side of a client's night."""
    with pytest.raises(ValueError, match="timezone-aware"):
        defer_past_quiet_hours(datetime(2026, 8, 14, 2, 0))


# ─── Staleness — EC-M8-05 ────────────────────────────────────────────────


def test_a_message_within_tolerance_is_not_stale():
    now = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)

    assert is_stale(now - timedelta(minutes=30), now=now, tolerance_minutes=60) is False


def test_a_message_past_tolerance_is_stale():
    """A Friday check-in delivered on Sunday says nobody is paying attention."""
    now = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)

    assert is_stale(now - timedelta(hours=48), now=now, tolerance_minutes=60) is True


def test_a_template_with_no_tolerance_never_goes_stale():
    """🔒 A plan delivery is still wanted late — NULL means never stale."""
    now = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)

    assert is_stale(now - timedelta(days=30), now=now, tolerance_minutes=None) is False


def test_exactly_at_the_tolerance_boundary_is_not_yet_stale():
    now = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)

    assert is_stale(now - timedelta(minutes=60), now=now, tolerance_minutes=60) is False


# ─── Check-in cadence — FR-M8-022 ────────────────────────────────────────


@pytest.mark.parametrize(
    "frequency,days",
    [
        (CheckinFrequency.WEEKLY, 7),
        (CheckinFrequency.FORTNIGHTLY, 14),
        (CheckinFrequency.MONTHLY, 28),
    ],
)
def test_the_next_check_in_advances_by_the_cadence(frequency: CheckinFrequency, days: int):
    last = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)

    assert next_checkin_due(last, frequency=frequency) == last + timedelta(days=days)


def test_monthly_is_a_cadence_not_a_calendar_date():
    """⚠️ 28 days, deliberately — "the 31st" has no meaning in February."""
    last = datetime(2026, 1, 31, 9, 0, tzinfo=UTC)

    assert next_checkin_due(last, frequency=CheckinFrequency.MONTHLY).month == 2


# ─── Vocabulary ──────────────────────────────────────────────────────────


def test_sms_is_absent_from_the_transports():
    """🔒 Approved proposal #7 — TRAI DLT is off the critical path at MVP."""
    assert {t.value for t in TransportType} == {"whatsapp", "email", "logged"}


def test_every_suppression_reason_the_schema_names_exists():
    """DB §11.5's enum, pinned so the code and the migration cannot drift."""
    assert {r.value for r in SuppressionReason} == {
        "client_stage_inactive",
        "consent_withdrawn",
        "tenant_suspended",
        "quota_exceeded",
        "frequency_capped",
        "template_paused",
        "client_unsubscribed",
    }
