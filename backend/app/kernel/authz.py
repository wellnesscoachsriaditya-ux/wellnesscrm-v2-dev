"""Authorization — the single decision point.

🔒 **ADR-05 / FR-M0-015 / NFR-032.** One function decides every permission
question in the system::

    can(actor, action, resource) -> Decision

**This module contains no domain knowledge.** It ships zero actions and zero
policies. `clients`, `leads`, `plans` and every future module register their own
at import time, which is what keeps the kernel reusable (Arch §3.1) and what
makes adding a module a module-local change rather than a kernel edit.

🔒 Five properties, each answering a specific failure:

1. **Deny by default** (FR-M0-019) — an unregistered action is denied. V1's
   permission checks were opt-in, so a forgotten check meant open access. Here a
   forgotten *registration* means no access, which fails safe and fails loudly.
2. **Declarative** — actions are declared next to the routes that use them, so
   the permission surface is enumerable by reading the registry rather than by
   auditing code paths.
3. **Startup validation** — a route without a declared action aborts startup
   (:func:`assert_all_routes_declared`). A decorator can be forgotten; a process
   that refuses to boot cannot be.
4. **Ownership is part of the decision, not a later filter.** "A practitioner
   may read a client" is not a decision — "may read *this* client" is. Splitting
   the two is how row-level leaks appear in role-based systems, so a policy
   receives the resource and returns one verdict.
5. 🔒 **The platform operator is not a superuser.** Running the SaaS does not
   confer the right to read a tenant's clients. :class:`DataScope` makes that a
   structural property rather than a policy someone must remember to write —
   see below.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.kernel.context import Actor, UserRole

# ─── Data classification ─────────────────────────────────────────────────


class DataScope(enum.StrEnum):
    """🔒 What class of data an action can reach. The operator boundary.

    The product is sold to practitioners who hold their clients' health data. We
    operate the platform; we are not a party to that relationship. Support,
    billing and analytics must therefore be possible **without** the ability to
    read a client's name, phone number, chat, plan, measurements or notes.

    "Operators are read-only" is not sufficient — a read is the whole problem.
    So the boundary is drawn on *what the data is*, not on what is done to it,
    and it is declared on the action rather than enforced by convention:

    ``PLATFORM``
        Data we own: tenant records, subscriptions, plan definitions, invoices,
        job runs. Operators may read and, where declared, write.

    ``TENANT_METADATA``
        Tenant-owned but non-identifying: row counts, statuses, timestamps,
        usage totals, feature flags. Answers "is this account healthy?" without
        answering "who are its clients?". Operators may read when declared.

    ``TENANT_PII``
        🔒 Identifiable or clinical data belonging to a tenant or its clients —
        names, contact details, messages, diet plans, health records,
        measurements, follow-up notes, files. **No operator action may declare
        this scope**; :func:`register_action` refuses at import time.

    ``AGGREGATE``
        Derived figures computed across tenants where no individual is
        identifiable: total clients, total coaches, MRR, AI credits consumed.
        This is the only scope in which cross-tenant reads are legitimate, and
        the intended foundation for a future platform analytics layer — which
        reads pre-aggregated figures under this scope and is structurally unable
        to reach the rows they were computed from.

    ⚠️ The default is ``TENANT_PII``: an action whose author did not think about
    classification gets the most restrictive treatment, not the least.
    """

    PLATFORM = "platform"
    TENANT_METADATA = "tenant_metadata"
    TENANT_PII = "tenant_pii"
    AGGREGATE = "aggregate"


#: Scopes an operator may ever be granted. Deliberately not derived by
#: exclusion — adding a scope should require a decision about operator access,
#: not silently inherit one.
OPERATOR_ELIGIBLE_SCOPES: frozenset[DataScope] = frozenset(
    {DataScope.PLATFORM, DataScope.TENANT_METADATA, DataScope.AGGREGATE}
)


# ─── What a policy sees ──────────────────────────────────────────────────


@runtime_checkable
class Resource(Protocol):
    """The minimum a policy needs to reason about ownership.

    A structural protocol, not a base class: an ORM model, a dataclass or a
    lightweight stand-in all satisfy it without importing the kernel. That is
    what lets the authorization matrix be a table test over fakes (Arch §5.4)
    instead of an integration test needing a database.

    ``owner_user_id`` is the assigned practitioner where the concept applies.
    ``None`` means the resource is tenant-wide rather than individually owned.
    """

    @property
    def tenant_id(self) -> object: ...

    @property
    def owner_user_id(self) -> object: ...


@dataclass(frozen=True, slots=True)
class Decision:
    """The verdict. Carries a reason so a denial is explainable in an audit row.

    🔒 ``reason`` is written to the audit log and to logs, never returned to the
    caller: telling a client precisely which rule refused them is a probing aid
    (API §5.4). The HTTP layer converts a denial into a generic 403 — or a 404
    where confirming existence would itself leak across the tenant boundary.
    """

    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def allow(reason: str = "") -> Decision:
    return Decision(allowed=True, reason=reason)


def deny(reason: str) -> Decision:
    """Build a denial. A reason is mandatory — an unexplained denial is
    unauditable, and the first thing anyone asks about a 403 is *why*."""
    if not reason:
        raise ValueError("A denial must carry a reason — it is written to the audit log.")
    return Decision(allowed=False, reason=reason)


#: A policy predicate. Receives the actor and the resource under consideration
#: (``None`` for create-style actions, where no instance exists yet).
Policy = Callable[[Actor, Resource | None], Decision]


# ─── The action registry ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Action:
    """A named, authorizable operation.

    🔒 The name is ``<resource>.<verb>`` and the verb is **semantic, not an HTTP
    method**. ``PATCH /clients/{id}/activate`` declares ``client.activate``, not
    ``client.update`` — the operation has its own authorization rule, its own
    audit meaning and its own failure mode (a 402 at the plan limit), and
    collapsing it into "update" would lose all three (ADR-A06).
    """

    name: str
    #: Which roles may attempt this action at all. A coarse gate evaluated
    #: before the policies, so the common case needs no predicate.
    roles: frozenset[UserRole]
    #: 🔒 What class of data the action reaches. Governs operator access.
    data_scope: DataScope = DataScope.TENANT_PII
    #: 🔒 Whether a platform operator may perform this action across tenants.
    #: Refused at registration for ``TENANT_PII``.
    operator_access: bool = False
    #: Fine-grained rules — ownership, state, realm. All must allow.
    policies: tuple[Policy, ...] = ()
    #: True for reads. Reads are not audited by default; the exception is an
    #: operator touching tenant data (FR-M0-032), which the audit writer decides.
    is_read: bool = False
    #: Metered resource code consumed by this action, if any. Read by the usage
    #: engine in Slice E. Declared with the action rather than hardcoded at the
    #: call site, so a new metered resource needs no kernel change (FR-M10-001).
    meters: str | None = None
    #: Extra keys this action may place in ``audit_log.metadata``, beyond the
    #: kernel's own allowlist. Declared per action so a new module extends the
    #: allowlist without editing the kernel.
    audit_metadata_keys: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if "." not in self.name:
            raise ValueError(
                f"Action name {self.name!r} must be '<resource>.<verb>' — the registry is "
                "grouped by resource, and an unqualified verb collides across modules."
            )


@dataclass
class ActionRegistry:
    """Every action the process knows about.

    A registry rather than an enum because modules are added over time and the
    kernel must not enumerate them (Arch §3.1, R1). Population happens at import
    time, so by the end of application startup the registry is complete and
    :func:`assert_all_routes_declared` can be trusted.
    """

    _actions: dict[str, Action] = field(default_factory=dict)

    def register(self, action: Action) -> Action:
        """Register an action. Conflicting re-registration is refused.

        ⚠️ Silently overwriting would let a later import widen an earlier
        module's permissions invisibly — a security hole no test would name.
        Re-registering the *same* action is tolerated, because a module imported
        twice under different names is a packaging accident, not a threat.
        """
        existing = self._actions.get(action.name)
        if existing is not None and existing != action:
            raise ValueError(
                f"Action {action.name!r} is already registered with different rules. "
                "Two modules must not define the same action; rename one, or move the "
                "shared operation into the module that owns the resource."
            )
        self._actions[action.name] = action
        return action

    def get(self, name: str) -> Action | None:
        return self._actions.get(name)

    def names(self) -> frozenset[str]:
        return frozenset(self._actions)

    def operator_visible(self) -> frozenset[str]:
        """Actions an operator may perform. The reviewable answer to "what can
        the SaaS owner see?" — one call, rather than an audit of every module."""
        return frozenset(name for name, a in self._actions.items() if a.operator_access)

    def clear(self) -> None:
        """Empty the registry. For tests only."""
        self._actions.clear()


