"""The client list — API §7.1, FR-M1-021/022.

🔒 In ``platform/`` for the reason every router is: R5 forbids a module importing
``platform``. The query and its scoping live in ``modules.clients.discovery``;
this owns the HTTP shape, the query-parameter vocabulary and the cursor's
presentation.

🔒 **Scoping happens inside the query, not around it** (AC-M1-006). Unlike every
other client route, this one cannot call ``authorized_client`` — there is no
single client to authorize. The actor's id and role are passed to
``list_clients``, which turns them into a WHERE clause; an owner's exemption is
the absence of that clause. A route that omitted them would hand a practitioner
the whole tenant, which is why the arguments are required rather than optional.

⚠️ **A separate module from ``clients.py``.** That file owns one client's
lifecycle; this owns the collection. Keeping them apart is what stops the
lifecycle router growing a second personality — and the list is where the query
tuning lives, which is a different kind of change from a field validation.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import Query, Request, status
from pydantic import BaseModel, Field

from app.kernel.clients import ClientStage, DietaryClass
from app.kernel.context import get_context
from app.kernel.discovery import (
    MAX_BULK_REASSIGN,
    MAX_SEARCH_LENGTH,
    ArchivedFilter,
    ClientSort,
    parse_sort,
)
from app.modules.clients import (
    CLIENT_BULK_REASSIGN,
    CLIENT_LIST,
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    ClientListItem,
    build_filters,
    bulk_reassign_owner,
    list_clients,
)
from app.platform.http.authz import requires
from app.platform.http.pipeline import get_session, realm_router, record_audit

router = realm_router("/api/v1/app/clients", tags=["clients"])


# ─── Schemas ─────────────────────────────────────────────────────────────


class ClientTagSummary(BaseModel):
    """A tag as the list renders it — API §7.1's `{id, name, color}`.

    ⚠️ Spelled ``colour`` here, matching ``TagColour`` and the rest of the
    codebase. API §7.1 writes `color`; the inconsistency is the spec's, and
    matching the code the frontend already generates types from is worth more
    than matching the prose.
    """

    id: uuid.UUID
    name: str
    colour: str


class ClientListItemResponse(BaseModel):
    """One row of the client list — API §7.1.

    🔒 ``owner_name`` is denormalised into the response to avoid an N+1 (API
    §7.1) and because Principle 3 makes it the server's job to supply what the
    client renders rather than something the UI resolves per row.

    ⏳ ``is_at_risk``, ``last_activity_at`` and ``active_plan_version_id`` are
    absent. API §7.1 lists them and DDR-13 requires at-risk state to be
    *precomputed* into ``client_daily_metrics`` — a table S7 creates. Computing
    them live is the read-time aggregation DDR-13 exists to reject, and shipping
    a field that is permanently ``false`` would be worse than its absence.
    """

    id: uuid.UUID
    full_name: str
    stage: ClientStage
    mobile: str | None
    email: str | None
    city: str | None
    dietary_class: DietaryClass | None
    owner_user_id: uuid.UUID
    owner_name: str | None
    tags: list[ClientTagSummary]
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ClientPageInfo(BaseModel):
    """Where the next page resumes — API §6.1."""

    next_cursor: str | None = None
    has_more: bool
    #: 🔒 Present only when `include_total` was asked for. API §6.1 omits it by
    #: default: a `COUNT(*)` on every keystroke is the expensive half of a search
    #: that must answer inside NFR-005's 300 ms.
    total: int | None = None


class ClientListResponse(BaseModel):
    """API §5.1's collection envelope."""

    items: list[ClientListItemResponse]
    page: ClientPageInfo


class ClientSortOption(BaseModel):
    """One ordering the list offers — API §6.3.

    🔒 A declared model rather than a bare ``dict[str, str]``, and the reason is
    NFR-079: the generated client is the contract. A dict serialises to
    ``{[key: string]: string}``, which types the *shape* of a map and tells the
    frontend nothing about ``value`` or ``label`` — so a renamed field would
    reach a dropdown as ``undefined`` instead of failing the build.
    """

    #: 🔒 The wire value for `?sort=`. Every one is index-backed (migration 0013).
    value: ClientSort
    #: How the ordering reads in a dropdown.
    label: str


class BulkReassignRequest(BaseModel):
    """Hand several clients to one practitioner — EC-M1-04."""

    client_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_BULK_REASSIGN)
    owner_user_id: uuid.UUID


class BulkReassignResponse(BaseModel):
    """What actually moved.

    ⚠️ ``moved`` can be lower than the number of ids submitted, and that is not
    a partial failure: clients already owned by the target are skipped, which in
    an overlapping bulk selection is the correct outcome rather than an error.
    """

    moved: int
    requested: int


