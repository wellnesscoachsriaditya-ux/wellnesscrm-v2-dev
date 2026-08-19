"""Authorizable actions for lead capture — ADR-05.

🔒 Declared by the module that owns the resource, at import time. The startup
check in ``verify_route_authorization`` refuses to boot if a route declares an
action nobody registered, so this file and the routers are two halves of one
statement.

⚠️ **The public endpoints declare no action at all**, and that is not an
omission. `POST /public/forms/{slug}/submit` establishes nothing and has no
actor — the same category as `/public/auth/login`. Both public paths are named in
``EXEMPT_PATHS``, which is where an exemption is visible to a reviewer rather
than inherited from a prefix. What protects them instead is stated at each
route: rate limiting (API §11), spam scoring (FR-M2-008), and consent as a
precondition (EC-M2-04).
"""

from __future__ import annotations

from app.kernel.authz import DataScope, register_action
from app.kernel.context import UserRole

#: Practitioners and owners. Clients never reach the practitioner realm, and
#: operators are excluded structurally by the data scope rather than by omission.
_PRACTITIONER = frozenset({UserRole.OWNER, UserRole.PRACTITIONER})


#: 🔒 Reading the enquiry list — API §7.2, FR-M2-011.
#:
#: ⚠️ **No `owner_or_assigned` policy, and this needs stating.** The same
#: reasoning as `client.list` (Slice E): the policy answers "may I see *this*
#: resource" and needs one row, while a list has none until it has run. Scoping a
#: list is a WHERE clause, and it lives in `leads.discovery._visible_to`.
#:
#: ⚠️ `TENANT_PII` — a submission is a name, a mobile number and a stated health
#: goal. That is the most sensitive thing on the public surface, and the operator
#: boundary must put it permanently out of reach.
ENQUIRY_LIST = register_action(
    "enquiry.list",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    is_read=True,
)

#: 🔒 Marking an enquiry answered — FR-M2-011's other half.
#:
#: ⚠️ Deliberately **not** `owner_or_assigned`. The action names a *submission*,
#: not a client, and `owner_or_assigned` inspects a resource's `owner_user_id` —
#: a submission has none. The scoping that matters is applied where the row is
#: loaded: `respond.mark_responded` resolves the submission's client and refuses
#: unless the caller may reach it. Declaring a policy that silently could not run
#: on this resource would be worse than declaring none and checking explicitly —
#: the same call `client.bulk_reassign` makes.
ENQUIRY_RESPOND = register_action(
    "enquiry.respond",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    audit_metadata_keys={"submission_id"},
)

#: 🔒 Reading the tenant's own form definition — API §7.2.
#:
#: ⚠️ `TENANT_METADATA`, not `TENANT_PII`: a form is a title, some intro prose and
#: an active flag. It contains no fact about any person, which is precisely why
#: the public endpoint can serve it to an anonymous caller (API §11.1). Marking
#: it PII would be a classification the public route already contradicts.
ENQUIRY_FORM_READ = register_action(
    "enquiry_form.read",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_METADATA,
    is_read=True,
)

#: 🔒 Editing the form — API §7.2's PATCH, FR-M2-001.
#:
#: ⚠️ Not owner-only, unlike `client.manage_access`. Deactivating a form stops
#: enquiries arriving, which is disruptive but not a disclosure — and in a
#: single-practitioner practice (the launch persona) an owner-only gate would
#: mean the only person who can edit the form is the only person there anyway.
#: A clinic wanting to restrict this is a Phase 2 role question, not an MVP one.
ENQUIRY_FORM_UPDATE = register_action(
    "enquiry_form.update",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_METADATA,
    audit_metadata_keys={"form_id", "is_active"},
)
