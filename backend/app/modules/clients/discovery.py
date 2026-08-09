"""Listing, searching and filtering clients — FR-M1-021/022, NFR-005.

🔒 **The screen a practitioner lives in.** Every other client route answers a
question about one person; this one answers "who is there", which is the question
the app opens on. A wrong answer here is invisible — a filter that silently drops
somebody looks exactly like a practice that does not have them.

🔒 **Practitioner scoping is applied in the query, not after it** (AC-M1-006,
FR-M0-017). ``load_for_access`` cannot help: it answers "may I open *this*
client", one row at a time, and a list that loaded everything and then filtered
would leak the total count, page short, and paginate incoherently. So the
ownership predicate is a WHERE clause, and the owner's exemption is the absence
of it.

⚠️ **One query, no N+1.** API §7.1's list item embeds the owner's name and the
client's tags. Loading those per row is the classic list-view mistake — 25 rows
becomes 51 queries. Tags arrive through a single aggregated join; the owner's
name through one join to ``users``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Select, and_, func, literal_column, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnClause, ColumnElement

from app.kernel.clients import ClientStage
from app.kernel.context import UserRole
from app.kernel.discovery import (
    ArchivedFilter,
    ClientSort,
    SearchTerms,
    SortOrder,
    parse_search,
)
from app.kernel.models import User
from app.modules.clients.models import Client, ClientAssignment, ClientTag, Tag

#: The default page size for the client list — API §6.1.
DEFAULT_PAGE_SIZE = 25

#: 🔒 The ceiling (API §6.1). NFR-005 budgets 300 ms for a search; 100 rows with
#: their tags aggregated stays inside it, and a larger page is a scroll nobody
#: reads rather than a feature.
MAX_PAGE_SIZE = 100

#: 🔒 The generated tsvector migration 0009 adds by raw ALTER — FR-M1-021.
#:
#: ⚠️ Named as a literal rather than mapped on ``Client``, and the reason is in
#: ``models.Client``: the schema-drift test reads ``op.create_table`` and cannot
#: see an ALTER-added column, so mapping it would look like drift and the fix
#: would be to weaken the one test keeping a hand-written migration honest.
#:
#: 🔒 **Table-qualified deliberately.** A bare ``search_vector`` would resolve by
#: luck; qualified, it stays correct if this predicate is ever used in a
#: statement that joins another table. No user input reaches this string — the
#: query text is bound as a parameter to ``to_tsquery`` below.
_SEARCH_VECTOR: ColumnClause[Any] = literal_column("clients.search_vector")

#: 🔒 Migration 0014 adds ``mobile_reversed`` as a generated column and maps it
#: here as a plain column. Unlike ``search_vector`` (which 0009 adds by raw
#: ALTER, invisible to the schema-drift test), 0014 is *not* a hand-written
#: migration of a kind the drift test cannot see — but the column is still named
#: explicitly for the same reason ``search_vector`` is: mapping it on ``Client``
#: is optional, and the query layer is the only consumer.
_MOBILE_REVERSED: ColumnClause[Any] = literal_column("clients.mobile_reversed")


@dataclass(frozen=True, slots=True)
class ClientFilters:
    """What narrows the list — FR-M1-022.

    ⚠️ Every collection field is OR *within* itself and AND *across* fields
    (API §6.2). "Active or paused, tagged PCOS" is the query a practitioner
    means when they tick two stages and one tag; making stages AND would return
    nothing, which is the more confusing failure.
    """

    #: Free text from the search box. Parsed, never interpolated.
    search: SearchTerms = field(default_factory=lambda: SearchTerms("", ""))
    stages: frozenset[ClientStage] = frozenset()
    #: 🔒 AND across tags, not OR — see :func:`_apply_tag_filter`.
    tag_ids: frozenset[uuid.UUID] = frozenset()
    owner_user_ids: frozenset[uuid.UUID] = frozenset()
    archived: ArchivedFilter = ArchivedFilter.EXCLUDE


@dataclass(frozen=True, slots=True)
class ClientListItem:
    """One row of the list — API §7.1's response item.

    ⚠️ Deliberately not the ORM ``Client``. The list's shape is a contract with
    the UI, and returning rows would couple it to every column of the table and
    invite a lazy load per row inside the response serialiser.

    ⏳ ``is_at_risk``, ``last_activity_at`` and ``active_plan_version_id`` are
    absent. API §7.1 lists them, and DDR-13 is explicit that at-risk state is
    *precomputed* into ``client_daily_metrics`` — a table S7 creates and a
    nightly job populates. Deriving them live here would be the read-time
    computation DDR-13 exists to reject, and inventing the column now would ship
    a field that is always ``false``.
    """

    id: uuid.UUID
    full_name: str
    stage: ClientStage
    mobile: str | None
    email: str | None
    city: str | None
    dietary_class: str | None
    owner_user_id: uuid.UUID
    #: 🔒 Denormalised into the response to avoid an N+1 (API §7.1). ``None``
    #: only if the owning user row has been removed, which the FK prevents.
    owner_name: str | None
    tags: tuple[tuple[uuid.UUID, str, str], ...]
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ClientPage:
    """One page, plus where to resume."""

    items: list[ClientListItem]
    next_cursor: str | None
    #: 🔒 Only when the caller asked (`include_total`). API §6.1 omits it by
    #: default because a `COUNT(*)` on every keystroke is the expensive half of
    #: a search that must answer in 300 ms.
    total: int | None = None

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None


# ─── The cursor (ADR-A05) ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ClientCursor:
    """Where a page resumes.

    🔒 Two parts, for the reason the timeline's cursor has two: the sort column
    is not unique. A bulk reassignment stamps ``updated_at`` on a whole batch in
    one transaction, so ties are routine rather than theoretical, and a cursor
    without a unique tiebreaker skips or repeats rows at exactly that boundary.

    ⚠️ The sort key is carried as text because it may be a timestamp or a name.
    Decoding re-parses it against the sort the *current* request asked for,
    which is what makes a cursor from a different sort order fail closed rather
    than paginate nonsensically.
    """

    sort_value: str
    client_id: uuid.UUID

    def encode(self) -> str:
        """Opaque to the caller (ADR-A05), trivially decodable by us.

        ⚠️ Unsigned, like the timeline's. A cursor names a position in the
        caller's own tenant-scoped, authorization-filtered result set; both
        checks run again on the next page, so forging one buys a different
        offset into data the caller may already read.
        """
        return f"{self.sort_value}|{self.client_id}"

    @classmethod
    def decode(cls, raw: str) -> ClientCursor | None:
        """Parse a cursor, or ``None`` if it is unusable.

        Returns ``None`` rather than raising: a stale bookmark should show the
        first page, not an error about a parameter the practitioner never typed.
        """
        value, separator, identifier = raw.rpartition("|")
        if not separator or not value:
            return None
        try:
            return cls(sort_value=value, client_id=uuid.UUID(identifier))
        except ValueError:
            return None


def _sort_column(sort: ClientSort) -> Any:
    """The column each sort orders by.

    🔒 Every one of these is indexed by migration 0013 — API §6.3 allowlists
    sort fields precisely so an unindexed ORDER BY cannot reach production.
    """
    if sort is ClientSort.NAME:
        # Case-insensitive, matching `ix_clients__tenant_name`. A practitioner
        # scanning an alphabetical list does not expect "asha" after "Zara".
        return func.lower(Client.full_name)
    if sort is ClientSort.CREATED:
        return Client.created_at
    return Client.updated_at


def _encode_sort_value(item: Client, sort: ClientSort) -> str:
    """The cursor's first half, in a form :func:`_decode_sort_value` reverses."""
    if sort is ClientSort.NAME:
        return item.full_name.lower()
    if sort is ClientSort.CREATED:
        return item.created_at.isoformat()
    return item.updated_at.isoformat()


