"""Enquiries and the enquiry form — API §7.2, FR-M2-010/011.

🔒 In ``platform/`` for the reason every router is: R5 forbids a module importing
``platform``, and a router needs ``realm_router``, the session and the audit hook.
The queries and their scoping live in ``modules.leads``; this owns the HTTP shape.

⚠️ **There is no "create a lead" endpoint here, and that is not an omission.**
FR-M2-010 wants a manual lead added in ≤3 interactions, and M1.3 already makes a
lead a ``clients`` row at stage ``lead`` — which ``POST /app/clients`` creates,
defaulting to exactly that stage. Adding a second write path into ``clients``
would be a duplicate of Slice A's, subject to drift, for no capability. FR-M2-010
is met in the UI, by a form with three fields.

🔒 **Scoping runs inside the query** (AC-M1-006), like the client list and for
the same reason: an enquiry carries a name, a mobile and a stated health goal, so
a practitioner who cannot open a client must not read the enquiry that created
them. See ``leads.discovery._visible_to``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Query, Request, Response, status
from pydantic import BaseModel, Field

from app.kernel.clients import ClientStage
from app.kernel.context import UserRole, get_context
from app.kernel.errors import NotFoundError
from app.kernel.leads import (
    AGEING_LEAD_HOURS,
    MAX_FORM_INTRO_LENGTH,
    MAX_FORM_TITLE_LENGTH,
    LeadSource,
)
from app.modules.clients import load_for_access
from app.modules.leads import (
    DEFAULT_PAGE_SIZE,
    ENQUIRY_FORM_READ,
    ENQUIRY_FORM_UPDATE,
    ENQUIRY_LIST,
    ENQUIRY_RESPOND,
    MAX_PAGE_SIZE,
    EnquiryListItem,
    OwnForm,
    ensure_form,
    list_enquiries,
    load_own_form,
    load_submission_client,
    mark_responded,
    update_form,
)
from app.platform.config import get_settings
from app.platform.http.authz import requires
from app.platform.http.pipeline import authorize, get_session, realm_router, record_audit

router = realm_router("/api/v1/app/enquiries", tags=["leads"])
form_router = realm_router("/api/v1/app/enquiry-forms", tags=["leads"])


# ─── Schemas ─────────────────────────────────────────────────────────────


class EnquiryListItemResponse(BaseModel):
    """One enquiry as the practitioner sees it — API §7.2.

    🔒 Carries the **submitted** values, not the client's current ones (DB §6.2).
    A practitioner triaging an enquiry needs what the prospect actually typed,
    which may since have been corrected on the client record.

    🔒 ``age_hours`` and ``is_ageing`` are server-computed (Principle 3) — a
    browser deriving them would disagree across a timezone or a clock skew, and
    the number deciding who gets called next would differ per device.
    """

    id: uuid.UUID
    client_id: uuid.UUID | None
    submitted_name: str
    submitted_mobile: str | None
    submitted_email: str | None
    primary_goal: str
    source: LeadSource | None
    source_detail: str | None
    #: 🔒 EC-M2-02 — shown to the practitioner, never to the submitter.
    is_duplicate_of_existing: bool
    submitted_at: datetime
    responded_at: datetime | None
    responded_by_user_id: uuid.UUID | None
    age_hours: float
    is_ageing: bool
    client_stage: ClientStage | None
    client_owner_user_id: uuid.UUID | None
    owner_name: str | None


class EnquiryPageInfo(BaseModel):
    """Where the next page resumes — API §6.1."""

    next_cursor: str | None = None
    has_more: bool
    total: int | None = None


class EnquiryListResponse(BaseModel):
    """API §5.1's collection envelope."""

    items: list[EnquiryListItemResponse]
    page: EnquiryPageInfo
    #: 🔒 The threshold the UI highlights against, sent rather than hardcoded.
    #: FR-M2-011's "ageing" is a server rule (`kernel.leads.AGEING_LEAD_HOURS`);
    #: a duplicate constant in the frontend would drift the day it is tuned.
    ageing_after_hours: int = AGEING_LEAD_HOURS


class EnquiryFormResponse(BaseModel):
    """The practitioner's own view of their form — API §7.2."""

    id: uuid.UUID
    slug: str
    title: str
    intro_text: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    #: 🔒 **The complete shareable URL, assembled server-side** — FR-M2-001,
    #: US-M2-01 ("a link I can put in my Instagram bio").
    #:
    #: Principle 3: the client renders, never derives. A frontend building this
    #: from a slug would have to know the public URL shape, so changing that
    #: shape — a custom domain, a shorter path — would become a coordinated
    #: release instead of a server-side edit.
    share_url: str


class EnquiryFormPatch(BaseModel):
    """A partial edit of the form — API §7.2's PATCH.

    ⚠️ ``intro_text`` uses the model's own "was this key present" information
    rather than a sentinel: ``model_fields_set`` distinguishes "clear the intro"
    (sent as ``null``) from "leave it alone" (absent). Both arrive as ``None``,
    and conflating them means a practitioner editing only the title silently
    loses their intro text.
    """

    title: str | None = Field(default=None, max_length=MAX_FORM_TITLE_LENGTH)
    intro_text: str | None = Field(default=None, max_length=MAX_FORM_INTRO_LENGTH)
    is_active: bool | None = None


