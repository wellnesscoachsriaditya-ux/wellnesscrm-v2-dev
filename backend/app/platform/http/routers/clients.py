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

🔒 **Stage changes are named POST actions** (ADR-A06), not a field on
:class:`ClientPatch` — which deliberately has no ``stage`` field, so the absence
is enforced by the schema rather than by reviewer vigilance. A ``PATCH
{stage: "active"}`` would hide an entitlement check, an activation anchor, a
history row and an event behind a field assignment.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated

from fastapi import Header, Request, Response, status
from pydantic import BaseModel, EmailStr, Field

from app.kernel.clients import (
    ClientStage,
    DietaryClass,
    SelectableStage,
    SexType,
)
from app.kernel.context import get_context
from app.kernel.errors import PreconditionRequiredError
from app.modules.clients import (
    CLIENT_ARCHIVE,
    CLIENT_CHANGE_STAGE,
    CLIENT_CREATE,
    CLIENT_READ,
    CLIENT_RESTORE,
    CLIENT_UPDATE,
    MAX_REASON_LENGTH,
    UNSET,
    ClientCreate,
    ClientUpdate,
    Unset,
    archive,
    change_stage,
    create_client,
    get_client,
    restore,
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
    #: 🔒 Not ``ClientStage``. ``archived`` is not a stage a client is created
    #: into — archiving is a soft delete of an existing record (FR-M1-010), and
    #: migration 0010 refuses the value at the table. Typing the field to the
    #: selectable set keeps the published schema honest.
    stage: SelectableStage = ClientStage.LEAD
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


# ─── Lifecycle (ADR-A06, API §7.1) ───────────────────────────────────────
#
# 📌 Named POST actions, not `PATCH {stage: ...}`. A field assignment would hide
# an entitlement check, an activation anchor, a history row and a domain event —
# and it could not return a transition-specific 402.
#
# ⚠️ **No `If-Match` on any of these, and that is a considered difference from
# PATCH.** A precondition protects against a *lost update*: two people editing
# the same field, where the second save silently discards the first. A concurrent
# stage change loses nothing — both transitions are recorded in
# `client_stage_history` with their actors and timestamps, and the final stage is
# one of the two a practitioner actually asked for. Requiring a token here would
# add a failed request and a reload to the two-interaction budget NFR-012 sets,
# to protect against a loss that does not occur. The race that *does* matter —
# two activations both passing one entitlement check — is handled where it lives,
# by the row lock and the tenant mutex in the transitions service.


class StageChangeRequest(BaseModel):
    """`POST /app/clients/{id}/stage` — API §7.1."""

    #: 🔒 Excludes `archived`. Archiving is a soft delete with its own endpoint
    #: (FR-M1-010); migration 0010 refuses the value at the table, and typing it
    #: out of the request keeps the published contract honest rather than
    #: advertising a value every request carrying it is refused.
    to_stage: SelectableStage
    #: Optional, and recorded on the history row (DB §5.3). Short by design —
    #: `MAX_REASON_LENGTH` is enforced again in the service, because this model is
    #: not the only caller.
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)


@router.post(
    "/{client_id}/stage",
    summary="Change a client's lifecycle stage",
    operation_id="clientsChangeStage",
)
@requires(CLIENT_CHANGE_STAGE)
async def change_client_stage(
    request: Request, client_id: uuid.UUID, body: StageChangeRequest, response: Response
) -> ClientResponse:
    """Move a client between stages — ADR-A06, FR-M1-015.

    🔒 Returns **402** with the limit, the usage, the plan and the upgrade path
    when the move enters ``active`` at the plan's ceiling (FR-M1-002). The error
    envelope carries everything the UI needs to explain the refusal, so there is
    no second request to make.

    🔒 AC-M1-003 — converting a lead keeps the record, its identifier and all its
    prior history, because this changes a column rather than moving a row.
    """
    actor = get_context().actor
    updated = await change_stage(
        get_session(request),
        tenant_id=actor.require_tenant(),
        client_id=client_id,
        to_stage=body.to_stage,
        actor_user_id=actor.require_subject(),
        reason=body.reason,
    )

    record_audit(
        request,
        resource_id=updated.id,
        changed_fields=["stage"],
        # ⚠️ The reason is deliberately absent from the audit metadata: it is
        # practitioner free text and the audit log is retained long and read by
        # operators (NFR-033). It lives on the history row, under the tenant's
        # own retention.
        metadata={"to_stage": updated.stage.value},
    )
    response.headers["ETag"] = _etag(updated.updated_at)
    return _response(updated)


@router.post(
    "/{client_id}/archive",
    summary="Archive a client",
    operation_id="clientsArchive",
)
@requires(CLIENT_ARCHIVE)
async def archive_client(
    request: Request, client_id: uuid.UUID, response: Response
) -> ClientResponse:
    """Soft-delete a client — FR-M1-010, AC-M1-007.

    🔒 Removes them from default views without deleting anything, and frees the
    entitlement slot immediately if they were ``active``. The stage is preserved,
    which is what lets :func:`restore_client` put them back where they were.

    ⚠️ Takes no body. A reason has nowhere safe to go in this slice — see
    ``transitions.archive``.
    """
    actor = get_context().actor
    archived = await archive(
        get_session(request),
        tenant_id=actor.require_tenant(),
        client_id=client_id,
        actor_user_id=actor.require_subject(),
    )

    record_audit(
        request,
        resource_id=archived.id,
        changed_fields=["archived_at"],
        metadata={"stage": archived.stage.value},
    )
    response.headers["ETag"] = _etag(archived.updated_at)
    return _response(archived)


@router.post(
    "/{client_id}/restore",
    summary="Restore an archived client",
    operation_id="clientsRestore",
)
@requires(CLIENT_RESTORE)
async def restore_client(
    request: Request, client_id: uuid.UUID, response: Response
) -> ClientResponse:
    """Bring an archived client back — EC-M1-02, AC-M1-007.

    🔒 Returns them to the stage they were archived at. A returning client is
    reactivated in place; there is never a second record.

    🔒 Returns **402** when the restored stage is ``active`` and the plan is at
    its ceiling (EC-M1-06) — archiving frees a slot, so restoring takes one back.
    """
    actor = get_context().actor
    restored = await restore(
        get_session(request),
        tenant_id=actor.require_tenant(),
        client_id=client_id,
        actor_user_id=actor.require_subject(),
    )

    record_audit(
        request,
        resource_id=restored.id,
        changed_fields=["archived_at"],
        metadata={"stage": restored.stage.value},
    )
    response.headers["ETag"] = _etag(restored.updated_at)
    return _response(restored)
