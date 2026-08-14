"""Authorizable actions for the messaging engine — ADR-05.

🔒 Declared by the module that owns the resource, at import time. The startup
check in ``verify_route_authorization`` refuses to boot if a route declares an
action nobody registered, so this file and the routers are two halves of one
statement.

🔒 **Every action here is `TENANT_PII`, without exception, and that is not
laziness in classification.** A message history is a list of what a practitioner
told a client and when; a pending-message view is a list of what they are about
to be told; even a template *preview* is rendered with a client's name in it. The
operator boundary must put all of it permanently out of reach — FR-M11-003 is
explicit that support sees counts, not content, and `register_action` refuses at
import time if any of these ever declares `operator_access`.

⚠️ The one adjacent surface that is *not* here is the delivery-log summary an
operator reads for support (FR-M11-004, API §15.1). That is an S12 admin action
over `TENANT_METADATA` — counts and statuses, never recipients or bodies — and
it will be declared by the admin surface that owns it.
"""

from __future__ import annotations

from app.kernel.authz import DataScope, owner_or_assigned, register_action
from app.kernel.context import UserRole

#: Practitioners and owners. Clients never reach the practitioner realm, and
#: operators are excluded structurally by the data scope rather than by omission.
_PRACTITIONER = frozenset({UserRole.OWNER, UserRole.PRACTITIONER})

#: 🔒 **Every client-bound action carries this, and the omission was a real
#: defect.** Without a policy, ``authorize(request, client)`` re-enters
#: ``can()`` with a resource nothing consults: the role gate passes, and any
#: practitioner in the tenant reads any client's messages. The route calling
#: ``authorized_client`` is *not* sufficient on its own — it supplies the
#: resource, and this is what judges it.
#:
#: ⚠️ ``owner_or_assigned`` allows when the resource is ``None`` ("nothing to own
#: yet"), so the same action serves a tenant-wide route and a client-scoped one
#: without weakening either. FR-M0-017 gives an owner the whole tenant; a
#: practitioner sees what is assigned or granted (EC-M0-04), and the denial is
#: mapped to 404 rather than 403 so a colleague's caseload cannot be enumerated.
_SCOPED = (owner_or_assigned,)


#: 🔒 FR-M8-011 — the message history for one client.
#:
#: ⚠️ The resource judged is the **client**, not the dispatch row: the route
#: resolves it through ``authorized_client`` and this policy decides. A
#: `message_dispatches` row carries no `owner_user_id` of its own, and scoping
#: the history any other way would be the AC-M1-006 leak arriving through a new
#: door.
MESSAGE_HISTORY_READ = register_action(
    "message.history_read",
    roles=_PRACTITIONER,
    policies=_SCOPED,
    data_scope=DataScope.TENANT_PII,
    is_read=True,
)

#: 🔒 FR-M8-028 — pending scheduled messages for a client, so a practitioner can
#: see what is *about* to be sent on their behalf before it is.
MESSAGE_PENDING_READ = register_action(
    "message.pending_read",
    roles=_PRACTITIONER,
    policies=_SCOPED,
    data_scope=DataScope.TENANT_PII,
    is_read=True,
)

#: 🔒 FR-M8-028's other half — cancelling something scheduled but not yet sent.
#:
#: ⚠️ The route resolves the message's own client and authorizes against them
#: before cancelling, so a colleague cannot reach into a caseload they cannot
#: open. RLS already confines this to the tenant; the policy is what confines it
#: within one.
#:
#: ⚠️ Cancellation is only possible while a message is `pending`. There is no
#: "unsend": once the dispatch engine has handed a message to a transport, the
#: only honest record is the delivery log, and an endpoint that appeared to undo
#: a sent message would be lying to the practitioner about what their client saw.
MESSAGE_CANCEL = register_action(
    "message.cancel",
    roles=_PRACTITIONER,
    policies=_SCOPED,
    data_scope=DataScope.TENANT_PII,
    audit_metadata_keys={"scheduled_message_id"},
)

#: 🔒 FR-M8-026 — previewing a template "as the client will receive it".
#:
#: ⚠️ `TENANT_PII` even though a template is platform-owned reference data. The
#: preview is rendered *with this client's values*, which is the entire point of
#: the requirement — a preview of the raw template with `{client_name}` in it
#: would not answer the question a practitioner is asking.
MESSAGE_TEMPLATE_PREVIEW = register_action(
    "message_template.preview",
    roles=_PRACTITIONER,
    policies=_SCOPED,
    data_scope=DataScope.TENANT_PII,
    is_read=True,
)

#: 🔒 FR-M8-027 — reading the tenant's message-type toggles and quiet hours.
MESSAGE_PREFERENCE_READ = register_action(
    "message_preference.read",
    roles=_PRACTITIONER,
    policies=_SCOPED,
    data_scope=DataScope.TENANT_PII,
    is_read=True,
)

#: 🔒 FR-M8-027 — disabling a non-essential message type tenant-wide, or setting
#: a per-client override.
#:
#: ⚠️ Not owner-only. The same reasoning as `enquiry_form.update`: in the
#: single-practitioner practice that is the launch persona, an owner-only gate
#: means the only person who can change it is the only person there. A clinic
#: wanting to restrict this is a Phase 2 role question.
#:
#: 🔒 What it can never do is silence an *essential* template — that is refused
#: by `ck_message_templates__essential_not_disableable` at the database and by
#: `preferences.assert_disableable` before the write, so a client cannot be
#: locked out of their own portal by a preference.
MESSAGE_PREFERENCE_UPDATE = register_action(
    "message_preference.update",
    roles=_PRACTITIONER,
    policies=_SCOPED,
    data_scope=DataScope.TENANT_PII,
    audit_metadata_keys={"template_code", "is_enabled", "client_id"},
)

#: 🔒 FR-M8-022 — reading a client's check-in cadence.
CHECKIN_SCHEDULE_READ = register_action(
    "checkin_schedule.read",
    roles=_PRACTITIONER,
    policies=_SCOPED,
    data_scope=DataScope.TENANT_PII,
    is_read=True,
)

#: 🔒 FR-M8-022/024 — configuring or pausing a client's check-in cadence.
CHECKIN_SCHEDULE_UPDATE = register_action(
    "checkin_schedule.update",
    roles=_PRACTITIONER,
    policies=_SCOPED,
    data_scope=DataScope.TENANT_PII,
    audit_metadata_keys={"client_id", "frequency", "is_paused"},
)

#: 🔒 A practitioner sending a message by hand — the "message the client" step of
#: the core loop (PRD §J2 step 6).
#:
#: ⚠️ This creates a `scheduled_messages` row like every other producer; it does
#: not send. FR-M8-001 admits no exception for a human-initiated message, and the
#: suppression rules a practitioner would bypass by sending directly are exactly
#: the ones protecting them from messaging a client who withdrew consent.
MESSAGE_SEND = register_action(
    "message.send",
    roles=_PRACTITIONER,
    policies=_SCOPED,
    data_scope=DataScope.TENANT_PII,
    audit_metadata_keys={"template_code", "client_id"},
)
