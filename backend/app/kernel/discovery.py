"""Finding a client — the rules behind search, filtering and sorting.

🔒 **FR-M1-021/022, AC-M1-002, NFR-005.** The client list is the screen a
practitioner lives in, and the one place where a wrong answer is invisible: a
filter that silently excludes somebody looks identical to a practice that does
not have them.

As with the rest of the kernel, this module is rules only — pure functions over
values, testable without a database. The query itself lives in
``app.modules.clients.discovery``.

🔒 **Search is parsed here, not interpolated there.** A search box is the one
input a practitioner can type anything into, and it reaches a `tsquery` — which
has its own syntax. :func:`parse_search` turns free text into a structure the
query layer binds as parameters; nothing downstream concatenates user text into
SQL. That is the whole reason the parsing is separated from the querying.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass

# ─── Search (FR-M1-021, AC-M1-002) ───────────────────────────────────────

#: 🔒 The shortest query worth running. One character matches most of a
#: caseload, so the result is a full scan the practitioner cannot use — and
#: FR-M1-021 wants results *as they type*, which means this fires on every
#: keystroke. Two is where a prefix starts to discriminate.
MIN_SEARCH_LENGTH = 2

#: 🔒 The longest. A search box is a paste target, and a 10,000-character query
#: becomes a `tsquery` with 10,000 terms — a planner hazard on the hot path.
#: Truncated rather than refused: somebody who pastes a paragraph meant to
#: search for something in it.
MAX_SEARCH_LENGTH = 100

#: Digits that make a mobile-suffix search worth attempting — AC-M1-002 says
#: "the last 4 digits", so four is the documented floor.
MIN_MOBILE_SUFFIX_DIGITS = 4

#: 🔒 Everything `to_tsquery` treats as an operator. A practitioner typing
#: "O'Brien" or "priya & asha" means those as *text*; passed through, `&` and
#: `!` would be parsed as boolean operators and `'` would open a quoted phrase
#: that never closes — a syntax error rendered as "search is broken".
_TSQUERY_OPERATORS = re.compile(r"[&|!()<>:*'\"\\]")

#: Runs of whitespace, collapsed so "asha   menon" is one query, not three terms
#: with empty strings between them.
_WHITESPACE_RUN = re.compile(r"\s+")

#: Anything that is not a digit — used to decide whether a query is "a phone
#: number" regardless of how the practitioner spaced or punctuated it.
_NON_DIGIT = re.compile(r"\D")


@dataclass(frozen=True, slots=True)
class SearchTerms:
    """A parsed search box — what the query layer needs, already made safe.

    ⚠️ Both halves can be populated at once, and that is the point. "9876" is a
    plausible mobile suffix *and* a plausible name fragment (a flat number, a
    batch code); running both and unioning is what stops a practitioner's
    perfectly reasonable query returning nothing.
    """

    #: The `to_tsquery` expression, already escaped and prefix-marked. Empty when
    #: the text held nothing searchable.
    tsquery: str
    #: Digits to match against the *end* of a mobile number. Empty when the query
    #: was too short or held too few digits to be a phone number.
    mobile_suffix: str

    @property
    def is_empty(self) -> bool:
        """Whether this query can match anything at all.

        🔒 An empty search must return the unfiltered list, not zero rows. A
        practitioner who clears the box means "show me everyone" — the natural
        reading, and the one that avoids an empty screen with no explanation.
        """
        return not self.tsquery and not self.mobile_suffix


def parse_search(raw: str) -> SearchTerms:
    """Turn a search box into terms the query layer can bind — FR-M1-021.

    🔒 **Prefix matching on the last token, exact on the rest.** FR-M1-021 wants
    results as the practitioner types, so the word being typed is necessarily
    incomplete: "ash" must find "Asha". Earlier tokens are complete words the
    practitioner finished typing, and prefix-matching them too would make
    "asha men" match "ashamed mention" — noise that grows with the caseload.

    ⚠️ Operators are **stripped, not escaped**. `to_tsquery` has no escape
    syntax worth relying on, and `plainto_tsquery` — which would handle this —
    cannot express the trailing prefix match this needs. Removing the characters
    is the only approach that is both safe and capable of `:*`.
    """
    trimmed = _WHITESPACE_RUN.sub(" ", raw.strip())[:MAX_SEARCH_LENGTH]
    if len(trimmed) < MIN_SEARCH_LENGTH:
        return SearchTerms(tsquery="", mobile_suffix="")

    digits = _NON_DIGIT.sub("", trimmed)
    # 🔒 Only when the query is *mostly* digits. "Priya 2" holds a digit and is
    # plainly a name; treating it as a phone-number search would return every
    # client whose number ends in 2, burying the one match.
    suffix = digits if len(digits) >= MIN_MOBILE_SUFFIX_DIGITS and _is_numeric(trimmed) else ""

    tokens = [
        stripped for token in trimmed.split(" ") if (stripped := _TSQUERY_OPERATORS.sub("", token))
    ]
    if not tokens:
        return SearchTerms(tsquery="", mobile_suffix=suffix)

    # The last token gets `:*`; earlier ones are complete words. See the
    # docstring for why that asymmetry is deliberate.
    *complete, partial = tokens
    expression = " & ".join([*complete, f"{partial}:*"])
    return SearchTerms(tsquery=expression, mobile_suffix=suffix)


def _is_numeric(text: str) -> bool:
    """Whether this query is a phone number rather than a name containing digits.

    Permits the punctuation a practitioner actually types into a phone field —
    spaces, `+`, dashes, brackets — and nothing else.
    """
    return bool(text) and all(character.isdigit() or character in " +-()" for character in text)


# ─── Sorting (FR-M1-022) ─────────────────────────────────────────────────


class ClientSort(enum.StrEnum):
    """How the list may be ordered — FR-M1-022.

    🔒 **A closed enum, because every value must be indexed.** API §6.3
    allowlists sort columns per endpoint precisely so an unindexed sort cannot
    reach production. Free-text `?sort=` would be a full scan on whichever
    column somebody guessed.

    ⚠️ FR-M1-022 asks for "name, recent activity and creation date". *Recent
    activity* maps to ``updated_at`` in this slice, and that is an approximation
    worth stating: a true activity clock spans appointments, messages and plan
    events, which DDR-13 precomputes into ``client_daily_metrics`` — a table S7
    creates. ``updated_at`` moves on every write to the client record, which is
    the closest honest signal available now, and the enum value is named for the
    intent so the column beneath it can change without breaking callers.
    """

    NAME = "name"
    RECENT_ACTIVITY = "recent_activity"
    CREATED = "created"


#: 🔒 The default. Recent activity first, because the list's job is "who needs
#: me now" rather than "who exists" — alphabetical is a directory, and a
#: practitioner opening the app is not looking one person up.
DEFAULT_SORT = ClientSort.RECENT_ACTIVITY


@dataclass(frozen=True, slots=True)
class SortOrder:
    """A parsed `?sort=` parameter — API §6.3's `-` prefix convention."""

    field: ClientSort
    descending: bool


