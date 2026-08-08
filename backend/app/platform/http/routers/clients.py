"""Client records — API §7.1, FR-M1-004..010.

🔒 This router lives in ``platform/`` rather than in ``app/modules/clients/``
because Arch R5 forbids a module importing ``platform`` — and a router needs
``realm_router``, the session and the audit hook, all of which live there. The
module owns the domain logic and the tables; this owns the HTTP shape and calls
into it through the module's public surface (R2).

⚠️ **The list and search endpoints are not here.** ``GET /app/clients`` needs
filtering, sorting and cursor pagination over tags and owners (API §7.1), which
depend on tables Slice C creates. It lands in Slice E, with the query tuning that
NFR-005 requires measured rather than assumed.

⚠️ **Stage changes are not here either.** ADR-A06 makes each transition a named
POST action with entitlement checks and history — ``POST /app/clients/{id}/stage``
is Slice B. :class:`ClientPatch` deliberately has no ``stage`` field, so the
absence is enforced by the schema rather than by reviewer vigilance.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated

from fastapi import Header, Request, Response, status
from pydantic import BaseModel, EmailStr, Field

from app.kernel.clients import ClientStage, DietaryClass, SexType
from app.kernel.context import get_context
from app.kernel.errors import PreconditionRequiredError
from app.modules.clients import (
    CLIENT_CREATE,
    CLIENT_READ,
    CLIENT_UPDATE,
    UNSET,
    ClientCreate,
    ClientUpdate,
    Unset,
    create_client,
    get_client,
    update_client,
)
from app.platform.http.authz import requires
from app.platform.http.pipeline import get_session, realm_router, record_audit

router = realm_router("/api/v1/app/clients", tags=["clients"])


# ─── Schemas ─────────────────────────────────────────────────────────────


class ClientCreateRequest(BaseModel):
    """FR-M1-004 — name plus one contact method is the whole requirement.

    🔒 Everything else is optional. A practitioner capturing a lead mid-call has
    a name and a number; a form demanding more is a form they abandon, and
    NFR-011 budgets the whole interaction at three steps.
    """

    full_name: str = Field(min_length=1, max_length=120)
    mobile: str | None = Field(default=None, max_length=32)
    email: EmailStr | None = None
    stage: ClientStage = ClientStage.LEAD
    date_of_birth: date | None = None
    sex: SexType | None = None
    city: str | None = Field(default=None, max_length=120)
    preferred_language: str = Field(default="en", max_length=16)
    source: str | None = Field(default=None, max_length=64)
    source_detail: str | None = Field(default=None, max_length=256)
    dietary_class: DietaryClass | None = None
    #: Defaults to the caller. 🔒 A practitioner may create a client for a
    #: colleague in a clinic, but the *default* must be themselves — an
    #: unattributed client is one nobody is accountable for (FR-M1-009).
    owner_user_id: uuid.UUID | None = None


class ClientPatch(BaseModel):
    """A partial edit — API §4.4.

    ⚠️ **No ``stage``** (ADR-A06). Present-and-null is a meaningful edit here
    (clearing an email), so the model distinguishes "absent" from "null" via
    ``model_fields_set`` rather than treating ``None`` as "unchanged".
    """

    full_name: str | None = Field(default=None, min_length=1, max_length=120)
    mobile: str | None = Field(default=None, max_length=32)
    email: EmailStr | None = None
    date_of_birth: date | None = None
    sex: SexType | None = None
    city: str | None = Field(default=None, max_length=120)
    preferred_language: str | None = Field(default=None, max_length=16)
    dietary_class: DietaryClass | None = None


class ClientResponse(BaseModel):
    """One client, as the practitioner realm sees it — API §7.1."""

    id: uuid.UUID
    full_name: str
    stage: ClientStage
    mobile: str | None
    email: str | None
    date_of_birth: date | None
    sex: SexType | None
    city: str | None
    preferred_language: str
    source: str | None
    source_detail: str | None
    owner_user_id: uuid.UUID
    dietary_class: DietaryClass | None
    #: 🔒 FR-M0-028 — derived from ``date_of_birth`` on every read, never stored.
    #: ``None`` when the date of birth is unknown, which is not the same as
    #: "adult": the enquiry form does not ask for one.
    is_minor: bool | None
    activated_at: datetime | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _response(client: object) -> ClientResponse:
    """Project a persisted client onto the wire shape."""
    return ClientResponse.model_validate(client, from_attributes=True)


def _etag(updated_at: datetime) -> str:
    """The concurrency token for a client — API §4.4, ADR-14.

    ``updated_at`` rather than a version counter: the column already exists, is
    already maintained on every write, and has microsecond resolution. A separate
    counter would be a second thing to remember to bump.
    """
    return f'W/"{updated_at.isoformat()}"'


def _parse_if_match(raw: str) -> datetime:
    """Recover the timestamp a caller is asserting they last saw.

    Raises:
        PreconditionRequiredError: On a malformed header. 🔒 Refusing is the safe
            direction — an unparseable token treated as "no precondition" would
            silently downgrade the request to last-write-wins, which is the exact
            data loss ADR-14 exists to prevent.
    """
    token = raw.strip()
    if token.startswith("W/"):
        token = token[2:]
    token = token.strip('"')
    try:
        return datetime.fromisoformat(token)
    except ValueError as exc:
        raise PreconditionRequiredError("client") from exc


# ─── Endpoints ───────────────────────────────────────────────────────────


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a client",
    operation_id="clientsCreate",
)
@requires(CLIENT_CREATE)
async def create(request: Request, body: ClientCreateRequest, response: Response) -> ClientResponse:
    """Create a client or lead — FR-M1-004, AC-M1-001.

    🔒 Not metered. FR-M1-003 keeps leads unmetered at every stage before
    ``active``, and EC-M2-06 requires a tenant at their limit to keep accepting
    them. The entitlement binds on the transition to ``active`` (Slice B).
    """
    actor = get_context().actor
    # 🔒 Required, not optional. This route is practitioner-realm only, so an
    # actor with no subject is a wiring error rather than an anonymous caller —
    # and defaulting `owner_user_id` to it would create the unattributed client
    # FR-M1-009 exists to prevent.
    actor_id = actor.require_subject()

    created = await create_client(
        get_session(request),
        tenant_id=actor.require_tenant(),
        payload=ClientCreate(
            full_name=body.full_name,
            owner_user_id=body.owner_user_id or actor_id,
            mobile=body.mobile,
            email=str(body.email) if body.email else None,
            stage=body.stage,
            date_of_birth=body.date_of_birth,
            sex=body.sex,
            city=body.city,
            preferred_language=body.preferred_language,
            source=body.source,
            source_detail=body.source_detail,
            dietary_class=body.dietary_class,
        ),
        actor_user_id=actor_id,
    )

    record_audit(request, resource_id=created.id, metadata={"stage": created.stage.value})
    response.headers["ETag"] = _etag(created.updated_at)
    return _response(created)


@router.get(
    "/{client_id}",
    summary="Read a client",
    operation_id="clientsRead",
)
@requires(CLIENT_READ)
async def read(request: Request, client_id: uuid.UUID, response: Response) -> ClientResponse:
    """One client by id — API §7.1.

    ⚠️ Archived clients are returned. AC-M1-007 requires archiving to remove them
    from default *views* without deleting anything, and restoring one requires
    being able to read it first.
    """
    actor = get_context().actor
    client = await get_client(
        get_session(request), tenant_id=actor.require_tenant(), client_id=client_id
    )
    record_audit(request, resource_id=client.id)
    response.headers["ETag"] = _etag(client.updated_at)
    return _response(client)


@router.patch(
    "/{client_id}",
    summary="Update a client",
    operation_id="clientsUpdate",
)
@requires(CLIENT_UPDATE)
async def update(
    request: Request,
    client_id: uuid.UUID,
    body: ClientPatch,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ClientResponse:
    """Apply a partial edit — API §4.4.

    🔒 ``If-Match`` is **required**. Two practitioners editing one client is
    routine in a clinic, and without a precondition the second save silently
    discards the first one's work with no error and no trace.
    """
    if if_match is None:
        raise PreconditionRequiredError("client")

    actor = get_context().actor
    supplied = body.model_fields_set

    def field(name: str) -> object | Unset:
        """Absent stays :data:`UNSET`; present-and-null clears the column."""
        return getattr(body, name) if name in supplied else UNSET

    updated = await update_client(
        get_session(request),
        tenant_id=actor.require_tenant(),
        client_id=client_id,
        payload=ClientUpdate(
            full_name=body.full_name,
            preferred_language=body.preferred_language,
            mobile=field("mobile"),  # type: ignore[arg-type]
            email=str(body.email) if body.email else field("email"),  # type: ignore[arg-type]
            date_of_birth=field("date_of_birth"),  # type: ignore[arg-type]
            sex=field("sex"),  # type: ignore[arg-type]
            city=field("city"),  # type: ignore[arg-type]
            dietary_class=field("dietary_class"),  # type: ignore[arg-type]
        ),
        expected_updated_at=_parse_if_match(if_match),
    )

    record_audit(request, resource_id=updated.id, changed_fields=sorted(supplied))
    response.headers["ETag"] = _etag(updated.updated_at)
    return _response(updated)
