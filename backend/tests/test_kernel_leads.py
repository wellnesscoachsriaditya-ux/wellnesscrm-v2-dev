"""The rules of lead capture — DB §6, FR-M2-001..011, M2.3.

🔒 No database. ``kernel.leads`` is split from the persistence in
``app.modules.leads`` precisely so that every rule below is a pure function over
values — and these rules govern the **only unauthenticated write in the product**,
so a drift here is a drift on the public surface.

Four groups, each pinning a different failure the public form invites:

* **The acknowledgement** — EC-M2-02. The response must not vary on whether the
  submitted mobile matched an existing client, because a response that varied
  would make the endpoint a client-enumeration oracle. Tested structurally, on
  the signature, not just on the string.
* **Consent** — FR-M2-004, EC-M2-04. A precondition, refused before anything is
  created, and refused again when the notice version has moved on.
* **Spam** — FR-M2-008, EC-M2-03. A score, not a verdict, and the weights are
  ordered so that no single weak signal can refuse a genuine enquiry.
* **Normalisation and ageing** — FR-M2-009, FR-M2-011. What makes the source
  reportable and the queue orderable.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.kernel.errors import ConsentError, ValidationError
from app.kernel.leads import (
    AGEING_LEAD_HOURS,
    MAX_PRIMARY_GOAL_LENGTH,
    MAX_SOURCE_DETAIL_LENGTH,
    MAX_SOURCE_LENGTH,
    SPAM_REJECT_THRESHOLD,
    EnquiryReceived,
    LeadSource,
    SpamSignals,
    acknowledgement,
    age_in_hours,
    assert_consent_granted,
    assert_notice_current,
    is_ageing,
    is_known_source,
    is_spam,
    looks_like_link_spam,
    normalise_source,
    normalise_source_detail,
    score_submission,
    validate_primary_goal,
)

# ─── The acknowledgement (EC-M2-02, API §11.2) ────────────────────────────


def test_the_acknowledgement_cannot_vary_on_the_match() -> None:
    """🔒 EC-M2-02 / API §11.2 — the most serious leak the public surface offers.

    ⚠️ Asserted on the **signature**, not on the returned string. A test that only
    compared two calls' output would keep passing if someone added an
    ``is_duplicate`` parameter and a caller started passing it; taking no
    arguments is what makes the guarantee structural rather than conventional.
    """
    signature = inspect.signature(acknowledgement)
    assert not signature.parameters, (
        "acknowledgement() has gained a parameter. Anything it can branch on is "
        "something the response can leak — EC-M2-02 requires the reply to be "
        "identical whether or not the mobile matched an existing client."
    )


def test_the_acknowledgement_says_nothing_about_records() -> None:
    """🔒 EC-M2-02 — the wording itself must not hint at a match.

    A signature that cannot branch is still defeatable by prose that says "we
    have your details on file", which a returning prospect would read as
    confirmation they are known to this practice.
    """
    message = acknowledgement()
    assert message
    lowered = message.lower()
    for leak in ("already", "existing", "again", "record", "on file", "welcome back"):
        assert leak not in lowered, (
            f"The acknowledgement contains {leak!r}, which reads as a statement "
            "about whether this person is already known — EC-M2-02."
        )


# ─── Consent (FR-M2-004, EC-M2-04, NFR-051) ───────────────────────────────


def test_consent_must_be_granted_before_anything_is_created() -> None:
    """🔒 EC-M2-04 — a declined consent creates nothing at all."""
    assert_consent_granted(granted=True)

    with pytest.raises(ConsentError) as raised:
        assert_consent_granted(granted=False)

    # ⚠️ 403, not 422. The submission was well-formed; it was refused on a legal
    # basis, and a validation error would invite the form to retry it.
    assert raised.value.status_code == 403


def test_a_superseded_notice_is_refused() -> None:
    """🔒 NFR-051 — consent is against text the person actually saw.

    The form renders a notice, the practitioner's locale gets a new version, and
    the prospect submits the old one. Accepting it would record agreement to text
    that was never displayed, which is exactly what DPDP's demonstrability
    requirement forbids.
    """
    displayed = uuid.uuid4()
    in_force = uuid.uuid4()

    assert_notice_current(submitted_notice_id=displayed, notice_in_force=displayed)

    with pytest.raises(ValidationError):
        assert_notice_current(submitted_notice_id=displayed, notice_in_force=in_force)


# ─── Spam (FR-M2-008, EC-M2-03) ───────────────────────────────────────────


def test_a_clean_submission_scores_zero() -> None:
    """The default path: nothing observed against it, nothing held against it."""
    assert score_submission(SpamSignals()) == 0.0
    assert not is_spam(score_submission(SpamSignals()))


def test_the_honeypot_alone_is_decisive() -> None:
    """🔒 FR-M2-008 — the one signal a human cannot trip.

    The field is hidden by CSS and has no label. A human never sees it, so a
    value in it is not evidence of automation, it is automation. Every other
    signal is circumstantial and weighted accordingly.
    """
    score = score_submission(SpamSignals(honeypot_filled=True))
    assert score >= SPAM_REJECT_THRESHOLD
    assert is_spam(score)


def test_no_single_weak_signal_can_refuse_a_genuine_enquiry() -> None:
    """🔒 EC-M2-03's real cost — a false positive is a lost client.

    M2.2 prices a missed enquiry at ₹2,500–4,000/month of recurring revenue, and
    a refused submission is *silent* (the caller still gets a 202), so the
    practitioner never learns it happened. A prospect who types fast, or whose
    goal happens to be short, must not be refused on that alone.
    """
    for signal in (
        SpamSignals(elapsed_seconds=0.5),
        SpamSignals(goal_is_link_only=True),
        SpamSignals(captcha_verified=False),
    ):
        assert not is_spam(score_submission(signal)), (
            f"{signal} alone refuses a submission. Only the honeypot may do that "
            "— every other signal is circumstantial (FR-M2-008)."
        )


def test_weak_signals_accumulate() -> None:
    """FR-M2-008 — a score, not a checklist.

    Filled instantly *and* a goal that is only a URL is a different claim from
    either alone, and the combination is what a scripted submission looks like.
    """
    combined = score_submission(SpamSignals(elapsed_seconds=0.2, goal_is_link_only=True))
    assert is_spam(combined)


def test_a_slow_thoughtful_submission_is_never_penalised() -> None:
    """⚠️ The timing signal is one-directional. Taking a long time over the form
    is what a real prospect does, and a ceiling would refuse the most considered
    enquiries."""
    assert score_submission(SpamSignals(elapsed_seconds=600.0)) == 0.0


@pytest.mark.parametrize(
    "goal",
    [
        "http://example.com",
        "  https://bit.ly/x  ",
        "www.example.com/offer",
        "https://a.example https://b.example",
    ],
)
def test_link_only_goals_are_recognised(goal: str) -> None:
    """FR-M2-008 — the shape of a link-spam submission's only real content."""
    assert looks_like_link_spam(goal)