def _item(item: EnquiryListItem) -> EnquiryListItemResponse:
    return EnquiryListItemResponse(
        id=item.id,
        client_id=item.client_id,
        submitted_name=item.submitted_name,
        submitted_mobile=item.submitted_mobile,
        submitted_email=item.submitted_email,
        primary_goal=item.primary_goal,
        source=item.source,
        source_detail=item.source_detail,
        is_duplicate_of_existing=item.is_duplicate_of_existing,
        submitted_at=item.submitted_at,
        responded_at=item.responded_at,
        responded_by_user_id=item.responded_by_user_id,
        age_hours=round(item.age_hours, 2),
        is_ageing=item.is_ageing,
        client_stage=item.client_stage,
        client_owner_user_id=item.client_owner_user_id,
        owner_name=item.owner_name,
    )


def _form(form: OwnForm) -> EnquiryFormResponse:
    return EnquiryFormResponse(
        id=form.id,
        slug=form.slug,
        title=form.title,
        intro_text=form.intro_text,
        is_active=form.is_active,
        created_at=form.created_at,
        updated_at=form.updated_at,
        share_url=_share_url(form.tenant_slug),
    )


def _share_url(tenant_slug: str) -> str:
    """The public enquiry URL for a tenant — FR-M2-001.

    ⚠️ Built from ``app_base_url`` rather than the request's own Host header. A
    caller-supplied Host would let anyone mint a link pointing at a domain they
    control, and the practitioner would paste it into their Instagram bio.
    """
    base = get_settings().app_base_url.rstrip("/")
    return f"{base}/enquire/{tenant_slug}"


# ─── Enquiries ───────────────────────────────────────────────────────────