def _decode_sort_value(raw: str, sort: ClientSort) -> Any | None:
    """Re-parse a cursor's sort value against the current sort.

    🔒 Returns ``None`` on a mismatch — a timestamp cursor arriving with
    `?sort=name` — which the caller turns into "start from the beginning". That
    is the honest response: the position named by that cursor does not exist in
    this ordering, and paginating from a coerced value would silently skip rows.
    """
    if sort is ClientSort.NAME:
        return raw
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


# ─── Scoping (AC-M1-006, FR-M0-017) ──────────────────────────────────────


def _visible_to(
    statement: Select[Any], *, actor_user_id: uuid.UUID, role: UserRole | None
) -> Select[Any]:
    """Restrict the list to clients this practitioner may see.

    🔒 **The list half of AC-M1-006.** ``kernel.authz.owner_or_assigned`` decides
    it for one client; this is the same rule expressed as a predicate, and the
    two must agree — a client reachable by the detail route but missing from the
    list is as much a bug as the reverse.

    🔒 An **owner sees the whole tenant** (FR-M0-017), so the predicate is simply
    absent for them. Everyone else sees what they own or have been granted.

    ⚠️ ``EXISTS`` rather than a join. A join against ``client_assignments``
    would multiply rows for a client with several grants, and the duplicate
    would then be silently absorbed by a `DISTINCT` that also defeats the
    keyset cursor.
    """
    if role is UserRole.OWNER:
        return statement

    granted = select(1).where(
        ClientAssignment.client_id == Client.id,
        ClientAssignment.user_id == actor_user_id,
        ClientAssignment.revoked_at.is_(None),
    )
    return statement.where(
        or_(Client.owner_user_id == actor_user_id, granted.exists()),
    )