#: The process-wide registry. One per process, populated at import time.
REGISTRY = ActionRegistry()


def register_action(
    name: str,
    *,
    roles: Iterable[UserRole],
    data_scope: DataScope = DataScope.TENANT_PII,
    operator_access: bool = False,
    policies: Iterable[Policy] = (),
    is_read: bool = False,
    meters: str | None = None,
    audit_metadata_keys: Iterable[str] = (),
) -> Action:
    """Declare an authorizable action.

    Called at import time by the module that owns the resource::

        # app/modules/clients/actions.py  (S2 — not the kernel's business)
        CLIENT_READ = register_action(
            "client.read",
            roles={UserRole.OWNER, UserRole.PRACTITIONER},
            data_scope=DataScope.TENANT_PII,
            policies=[owner_or_assigned],
            is_read=True,
        )

    Nothing here knows what a client is. That is the point.

    Raises:
        ValueError: If the declaration would grant an operator access to tenant
            PII. 🔒 Import-time rather than request-time: the boundary should
            break the build, not produce a 403 nobody notices.
    """
    if operator_access and data_scope not in OPERATOR_ELIGIBLE_SCOPES:
        raise ValueError(
            f"Action {name!r} declares operator_access with data_scope={data_scope.value!r}.\n\n"
            "🔒 Platform operators must not reach tenant PII — names, contact details, "
            "messages, plans, health records, measurements, notes or files. Running the "
            "platform is not a basis for reading a practitioner's clients.\n\n"
            "If this action supports an operator workflow, expose the non-identifying "
            "form instead: DataScope.TENANT_METADATA for per-tenant counts and statuses, "
            "or DataScope.AGGREGATE for figures computed across tenants."
        )

    return REGISTRY.register(
        Action(
            name=name,
            roles=frozenset(roles),
            data_scope=data_scope,
            operator_access=operator_access,
            policies=tuple(policies),
            is_read=is_read,
            meters=meters,
            audit_metadata_keys=frozenset(audit_metadata_keys),
        )
    )


