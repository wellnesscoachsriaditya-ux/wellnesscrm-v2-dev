"""The messaging engine over HTTP — PRD M8, API §7.

🔒 In ``platform/`` for the reason every router is: R5 forbids a module importing
``platform``, and this needs ``realm_router``, the session and the authorization
seam. The rules live in ``kernel.messaging`` and the persistence in
``modules.messaging``; this owns the HTTP shape and nothing else.

🔒 **Every client-bound route authorizes against the client** through
``authorized_client``, not merely against the action. A practitioner who cannot
open a client must not read what was sent to them, see what is queued for them,
or send them anything — the AC-M1-006 leak through five more doors.

🔒 **Nothing here sends.** The practitioner-triggered endpoint creates a
``scheduled_messages`` row like every other producer (FR-M8-001). A route that
called a transport directly would be a second dispatch path, and the suppression
rules it skipped would be exactly the ones protecting the practitioner from
messaging a client who withdrew consent.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time
from typing import Annotated, Any

from fastapi import Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.context import get_context
from app.kernel.messaging import CheckinFrequency, DispatchStatus, ScheduledState, utc_now
from app.kernel.models import TransportType
from app.modules.messaging import (
    CHECKIN_SCHEDULE_READ,
    CHECKIN_SCHEDULE_UPDATE,
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    MESSAGE_CANCEL,
    MESSAGE_HISTORY_READ,
    MESSAGE_PENDING_READ,
    MESSAGE_PREFERENCE_READ,
    MESSAGE_PREFERENCE_UPDATE,
    MESSAGE_SEND,
    MESSAGE_TEMPLATE_PREVIEW,
    CheckinSettings,
    MessageRequest,
    Template,
    assert_disableable,
    cancel_one,
    configure_checkin,
    declared_variables,
    list_message_history,
    list_pending,
    list_preferences,
    list_published,
    load_checkin_schedule,
    load_current_template,
    load_scheduled,
    load_template,
    pause_checkins,
    portal_url,
    recent_failures,
    render,
    resume_checkins,
    schedule,
    upsert_preference,
)
from app.platform.http.authz import requires
from app.platform.http.pipeline import get_session, realm_router, record_audit
from app.platform.http.routers.clients import authorized_client

client_router = realm_router("/api/v1/app/clients", tags=["messaging"])
router = realm_router("/api/v1/app/messaging", tags=["messaging"])

#: 🔒 The source recorded on a message a practitioner triggered by hand. Distinct
#: from the automated producers so EC-M8-08's cancellation, and any later
#: question about "who caused this message", can tell them apart.
_MANUAL_SOURCE = "messaging.manual"


# ─── Schemas ─────────────────────────────────────────────────────────────


class DispatchResponse(BaseModel):
    """One delivery attempt — FR-M8-003, FR-M8-011.

    ⚠️ ``failure_reason`` is the provider's own text. It is shown to the
    *practitioner*, who needs to know why their client did not receive something,
    and must never be forwarded to a client.
    """

    id: uuid.UUID
    template_code: str
    template_version: int
    transport: TransportType
    recipient_address: str
    status: DispatchStatus
    attempt_number: int
    failure_code: str | None
    failure_reason: str | None
    created_at: datetime
    sent_at: datetime | None
    delivered_at: datetime | None
    read_at: datetime | None


class HistoryPageInfo(BaseModel):
    """Where the next page resumes — API §6.1."""

    next_cursor: str | None = None
    has_more: bool


class MessageHistoryResponse(BaseModel):
    """API §5.1's collection envelope."""

    items: list[DispatchResponse]
    page: HistoryPageInfo


class PendingMessageResponse(BaseModel):
    """A message queued but not yet sent — FR-M8-028.

    🔒 ``preview`` is the rendered body, because "you have three messages
    scheduled" is not something a practitioner can act on. What they need to know
    before it goes out on their behalf is what it *says*.
    """

    id: uuid.UUID
    template_code: str
    scheduled_for: datetime
    #: 🔒 AC-M8-006's audit trail — set when quiet hours moved this message.
    deferred_from: datetime | None
    state: ScheduledState
    preview: str


