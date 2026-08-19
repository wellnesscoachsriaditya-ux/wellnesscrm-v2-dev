"""Mutation gate for the lead-capture rules — S2 Slice F.

🔒 **Why this file exists at all.** `tests/test_kernel_leads.py` asserts the
consent, spam and source rules hold. It cannot tell you whether it would *notice*
if they stopped holding — and a suite that passes against broken code is worse
than no suite, because it is trusted. This file breaks each rule deliberately and
asserts the suite fails.

⚠️ **Not `mutmut`**, for the reason `test_timeline_mutation.py` gives: mutmut 3.x
refuses to run on native Windows, so it would be a gate that never runs on this
project's development machine. This is deterministic, cross-platform, and runs in
the ordinary suite.

🔒 **Scope: the rules whose failure is silent *and* on the public surface.** Lead
capture is the one place an unauthenticated caller writes tenant data, and every
mutation below produces an endpoint that answers 202 and behaves wrongly:

* consent that is not actually required — personal data held on no lawful basis
  (EC-M2-04), with the prospect told their enquiry went through
* an acknowledgement that varies on the match — the client-enumeration oracle
  API §11.2 calls the most serious leak available on the public surface
* a spam threshold that refuses genuine enquiries — silently, because a refusal
  returns the same 202 as an acceptance (EC-M2-03)
* a superseded notice accepted — a consent record citing text nobody saw
  (NFR-051)
* an ageing rule that never fires — FR-M2-011's queue stops warning about
  anything, which looks exactly like a practice with no old enquiries
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import pytest

# ⚠️ `pytest.raises` reports a *missing* exception by raising `Failed`, not
# `AssertionError`. Several assertions below use `pytest.raises` internally, so a
# mutation that stops them raising surfaces as `Failed` — and a gate catching
# only `AssertionError` would report those rows as "not noticed".
from _pytest.outcomes import Failed

from app.kernel import leads as rules

MOMENT = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
NOTICE = uuid.UUID("55555555-0000-4000-8000-000000000001")
OTHER_NOTICE = uuid.UUID("55555555-0000-4000-8000-000000000002")


@contextmanager
def _patched(module: ModuleType, attribute: str, value: Any) -> Iterator[None]:
    """Swap one module attribute, then put it back.

    ⚠️ Not `monkeypatch` — a mutation must be reverted even when the assertion it
    trips raises, and inside a `pytest.raises` block the fixture's own teardown
    ordering is easy to get subtly wrong.
    """
    original = getattr(module, attribute)
    setattr(module, attribute, value)
    try:
        yield
    finally:
        setattr(module, attribute, original)


# ─── The real assertions, lifted from the unit suite ─────────────────────


def _assert_consent_is_required() -> None:
    """🔒 EC-M2-04 — a declined consent creates nothing at all."""
    from app.kernel.errors import ConsentError

    with pytest.raises(ConsentError):
        rules.assert_consent_granted(granted=False)


def _assert_a_superseded_notice_is_refused() -> None:
    """🔒 NFR-051 — consent is against text the person actually saw."""
    from app.kernel.errors import ValidationError

    with pytest.raises(ValidationError):
        rules.assert_notice_current(submitted_notice_id=NOTICE, notice_in_force=OTHER_NOTICE)


def _assert_the_acknowledgement_takes_no_arguments() -> None:
    """🔒 EC-M2-02 — the signature *is* the guarantee."""
    import inspect

    assert not inspect.signature(rules.acknowledgement).parameters


def _assert_the_honeypot_is_decisive() -> None:
    """🔒 FR-M2-008 — a field no human can see was filled in."""
    assert rules.is_spam(rules.score_submission(rules.SpamSignals(honeypot_filled=True)))


def _assert_a_clean_submission_is_accepted() -> None:
    """🔒 EC-M2-03's expensive direction — a false positive is a lost client."""
    assert not rules.is_spam(rules.score_submission(rules.SpamSignals()))


def _assert_one_weak_signal_is_not_enough() -> None:
    """🔒 A slow connection is not automation."""
    assert not rules.is_spam(rules.score_submission(rules.SpamSignals(elapsed_seconds=0.5)))


