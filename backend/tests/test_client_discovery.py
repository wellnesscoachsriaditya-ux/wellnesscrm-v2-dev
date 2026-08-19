"""The client list's cursor and filter assembly — FR-M1-021/022, ADR-A05.

🔒 No database. ``modules.clients.discovery`` is mostly a query, and the query is
asserted against a real cluster in ``tests/integration/test_discovery.py``. What
is worth testing without one is the part that has no SQL in it and the highest
cost of being wrong: **the cursor**.

⚠️ A broken cursor does not raise. It skips a client, or shows one twice, and the
practitioner reads that as "the list is missing somebody" — the failure this
whole slice's docstrings keep returning to, because it is invisible.
"""

from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from app.kernel.clients import ClientStage
from app.kernel.discovery import ArchivedFilter, ClientSort
from app.modules.clients.discovery import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    ClientCursor,
    ClientPage,
    _decode_sort_value,
    _encode_sort_value,
    build_filters,
)
from app.modules.clients.models import Client

CLIENT = uuid.UUID("33333333-0000-4000-8000-000000000001")
OWNER = uuid.UUID("33333333-0000-4000-8000-000000000002")
TAG = uuid.UUID("33333333-0000-4000-8000-000000000003")
MOMENT = datetime(2026, 8, 9, 10, 30, tzinfo=UTC)


def _row(*, full_name: str, created_at: datetime, updated_at: datetime) -> Client:
    """A `Client` carrying only the three attributes the cursor reads.

    ⚠️ Unflushed and sessionless on purpose. NOT NULL is a constraint the
    *database* enforces, so an in-memory instance needs nothing but the columns
    under test — and using the real model means a renamed column fails here
    rather than passing against a stub that agreed with the old name.
    """
    return Client(full_name=full_name, created_at=created_at, updated_at=updated_at)


# ─── The cursor (ADR-A05) ────────────────────────────────────────────────


def test_a_cursor_survives_a_round_trip() -> None:
    """The baseline: what `encode` writes, `decode` must read back."""
    cursor = ClientCursor(sort_value=MOMENT.isoformat(), client_id=CLIENT)
    decoded = ClientCursor.decode(cursor.encode())

    assert decoded == cursor


def test_a_name_containing_the_separator_still_decodes() -> None:
    """🔒 The reason `decode` uses `rpartition` rather than `split`.

    A client legitimately named "Asha | Menon" produces a cursor with two
    separators. Splitting on the first would read the id as "Menon" and fail to
    parse, silently restarting the list from page one — an infinite scroll that
    never advances past the second page.
    """
    cursor = ClientCursor(sort_value="asha | menon", client_id=CLIENT)
    decoded = ClientCursor.decode(cursor.encode())

    assert decoded is not None
    assert decoded.sort_value == "asha | menon"
    assert decoded.client_id == CLIENT


def test_an_unusable_cursor_decodes_to_nothing_rather_than_raising() -> None:
    """🔒 A stale bookmark shows the first page, not an error.

    The practitioner never typed this parameter, so a 422 naming it is a dead end
    they cannot act on. `list_clients` reads `None` as "start from the beginning".
    """
    for raw in ("", "|", "no-separator", f"{MOMENT.isoformat()}|not-a-uuid", "|" + str(CLIENT)):
        assert ClientCursor.decode(raw) is None


def test_a_cursor_from_a_different_sort_is_refused_rather_than_coerced() -> None:
    """🔒 The keyset comparison is only coherent within one ordering.

    A timestamp cursor arriving with `?sort=name` names a position that does not
    exist in an alphabetical ordering. Starting over is the honest answer;
    comparing a timestamp against `lower(full_name)` would skip rows.
    """
    assert _decode_sort_value(MOMENT.isoformat(), ClientSort.NAME) == MOMENT.isoformat()
    assert _decode_sort_value("asha menon", ClientSort.RECENT_ACTIVITY) is None
    assert _decode_sort_value("asha menon", ClientSort.CREATED) is None


def test_each_sort_encodes_the_value_its_ordering_compares() -> None:
    """🔒 The cursor's first half must be the *same expression* as the ORDER BY.

    ⚠️ The name case is lowercased, matching `func.lower(Client.full_name)` and
    `ix_clients__tenant_name`. A cursor carrying "Zara" while the ordering
    compares "zara" straddles the boundary at every capitalised name.
    """
    created = datetime(2026, 1, 1, tzinfo=UTC)
    row = _row(full_name="Zara Khan", created_at=created, updated_at=MOMENT)

    assert _encode_sort_value(row, ClientSort.NAME) == "zara khan"
    assert _encode_sort_value(row, ClientSort.CREATED) == created.isoformat()
    assert _encode_sort_value(row, ClientSort.RECENT_ACTIVITY) == MOMENT.isoformat()