class SendMessageRequest(BaseModel):
    """A practitioner sending a message by hand.

    ⚠️ ``variables`` carries only what the server cannot know. The recipient's
    name, their practitioner's name and the portal link are filled in server-side
    — a caller-supplied ``client_name`` would let one client's name be sent to
    another, and a caller-supplied URL would be an open redirect delivered over
    WhatsApp.
    """

    template_code: str = Field(min_length=3, max_length=64)
    variables: dict[str, str] = Field(default_factory=dict)
    #: Defaults to now, which the sweep picks up on its next pass.
    scheduled_for: datetime | None = None


class ScheduledMessageResponse(BaseModel):
    """What a caller gets back after queueing a message."""

    id: uuid.UUID | None
    template_code: str
    scheduled_for: datetime
    state: ScheduledState
    #: 🔒 False when an identical occasion was already queued (EC-M8-06). Not an
    #: error: the idempotency constraint did its job, and the caller's client
    #: will receive exactly one message.
    created: bool


class TemplateResponse(BaseModel):
    """One message type a practitioner can preview or control — FR-M8-026/027."""

    code: str
    version: int
    category: str
    is_essential: bool
    default_transport: TransportType
    provider_template_status: str
    is_practitioner_disableable: bool
    variables: dict[str, Any]


class PreviewResponse(BaseModel):
    """🔒 FR-M8-026 — the message exactly as the client will receive it."""

    template_code: str
    version: int
    transport: TransportType
    body: str
    #: True when the values are illustrative rather than a real client's.
    is_sample: bool


class PreferenceResponse(BaseModel):
    """One preference row — DB §11.8."""

    client_id: uuid.UUID | None
    template_code: str | None
    transport: TransportType | None
    is_enabled: bool
    quiet_hours_start: time | None
    quiet_hours_end: time | None
    max_messages_per_week: int | None


class PreferenceUpdateRequest(BaseModel):
    """A change to a message-type toggle or a quiet window — FR-M8-027.

    ⚠️ Every field is optional and ``None`` means "leave alone", so a screen that
    submits one section does not clear another. See ``preferences.upsert``.
    """

    template_code: str | None = Field(default=None, max_length=64)
    is_enabled: bool | None = None
    quiet_hours_start: time | None = None
    quiet_hours_end: time | None = None
    max_messages_per_week: int | None = Field(default=None, ge=0, le=100)
    transport: TransportType | None = None


class CheckinScheduleResponse(BaseModel):
    """A client's check-in cadence — FR-M8-022…024."""

    client_id: uuid.UUID
    frequency: CheckinFrequency
    day_of_week: int | None
    time_of_day: time
    is_paused: bool
    next_due_on: str | None
    last_generated_for: str | None


class CheckinScheduleRequest(BaseModel):
    """Configure or pause a client's check-ins."""

    frequency: CheckinFrequency = CheckinFrequency.WEEKLY
    #: ISO weekday, Monday = 1 … Sunday = 7.
    day_of_week: int | None = Field(default=None, ge=1, le=7)
    time_of_day: time = time(9, 0)
    #: 🔒 FR-M8-024 — pausing must not change the client's lifecycle stage, so it
    #: is a field on this resource rather than a stage transition.
    is_paused: bool = False


# ─── Message history and pending queue ───────────────────────────────────


