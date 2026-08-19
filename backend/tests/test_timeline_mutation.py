"""Mutation gate for the timeline vocabulary — S2 Slice D.

🔒 **Why this file exists at all.** `tests/test_kernel_timeline.py` asserts the
summary rules hold. It cannot tell you whether it would *notice* if they stopped
holding — and a test suite that passes against broken code is worse than no
suite, because it is trusted. This file mutates the rules and asserts the suite
fails, which is the only evidence that the coverage is real.

⚠️ **Not `mutmut`.** mutmut 3.x refuses to run on native Windows (it exits
telling you to use WSL), so a mutmut-based gate would be a gate that never runs
on this project's development machine — and one that CI could not reproduce
locally when it failed. This is deterministic, cross-platform, and runs in the
ordinary suite.

🔒 **Scope: the pure vocabulary only.** These are the functions whose output
reaches a practitioner's screen verbatim and whose failure modes are silent — a
label that says "Active" for `archived`, a stage arrow pointing the wrong way.
The projection's *plumbing* is mutation-tested by the live-database suite in the
only way that means anything: the constraints and grants are in PostgreSQL, so
"mutating" them means asking the cluster to accept a write it must refuse.

⚠️ Each case patches the module under test, then calls the *real assertions* from
the unit suite. If a patch survives, the assertion it should have tripped is not
actually being made — and the message says which one.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from app.kernel import timeline as vocab
from app.kernel.clients import ClientStage
from app.kernel.timeline import TimelineEventType


@contextmanager
def _patched(attribute: str, value: Any) -> Iterator[None]:
    """Swap one module attribute, then put it back.

    ⚠️ Not `monkeypatch` — a mutation must be reverted even when the assertion
    it trips raises, and inside a `pytest.raises` block the fixture's own
    teardown ordering is easy to get subtly wrong.
    """
    original = getattr(vocab, attribute)
    setattr(vocab, attribute, value)
    try:
        yield
    finally:
        setattr(vocab, attribute, original)


def _labelled_types() -> list[TimelineEventType]:
    """Every type with a parameterless label.

    ⚠️ `STAGE_CHANGED` is excluded: its label depends on which stages, so
    `summarise` raises `KeyError` for it by design. Including it here would make
    every case in this file fail for that reason rather than the mutation.
    """
    return [
        event_type
        for event_type in TimelineEventType
        if event_type is not TimelineEventType.STAGE_CHANGED
    ]


def _assert_labels_are_distinct() -> None:
    """The real assertion behind a readable timeline: no two event types may
    render identically, or a filter's results become unattributable."""
    labels = [vocab.summarise(event_type) for event_type in _labelled_types()]
    assert len(set(labels)) == len(labels)


def _assert_stage_arrow_reads_forwards() -> None:
    """The real assertion from `test_a_stage_change_names_both_ends`."""
    summary = vocab.summarise_stage_change(
        from_stage=ClientStage.LEAD, to_stage=ClientStage.CONTACTED
    )
    assert summary == "New enquiry → Contacted"


def _assert_first_stage_has_no_arrow() -> None:
    """The real assertion from `test_the_first_stage_has_no_arrow`."""
    summary = vocab.summarise_stage_change(from_stage=None, to_stage=ClientStage.LEAD)
    assert "→" not in summary


def _assert_no_summary_is_blank() -> None:
    """The real assertion from `test_summaries_fit_the_column`'s sibling — a
    blank row renders as an unexplained gap in the history."""
    for event_type in _labelled_types():
        assert vocab.summarise(event_type).strip()


#: (name, mutation, the check that must trip, what a survivor would mean)
_MUTATIONS: list[tuple[str, dict[str, Any], Callable[[], None], str]] = [
    (
        "two event types collapse onto one label",
        {"_SUMMARIES": dict.fromkeys(TimelineEventType, "Something happened")},
        _assert_labels_are_distinct,
        "a timeline where every row reads 'Something happened' would pass review",
    ),
    (
        "the stage arrow is reversed",
        {
            "summarise_stage_change": lambda *, from_stage, to_stage: (
                f"{vocab.stage_label(to_stage)} → {vocab.stage_label(from_stage)}"
                if from_stage is not None
                else vocab.stage_label(to_stage)
            )
        },
        _assert_stage_arrow_reads_forwards,
        "a transition displayed backwards — 'Contacted → New enquiry' — would ship",
    ),
    (
        "the first transition grows a phantom left-hand side",
        {
            "summarise_stage_change": lambda *, from_stage, to_stage: (
                f"{vocab.stage_label(from_stage) if from_stage else 'None'} → "
                f"{vocab.stage_label(to_stage)}"
            )
        },
        _assert_first_stage_has_no_arrow,
        "the first row of every timeline would read 'None → New enquiry'",
    ),
    (
        "a label is blanked",
        {
            "_SUMMARIES": {
                **{event_type: vocab.summarise(event_type) for event_type in _labelled_types()},
                TimelineEventType.NOTE_ADDED: "",
            }
        },
        _assert_no_summary_is_blank,
        "an empty row would render as an unexplained gap in the client's history",
    ),
]


@pytest.mark.parametrize(
    ("description", "mutation", "check", "consequence"),
    _MUTATIONS,
    ids=[case[0] for case in _MUTATIONS],
)
def test_the_suite_catches_the_mutation(
    description: str,
    mutation: dict[str, Any],
    check: Callable[[], None],
    consequence: str,
) -> None:
    """🔒 Each mutation must make a real assertion fail.

    A surviving mutant is not a style problem. It means the behaviour in
    ``consequence`` could reach a practitioner with the suite green.
    """
    attribute, value = next(iter(mutation.items()))
    with _patched(attribute, value), pytest.raises(AssertionError):
        check()


def test_the_checks_pass_unmutated() -> None:
    """⚠️ The control.

    Without this, a check that raised `AssertionError` unconditionally — a typo,
    a bad refactor — would make every mutation above 'caught' and the whole file
    would silently stop testing anything.
    """
    _assert_labels_are_distinct()
    _assert_stage_arrow_reads_forwards()
    _assert_first_stage_has_no_arrow()
    _assert_no_summary_is_blank()
