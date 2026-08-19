"""Mutation gate for the discovery rules — S2 Slice E.

🔒 **Why this file exists at all.** `tests/test_kernel_discovery.py` and
`tests/test_client_discovery.py` assert the search, sort and cursor rules hold.
Neither can tell you whether it would *notice* if they stopped holding — and a
suite that passes against broken code is worse than no suite, because it is
trusted. This file breaks the rules deliberately and asserts the suite fails.

⚠️ **Not `mutmut`**, for the reason `test_timeline_mutation.py` gives: mutmut 3.x
refuses to run on native Windows, so it would be a gate that never runs on this
project's development machine. This is deterministic, cross-platform, and runs in
the ordinary suite.

🔒 **Scope: the pure rules whose failures are silent.** Discovery is the slice
where a bug looks like an absence — a filter that drops somebody looks exactly
like a practice that does not have them, and a cursor that skips a row looks like
a client who was never there. Every mutation below produces a screen that renders
perfectly and is wrong:

* an operator surviving `parse_search` — a `tsquery` syntax error on the hot path
* a lost `:*` — search that only ever matches complete words
* an unreversed mobile suffix — AC-M1-002 quietly finding nothing
* a sort defaulting to the wrong column — an unindexed ORDER BY (API §6.3)
* a cursor that does not round-trip — pagination that skips or repeats

The *query* half is mutation-tested by the live-database suite in the only way
that means anything: `tests/integration/test_discovery.py` asks PostgreSQL for
its plan, so "mutating" the index means the cluster stops choosing it.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

import pytest

from app.kernel import discovery as rules
from app.kernel.discovery import ClientSort
from app.modules.clients import discovery as query
from app.modules.clients.models import Client

MOMENT = datetime(2026, 8, 9, 10, 30, tzinfo=UTC)
CLIENT = uuid.UUID("44444444-0000-4000-8000-000000000001")


@contextmanager
def _patched(module: ModuleType, attribute: str, value: Any) -> Iterator[None]:
    """Swap one module attribute, then put it back.

    ⚠️ Not `monkeypatch` — a mutation must be reverted even when the assertion
    it trips raises, and inside a `pytest.raises` block the fixture's own
    teardown ordering is easy to get subtly wrong.
    """
    original = getattr(module, attribute)
    setattr(module, attribute, value)
    try:
        yield
    finally:
        setattr(module, attribute, original)


# ─── The real assertions, lifted from the unit suites ────────────────────


def _assert_operators_are_stripped() -> None:
    """🔒 The safety boundary: nothing a practitioner types reaches `to_tsquery`
    as syntax. From `test_no_tsquery_operator_survives_parsing`."""
    for operator in "&|!()<>:*'\"\\":
        assert rules.parse_search(f"asha{operator}menon").tsquery == "ashamenon:*"


def _assert_the_last_word_is_a_prefix() -> None:
    """🔒 FR-M1-021 — "ash" must find "Asha" while the practitioner is typing."""
    assert rules.parse_search("ash").tsquery == "ash:*"
    assert rules.parse_search("asha men").tsquery == "asha & men:*"


def _assert_a_single_character_searches_for_nothing() -> None:
    """🔒 One character matches most of a caseload, and this fires per keystroke."""
    assert rules.parse_search("a").is_empty


def _assert_a_name_with_digits_is_not_a_phone_number() -> None:
    """🔒 "Flat 9876" is an address, not a mobile suffix. Treating it as one
    returns every client whose number ends 9876, burying the real match."""
    assert rules.parse_search("Flat 9876").mobile_suffix == ""


def _assert_the_mobile_suffix_is_the_digits() -> None:
    """🔒 AC-M1-002 — punctuation is not part of the number."""
    assert rules.parse_search("+91 98765 43210").mobile_suffix == "919876543210"


def _assert_the_default_sort_is_recent_activity() -> None:
    """🔒 The list answers "who needs me now", not "who exists"."""
    parsed = rules.parse_sort(None)
    assert parsed.field is ClientSort.RECENT_ACTIVITY
    assert parsed.descending


def _assert_a_name_sorts_ascending_by_default() -> None:
    """A reverse-alphabetical directory is nobody's intent."""
    assert not rules.parse_sort("name").descending


def _assert_an_unknown_sort_falls_back() -> None:
    """⚠️ API §6.3 allowlists sort columns so an unindexed ORDER BY cannot reach
    production. A stale bookmark must not become a sequential scan."""
    assert rules.parse_sort("full_name").field is ClientSort.RECENT_ACTIVITY