# ─── The decision ────────────────────────────────────────────────────────


def can(actor: Actor, action_name: str, resource: Resource | None = None) -> Decision:
    """🔒 The one authorization decision point in the system.

    No module implements permission logic; every module calls this (R3, checked
    by ``tools/check_boundaries.py``). Evaluation order is cheapest-first, and
    each step can only narrow the outcome:

    1. Is the action known? Unknown → deny (FR-M0-019).
    2. Is the actor authenticated, with a role?
    3. 🔒 If the actor is a platform operator: is this action one they may
       perform at all, and does it stay clear of tenant PII?
    4. 🔒 Otherwise: does the actor's tenant match the resource's? Checked here
       as well as by RLS — defence in depth (Arch §6.4), because a resource may
       reach a policy from a path that never touched the database.
    5. Is the actor's role permitted this action?
    6. Do all fine-grained policies allow it?

    Returns:
        A :class:`Decision`. Callers must treat a falsy decision as final —
        there is no "retry with more privilege".
    """
    action = REGISTRY.get(action_name)
    if action is None:
        # 🔒 Deny by default. An action absent from the registry is far more
        # likely a forgotten registration than an intentional public operation,
        # and the safe reading of ambiguity is refusal.
        return deny(f"action_not_registered:{action_name}")

    if not actor.is_authenticated:
        return deny(f"unauthenticated:{action_name}")

    if actor.role is None:
        return deny(f"no_role:{action_name}")

    if actor.is_operator:
        # 🔒 Belt and braces with the registration-time check: a hand-built
        # Action that never passed through `register_action` still cannot reach
        # tenant PII.
        if action.data_scope not in OPERATOR_ELIGIBLE_SCOPES:
            return deny(f"operator_denied_pii:{action_name}")
        if not action.operator_access:
            return deny(f"operator_access_not_declared:{action_name}")
        # 🔒 The role gate applies to operators as it does to everyone else.
        # Operator status is not an implicit override: `operator_access` says
        # the action is *reachable* from the platform side, and `roles` still
        # says *who* may reach it. Skipping this would make an operator token
        # able to perform an action declared for tenant owners alone, which is
        # authority nobody granted and no declaration records.
        if actor.role not in action.roles:
            return deny(f"role_not_permitted:{actor.role.value}:{action_name}")
        # Being cross-tenant is the operator's defining property, so no tenant
        # comparison applies. Every such access is audited instead (FR-M0-032).
        return _apply_policies(action, actor, resource)

    # 🔒 Non-operators are confined to their own tenant, ahead of any policy, so
    # a module's predicate is never the only thing between two tenants.
    if resource is not None:
        resource_tenant = resource.tenant_id
        if resource_tenant is not None and resource_tenant != actor.tenant_id:
            return deny(f"cross_tenant:{action_name}")

    if actor.role not in action.roles:
        return deny(f"role_not_permitted:{actor.role.value}:{action_name}")

    return _apply_policies(action, actor, resource)