def test_a_timestamp_cursor_round_trips_through_its_encoding() -> None:
    """⚠️ The pairing that matters: encode then decode must yield the value the
    query compares, not a string that merely looks like it.

    A timezone dropped in either direction shifts the page boundary by hours,
    which shows up as a handful of clients missing from the second page.
    """
    row = _row(full_name="Asha", created_at=MOMENT, updated_at=MOMENT)
    encoded = _encode_sort_value(row, ClientSort.RECENT_ACTIVITY)

    assert _decode_sort_value(encoded, ClientSort.RECENT_ACTIVITY) == MOMENT


# ─── Filter assembly (FR-M1-022) ─────────────────────────────────────────


def test_no_filters_asked_for_still_excludes_archived_clients() -> None:
    """🔒 FR-M1-014 / DB §22.2 — the default is not "everything"."""
    filters = build_filters()

    assert filters.archived is ArchivedFilter.EXCLUDE
    assert filters.search.is_empty
    assert filters.stages == frozenset()
    assert filters.tag_ids == frozenset()
    assert filters.owner_user_ids == frozenset()


def test_an_empty_search_string_is_not_a_search() -> None:
    """⚠️ `""` and `None` must behave alike.

    A practitioner who clears the box means "show me everyone". A blank string
    reaching `parse_search` is harmless today, and the falsy check is what keeps
    it that way if the parser's floor ever changes.
    """
    assert build_filters(search="").search.is_empty
    assert build_filters(search=None).search.is_empty


def test_a_search_reaches_the_filters_parsed_rather_than_raw() -> None:
    """🔒 The one input a practitioner can type anything into is parsed here.

    Nothing downstream concatenates it into SQL — see
    ``tests/test_kernel_discovery.py`` for the operator-stripping contract.
    """
    filters = build_filters(search="O'Brien")

    assert filters.search.tsquery == "OBrien:*"
    assert "'" not in filters.search.tsquery


def test_filters_are_frozen_so_a_page_cannot_mutate_the_next_one() -> None:
    """⚠️ Both the dataclass and its collections.

    `list_clients` is called once per page with filters the caller may reuse; a
    mutable set could be widened by a later request and the change would apply
    retroactively to a cursor already handed out.
    """
    filters = build_filters(stages=frozenset({ClientStage.ACTIVE}), tag_ids=frozenset({TAG}))

    assert isinstance(filters.stages, frozenset)
    assert isinstance(filters.tag_ids, frozenset)

    # ⚠️ The refusal itself, not `__dataclass_params__.frozen` — that is a
    # private CPython attribute mypy does not know about, and asserting on the
    # *behaviour* is what a reader needs to see anyway.
    with pytest.raises(FrozenInstanceError):
        filters.archived = ArchivedFilter.INCLUDE  # type: ignore[misc]


def test_selected_values_arrive_intact() -> None:
    """The plumbing, asserted once so a renamed keyword is caught here rather
    than by a route test that would blame the router."""
    filters = build_filters(
        stages=frozenset({ClientStage.ACTIVE, ClientStage.PAUSED}),
        tag_ids=frozenset({TAG}),
        owner_user_ids=frozenset({OWNER}),
        archived=ArchivedFilter.ONLY,
    )

    assert filters.stages == frozenset({ClientStage.ACTIVE, ClientStage.PAUSED})
    assert filters.tag_ids == frozenset({TAG})
    assert filters.owner_user_ids == frozenset({OWNER})
    assert filters.archived is ArchivedFilter.ONLY


# ─── The page envelope (API §6.1) ────────────────────────────────────────


def test_a_page_without_a_cursor_has_nothing_more() -> None:
    """🔒 `has_more` is derived, not passed in.

    The UI's "load more" button reads it, and a page that reported more while
    carrying no cursor would render a button that cannot do anything.
    """
    assert not ClientPage(items=[], next_cursor=None).has_more
    assert ClientPage(items=[], next_cursor="x|y").has_more


def test_a_total_is_absent_unless_it_was_asked_for() -> None:
    """🔒 API §6.1 — a `COUNT(*)` on every keystroke is the expensive half of a
    search that must answer inside NFR-005's 300 ms."""
    assert ClientPage(items=[], next_cursor=None).total is None


def test_the_page_size_ceiling_leaves_the_default_room_to_grow() -> None:
    """⚠️ A change-detector on the pair, not on either number.

    They are set in different documents (API §6.1 for both, NFR-005 for the
    ceiling's justification) and read together by `list_clients`, so the
    relationship is what matters.
    """
    assert DEFAULT_PAGE_SIZE == 25
    assert MAX_PAGE_SIZE == 100
    assert DEFAULT_PAGE_SIZE < MAX_PAGE_SIZE
