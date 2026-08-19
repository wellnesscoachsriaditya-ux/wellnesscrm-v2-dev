"""The rules behind finding a client — FR-M1-021/022, AC-M1-002.

🔒 No database. ``kernel.discovery`` is pure, and the parts that matter most are
the ones a live-database test would never reach: what :func:`parse_search` does
to a search box before its output is handed to ``to_tsquery``.

Three groups:

* **Search parsing** — the safety boundary. Everything downstream binds these
  values as parameters, so this is the only place that decides whether a
  practitioner can type something that reaches PostgreSQL as syntax.
* **Sort parsing** — the allowlist. API §6.3 exists so an unindexed ORDER BY
  cannot reach production, and an enum that silently accepts a new value would
  be that hole.
* **The bounds** — that the constants still say what the code around them
  assumes they say.
"""

from __future__ import annotations

import pytest

from app.kernel.discovery import (
    MAX_BULK_REASSIGN,
    MAX_SEARCH_LENGTH,
    MIN_MOBILE_SUFFIX_DIGITS,
    MIN_SEARCH_LENGTH,
    ArchivedFilter,
    ClientSort,
    SearchTerms,
    parse_search,
    parse_sort,
)

#: 🔒 Every character `to_tsquery` would read as syntax rather than text. The
#: parser must not see any of these, so this list is the test's whole subject —
#: see :func:`test_no_tsquery_operator_survives_parsing`.
TSQUERY_OPERATORS = "&|!()<>:*'\"\\"


# ─── Search parsing (FR-M1-021, AC-M1-002) ───────────────────────────────


def test_a_short_query_searches_for_nothing() -> None:
    """🔒 One character matches most of a caseload — see `MIN_SEARCH_LENGTH`.

    FR-M1-021 fires this on every keystroke, so the first character typed must
    not become a full scan.
    """
    assert parse_search("a").is_empty
    assert parse_search("").is_empty
    assert parse_search("   ").is_empty


def test_a_two_character_query_is_worth_running() -> None:
    """The documented floor, asserted so it cannot drift from the constant."""
    assert MIN_SEARCH_LENGTH == 2
    assert not parse_search("as").is_empty


def test_the_last_word_is_a_prefix_and_earlier_words_are_not() -> None:
    """🔒 FR-M1-021 — results *as the practitioner types*.

    The word being typed is necessarily incomplete, so "ash" must find "Asha".
    Earlier words are finished, and prefix-matching them too would make
    "asha men" match "ashamed mention" — noise that grows with the caseload.
    """
    assert parse_search("ash").tsquery == "ash:*"
    assert parse_search("asha men").tsquery == "asha & men:*"
    assert parse_search("asha priya menon").tsquery == "asha & priya & menon:*"


def test_runs_of_whitespace_collapse_into_one_separator() -> None:
    """Otherwise "asha   menon" becomes terms with empty strings between them,
    and the resulting `a & & b` is a syntax error rather than a search."""
    assert parse_search("asha   menon").tsquery == "asha & menon:*"
    assert parse_search("  asha menon  ").tsquery == "asha & menon:*"
    assert parse_search("asha\tmenon").tsquery == "asha & menon:*"


@pytest.mark.parametrize("operator", list(TSQUERY_OPERATORS))
def test_no_tsquery_operator_survives_parsing(operator: str) -> None:
    """🔒 **The safety boundary of this slice.**

    A practitioner typing "O'Brien" or "priya & asha" means those as text. Passed
    through, `&` and `!` are boolean operators and `'` opens a phrase that never
    closes — a syntax error the practitioner reads as "search is broken", and one
    a hostile input could aim more precisely.

    ⚠️ Asserted per character rather than on one combined string, so a regex that
    stopped stripping exactly one of them names which.

    ⚠️ Compared against the whole expected expression rather than "the character
    is absent", because `:` and `*` legitimately appear in the output — they are
    the prefix marker this function *adds*. An absence check would have to skip
    those two, which is exactly where a stripping bug would hide.
    """
    parsed = parse_search(f"asha{operator}menon")
    assert parsed.tsquery == "ashamenon:*"


def test_a_query_of_pure_operators_matches_everything_rather_than_erroring() -> None:
    """⚠️ The surviving edge of stripping rather than escaping.

    "&&" holds nothing searchable once the operators are gone, so it parses to
    an empty query — and an empty query is the unfiltered list, not zero rows.
    Returning everyone for a nonsense query is odd; returning an error, or an
    empty screen with no explanation, is worse.
    """
    assert parse_search("&&").is_empty
    assert parse_search("!!!").is_empty


def test_an_apostrophe_leaves_a_searchable_name() -> None:
    """The motivating case for stripping rather than rejecting — a real name."""
    assert parse_search("O'Brien").tsquery == "OBrien:*"


def test_a_long_paste_is_truncated_rather_than_refused() -> None:
    """🔒 A search box is a paste target — see `MAX_SEARCH_LENGTH`.

    A 10,000-character query becomes a `tsquery` with 10,000 terms, which is a
    planner hazard on the hot path. Truncated, because somebody who pastes a
    paragraph usually means to search for something inside it.

    ⚠️ The bound is on *terms*, not on the output's length. Each ` ` becomes
    ` & `, so the expression is necessarily longer than the input that produced
    it — asserting on `len` would be asserting the wrong thing about a limit
    that exists to cap planner work.
    """
    parsed = parse_search("asha " * 500)
    terms = parsed.tsquery.split(" & ")

    # A term needs at least one character plus a separator, so the truncated
    # input cannot yield more than half its length in terms.
    assert len(terms) <= MAX_SEARCH_LENGTH // 2
    assert len(terms) < 500, "the paste was not truncated at all"