def _assert_a_cursor_round_trips() -> None:
    """🔒 ADR-A05 — what `encode` writes, `decode` must read back."""
    cursor = query.ClientCursor(sort_value=MOMENT.isoformat(), client_id=CLIENT)
    assert query.ClientCursor.decode(cursor.encode()) == cursor


def _assert_an_unusable_cursor_decodes_to_nothing() -> None:
    """🔒 A stale bookmark shows the first page, not a coerced position."""
    assert query.ClientCursor.decode("no-separator") is None
    assert query.ClientCursor.decode(f"{MOMENT.isoformat()}|not-a-uuid") is None


def _assert_a_name_cursor_is_lowercased() -> None:
    """🔒 The cursor's value must be the *same expression* the ORDER BY compares
    (`lower(full_name)`), or the page boundary straddles every capital."""
    row = Client(full_name="Zara Khan", created_at=MOMENT, updated_at=MOMENT)
    assert query._encode_sort_value(row, ClientSort.NAME) == "zara khan"


def _assert_a_cursor_from_another_sort_is_refused() -> None:
    """🔒 Comparing a timestamp against `lower(full_name)` would skip rows."""
    assert query._decode_sort_value("asha menon", ClientSort.RECENT_ACTIVITY) is None


# ─── The mutations ───────────────────────────────────────────────────────

#: ⚠️ Bound at import, before any patch. A mutant that wraps the real function
#: must not reach for it through the module attribute it is currently replacing —
#: that recurses until the stack gives out, and the resulting `RecursionError`
#: is not the `AssertionError` the gate is looking for.
_REAL_PARSE_SEARCH = rules.parse_search
_REAL_PARSE_SORT = rules.parse_sort


def _no_prefix_marker(raw: str) -> rules.SearchTerms:
    """`parse_search` without the trailing `:*`."""
    parsed = _REAL_PARSE_SEARCH(raw)
    return rules.SearchTerms(
        tsquery=parsed.tsquery.removesuffix(":*"), mobile_suffix=parsed.mobile_suffix
    )


def _unreversed_suffix(raw: str) -> rules.SearchTerms:
    """`parse_search` whose suffix is not the digits of the query."""
    parsed = _REAL_PARSE_SEARCH(raw)
    return rules.SearchTerms(tsquery=parsed.tsquery, mobile_suffix=parsed.mobile_suffix[::-1])


def _always_ascending(raw: str | None) -> rules.SortOrder:
    """`parse_sort` that forgets each field's natural direction."""
    parsed = _REAL_PARSE_SORT(raw)
    return rules.SortOrder(field=parsed.field, descending=False)


def _falls_back_to_name(raw: str | None) -> rules.SortOrder:
    """`parse_sort` whose unknown-value fallback is the wrong column."""
    if raw and raw.lstrip("-") not in {option.value for option in ClientSort}:
        return rules.SortOrder(field=ClientSort.NAME, descending=False)
    return _REAL_PARSE_SORT(raw)


def _lenient_decode(raw: str) -> query.ClientCursor | None:
    """`ClientCursor.decode` that accepts anything, inventing a position."""
    value, _, identifier = raw.rpartition("|")
    try:
        return query.ClientCursor(sort_value=value, client_id=uuid.UUID(identifier))
    except ValueError:
        return query.ClientCursor(sort_value=raw, client_id=uuid.UUID(int=0))


def _case_preserving_encode(item: Any, sort: ClientSort) -> str:
    """`_encode_sort_value` that forgets to lowercase a name."""
    if sort is ClientSort.NAME:
        return str(item.full_name)
    if sort is ClientSort.CREATED:
        return str(item.created_at.isoformat())
    return str(item.updated_at.isoformat())


def _coercing_decode(raw: str, sort: ClientSort) -> Any:
    """`_decode_sort_value` that returns the raw text rather than refusing."""
    return raw


