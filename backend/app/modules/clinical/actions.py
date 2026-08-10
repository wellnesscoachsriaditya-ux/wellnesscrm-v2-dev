"""Authorizable actions for the clinical workspace — ADR-05, AC-M1-006.

🔒 Declared by the module that owns the resource, at import time. The startup
check in ``verify_route_authorization`` refuses to boot if a route declares an
action nobody registered, so this file and the routers are two halves of one
statement.

🔒 **Every action here is ``owner_or_assigned``-scoped**, without exception, and
this module is where that matters most: an assessment holds a person's medical
history, a measurement holds their weight, a document holds their lab results.
Slices A/B of S2 shipped client actions with *no* policy and any practitioner in
the tenant could reach any client (fixed in Slice C); repeating that here would
expose clinical data rather than contact details.

⚠️ **``DataScope.TENANT_PII`` on all of them**, which ``kernel.authz`` defines to
cover "health records, measurements" explicitly. That scope is what puts this
data permanently out of the operator console's reach, and it is enforced rather
than documented: ``register_action`` **refuses at import time** if an operator
action declares it (Arch §15.4, FR-M11-003 — "aggregate views serve most support
cases"). A support operator diagnosing a billing problem cannot read a client's
medical history.
"""

from __future__ import annotations

from app.kernel.authz import DataScope, owner_or_assigned, register_action
from app.kernel.context import UserRole

#: Practitioners and owners. Clients reach their own records through the portal
#: realm, which is a separate token and a separate action set entirely.
_PRACTITIONER = frozenset({UserRole.OWNER, UserRole.PRACTITIONER})

#: 🔒 The row-level check. See the module docstring.
_SCOPED = (owner_or_assigned,)


# ─── Assessments (FR-M3-001…008) ─────────────────────────────────────────

#: Reading a client's assessments and their answers.
ASSESSMENT_READ = register_action(
    "assessment.read",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    policies=_SCOPED,
    is_read=True,
)

#: 🔒 Starting an administration, saving progress, submitting — FR-M3-004/005.
#:
#: ⚠️ One action for all three rather than three. They are the same authority
#: over the same resource, and a practitioner who may start an assessment but not
#: submit it is not a role anybody has asked for. Splitting it would put two more
#: names in the registry for no decision anyone makes.
ASSESSMENT_WRITE = register_action(
    "assessment.write",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    policies=_SCOPED,
    audit_metadata_keys={"response_id", "definition_version"},
)


# ─── Measurements (FR-M3-011…015) ────────────────────────────────────────

MEASUREMENT_READ = register_action(
    "measurement.read",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    policies=_SCOPED,
    is_read=True,
)

MEASUREMENT_RECORD = register_action(
    "measurement.record",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    policies=_SCOPED,
    audit_metadata_keys={"measurement_id", "measured_on"},
)


# ─── Consultation notes (FR-M3-018…021) ──────────────────────────────────
#
# 🔒 There is deliberately **no client-facing counterpart** to any of these.
# FR-M3-021 and AC-M3-006 make notes invisible to the client, and the absence of
# an action is one of the three mechanisms that holds it — the others being the
# missing RLS policy and the portal projection's exclusion.

CONSULTATION_NOTE_READ = register_action(
    "consultation_note.read",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    policies=_SCOPED,
    is_read=True,
)

CONSULTATION_NOTE_WRITE = register_action(
    "consultation_note.write",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    policies=_SCOPED,
    audit_metadata_keys={"note_id"},
)

#: 🔒 Editing is author-only and archiving is author-or-owner, but **neither rule
#: is expressible here**: both depend on who wrote the row, which the policy
#: engine cannot see before the row is loaded. `modules.clinical.notes` enforces
#: them, exactly as `modules.clients.notes` does for `client_notes` (FR-M3-020).
CONSULTATION_NOTE_ARCHIVE = register_action(
    "consultation_note.archive",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    policies=_SCOPED,
    audit_metadata_keys={"note_id"},
)


# ─── Documents (FR-M3-024…027) ───────────────────────────────────────────

CLIENT_DOCUMENT_READ = register_action(
    "client_document.read",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    policies=_SCOPED,
    is_read=True,
)

#: 🔒 Authorizing an upload and attaching the confirmed file. The storage layer
#: applies the type, size and quota limits (EC-M3-04, EC-M3-07) before any bytes
#: move; this decides whether *this* practitioner may put a document on *this*
#: client at all.
CLIENT_DOCUMENT_UPLOAD = register_action(
    "client_document.upload",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    policies=_SCOPED,
    audit_metadata_keys={"document_id", "document_type"},
)

#: 🔒 Issuing a download URL is its own action, not part of `read`.
#:
#: ⚠️ FR-M0-038/NFR-035: a signed URL outlives the request and leaks through
#: logs, history and screenshots. Auditing it separately is what makes "who
#: obtained the bytes of this lab report" answerable, which "who listed the
#: documents" does not.
CLIENT_DOCUMENT_DOWNLOAD = register_action(
    "client_document.download",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    policies=_SCOPED,
    audit_metadata_keys={"document_id"},
)

CLIENT_DOCUMENT_ARCHIVE = register_action(
    "client_document.archive",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    policies=_SCOPED,
    audit_metadata_keys={"document_id"},
)
