"""The client timeline — API §7.1, FR-M1-018/019.

🔒 In ``platform/`` for the reason every router is: R5 forbids a module importing
``platform``, and this needs ``realm_router``, the session and the authorization
seam. The projection and its query live in ``modules.clients``; this owns the
HTTP shape and the cursor's presentation.

🔒 **Authorizes against the client, not merely the action.** A practitioner who
cannot open a client must not be able to read their history — the same AC-M1-006
leak the notes routes guard against, through a different door. ``authorized_client``
is the shared seam.

⚠️ **Its own module rather than a route in ``collaboration.py``.** The timeline
is not a collaboration concern: it aggregates events from six modules, and by S6
its producers will outnumber everything in that file. Keeping it separate is what
stops one router growing to cover five unrelated things.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import Query, Request
from pydantic import BaseModel

from app.kernel.context import get_context
from app.kernel.timeline import TimelineActorType, TimelineEventType, is_producible
from app.modules.clients import (
    CLIENT_READ_TIMELINE,
    DEFAULT_LIMIT,
    MAX_LIMIT,
    read_timeline,
)
from app.platform.http.authz import requires
from app.platform.http.pipeline import get_session, realm_router
from app.platform.http.routers.clients import authorized_client

router = realm_router("/api/v1/app/clients", tags=["timeline"])


class TimelineEntryResponse(BaseModel):
    """One timeline entry — DB §5.6.

    🔒 ``summary`` is a **non-clinical label** written by
    ``kernel.timeline.summarise``. It never contains a measurement, a note body
    or a diagnosis; see that module for why the guarantee is structural rather
    than a review convention.

    ⚠️ ``source_record_id`` may point at a record that no longer exists — a
    retired tag, a revoked grant. The UI renders it as a link only when it can
    resolve it, rather than the API pre-checking six tables on every read.
    """

    id: uuid.UUID
    event_type: TimelineEventType
    occurred_at: datetime
    summary: str
    source_module: str
    source_record_id: uuid.UUID | None
    actor_type: TimelineActorType
    actor_id: uuid.UUID | None


class TimelinePageInfo(BaseModel):
    """Where the next page resumes — API §6.1.

    ⚠️ ``total`` is absent. API §6.1 omits it by default because a ``COUNT(*)``
    on every request is wasteful, and a timeline is scrolled rather than counted
    — nobody needs to know a client has 431 events.
    """

    next_cursor: str | None = None
    has_more: bool


class TimelineResponse(BaseModel):
    """API §5.1's collection envelope."""

    items: list[TimelineEntryResponse]
    page: TimelinePageInfo


class TimelineFilterResponse(BaseModel):
    """One filter a practitioner may apply — FR-M1-019."""

    event_type: TimelineEventType
    label: str


@router.get(
    "/{client_id}/timeline",
    summary="A client's unified timeline",
    operation_id="clientTimelineList",
)
@requires(CLIENT_READ_TIMELINE)
async def timeline_list(
    request: Request,
    client_id: uuid.UUID,
    event_type: Annotated[
        list[TimelineEventType] | None,
        Query(description="Filter by event type. Repeat for OR — API §6.2."),
    ] = None,
    cursor: Annotated[str | None, Query(description="Opaque, from a previous page.")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> TimelineResponse:
    """Reverse-chronological, cursor-paginated — FR-M1-018, NFR-006.

    🔒 One indexed query regardless of how many modules feed the timeline, which
    is the whole point of DDR-06's materialised projection and what keeps the
    800 ms budget from eroding as S3–S6 add producers.

    ⚠️ Repeated ``event_type`` params are OR within the field (API §6.2). An
    absent one means everything — including event types this build cannot yet
    produce, which is harmless: they simply match nothing.
    """
    await authorized_client(request, client_id)

    page = await read_timeline(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        client_id=client_id,
        event_types=frozenset(event_type) if event_type else None,
        cursor=cursor,
        limit=limit,
    )

    return TimelineResponse(
        items=[
            TimelineEntryResponse.model_validate(entry, from_attributes=True)
            for entry in page.items
        ],
        page=TimelinePageInfo(next_cursor=page.next_cursor, has_more=page.has_more),
    )


@router.get(
    "/{client_id}/timeline/filters",
    summary="The event types worth filtering by",
    operation_id="clientTimelineFilters",
)
@requires(CLIENT_READ_TIMELINE)
async def timeline_filters(request: Request, client_id: uuid.UUID) -> list[TimelineFilterResponse]:
    """The filter list FR-M1-019 drives.

    🔒 **Only event types something can actually produce.** ``timeline_event_type``
    declares the full S3–S6 vocabulary up front (one enum migration rather than
    nine), so offering every member would give a practitioner filters like "Plan
    issued" that always return nothing — which reads as a broken timeline rather
    than an unbuilt feature.

    ⚠️ Client-scoped in its path but not in its content: it lists what *the
    system* can produce, not what this client has. Making it depend on the
    client's own history would mean a filter appearing and disappearing as
    events age out, and a `SELECT DISTINCT` on every timeline load.
    """
    await authorized_client(request, client_id)

    return [
        TimelineFilterResponse(event_type=event_type, label=_FILTER_LABELS[event_type])
        for event_type in TimelineEventType
        if is_producible(event_type)
    ]


#: 🔒 How each filter reads. Deliberately not ``summarise()``: a filter label is
#: a noun for a category ("Stage changes") while a summary is a statement about
#: one occurrence ("Note added"), and forcing one string to do both makes a
#: filter list read like a sentence fragment.
#: ⚠️ 🔒 **Every producible type must appear here.** The comprehension above
#: subscripts this dict for each one, so a producer added without a label raises
#: `KeyError` and returns 500 from the filters endpoint — not a missing filter, a
#: broken page. `enquiry_received` shipped in that state in S2 Slice F and was
#: found in M3; `test_every_producible_event_type_has_a_filter_label` now fails
#: instead of a practitioner's timeline.
_FILTER_LABELS: dict[TimelineEventType, str] = {
    TimelineEventType.STAGE_CHANGED: "Stage changes",
    TimelineEventType.NOTE_ADDED: "Notes",
    TimelineEventType.TAG_APPLIED: "Tags",
    TimelineEventType.OWNERSHIP_CHANGED: "Ownership",
    TimelineEventType.ACCESS_CHANGED: "Access",
    TimelineEventType.CLIENT_ARCHIVED: "Archived",
    TimelineEventType.CLIENT_RESTORED: "Restored",
    TimelineEventType.ENQUIRY_RECEIVED: "Enquiries",
    # ── M3, the clinical workspace ──
    TimelineEventType.ASSESSMENT_COMPLETED: "Assessments",
    TimelineEventType.MEASUREMENT_RECORDED: "Measurements",
    TimelineEventType.DOCUMENT_UPLOADED: "Documents",
}