# ─── Filters (FR-M1-022) ─────────────────────────────────────────────────


def _apply_search(statement: Select[Any], terms: SearchTerms) -> Select[Any]:
    """Name, email and mobile — FR-M1-021, AC-M1-002.

    🔒 Two access paths, unioned with OR because a numeric query is plausibly
    both. The tsquery half is served by ``ix_clients__search``; the suffix half
    by ``ix_clients__tenant_mobile_reversed``, which is why the predicate is
    written as range operators on the *reversed* column — see migrations 0013
    and 0014. Under RLS, only leakproof predicates can be pushed into an index
    condition; ``LIKE`` and ``reverse()`` are not leakproof, so a functional
    index on ``reverse(mobile)`` is unusable. The column is a plain STORED
    generated column, and the ``~>=~`` / ``~<~`` operators are leakproof.
    """
    if terms.is_empty:
        return statement

    predicates: list[ColumnElement[bool]] = []
    if terms.tsquery:
        # ⚠️ Bound as a parameter, never interpolated. `parse_search` has already
        # stripped the operator characters that would make this a syntax error
        # (or worse) — this is the second half of that contract.
        predicates.append(_SEARCH_VECTOR.op("@@")(func.to_tsquery("simple", terms.tsquery)))
    if terms.mobile_suffix:
        # Range operators on the reversed column. `mobile_reversed ~>=~ 'suffix'
        # AND mobile_reversed ~<~ 'suffix_upper'` is a prefix match the btree can
        # seek — semantically identical to `mobile_reversed LIKE 'suffix%'` but
        # leakproof, so it lands in the Index Cond under RLS. The upper bound is
        # the suffix incremented at the last character: '9876' → '9877'.
        rev = terms.mobile_suffix[::-1]
        upper = rev[:-1] + chr(ord(rev[-1]) + 1)
        predicates.append(
            and_(
                _MOBILE_REVERSED.op("~>=~")(rev),
                _MOBILE_REVERSED.op("~<~")(upper),
            )
        )

    return statement.where(or_(*predicates))