def _apply_policies(action: Action, actor: Actor, resource: Resource | None) -> Decision:
    """Run every policy. All must allow; the first denial wins and is returned
    verbatim so the audit log records which rule refused."""
    for policy in action.policies:
        decision = policy(actor, resource)
        if not decision.allowed:
            return decision
    return allow(action.name)


# ─── Reusable policies ───────────────────────────────────────────────────
#
# Generic enough to belong to the kernel: they reason about actors, tenants and
# ownership — concepts the kernel already owns — and mention no domain entity.
# A module composes these with its own predicates.


def owner_or_assigned(actor: Actor, resource: Resource | None) -> Decision:
    """Allow a tenant owner anything in their tenant; a practitioner only what
    is assigned to them (FR-M0-017).

    🔒 The unassigned case is a *denial*, not an empty result. A practitioner
    reaching for a colleague's client is either a bug or an attempt; both should
    be visible in the audit log rather than silently returning nothing.

    🔒 No operator branch. An operator that reaches this policy has already been
    confined to non-PII scopes by :func:`can`; granting them "owner" rights here
    would quietly undo that.
    """
    if actor.role is UserRole.OWNER:
        return allow("tenant_owner")

    if resource is None:
        # Nothing to own yet — a create-style action. The role gate already
        # decided, and ownership will be established by the write itself.
        return allow("no_resource_yet")

    owner_id = resource.owner_user_id
    if owner_id is None:
        return allow("resource_not_individually_owned")

    if owner_id == actor.subject_id:
        return allow("assigned_practitioner")

    return deny("not_assigned_to_actor")


def read_only(actor: Actor, _resource: Resource | None) -> Decision:
    """Refuse a mutation to an actor whose access is read-only.

    🔒 Platform operators are read-only by default (FR-M0-016). Write access for
    an operator is a deliberate, separately registered action — never a side
    effect of holding an operator token.
    """
    if actor.is_operator:
        return deny("operator_is_read_only")
    return allow()


def self_only(actor: Actor, resource: Resource | None) -> Decision:
    """Restrict a client to their own records (FR-M0-018).

    Used by the client realm, where ``subject_id`` is the client's own id.
    """
    if resource is None:
        return allow("no_resource_yet")
    owner_id = resource.owner_user_id
    if owner_id is not None and owner_id != actor.subject_id:
        return deny("not_own_record")
    return allow()


# ─── Startup validation ──────────────────────────────────────────────────


class UndeclaredActionError(RuntimeError):
    """A route reached startup without a declared authorization action.

    🔒 Raised during application startup, never at request time. The alternative
    — discovering it when a request arrives — means the endpoint was
    unprotected in production until someone happened to call it.
    """


def assert_all_routes_declared(
    declared: Iterable[tuple[str, str | None]],
    *,
    exempt: Iterable[str] = (),
) -> None:
    """🔒 Abort startup if any route lacks a registered action.

    Args:
        declared: ``(route_label, action_name)`` for every route in the
            application. ``None`` means the route declared nothing.
        exempt: Route labels legitimately outside authorization — health checks,
            the OpenAPI schema, and the public authentication surface, which
            must be reachable *before* an actor exists.

    Raises:
        UndeclaredActionError: naming every offending route at once, so the fix
            is one pass rather than one restart per route.
    """
    exempt_set = frozenset(exempt)
    missing: list[str] = []
    unknown: list[str] = []

    for label, action_name in declared:
        if label in exempt_set:
            continue
        if action_name is None:
            missing.append(label)
        elif REGISTRY.get(action_name) is None:
            unknown.append(f"{label} → {action_name}")

    if not missing and not unknown:
        return

    lines = ["Authorization declaration check failed (ADR-05)."]
    if missing:
        lines.append("")
        lines.append("Routes with no declared action:")
        lines.extend(f"  - {label}" for label in sorted(missing))
        lines.append("")
        lines.append(
            "Declare one with `@requires(ACTION)`, or add the route to the exempt "
            "list if it is genuinely public (and say why in a comment)."
        )
    if unknown:
        lines.append("")
        lines.append("Routes declaring an action that was never registered:")
        lines.extend(f"  - {entry}" for entry in sorted(unknown))
        lines.append("")
        lines.append(
            "Call `register_action(...)` in the owning module, and make sure that "
            "module is imported during startup — an unimported registration is "
            "invisible to the registry."
        )

    raise UndeclaredActionError("\n".join(lines))
