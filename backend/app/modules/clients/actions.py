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