def _apply_tag_filter(statement: Select[Any], tag_ids: frozenset[uuid.UUID]) -> Select[Any]:
    """Narrow to clients carrying **all** the named tags.

    🔒 AND rather than OR, and this is the one place the API §6.2 default is
    deliberately not followed. Tags are how a practitioner narrows a caseload —
    "PCOS *and* post-natal" is a cohort; "PCOS *or* post-natal" is a longer list
    than they started with, which is the opposite of what picking a second
    filter is for.

    ⚠️ One correlated EXISTS per tag rather than a `GROUP BY ... HAVING count`.
    The having-count form needs a join that multiplies rows before it collapses
    them, which fights the keyset cursor; each EXISTS is an index-only probe
    into ``ix_client_tags__tag_client``.
    """
    for tag_id in sorted(tag_ids):
        statement = statement.where(
            select(1).where(ClientTag.client_id == Client.id, ClientTag.tag_id == tag_id).exists()
        )
    return statement


def _apply_archived(statement: Select[Any], archived: ArchivedFilter) -> Select[Any]:
    """FR-M1-014 / DB §22.2 — archived clients are out of every working view.

    ⚠️ ``ONLY`` exists so a client archived by mistake can be found and restored
    (EC-M1-02). Without it the archive is a one-way door.
    """
    if archived is ArchivedFilter.EXCLUDE:
        return statement.where(Client.archived_at.is_(None))
    if archived is ArchivedFilter.ONLY:
        return statement.where(Client.archived_at.is_not(None))
    return statement


def _apply_filters(statement: Select[Any], filters: ClientFilters) -> Select[Any]:
    """Every narrowing clause, in the order a reader would expect them."""
    statement = _apply_archived(statement, filters.archived)
    statement = _apply_search(statement, filters.search)

    if filters.stages:
        statement = statement.where(Client.stage.in_(filters.stages))
    if filters.owner_user_ids:
        statement = statement.where(Client.owner_user_id.in_(filters.owner_user_ids))

    return _apply_tag_filter(statement, filters.tag_ids)


# ─── The read ────────────────────────────────────────────────────────────


async def list_clients(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    actor_role: UserRole | None,
    filters: ClientFilters | None = None,
    sort: SortOrder,
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    include_total: bool = False,
) -> ClientPage:
    """The client list — FR-M1-021/022, NFR-005.

    Args:
        actor_user_id: 🔒 Whose view this is. Combined with ``actor_role`` to
            apply AC-M1-006 *inside* the query.
        include_total: API §6.1 — off by default, because a `COUNT(*)` on every
            keystroke is what makes a fast search slow.

    ⚠️ The caller has already been authorized for the *action* (``client.list``).
    This applies the row-level half; neither is sufficient alone.
    """
    active_filters = filters if filters is not None else ClientFilters()
    page_size = max(1, min(limit, MAX_PAGE_SIZE))

    statement: Select[Any] = select(Client).where(Client.tenant_id == tenant_id)
    statement = _visible_to(statement, actor_user_id=actor_user_id, role=actor_role)
    statement = _apply_filters(statement, active_filters)

    total = await _count(session, statement) if include_total else None

    column = _sort_column(sort.field)
    if cursor is not None:
        statement = _apply_cursor(statement, cursor, sort=sort, column=column)

    # 🔒 The tiebreaker is part of the ordering, not decoration — see
    # `ClientCursor`. Both halves must point the same way or the keyset
    # comparison below stops matching the order.
    ordering = (
        (column.desc(), Client.id.desc()) if sort.descending else (column.asc(), Client.id.asc())
    )
    statement = statement.order_by(*ordering)

    rows = list(await session.scalars(statement.limit(page_size + 1)))

    has_more = len(rows) > page_size
    visible = rows[:page_size]
    next_cursor = (
        ClientCursor(
            sort_value=_encode_sort_value(visible[-1], sort.field),
            client_id=visible[-1].id,
        ).encode()
        if has_more and visible
        else None
    )

    return ClientPage(
        items=await _project(session, visible, tenant_id=tenant_id),
        next_cursor=next_cursor,
        total=total,
    )