def _assert_spellings_fold_together() -> None:
    """🔒 US-M2-04 — one channel typed two ways is one channel."""
    assert rules.normalise_source("Instagram Ads") == rules.normalise_source("instagram-ads")


def _assert_an_absent_source_is_not_other() -> None:
    """⚠️ "We do not know" and "the prospect chose other" are different facts."""
    assert rules.normalise_source(None) is None


def _assert_an_old_enquiry_is_ageing() -> None:
    """🔒 FR-M2-011 — the warning the working queue exists to raise."""
    assert rules.is_ageing(MOMENT - timedelta(hours=rules.AGEING_LEAD_HOURS), now=MOMENT)


def _assert_an_answered_enquiry_is_never_ageing() -> None:
    """FR-M2-011 — the queue is about *unanswered* enquiries."""
    assert not rules.is_ageing(
        MOMENT - timedelta(days=7), now=MOMENT, responded_at=MOMENT - timedelta(days=6)
    )


def _assert_age_never_goes_negative() -> None:
    """⚠️ Clock skew must not sort a new enquiry ahead of an old one."""
    assert rules.age_in_hours(MOMENT + timedelta(minutes=5), now=MOMENT) == 0.0


def _assert_a_genuine_goal_is_not_link_spam() -> None:
    """⚠️ A prospect may paste the article that prompted them to enquire."""
    assert not rules.looks_like_link_spam("I read this: https://example.com/pcos")


# ─── The mutations ───────────────────────────────────────────────────────

#: ⚠️ Bound at import, before any patch. A mutant that wraps the real function
#: must not reach for it through the module attribute it is currently replacing —
#: that recurses until the stack gives out, and the resulting `RecursionError` is
#: not the `AssertionError` the gate is looking for.
_REAL_SCORE = rules.score_submission
_REAL_NORMALISE_SOURCE = rules.normalise_source
_REAL_LINK_SPAM = rules.looks_like_link_spam


def _consent_never_refuses(*, granted: bool) -> None:
    """`assert_consent_granted` that accepts a declined consent."""
    return None


def _notice_check_is_a_no_op(*, submitted_notice_id: uuid.UUID, notice_in_force: uuid.UUID) -> None:
    """`assert_notice_current` that tolerates a superseded notice."""
    return None


def _acknowledgement_names_the_match(is_duplicate: bool = False) -> str:
    """🔒 The oracle, in the one line it would take to create.

    Innocuous-looking and catastrophic: EC-M2-02 becomes probeable one mobile
    number at a time, against every practitioner's client list.
    """
    return "We already have your details." if is_duplicate else "Thanks for your enquiry."


def _honeypot_ignored(signals: rules.SpamSignals) -> float:
    """Scoring that drops the one decisive signal."""
    return _REAL_SCORE(rules.SpamSignals(**{**vars_of(signals), "honeypot_filled": False}))


def vars_of(signals: rules.SpamSignals) -> dict[str, Any]:
    """`SpamSignals` is a slots dataclass, so `vars()` does not work on it."""
    return {
        "honeypot_filled": signals.honeypot_filled,
        "elapsed_seconds": signals.elapsed_seconds,
        "captcha_verified": signals.captcha_verified,
        "goal_is_link_only": signals.goal_is_link_only,
    }


def _threshold_is_zero(score: float) -> bool:
    """`is_spam` that refuses everything — every enquiry silently dropped."""
    return score >= 0.0


def _threshold_is_unreachable(score: float) -> bool:
    """`is_spam` that refuses nothing — FR-M2-008 stops existing."""
    return score > 999.0


def _source_is_not_folded(source: str | None) -> str | None:
    """`normalise_source` that only trims, so spellings stay distinct."""
    return source.strip() if source is not None else None


def _absent_source_becomes_other(source: str | None) -> str | None:
    """`normalise_source` that buckets "unknown" as a real channel."""
    return _REAL_NORMALISE_SOURCE(source) or rules.LeadSource.OTHER.value