@router.get("", summary="List enquiries", operation_id="enquiriesList")
@requires(ENQUIRY_LIST)
async def enquiries_list(
    request: Request,
    source: Annotated[LeadSource | None, Query(description="FR-M2-009 attribution.")] = None,
    cursor: Annotated[str | None, Query(description="Opaque, from a previous page.")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    include_total: Annotated[bool, Query(description="Adds a COUNT — API §6.1.")] = False,
) -> EnquiryListResponse:
    """Every enquiry, newest first — API §7.2.

    ⚠️ **Newest first, unlike the needs-response view.** This is the archive a
    practitioner searches ("did she ever contact us?"); that one is a work queue
    where the oldest is the most urgent. Same rows, opposite ordering, because
    they answer opposite questions.
    """
    return await _page(
        request,
        needs_response_only=False,
        source=source,
        cursor=cursor,
        limit=limit,
        include_total=include_total,
    )


@router.get(
    "/needs-response",
    summary="Enquiries awaiting a response",
    operation_id="enquiriesNeedsResponse",
)
@requires(ENQUIRY_LIST)
async def enquiries_needs_response(
    request: Request,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    include_total: Annotated[bool, Query()] = False,
) -> EnquiryListResponse:
    """🔒 The work queue — FR-M2-011, AC-M2-005. **Oldest first.**

    US-M2-03 is "so none are forgotten", and M2.2 prices a forgotten enquiry at
    ₹2,500–4,000/month of recurring revenue. The oldest unanswered enquiry is the
    most urgent one; a queue that buried it under today's arrivals would be the
    failure this view exists to prevent.

    ⚠️ Registered before ``/{submission_id}/respond`` matters not at all —
    FastAPI matches literal segments ahead of parameters regardless of
    declaration order. Named because a reader will wonder.
    """
    return await _page(
        request,
        needs_response_only=True,
        source=None,
        cursor=cursor,
        limit=limit,
        include_total=include_total,
    )


async def _page(
    request: Request,
    *,
    needs_response_only: bool,
    source: LeadSource | None,
    cursor: str | None,
    limit: int,
    include_total: bool,
) -> EnquiryListResponse:
    """Both list endpoints, which differ only in filter and ordering.

    🔒 One implementation so the scoping cannot be applied to one and forgotten
    on the other — the failure would be a practitioner reading a colleague's
    enquiries from whichever endpoint was missed.
    """
    actor = get_context().actor
    page = await list_enquiries(
        get_session(request),
        tenant_id=actor.require_tenant(),
        actor_user_id=actor.require_subject(),
        actor_role=actor.role,
        needs_response_only=needs_response_only,
        source=source,
        cursor=cursor,
        limit=limit,
        include_total=include_total,
        # 🔒 One clock reading for the whole page — two rows in one response must
        # not disagree about what time it is.
        now=datetime.now(UTC),
    )
    return EnquiryListResponse(
        items=[_item(item) for item in page.items],
        page=EnquiryPageInfo(
            next_cursor=page.next_cursor, has_more=page.has_more, total=page.total
        ),
    )


@router.post(
    "/{submission_id}/respond",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark an enquiry as answered",
    operation_id="enquiriesMarkResponded",
)
@requires(ENQUIRY_RESPOND)
async def enquiries_mark_responded(request: Request, submission_id: uuid.UUID) -> Response:
    """Clear an enquiry from the needs-response queue — FR-M2-011.

    🔒 **The row-level check runs here, not in the module.** ``enquiry.respond``
    carries no ``owner_or_assigned`` policy because a submission has no
    ``owner_user_id`` for one to inspect. So the client behind the enquiry is
    loaded through ``clients.load_for_access`` and authorized explicitly — which
    is what stops a practitioner clearing a colleague's enquiry (AC-M1-006).

    ⚠️ ``leads`` could not do this itself: R3 forbids it importing ``clients``,
    and the access model belongs to ``clients``. The router is the one layer
    permitted to see both.

    🔒 Idempotent — a second call keeps the original responder and timestamp.
    """
    actor = get_context().actor
    session = get_session(request)
    tenant_id = actor.require_tenant()

    # 🔒 Resolve the enquiry's client, then authorize *that*. A submission whose
    # client was erased (FR-M0-027) has no subject to scope by, so it is
    # owner-only — `list_enquiries` applies the identical rule, and the two must
    # agree or a row would be listable but not actionable.
    exists, client_id = await load_submission_client(
        session, tenant_id=tenant_id, submission_id=submission_id
    )
    if not exists:
        raise NotFoundError(
            "That enquiry could not be found.",
            action="Refresh the list and try again.",
        )

    if client_id is not None:
        _client, access = await load_for_access(session, tenant_id=tenant_id, client_id=client_id)
        await authorize(request, access)
    elif actor.role is not UserRole.OWNER:
        # Consent evidence with no subject left. Widening access at the exact
        # moment someone asked to be forgotten would be the wrong call.
        raise NotFoundError(
            "That enquiry could not be found.",
            action="Refresh the list and try again.",
        )

    await mark_responded(
        session,
        tenant_id=tenant_id,
        submission_id=submission_id,
        responded_by_user_id=actor.require_subject(),
    )
    record_audit(request, resource_id=submission_id, metadata={"submission_id": str(submission_id)})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ─── The form (API §7.2) ─────────────────────────────────────────────────


@form_router.get("", summary="The tenant's enquiry form", operation_id="enquiryFormsList")
@requires(ENQUIRY_FORM_READ)
async def enquiry_forms_list(request: Request) -> list[EnquiryFormResponse]:
    """The practitioner's own form — API §7.2, FR-M2-001.

    ⚠️ **A list, though MVP has exactly one.** API §7.2 names the endpoint
    ``GET /app/enquiry-forms`` and FR-M2-012 makes several per tenant a Phase 2
    feature. Returning a list now means the frontend's type does not change when
    the second form arrives — a bare object would make that a breaking change to
    the generated client.

    🔒 Creates the form on first read if the tenant has none. See
    ``leads.ensure_form``: doing it here rather than at registration keeps the
    identity path from importing a domain module.
    """
    actor = get_context().actor
    session = get_session(request)
    tenant_id = actor.require_tenant()

    form = await load_own_form(session, tenant_id=tenant_id)
    if form is None:
        await ensure_form(session, tenant_id=tenant_id, practice_name=_practice_name(actor))
        form = await load_own_form(session, tenant_id=tenant_id)
    if form is None:  # pragma: no cover — ensure_form returns or raises
        return []
    return [_form(form)]


@form_router.patch(
    "/{form_id}",
    summary="Edit the enquiry form",
    operation_id="enquiryFormsUpdate",
)
@requires(ENQUIRY_FORM_UPDATE)
async def enquiry_forms_update(
    request: Request, form_id: uuid.UUID, payload: EnquiryFormPatch
) -> EnquiryFormResponse:
    """Change the form's heading, introduction, or whether it accepts enquiries.

    🔒 **Deactivating is how a practitioner closes their books** — EC-M2-07's
    other half. The form 404s publicly while every submission it ever took stays
    intact; migration 0015 revokes DELETE so there is no way to do otherwise.

    ⚠️ No ``If-Match``. A form has one editor in the launch persona's practice and
    the fields are independent — a lost update here costs a re-typed sentence,
    where on a client record it would cost clinical data. ``PATCH /app/clients``
    requires a precondition for that reason; this deliberately does not.
    """
    actor = get_context().actor

    form = await update_form(
        get_session(request),
        tenant_id=actor.require_tenant(),
        form_id=form_id,
        title=payload.title,
        intro_text=payload.intro_text,
        # 🔒 Present-and-null means "clear it"; absent means "leave it".
        intro_cleared="intro_text" in payload.model_fields_set and payload.intro_text is None,
        is_active=payload.is_active,
    )

    record_audit(
        request,
        resource_id=form.id,
        metadata={"form_id": str(form.id), "is_active": str(form.is_active).lower()},
    )
    return _form(form)


def _practice_name(actor: object) -> str:
    """A fallback name for a form created on first read.

    ⚠️ The token carries no practice name (NFR-033 — ``CurrentSessionResponse``
    is identifiers and role only), and reading ``tenants`` here to get one would
    be a query per form load for a string the practitioner immediately edits.
    The generic default is honest and one keystroke from being replaced.
    """
    return "your practitioner"