# ─── Mobile-suffix search (AC-M1-002) ────────────────────────────────────


def test_the_last_digits_of_a_mobile_are_searchable() -> None:
    """🔒 AC-M1-002 names this exactly: "the last 4 digits of a mobile"."""
    assert MIN_MOBILE_SUFFIX_DIGITS == 4
    assert parse_search("9876").mobile_suffix == "9876"


def test_a_phone_number_keeps_its_punctuation_out_of_the_suffix() -> None:
    """A practitioner types the number as they read it; the digits are what
    match. `+91 98765 43210` and `9876543210` must find the same person."""
    assert parse_search("+91 98765 43210").mobile_suffix == "919876543210"
    assert parse_search("98765-43210").mobile_suffix == "9876543210"
    assert parse_search("(98765) 43210").mobile_suffix == "9876543210"


def test_three_digits_are_too_few_to_be_a_phone_number() -> None:
    """Below the floor, a numeric query is a name fragment — a flat number, a
    batch code — and treating it as a suffix would bury the real match."""
    assert parse_search("987").mobile_suffix == ""


def test_a_name_containing_a_digit_is_not_a_phone_number() -> None:
    """🔒 "Priya 2" is plainly a name.

    Treated as a suffix search it would return every client whose number ends
    in 2 — the one match buried under the caseload.
    """
    assert parse_search("Priya 2").mobile_suffix == ""
    assert parse_search("Flat 9876").mobile_suffix == ""


def test_a_numeric_query_is_searched_both_ways() -> None:
    """⚠️ Both halves populated at once, and that is the point.

    "9876" is a plausible mobile suffix *and* a plausible name fragment. Running
    both and unioning is what stops a reasonable query returning nothing.
    """
    parsed = parse_search("9876")
    assert parsed.tsquery == "9876:*"
    assert parsed.mobile_suffix == "9876"
    assert not parsed.is_empty


def test_an_empty_search_is_empty_in_both_halves() -> None:
    """🔒 `is_empty` gates the WHERE clause entirely.

    If it were ever true while a half was populated, `_apply_search` would build
    an `or_()` over no predicates; if false while both were blank, it would
    filter on nothing. Both are silent.
    """
    assert SearchTerms("", "").is_empty
    assert not SearchTerms("asha:*", "").is_empty
    assert not SearchTerms("", "9876").is_empty


# ─── Sort parsing (FR-M1-022, API §6.3) ──────────────────────────────────


def test_no_sort_asked_for_is_recent_activity_first() -> None:
    """🔒 The list's job is "who needs me now", not "who exists".

    Alphabetical is a directory, and a practitioner opening the app is not
    looking one person up.
    """
    parsed = parse_sort(None)
    assert parsed.field is ClientSort.RECENT_ACTIVITY
    assert parsed.descending


def test_each_sort_takes_the_direction_its_name_implies() -> None:
    """ "Recent" and "newest" mean descending; a reverse-alphabetical directory
    is nobody's intent."""
    assert parse_sort("name") == parse_sort("name")
    assert not parse_sort("name").descending
    assert parse_sort("created").descending
    assert parse_sort("recent_activity").descending


def test_a_minus_prefix_reverses_the_natural_direction() -> None:
    """API §6.3's convention."""
    assert parse_sort("-name").descending
    assert parse_sort("-name").field is ClientSort.NAME


def test_an_unknown_sort_falls_back_rather_than_raising() -> None:
    """⚠️ A stale bookmark naming a renamed sort should show the list, not a
    validation error about a parameter the practitioner never typed."""
    for raw in ("nonsense", "-nonsense", "full_name", "1", "-"):
        assert parse_sort(raw).field is ClientSort.RECENT_ACTIVITY


def test_the_sort_vocabulary_is_closed() -> None:
    """🔒 Every value must be index-backed (migration 0013).

    ⚠️ This is a change-detector on purpose. Adding a sort without adding its
    index is an unindexed ORDER BY on the largest table in the product, and the
    symptom — a list that is merely slow — is one nobody files a bug about.
    """
    assert {option.value for option in ClientSort} == {"name", "recent_activity", "created"}


# ─── Bounds ──────────────────────────────────────────────────────────────


def test_archived_clients_are_excluded_unless_asked_for() -> None:
    """🔒 DB §22.2 — archived clients are out of lists, search and messaging.

    ⚠️ `ONLY` exists so a client archived by mistake can be found and restored
    (EC-M1-02). Without it the archive is a one-way door.
    """
    assert {option.value for option in ArchivedFilter} == {"exclude", "only", "include"}
    assert ArchivedFilter.EXCLUDE.value == "exclude"


def test_a_bulk_reassignment_is_bounded() -> None:
    """🔒 EC-M1-04 — every request is one transaction.

    An unbounded batch is a long-running write holding row locks across a
    practitioner's whole list. 100 is four pages of 25, so a selection spanning
    several pages does not discover the limit mid-task.
    """
    assert MAX_BULK_REASSIGN == 100