def _item(item: ClientListItem) -> ClientListItemResponse:
    return ClientListItemResponse(
        id=item.id,
        full_name=item.full_name,
        stage=item.stage,
        mobile=item.mobile,
        email=item.email,
        city=item.city,
        dietary_class=DietaryClass(item.dietary_class) if item.dietary_class else None,
        owner_user_id=item.owner_user_id,
        owner_name=item.owner_name,
        tags=[
            ClientTagSummary(id=tag_id, name=name, colour=colour)
            for tag_id, name, colour in item.tags
        ],
        archived_at=item.archived_at,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


# ─── The list (FR-M1-021/022) ────────────────────────────────────────────


@router.get("", summary="List, search and filter clients", operation_id="clientsList")
@requires(CLIENT_LIST)
async def clients_list(
    request: Request,
    q: Annotated[
        str | None,
        Query(
            max_length=MAX_SEARCH_LENGTH,
            description="Search name, email or the last digits of a mobile (FR-M1-021).",
        ),
    ] = None,
    stage: Annotated[
        list[ClientStage] | None,
        Query(description="Repeat for OR within the field — API §6.2."),
    ] = None,
    tag_id: Annotated[
        list[uuid.UUID] | None,
        Query(description="Repeat to require ALL named tags — see below."),
    ] = None,
    owner_user_id: Annotated[list[uuid.UUID] | None, Query()] = None,
    archived: Annotated[ArchivedFilter, Query()] = ArchivedFilter.EXCLUDE,
    sort: Annotated[
        str | None,
        Query(description="`name`, `recent_activity` or `created`; `-` prefix for descending."),
    ] = None,
    cursor: Annotated[str | None, Query(description="Opaque, from a previous page.")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    include_total: Annotated[bool, Query(description="Adds a COUNT — API §6.1.")] = False,
) -> ClientListResponse:
    """The client list — FR-M1-021/022, NFR-005 (≤300 ms).

    🔒 **A practitioner sees only their own and their assigned clients**
    (AC-M1-006); an owner sees the tenant (FR-M0-017). Applied as a predicate
    inside the query, because a list filtered after the fact would page short and
    leak totals.

    ⚠️ **Stages are OR, tags are AND.** Ticking two stages means "either" — the
    natural reading of a status filter. Ticking two tags means "both", because
    tags are how a caseload is *narrowed*: "PCOS or post-natal" would return a
    longer list than the practitioner started with, which is the opposite of what
    choosing a second filter is for.

    ⚠️ Archived clients are excluded by default (FR-M1-014, DB §22.2).
    ``archived=only`` exists so a client archived by mistake can be found and
    restored — without it the archive is a one-way door.
    """
    actor = get_context().actor

    page = await list_clients(
        get_session(request),
        tenant_id=actor.require_tenant(),
        actor_user_id=actor.require_subject(),
        actor_role=actor.role,
        filters=build_filters(
            search=q,
            stages=frozenset(stage) if stage else None,
            tag_ids=frozenset(tag_id) if tag_id else None,
            owner_user_ids=frozenset(owner_user_id) if owner_user_id else None,
            archived=archived,
        ),
        sort=parse_sort(sort),
        cursor=cursor,
        limit=limit,
        include_total=include_total,
    )

    return ClientListResponse(
        items=[_item(item) for item in page.items],
        page=ClientPageInfo(next_cursor=page.next_cursor, has_more=page.has_more, total=page.total),
    )


@router.get(
    "/sort-options",
    summary="The orderings the list supports",
    operation_id="clientsSortOptions",
)
@requires(CLIENT_LIST)
async def clients_sort_options(request: Request) -> list[ClientSortOption]:
    """The sort vocabulary, so the UI does not hardcode it.

    🔒 Every value here is index-backed (migration 0013). Serving the list from
    the server is what keeps a dropdown from offering an ordering the database
    would have to sort in memory — API §6.3's allowlist, expressed as data.
    """
    return [ClientSortOption(value=option, label=_SORT_LABELS[option]) for option in ClientSort]


#: How each ordering reads. ⚠️ "Recent activity" is `updated_at` in this slice —
#: an approximation, stated as such in `kernel.discovery.ClientSort`, until
#: `client_daily_metrics` (DDR-13, S7) gives it a real activity clock.
_SORT_LABELS: dict[ClientSort, str] = {
    ClientSort.NAME: "Name",
    ClientSort.RECENT_ACTIVITY: "Recent activity",
    ClientSort.CREATED: "Date added",
}


# ─── Bulk reassignment (EC-M1-04) ────────────────────────────────────────


@router.post(
    "/reassign",
    status_code=status.HTTP_200_OK,
    summary="Reassign several clients to one practitioner",
    operation_id="clientsBulkReassign",
)
@requires(CLIENT_BULK_REASSIGN)
async def clients_bulk_reassign(
    request: Request, body: BulkReassignRequest
) -> BulkReassignResponse:
    """Hand a departing practitioner's caseload over — EC-M1-04.

    🔒 Owner-only, and **all or nothing**: one transaction, so a failure partway
    rolls the whole batch back. A half-completed handover would split a caseload
    between two practitioners with no record of the intent, and the practitioner
    could not tell whether re-running it was safe.

    ⚠️ ``moved`` may be lower than the number submitted — clients already owned
    by the target are skipped, which in an overlapping selection is correct
    rather than an error.

    ⚠️ Registered *before* ``/{client_id}`` matters not at all here, because
    FastAPI matches literal path segments ahead of parameters regardless of
    declaration order across routers. Named anyway because a reader will wonder.
    """
    actor = get_context().actor

    moved = await bulk_reassign_owner(
        get_session(request),
        tenant_id=actor.require_tenant(),
        client_ids=body.client_ids,
        new_owner_user_id=body.owner_user_id,
        actor_user_id=actor.require_subject(),
        actor_role=actor.role,
    )

    record_audit(
        request,
        metadata={"client_count": str(len(body.client_ids)), "to_user_id": str(body.owner_user_id)},
    )
    return BulkReassignResponse(moved=moved, requested=len(body.client_ids))