def _apply_cursor(statement: Select[Any], raw: str, *, sort: SortOrder, column: Any) -> Select[Any]:
    """Resume strictly past the cursor's row, in the index's own order.

    🔒 A row comparison rather than two chained conditions: `(sort, id) < (a, b)`
    is one ordering key, which is both what the index can seek on and what makes
    a tie impossible to straddle.
    """
    decoded = ClientCursor.decode(raw)
    if decoded is None:
        return statement

    value = _decode_sort_value(decoded.sort_value, sort.field)
    if value is None:
        # A cursor from a different sort order. Starting over is the honest
        # answer — see `_decode_sort_value`.
        return statement

    key = tuple_(column, Client.id)
    return statement.where(
        key < (value, decoded.client_id) if sort.descending else key > (value, decoded.client_id)
    )


async def _count(session: AsyncSession, statement: Select[Any]) -> int:
    """`COUNT(*)` over the same filters, without the ordering or the page.

    ⚠️ Built from the filtered statement rather than rebuilt, so a filter added
    above cannot be forgotten here — which would report a total that disagrees
    with the rows beside it.
    """
    subquery = statement.order_by(None).subquery()
    total = await session.scalar(select(func.count()).select_from(subquery))
    return int(total or 0)


async def _project(
    session: AsyncSession, rows: list[Client], *, tenant_id: uuid.UUID
) -> list[ClientListItem]:
    """Turn client rows into list items, resolving owners and tags in bulk.

    🔒 **Two queries regardless of page size**, which is the whole point. The
    obvious implementation — a property access per row — is the N+1 that makes a
    list view slow at exactly the caseload where it starts to matter.
    """
    if not rows:
        return []

    owner_ids = {row.owner_user_id for row in rows}
    client_ids = [row.id for row in rows]

    owner_rows = (
        await session.execute(select(User.id, User.full_name).where(User.id.in_(owner_ids)))
    ).all()
    owner_names: dict[uuid.UUID, str] = {row[0]: row[1] for row in owner_rows}

    tag_rows = (
        await session.execute(
            select(ClientTag.client_id, Tag.id, Tag.name, Tag.colour)
            .join(Tag, Tag.id == ClientTag.tag_id)
            .where(
                ClientTag.tenant_id == tenant_id,
                ClientTag.client_id.in_(client_ids),
                # ⚠️ Archived tags are withheld from the list even though the
                # junction row survives (`archive_tag` is a soft delete). A
                # retired label reappearing on 40 clients' rows would look like
                # the archive did nothing.
                Tag.archived_at.is_(None),
            )
            .order_by(func.lower(Tag.name))
        )
    ).all()

    tags_by_client: dict[uuid.UUID, list[tuple[uuid.UUID, str, str]]] = {}
    for client_id, tag_id, name, colour in tag_rows:
        tags_by_client.setdefault(client_id, []).append((tag_id, name, str(colour)))

    return [
        ClientListItem(
            id=row.id,
            full_name=row.full_name,
            stage=row.stage,
            mobile=row.mobile,
            email=row.email,
            city=row.city,
            dietary_class=row.dietary_class.value if row.dietary_class else None,
            owner_user_id=row.owner_user_id,
            owner_name=owner_names.get(row.owner_user_id),
            tags=tuple(tags_by_client.get(row.id, ())),
            archived_at=row.archived_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


def build_filters(
    *,
    search: str | None = None,
    stages: frozenset[ClientStage] | None = None,
    tag_ids: frozenset[uuid.UUID] | None = None,
    owner_user_ids: frozenset[uuid.UUID] | None = None,
    archived: ArchivedFilter = ArchivedFilter.EXCLUDE,
) -> ClientFilters:
    """Assemble filters from raw request values, parsing the search box safely."""
    return ClientFilters(
        search=parse_search(search) if search else SearchTerms("", ""),
        stages=stages or frozenset(),
        tag_ids=tag_ids or frozenset(),
        owner_user_ids=owner_user_ids or frozenset(),
        archived=archived,
    )


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "ClientCursor",
    "ClientFilters",
    "ClientListItem",
    "ClientPage",
    "build_filters",
    "list_clients",
]