@client_router.get(
    "/{client_id}/messages",
    summary="Every message sent to a client",
    operation_id="clientMessageHistory",
)
@requires(MESSAGE_HISTORY_READ)
async def message_history(
    request: Request,
    client_id: uuid.UUID,
    cursor: Annotated[str | None, Query(description="Opaque, from a previous page.")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> MessageHistoryResponse:
    """FR-M8-011 — the delivery log for one client, newest first.

    🔒 Every attempt, including failures and retries. A history that showed only
    successes would hide the case AC-M8-007 exists for.
    """
    await authorized_client(request, client_id)

    page = await list_message_history(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        client_id=client_id,
        limit=limit,
        cursor=cursor,
    )
    return MessageHistoryResponse(
        items=[
            DispatchResponse.model_validate(entry, from_attributes=True) for entry in page.items
        ],
        page=HistoryPageInfo(next_cursor=page.next_cursor, has_more=page.has_more),
    )


@client_router.get(
    "/{client_id}/messages/pending",
    summary="Messages queued for a client but not yet sent",
    operation_id="clientPendingMessages",
)
@requires(MESSAGE_PENDING_READ)
async def pending_messages(request: Request, client_id: uuid.UUID) -> list[PendingMessageResponse]:
    """FR-M8-028 — what is about to be sent on the practitioner's behalf."""
    await authorized_client(request, client_id)

    rows = await list_pending(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        client_id=client_id,
    )
    return [
        PendingMessageResponse(
            id=message.id,
            template_code=template.code,
            scheduled_for=message.scheduled_for,
            deferred_from=message.deferred_from,
            state=message.state,
            preview=render(
                template, {key: str(value) for key, value in message.template_variables.items()}
            ),
        )
        for message, template in rows
    ]


@client_router.post(
    "/{client_id}/messages",
    summary="Send a message to a client",
    operation_id="clientMessageSend",
)
@requires(MESSAGE_SEND)
async def send_message(
    request: Request, client_id: uuid.UUID, payload: SendMessageRequest
) -> ScheduledMessageResponse:
    """Queue a practitioner-triggered message — the core loop's "message the client".

    🔒 **Queues, never sends.** Suppression, quiet hours and the frequency cap are
    applied at dispatch by the same engine that handles every automated message
    (FR-M8-001). A practitioner cannot message a client who withdrew consent by
    doing it by hand, which is the protection they actually want.

    ⚠️ The occasion is the template plus the minute, so a double-tapped button
    queues one message while a deliberate re-send a minute later queues another.
    """
    client = await authorized_client(request, client_id)
    tenant_id = get_context().actor.require_tenant()
    session = get_session(request)

    template = await load_current_template(session, code=payload.template_code)
    when = payload.scheduled_for or utc_now()

    variables = _server_variables(template, client_name=client.full_name)
    variables.update(
        {key: value for key, value in payload.variables.items() if key not in variables}
    )

    message = await schedule(
        session,
        tenant_id=tenant_id,
        request=MessageRequest(
            template_code=template.code,
            occasion=f"manual:{template.code}:{when:%Y%m%d%H%M}",
            scheduled_for=when,
            source_module=_MANUAL_SOURCE,
            client_id=client_id,
            variables=variables,
        ),
    )

    record_audit(
        request,
        resource_id=message.id if message is not None else None,
        metadata={"template_code": template.code, "client_id": str(client_id)},
    )
    return ScheduledMessageResponse(
        id=message.id if message is not None else None,
        template_code=template.code,
        scheduled_for=when,
        state=ScheduledState.PENDING,
        created=message is not None,
    )


@router.post(
    "/scheduled/{scheduled_message_id}/cancel",
    summary="Cancel a message that has not been sent",
    operation_id="messageCancel",
)
@requires(MESSAGE_CANCEL)
async def cancel_scheduled(
    request: Request, scheduled_message_id: uuid.UUID
) -> ScheduledMessageResponse:
    """FR-M8-028's other half.

    ⚠️ Only a `pending` message can be cancelled. There is no "unsend": once the
    engine has handed a message to a transport, the honest record is the delivery
    log, and an endpoint that appeared to undo it would misinform the
    practitioner about what their client saw.
    """
    session = get_session(request)
    tenant_id = get_context().actor.require_tenant()

    # 🔒 Authorize against the *client* the message is for, not merely the
    # action. RLS confines this to the tenant; without this a colleague could
    # cancel a message queued for a client they cannot open (AC-M1-006).
    pending = await load_scheduled(
        session, tenant_id=tenant_id, scheduled_message_id=scheduled_message_id
    )
    if pending.client_id is not None:
        await authorized_client(request, pending.client_id)

    message = await cancel_one(
        session, tenant_id=tenant_id, scheduled_message_id=scheduled_message_id
    )
    template = await _template_of(session, message.template_id)

    record_audit(
        request,
        resource_id=message.id,
        metadata={"scheduled_message_id": str(message.id)},
    )
    return ScheduledMessageResponse(
        id=message.id,
        template_code=template.code,
        scheduled_for=message.scheduled_for,
        state=message.state,
        created=False,
    )


@router.get(
    "/failures",
    summary="Recent delivery failures across the practice",
    operation_id="messageFailures",
)
@requires(MESSAGE_HISTORY_READ)
async def message_failures(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> list[DispatchResponse]:
    """AC-M8-007 — terminal failures, in one place a practitioner will look."""
    entries = await recent_failures(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        limit=limit,
    )
    return [DispatchResponse.model_validate(entry, from_attributes=True) for entry in entries]


# ─── Templates and preview (FR-M8-026) ───────────────────────────────────


@router.get(
    "/templates",
    summary="Every message type this practice can send",
    operation_id="messageTemplateList",
)
@requires(MESSAGE_TEMPLATE_PREVIEW)
async def template_list(request: Request) -> list[TemplateResponse]:
    """The eight MVP message types — FR-M8-013…020, FR-M8-026."""
    templates = await list_published(get_session(request))
    return [_template_response(template) for template in templates]


@router.get(
    "/templates/{code}/preview",
    summary="A message template as the client will receive it",
    operation_id="messageTemplatePreview",
)
@requires(MESSAGE_TEMPLATE_PREVIEW)
async def template_preview(
    request: Request,
    code: str,
    client_id: Annotated[
        uuid.UUID | None,
        Query(description="Render with this client's own values."),
    ] = None,
) -> PreviewResponse:
    """🔒 FR-M8-026 — "as their clients will receive it".

    Rendered by :func:`modules.messaging.render`, the same function the dispatch
    path uses. A preview produced by different code would be a preview of
    something else, which is precisely the reassurance this requirement is for.

    ⚠️ With no ``client_id`` the values are illustrative and ``is_sample`` says
    so. Without that flag a practitioner could reasonably believe they were
    looking at a real client's message.
    """
    session = get_session(request)
    template = await load_current_template(session, code=code)

    client_name: str | None = None
    if client_id is not None:
        client = await authorized_client(request, client_id)
        client_name = client.full_name

    variables = _server_variables(template, client_name=client_name or "Priya Sharma")
    for name, spec in declared_variables(template).items():
        variables.setdefault(name, _sample_for(str(spec.get("type", "string"))))

    return PreviewResponse(
        template_code=template.code,
        version=template.version,
        transport=template.default_transport,
        body=render(template, variables),
        is_sample=client_id is None,
    )


# ─── Preferences (FR-M8-027) ─────────────────────────────────────────────


@router.get(
    "/preferences",
    summary="The practice's message-type settings",
    operation_id="messagePreferenceList",
)
@requires(MESSAGE_PREFERENCE_READ)
async def preference_list(request: Request) -> list[PreferenceResponse]:
    """FR-M8-027 — the tenant-wide toggles and quiet hours."""
    rows = await list_preferences(
        get_session(request), tenant_id=get_context().actor.require_tenant()
    )
    return [PreferenceResponse.model_validate(row, from_attributes=True) for row in rows]


@router.put(
    "/preferences",
    summary="Change a practice-wide message setting",
    operation_id="messagePreferenceUpdate",
)
@requires(MESSAGE_PREFERENCE_UPDATE)
async def preference_update(
    request: Request, payload: PreferenceUpdateRequest
) -> PreferenceResponse:
    """FR-M8-027 — disable a non-essential message type across the practice.

    🔒 An essential template is refused here, before the write, so the
    practitioner reads a sentence rather than a constraint violation. The
    database carries the same rule, which is what makes it true regardless of
    which path reaches the table.
    """
    session = get_session(request)
    tenant_id = get_context().actor.require_tenant()

    if payload.template_code is not None and payload.is_enabled is False:
        assert_disableable(await load_current_template(session, code=payload.template_code))

    row = await upsert_preference(
        session,
        tenant_id=tenant_id,
        client_id=None,
        template_code=payload.template_code,
        is_enabled=payload.is_enabled,
        quiet_hours_start=payload.quiet_hours_start,
        quiet_hours_end=payload.quiet_hours_end,
        max_messages_per_week=payload.max_messages_per_week,
        transport=payload.transport,
    )

    record_audit(
        request,
        resource_id=row.id,
        metadata={
            "template_code": payload.template_code or "all",
            "is_enabled": str(payload.is_enabled),
        },
    )
    return PreferenceResponse.model_validate(row, from_attributes=True)


@client_router.get(
    "/{client_id}/message-preferences",
    summary="A client's message settings",
    operation_id="clientMessagePreferences",
)
@requires(MESSAGE_PREFERENCE_READ)
async def client_preference_list(
    request: Request, client_id: uuid.UUID
) -> list[PreferenceResponse]:
    """The client's own overrides *and* the practice defaults they layer on.

    ⚠️ Both, deliberately: a screen showing an override without what it overrides
    cannot explain itself.
    """
    await authorized_client(request, client_id)
    rows = await list_preferences(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        client_id=client_id,
    )
    return [PreferenceResponse.model_validate(row, from_attributes=True) for row in rows]


@client_router.put(
    "/{client_id}/message-preferences",
    summary="Change a client's message settings",
    operation_id="clientMessagePreferenceUpdate",
)
@requires(MESSAGE_PREFERENCE_UPDATE)
async def client_preference_update(
    request: Request, client_id: uuid.UUID, payload: PreferenceUpdateRequest
) -> PreferenceResponse:
    """US-M8-06 — a per-client override, including an unsubscribe.

    ⚠️ 🔒 **A client-initiated unsubscribe must also write the consent ledger.**
    This is the practitioner-facing route and writes the preference only; the
    portal's own withdrawal path (S6) writes both, because the preference
    controls behaviour while the ledger is the legal record.
    """
    await authorized_client(request, client_id)
    session = get_session(request)

    if payload.template_code is not None and payload.is_enabled is False:
        assert_disableable(await load_current_template(session, code=payload.template_code))

    row = await upsert_preference(
        session,
        tenant_id=get_context().actor.require_tenant(),
        client_id=client_id,
        template_code=payload.template_code,
        is_enabled=payload.is_enabled,
        quiet_hours_start=payload.quiet_hours_start,
        quiet_hours_end=payload.quiet_hours_end,
        max_messages_per_week=payload.max_messages_per_week,
        transport=payload.transport,
    )

    record_audit(
        request,
        resource_id=row.id,
        metadata={
            "template_code": payload.template_code or "all",
            "is_enabled": str(payload.is_enabled),
            "client_id": str(client_id),
        },
    )
    return PreferenceResponse.model_validate(row, from_attributes=True)


# ─── Check-in schedules (FR-M8-022…024) ──────────────────────────────────


@client_router.get(
    "/{client_id}/checkin-schedule",
    summary="A client's check-in cadence",
    operation_id="clientCheckinScheduleRead",
)
@requires(CHECKIN_SCHEDULE_READ)
async def checkin_read(request: Request, client_id: uuid.UUID) -> CheckinScheduleResponse | None:
    """FR-M8-022. ``null`` when the client has no schedule yet."""
    await authorized_client(request, client_id)
    row = await load_checkin_schedule(
        get_session(request),
        tenant_id=get_context().actor.require_tenant(),
        client_id=client_id,
    )
    return _checkin_response(row) if row is not None else None


@client_router.put(
    "/{client_id}/checkin-schedule",
    summary="Configure or pause a client's check-ins",
    operation_id="clientCheckinScheduleUpdate",
)
@requires(CHECKIN_SCHEDULE_UPDATE)
async def checkin_update(
    request: Request, client_id: uuid.UUID, payload: CheckinScheduleRequest
) -> CheckinScheduleResponse:
    """FR-M8-022/024 — the cadence, and the pause that does not change the stage.

    🔒 Pausing cancels anything already queued. A pause that left Friday's nudge
    in the queue would send one more message after the practitioner asked us to
    stop.
    """
    await authorized_client(request, client_id)
    session = get_session(request)
    tenant_id = get_context().actor.require_tenant()

    row = await configure_checkin(
        session,
        tenant_id=tenant_id,
        client_id=client_id,
        settings=CheckinSettings(
            frequency=payload.frequency,
            day_of_week=payload.day_of_week,
            time_of_day=payload.time_of_day,
        ),
    )

    if payload.is_paused:
        paused = await pause_checkins(session, tenant_id=tenant_id, client_id=client_id)
        row = paused or row
    elif row.is_paused:
        row = await resume_checkins(session, tenant_id=tenant_id, client_id=client_id)

    record_audit(
        request,
        resource_id=row.id,
        metadata={
            "client_id": str(client_id),
            "frequency": payload.frequency.value,
            "is_paused": str(payload.is_paused),
        },
    )
    return _checkin_response(row)


# ─── Helpers ─────────────────────────────────────────────────────────────


def _template_response(template: Template) -> TemplateResponse:
    return TemplateResponse(
        code=template.code,
        version=template.version,
        category=template.category.value,
        is_essential=template.is_essential,
        default_transport=template.default_transport,
        provider_template_status=template.provider_template_status.value,
        is_practitioner_disableable=template.is_practitioner_disableable,
        variables=dict(template.variables),
    )


def _checkin_response(row: Any) -> CheckinScheduleResponse:
    return CheckinScheduleResponse(
        client_id=row.client_id,
        frequency=row.frequency,
        day_of_week=row.day_of_week,
        time_of_day=row.time_of_day,
        is_paused=row.is_paused,
        next_due_on=row.next_due_on.isoformat() if row.next_due_on else None,
        last_generated_for=row.last_generated_for.isoformat() if row.last_generated_for else None,
    )


def _server_variables(template: Template, *, client_name: str) -> dict[str, str]:
    """The variables the server fills in, whatever the caller sent.

    🔒 **The caller cannot supply these.** A caller-supplied ``client_name``
    would let one client's name be sent to another; a caller-supplied
    ``portal_url`` would be an open redirect delivered over WhatsApp under the
    practitioner's name. Both are filled here and the caller's values for them
    are discarded by :func:`send_message`.
    """
    declared = declared_variables(template)
    values: dict[str, str] = {}
    if "client_name" in declared:
        values["client_name"] = client_name
    if "lead_name" in declared:
        values["lead_name"] = client_name
    if "portal_url" in declared:
        values["portal_url"] = portal_url()
    return values


#: Illustrative values for a preview with no client — FR-M8-026.
#: ⚠️ Obviously fictitious. A sample that looked like real data would be
#: indistinguishable from a real client's message in a screenshot.
_SAMPLES: dict[str, str] = {
    "string": "Priya Sharma",
    "url": "https://example.invalid/portal",
    "datetime": "Friday 22 August, 10:30",
    "number": "20",
}


def _sample_for(variable_type: str) -> str:
    return _SAMPLES.get(variable_type, "…")


async def _template_of(session: AsyncSession, template_id: uuid.UUID) -> Template:
    """Load the exact template version a scheduled message points at."""
    return await load_template(session, template_id=template_id)
