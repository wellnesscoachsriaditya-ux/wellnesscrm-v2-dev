"""Authorizable actions for the client spine — ADR-05.

🔒 Declared by the module that owns the resource, at import time. The startup
check in ``verify_route_authorization`` refuses to boot if a route declares an
action nobody registered, so this file and the router are two halves of one
statement.

⚠️ Every action here is :data:`DataScope.TENANT_PII`. A client record is a name,
a mobile number and a dietary class attached to a person — the operator boundary
(``kernel.authz.DataScope``) puts that permanently out of operator reach, and
``register_action`` refuses at import time if any of these were ever marked
operator-accessible.
"""

from __future__ import annotations

from app.kernel.authz import DataScope, register_action
from app.kernel.context import UserRole

#: Practitioners and owners. Clients never reach the practitioner realm, and
#: operators are excluded structurally by the scope rather than by omission.
_PRACTITIONER = frozenset({UserRole.OWNER, UserRole.PRACTITIONER})


CLIENT_CREATE = register_action(
    "client.create",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    # ⚠️ Not metered here. FR-M1-003 — a client entering at stage `lead` costs
    # nothing, and EC-M2-06 requires a tenant at their limit to keep accepting
    # leads. Metering binds on the transition to `active` (Slice B), which is
    # the only moment a client consumes the entitlement.
    audit_metadata_keys={"stage"},
)

CLIENT_READ = register_action(
    "client.read",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    is_read=True,
)

CLIENT_UPDATE = register_action(
    "client.update",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
)

# ─── Lifecycle (ADR-A06) ─────────────────────────────────────────────────
#
# 🔒 Three actions rather than one, and separate from `client.update`. ADR-A06's
# claim is that a transition is "explicit, separately authorizable and separately
# auditable" — which is only true if it has its own action name. Folding these
# into `client.update` would make "who may archive a client" unanswerable
# independently of "who may correct a typo in their name", and a clinic owner
# will eventually want exactly that distinction (EC-M1-04).

CLIENT_CHANGE_STAGE = register_action(
    "client.change_stage",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    # 🔒 The metered one (FR-M1-002). `meters` names the resource this action can
    # consume so the declaration is inspectable — the enforcement itself happens
    # in `transitions.change_stage`, which is the only place that knows whether
    # *this particular* transition enters `active` (FR-M1-003).
    meters="active_clients",
    # Both stages, so the audit log answers "what changed" without joining to
    # `client_stage_history`. Enum values, not prose — NFR-033.
    audit_metadata_keys={"from_stage", "to_stage"},
)

CLIENT_ARCHIVE = register_action(
    "client.archive",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    # ⚠️ Not metered. Archiving *frees* a slot; it never consumes one.
    audit_metadata_keys={"stage"},
)

CLIENT_RESTORE = register_action(
    "client.restore",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    # 🔒 Metered, for the reason EC-M1-06 gives: restoring a client archived at
    # stage `active` puts them back on the meter, so a practitioner cannot
    # archive their way under a limit and then undo it.
    meters="active_clients",
    audit_metadata_keys={"stage"},
)