@pytest.mark.parametrize(
    "goal",
    [
        "I want to lose 8kg before my sister's wedding",
        "Manage my PCOS symptoms with diet",
        "My doctor at apollohospitals suggested I see a dietitian",
        # ⚠️ Prose wrapped around a link. A real prospect pastes the article
        # that prompted them to enquire, and refusing that is a lost lead.
        "visit example.com now",
        "I read this and it helped: https://example.com/pcos-diet",
    ],
)
def test_genuine_goals_are_not_link_spam(goal: str) -> None:
    """⚠️ The false-positive direction, which is the expensive one.

    The third case is the trap: a genuine goal can *contain* something that looks
    like a domain. The predicate must be about the goal being nothing but a link,
    not about a link appearing in it.
    """
    assert not looks_like_link_spam(goal)


# ─── The submitted goal (FR-M2-003, EC-M2-01) ─────────────────────────────


def test_the_goal_is_required_and_trimmed() -> None:
    """FR-M2-003 — one of exactly three mandatory fields."""
    assert validate_primary_goal("  Lose 8kg  ") == "Lose 8kg"

    for empty in ("", "   ", "\n\t "):
        with pytest.raises(ValidationError) as raised:
            validate_primary_goal(empty)
        # The form highlights the field, so the error has to name it.
        assert raised.value.details == {"field": "primary_goal"}


def test_an_over_long_goal_is_refused_rather_than_truncated() -> None:
    """🔒 EC-M2-01 — silently cutting the prospect's own words is worse.

    The goal is what the practitioner reads first. A truncated one changes its
    meaning without telling anybody, where a 422 lets the form say so while the
    person is still typing.
    """
    with pytest.raises(ValidationError):
        validate_primary_goal("x" * (MAX_PRIMARY_GOAL_LENGTH + 1))

    assert len(validate_primary_goal("x" * MAX_PRIMARY_GOAL_LENGTH)) == MAX_PRIMARY_GOAL_LENGTH


# ─── Lead source (FR-M2-009, US-M2-04) ────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Instagram", "instagram"),
        ("  WhatsApp  ", "whatsapp"),
        ("Instagram Ads", "instagram_ads"),
        ("instagram-ads", "instagram_ads"),
        ("walk in", "walk_in"),
        ("---", None),
        ("", None),
        (None, None),
    ],
)
def test_source_normalisation_folds_spellings_together(
    raw: str | None, expected: str | None
) -> None:
    """🔒 US-M2-04 — "which channel produces enquiries" must be answerable.

    ``Instagram Ads`` and ``instagram-ads`` are one channel typed two ways. Left
    alone they become two rows in the report, and the answer to the question the
    column exists for would be wrong rather than missing.
    """
    assert normalise_source(raw) == expected


