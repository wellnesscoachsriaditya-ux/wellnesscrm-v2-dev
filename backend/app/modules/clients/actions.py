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

🔒 **Every client-bound action carries ``owner_or_assigned``** (FR-M0-017,
AC-M1-006). The role gate alone says "a practitioner may read clients"; the
policy says "may read *this* client", which is the question that matters in a
two-practitioner clinic. Slice A and B shipped these actions with no policy,
because the grant model they need did not exist until Slice C — this file is
where that is closed.

⚠️ The policy is only consulted when a **resource** is supplied. ``can()``
receives one from ``pipeline.authorize()``, which a route calls after loading the
client through ``access.load_for_access``. A route that skips that call gets the
coarse decision and nothing more, which is why
``tests/test_client_access.py::test_every_client_bound_route_authorizes`` exists.
"""

from __future__ import annotations

from app.kernel.authz import DataScope, owner_or_assigned, register_action
from app.kernel.context import UserRole

#: Practitioners and owners. Clients never reach the practitioner realm, and
#: operators are excluded structurally by the scope rather than by omission.
_PRACTITIONER = frozenset({UserRole.OWNER, UserRole.PRACTITIONER})

#: 🔒 Owner-only. Granting access widens who can see a client, and FR-M0-017
#: makes the owner the one role with a view of the whole tenant — concentrating
#: the widening power there keeps "who can see this client" answerable by asking
#: one person. See ``kernel.collaboration.may_manage_access``.
_OWNER_ONLY = frozenset({UserRole.OWNER})

#: 🔒 The scoping policy, applied to every action that names a specific client.
#: Named once so that adding an action without it is visibly different from the
#: others rather than an omission a reader has to notice.
_SCOPED = (owner_or_assigned,)


CLIENT_CREATE = register_action(
    "client.create",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    # ⚠️ No `owner_or_assigned`: there is no client yet to be assigned to, and
    # the policy's own `resource is None` branch would allow it regardless.
    # Ownership is established *by* this write (`owner_user_id` defaults to the
    # caller), not checked before it.
    #
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
    policies=_SCOPED,
    is_read=True,
)

CLIENT_UPDATE = register_action(
    "client.update",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    policies=_SCOPED,
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
    policies=_SCOPED,
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
    policies=_SCOPED,
    # ⚠️ Not metered. Archiving *frees* a slot; it never consumes one.
    audit_metadata_keys={"stage"},
)

CLIENT_RESTORE = register_action(
    "client.restore",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    policies=_SCOPED,
    # 🔒 Metered, for the reason EC-M1-06 gives: restoring a client archived at
    # stage `active` puts them back on the meter, so a practitioner cannot
    # archive their way under a limit and then undo it.
    meters="active_clients",
    audit_metadata_keys={"stage"},
)


# ─── Collaboration (FR-M1-007/008, EC-M0-04) ─────────────────────────────
#
# 🔒 Notes, tags and access are all scoped by `owner_or_assigned` too. A
# practitioner who cannot read a client must not be able to read the notes
# written about them — which would be the same leak arriving through a different
# route, and the reason these declarations are not simply folded into
# `client.read`.

CLIENT_READ_NOTES = register_action(
    "client.read_notes",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    policies=_SCOPED,
    is_read=True,
)

CLIENT_WRITE_NOTE = register_action(
    "client.write_note",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    policies=_SCOPED,
)

CLIENT_MANAGE_TAGS = register_action(
    "client.manage_tags",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    policies=_SCOPED,
)

#: The tenant's tag vocabulary, which belongs to no single client.
#:
#: ⚠️ **No `owner_or_assigned`**, and that is not an oversight: a tag list is not
#: about a client, so there is no resource to scope it by. It carries
#: `TENANT_METADATA` rather than `TENANT_PII` for the same reason — a tag name is
#: the practice's vocabulary, not a fact about a person.
TAG_READ = register_action(
    "tag.read",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_METADATA,
    is_read=True,
)

TAG_MANAGE = register_action(
    "tag.manage",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_METADATA,
)

CLIENT_READ_ACCESS = register_action(
    "client.read_access",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    policies=_SCOPED,
    is_read=True,
)

#: 🔒 Owner-only, and the most security-relevant action in the module: it changes
#: who can see a client. The role gate here and the check in
#: `assignments._assert_may_manage` are deliberately redundant — a future route
#: that forgot this declaration would otherwise widen access silently.
CLIENT_MANAGE_ACCESS = register_action(
    "client.manage_access",
    roles=_OWNER_ONLY,
    data_scope=DataScope.TENANT_PII,
    policies=_SCOPED,
    audit_metadata_keys={"grantee_user_id", "operation"},
)


# ─── Timeline (FR-M1-018, DDR-06) ────────────────────────────────────────

#: 🔒 Its own action rather than folding into `client.read`, for the reason
#: ADR-05 gives: an action is the unit authorization is reasoned about, and
#: "who may read this client's history" is a question a clinic will eventually
#: want to answer separately from "who may see their phone number".
#:
#: ⚠️ `TENANT_PII` even though a summary is a non-clinical label. The *sequence*
#: is the sensitive part: "archived, restored, note added, note added" describes
#: a person's engagement with the practice, and the operator boundary must keep
#: that out of reach as firmly as the name it belongs to.
CLIENT_READ_TIMELINE = register_action(
    "client.read_timeline",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    policies=_SCOPED,
    is_read=True,
)


# ─── Discovery (FR-M1-021/022) ───────────────────────────────────────────

#: 🔒 **No `owner_or_assigned`, and this is the one place that absence is
#: correct rather than an omission.** The policy answers "may I see *this*
#: client", which needs a resource; a list has none until it has run. Scoping a
#: list is a different operation — a WHERE clause — and it lives in
#: `discovery._visible_to`, which the route is obliged to use.
#:
#: ⚠️ That obligation is the risk this declaration carries. A future list route
#: reaching `list_clients` without passing the actor's role would return the
#: whole tenant to a practitioner. `tests/test_client_access.py` asserts the
#: route list; the integration suite asserts the rows.
CLIENT_LIST = register_action(
    "client.list",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    is_read=True,
)

#: 🔒 Owner-only, exactly like `client.manage_access`: reassigning changes who is
#: accountable for a client, and doing it to fifty at once does not make it a
#: lesser act. EC-M1-04's motivating case — a practitioner leaving — is the
#: owner's decision by definition.
#:
#: ⚠️ Deliberately **not** `_SCOPED`. The policy takes one resource and this
#: action names many, so a per-client check happens inside `bulk_reassign_owner`
#: where every id is known. Declaring a policy that silently could not run on
#: the list is worse than declaring none and checking explicitly.
CLIENT_BULK_REASSIGN = register_action(
    "client.bulk_reassign",
    roles=_OWNER_ONLY,
    data_scope=DataScope.TENANT_PII,
    audit_metadata_keys={"client_count", "to_user_id"},
)