def _ageing_never_fires(
    submitted_at: datetime, *, now: datetime, responded_at: datetime | None = None
) -> bool:
    """`is_ageing` that never warns — a queue that looks permanently healthy."""
    return False


def _age_may_be_negative(submitted_at: datetime, *, now: datetime) -> float:
    """`age_in_hours` without the clamp."""
    return (now - submitted_at).total_seconds() / 3600.0


def _any_link_is_spam(goal: str) -> bool:
    """`looks_like_link_spam` that fires on a link anywhere in the text."""
    return "http" in goal.lower() or "www." in goal.lower()


#: Each entry is `(attribute, mutant, the assertion it must break)`.
#:
#: 🔒 The third element is the point of the table. A mutation that breaks
#: *something* proves nothing; each one here must break the specific assertion
#: that documents the rule, which is what shows the suite is watching the right
#: property rather than a coincidence.
_MUTATIONS: list[tuple[str, Any, Callable[[], None]]] = [
    ("assert_consent_granted", _consent_never_refuses, _assert_consent_is_required),
    ("assert_notice_current", _notice_check_is_a_no_op, _assert_a_superseded_notice_is_refused),
    (
        "acknowledgement",
        _acknowledgement_names_the_match,
        _assert_the_acknowledgement_takes_no_arguments,
    ),
    ("score_submission", _honeypot_ignored, _assert_the_honeypot_is_decisive),
    ("is_spam", _threshold_is_zero, _assert_a_clean_submission_is_accepted),
    ("is_spam", _threshold_is_unreachable, _assert_the_honeypot_is_decisive),
    ("normalise_source", _source_is_not_folded, _assert_spellings_fold_together),
    ("normalise_source", _absent_source_becomes_other, _assert_an_absent_source_is_not_other),
    ("is_ageing", _ageing_never_fires, _assert_an_old_enquiry_is_ageing),
    ("age_in_hours", _age_may_be_negative, _assert_age_never_goes_negative),
    ("looks_like_link_spam", _any_link_is_spam, _assert_a_genuine_goal_is_not_link_spam),
]


@pytest.mark.parametrize(
    ("attribute", "mutant", "assertion"),
    _MUTATIONS,
    ids=[f"{attribute}:{mutant.__name__}" for attribute, mutant, _ in _MUTATIONS],
)
def test_the_suite_notices_each_mutation(
    attribute: str, mutant: Any, assertion: Callable[[], None]
) -> None:
    """🔒 Break one rule; the assertion that documents it must fail.

    ⚠️ ``pytest.raises`` on the assertion itself, not a bare call — a mutation
    that made the assertion *error* for an unrelated reason (a TypeError from a
    changed signature) would otherwise read as a pass, and the gate would be
    reporting that it noticed something it did not.
    """
    assertion()  # The rule holds before the mutation — no false pass from a
    # pre-existing failure.

    with _patched(rules, attribute, mutant), pytest.raises((AssertionError, Failed)):
        assertion()

    assertion()  # And it holds again afterwards — the patch really was reverted.


def _assert_one_weak_signal_is_not_enough_survives() -> None:
    """The unmutated control — see :func:`test_an_unmutated_control_passes`."""
    _assert_one_weak_signal_is_not_enough()
    _assert_an_answered_enquiry_is_never_ageing()


def test_an_unmutated_control_passes() -> None:
    """🔒 The control the table needs to mean anything.

    ⚠️ Without this, a bug in `_patched` that left every mutation un-applied
    would still produce a green table — every `pytest.raises` would fail, which
    is a *failure*, so that specific bug would surface. What this catches is the
    opposite: an assertion helper that is broken in a way that makes it raise
    unconditionally, which would make every row pass for the wrong reason.
    """
    _assert_one_weak_signal_is_not_enough_survives()
    _assert_consent_is_required()
    _assert_the_honeypot_is_decisive()
    _assert_a_clean_submission_is_accepted()
    _assert_spellings_fold_together()
    _assert_an_old_enquiry_is_ageing()
    _assert_age_never_goes_negative()
    _assert_a_genuine_goal_is_not_link_spam()