def test_an_absent_source_is_not_other() -> None:
    """⚠️ FR-M2-009 — "we do not know" and "the prospect chose other" differ.

    Collapsing them would inflate ``OTHER`` with every enquiry whose link carried
    no parameter, which is most of them early on.
    """
    assert normalise_source(None) is None
    assert normalise_source("other") == LeadSource.OTHER.value


def test_a_long_source_is_truncated_not_refused() -> None:
    """🔒 The public path must not lose a real enquiry over a marketing URL.

    A campaign parameter nobody in the practice controls can be arbitrarily long;
    refusing the submission would discard a genuine prospect to protect a column
    width.
    """
    folded = normalise_source("a" * 500)
    assert folded is not None
    assert len(folded) == MAX_SOURCE_LENGTH


def test_source_detail_keeps_its_case() -> None:
    """⚠️ Not tokenised, unlike the source itself.

    The detail is shown back to the practitioner — "referred by Dr. Meera at
    Sunrise Clinic" — and folding its case would make it read wrong.
    """
    assert normalise_source_detail("  Dr. Meera at Sunrise Clinic  ") == (
        "Dr. Meera at Sunrise Clinic"
    )
    assert normalise_source_detail("") is None
    assert normalise_source_detail(None) is None

    trimmed = normalise_source_detail("z" * 500)
    assert trimmed is not None
    assert len(trimmed) == MAX_SOURCE_DETAIL_LENGTH


def test_known_sources_are_the_enum_and_nothing_else() -> None:
    """The predicate the public endpoint uses to decide keep-or-bucket."""
    for member in LeadSource:
        assert is_known_source(member.value)
    assert not is_known_source("instagram_ads")
    assert not is_known_source(None)


# ─── Ageing (FR-M2-011, AC-M2-005) ────────────────────────────────────────


def test_age_is_computed_in_hours_from_the_server_clock() -> None:
    """🔒 Principle 3 — the number that decides who gets called next.

    Server-computed because two devices in different timezones, or one with a
    skewed clock, would otherwise disagree about which enquiry is most urgent.
    """
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    assert age_in_hours(now - timedelta(hours=3), now=now) == pytest.approx(3.0)
    assert age_in_hours(now - timedelta(minutes=30), now=now) == pytest.approx(0.5)


def test_a_future_timestamp_reports_zero_rather_than_negative() -> None:
    """⚠️ Clock skew between the app and the database is real.

    A negative age would sort a freshly arrived enquiry to the *front* of an
    oldest-first queue, ahead of one that has genuinely waited two days.
    """
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    assert age_in_hours(now + timedelta(minutes=5), now=now) == 0.0


def test_ageing_begins_at_the_documented_threshold() -> None:
    """🔒 AC-M2-005 / FR-M2-011 — the boundary, pinned both sides.

    The threshold is what turns the queue from a list into a warning, and the
    exact-boundary case is the one an off-by-one would get wrong.
    """
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    just_under = now - timedelta(hours=AGEING_LEAD_HOURS, minutes=-1)
    exactly = now - timedelta(hours=AGEING_LEAD_HOURS)

    assert not is_ageing(just_under, now=now)
    assert is_ageing(exactly, now=now)


def test_a_responded_enquiry_is_never_ageing() -> None:
    """FR-M2-011 — the queue is about *unanswered* enquiries.

    An enquiry answered a week ago is old, not ageing. Conflating the two would
    fill the working queue with completed work.
    """
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    old = now - timedelta(days=7)
    assert is_ageing(old, now=now)
    assert not is_ageing(old, now=now, responded_at=now - timedelta(days=6))


# ─── The event (DDR-06, Arch §3.4) ────────────────────────────────────────


def test_the_enquiry_event_carries_no_submitted_content() -> None:
    """🔒 NFR-033 — an event is not a place to put a prospect's own words.

    ``EnquiryReceived`` fans out to the timeline and to the practitioner
    notification. Carrying ``primary_goal`` or the mobile would copy personal
    data into every subscriber's reach, and the timeline row would then hold
    free text the retention rules never accounted for.
    """
    fields = set(EnquiryReceived.__dataclass_fields__)
    for leak in ("primary_goal", "submitted_name", "submitted_mobile", "submitted_email"):
        assert leak not in fields, (
            f"EnquiryReceived carries {leak!r}. Subscribers that need the content "
            "should read the submission row under their own authorization."
        )


def test_the_enquiry_event_carries_the_match_flag_for_the_notification() -> None:
    """FR-M2-006 — "an existing client enquired again" is the practitioner's cue.

    ⚠️ This is the deliberate asymmetry: ``is_duplicate`` is visible to the
    *tenant's* subscribers and never to the submitter (EC-M2-02). Two audiences,
    two different truths, and the split lives in who reads the event versus who
    reads the response.
    """
    assert "is_duplicate" in EnquiryReceived.__dataclass_fields__