#: (name, module, attribute, replacement, the check that must trip, what a
#: survivor would mean)
_MUTATIONS: list[tuple[str, ModuleType, str, Any, Callable[[], None], str]] = [
    (
        "tsquery operators stop being stripped",
        rules,
        "_TSQUERY_OPERATORS",
        re.compile(r"(?!x)x"),  # matches nothing
        _assert_operators_are_stripped,
        "a practitioner typing O'Brien would get a tsquery syntax error, and a "
        "crafted query would reach PostgreSQL's parser intact",
    ),
    (
        "the prefix marker is dropped",
        rules,
        "parse_search",
        _no_prefix_marker,
        _assert_the_last_word_is_a_prefix,
        "search would only ever match complete words — typing 'ash' would find "
        "nobody named Asha, which reads as an empty practice",
    ),
    (
        "the minimum query length is removed",
        rules,
        "MIN_SEARCH_LENGTH",
        0,
        _assert_a_single_character_searches_for_nothing,
        "every first keystroke would run a query matching most of the caseload, "
        "on the path NFR-005 budgets at 300 ms",
    ),
    (
        "any digits make a query a phone number",
        rules,
        "_is_numeric",
        lambda text: True,
        _assert_a_name_with_digits_is_not_a_phone_number,
        "searching 'Flat 9876' would return every client whose mobile ends 9876 "
        "and bury the one intended match",
    ),
    (
        "the mobile suffix is mangled",
        rules,
        "parse_search",
        _unreversed_suffix,
        _assert_the_mobile_suffix_is_the_digits,
        "AC-M1-002 would silently find nothing — the search box would look "
        "broken only for phone numbers",
    ),
    (
        "every sort becomes ascending",
        rules,
        "parse_sort",
        _always_ascending,
        _assert_the_default_sort_is_recent_activity,
        "the list would open on the *least* recently active client, so the "
        "screen that answers 'who needs me now' would answer the opposite",
    ),
    (
        "an unknown sort falls back to the wrong column",
        rules,
        "parse_sort",
        _falls_back_to_name,
        _assert_an_unknown_sort_falls_back,
        "a stale bookmark would silently reorder the list rather than showing "
        "the default the practitioner expects",
    ),
    (
        "a malformed cursor is coerced into a position",
        query,
        "ClientCursor",
        type("_Lenient", (query.ClientCursor,), {"decode": staticmethod(_lenient_decode)}),
        _assert_an_unusable_cursor_decodes_to_nothing,
        "a stale bookmark would page from an invented offset, skipping clients "
        "with no error anywhere",
    ),
    (
        "a name cursor keeps its case",
        query,
        "_encode_sort_value",
        _case_preserving_encode,
        _assert_a_name_cursor_is_lowercased,
        "paging an alphabetical list would skip or repeat clients at every "
        "capitalised name, because the cursor and the ORDER BY would disagree",
    ),
    (
        "a cursor from another sort is coerced instead of refused",
        query,
        "_decode_sort_value",
        _coercing_decode,
        _assert_a_cursor_from_another_sort_is_refused,
        "switching sort mid-list would compare a timestamp against a name and "
        "silently drop every client before the coerced position",
    ),
]


@pytest.mark.parametrize(
    ("description", "module", "attribute", "value", "check", "consequence"),
    _MUTATIONS,
    ids=[case[0] for case in _MUTATIONS],
)
def test_the_suite_catches_the_mutation(
    description: str,
    module: ModuleType,
    attribute: str,
    value: Any,
    check: Callable[[], None],
    consequence: str,
) -> None:
    """🔒 Each mutation must make a real assertion fail.

    A surviving mutant is not a style problem. It means the behaviour in
    ``consequence`` could reach a practitioner with the suite green — and every
    consequence in this file is one the screen renders without complaint.
    """
    with _patched(module, attribute, value), pytest.raises(AssertionError):
        check()


def test_the_checks_pass_unmutated() -> None:
    """⚠️ The control.

    Without this, a check that raised `AssertionError` unconditionally — a typo,
    a bad refactor — would make every mutation above 'caught' and the whole file
    would silently stop testing anything.
    """
    _assert_operators_are_stripped()
    _assert_the_last_word_is_a_prefix()
    _assert_a_single_character_searches_for_nothing()
    _assert_a_name_with_digits_is_not_a_phone_number()
    _assert_the_mobile_suffix_is_the_digits()
    _assert_the_default_sort_is_recent_activity()
    _assert_a_name_sorts_ascending_by_default()
    _assert_an_unknown_sort_falls_back()
    _assert_a_cursor_round_trips()
    _assert_an_unusable_cursor_decodes_to_nothing()
    _assert_a_name_cursor_is_lowercased()
    _assert_a_cursor_from_another_sort_is_refused()