#: 🔒 Which direction each sort means when nobody says. Descending for the two
#: time columns — "recent" and "newest" are what those words mean — and
#: ascending for a name, because a reverse-alphabetical directory is nobody's
#: intent.
_NATURAL_DIRECTION: dict[ClientSort, bool] = {
    ClientSort.NAME: False,
    ClientSort.RECENT_ACTIVITY: True,
    ClientSort.CREATED: True,
}


def parse_sort(raw: str | None) -> SortOrder:
    """Read `?sort=-created` into a field and a direction — API §6.3.

    ⚠️ Falls back to the default on anything unrecognised rather than raising. A
    stale bookmark naming a sort that has been renamed should show the list, not
    a validation error about a parameter the practitioner never typed.
    """
    if not raw:
        return SortOrder(field=DEFAULT_SORT, descending=_NATURAL_DIRECTION[DEFAULT_SORT])

    descending = raw.startswith("-")
    name = raw[1:] if descending else raw
    try:
        field = ClientSort(name)
    except ValueError:
        return SortOrder(field=DEFAULT_SORT, descending=_NATURAL_DIRECTION[DEFAULT_SORT])

    # An explicit `-` always wins; a bare field name takes its natural direction.
    return SortOrder(field=field, descending=descending or _NATURAL_DIRECTION[field])


# ─── Archived clients (FR-M1-014, DB §22.2) ──────────────────────────────


class ArchivedFilter(enum.StrEnum):
    """Whether archived clients appear — FR-M1-014.

    🔒 DB §22.2: "Archived clients — excluded from lists, search, entitlement
    count, all messaging." So ``EXCLUDE`` is the default and the partial indexes
    carry the predicate.

    ⚠️ ``ONLY`` exists because a practitioner who archived somebody by mistake
    needs to find them to restore them (EC-M1-02), and a client who cannot be
    found cannot be restored. Without it the archive is a one-way door.
    """

    EXCLUDE = "exclude"
    ONLY = "only"
    INCLUDE = "include"


# ─── Bulk operations (EC-M1-04) ──────────────────────────────────────────

#: 🔒 The most clients one bulk reassignment may move. EC-M1-04's motivating
#: case is a departing practitioner's caseload, which is tens, not thousands.
#:
#: ⚠️ A bound is what makes the operation's failure mode describable: every
#: request is one transaction, so the whole batch commits or none of it does,
#: and an unbounded batch would eventually be a long-running write holding row
#: locks across a practitioner's entire list. The UI pages at 25, so 100 lets
#: somebody select several pages' worth without discovering a limit mid-task.
MAX_BULK_REASSIGN = 100
