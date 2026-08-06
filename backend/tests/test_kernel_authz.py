"""The authorization decision point — ADR-05 / FR-M0-015 / FR-M0-019.

🔒 **The sprint's second gate, after AC-M0-003.** `can()` is the only place a
permission is decided, so a defect here is a defect everywhere. These tests run
without a database on purpose: `Resource` is a structural protocol, so the
matrix is a table over fakes (Arch §5.4) rather than an integration suite, which
is what makes exhaustive coverage of 4 roles x ownership x realm affordable.

Three properties get the most attention, because each replaces a V1 failure:

* **Deny by default** — an unregistered action is refused. V1's checks were
  opt-in, so a forgotten one meant open access.
* **Ownership is part of the decision** — "may read *this* client", never "may
  read clients" followed by a filter someone might omit.
* 🔒 **The operator boundary** — running the platform confers no right to read a
  tenant's PII, enforced at registration *and* at decision time.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from app.kernel.authz import (
    OPERATOR_ELIGIBLE_SCOPES,
    REGISTRY,
    Action,
    DataScope,
    Decision,
    UndeclaredActionError,
    allow,
    assert_all_routes_declared,
    can,
    deny,
    owner_or_assigned,
    read_only,
    register_action,
    self_only,
)
from app.kernel.context import Actor, ActorType, AuthRealm, UserRole

TENANT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
TENANT_B = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
USER_1 = uuid.UUID("11111111-0000-4000-8000-000000000001")
USER_2 = uuid.UUID("22222222-0000-4000-8000-000000000002")


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    """Isolate the process-wide registry per test.

    ⚠️ `REGISTRY` is deliberately global — it is populated at import time and
    read by the startup check. That makes it shared mutable state for tests, so
    every test gets a snapshot restored afterwards. Without this, a registration
    in one test would widen or collide with another's, and the failure would
    depend on collection order.
    """
    saved = dict(REGISTRY._actions)
    REGISTRY.clear()
    try:
        yield
    finally:
        REGISTRY.clear()
        REGISTRY._actions.update(saved)


# ─── Fakes ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FakeResource:
    """A stand-in satisfying the `Resource` protocol structurally.

    No ORM model, no import of anything domain-shaped: the point of the protocol
    is that a policy can be tested against the two attributes it actually reads.
    """

    tenant_id: uuid.UUID | None
    owner_user_id: uuid.UUID | None


def practitioner(
    *, tenant: uuid.UUID = TENANT_A, subject: uuid.UUID = USER_1, role: UserRole | None = None
) -> Actor:
    return Actor(
        actor_type=ActorType.PRACTITIONER,
        realm=AuthRealm.PRACTITIONER,
        subject_id=subject,
        tenant_id=tenant,
        role=role or UserRole.PRACTITIONER,
    )


def owner(*, tenant: uuid.UUID = TENANT_A, subject: uuid.UUID = USER_1) -> Actor:
    return practitioner(tenant=tenant, subject=subject, role=UserRole.OWNER)


def client_actor(*, tenant: uuid.UUID = TENANT_A, subject: uuid.UUID = USER_1) -> Actor:
    return Actor(
        actor_type=ActorType.CLIENT,
        realm=AuthRealm.CLIENT,
        subject_id=subject,
        tenant_id=tenant,
        role=UserRole.CLIENT,
    )


def operator(*, subject: uuid.UUID = USER_2) -> Actor:
    """🔒 No tenant, by construction. An operator that carried one would be a
    tenant user with extra rights, which is the arrangement DataScope exists to
    prevent."""
    return Actor(
        actor_type=ActorType.OPERATOR,
        realm=AuthRealm.OPERATOR,
        subject_id=subject,
        tenant_id=None,
        role=UserRole.PLATFORM_OPERATOR,
    )


# ─── Decision ────────────────────────────────────────────────────────────


def test_decision_is_truthy_when_allowed() -> None:
    """`if can(...)` must read correctly — the idiom every caller will use."""
    assert bool(allow("because")) is True
    assert bool(deny("because")) is False


def test_denial_without_a_reason_is_refused() -> None:
    """🔒 An unexplained denial is unauditable, and "why was I refused?" is the
    first question asked about every 403."""
    with pytest.raises(ValueError, match="must carry a reason"):
        deny("")


# ─── Registration ────────────────────────────────────────────────────────


def test_action_name_must_be_resource_qualified() -> None:
    """An unqualified verb collides across modules — `read` owned by whom?"""
    with pytest.raises(ValueError, match="must be '<resource>.<verb>'"):
        Action(name="read", roles=frozenset())


def test_registering_the_same_action_twice_is_tolerated() -> None:
    """A module imported under two names is a packaging accident, not a threat."""
    first = register_action("thing.read", roles={UserRole.OWNER}, is_read=True)
    second = register_action("thing.read", roles={UserRole.OWNER}, is_read=True)
    assert first == second


def test_conflicting_registration_is_refused() -> None:
    """⚠️ Silent overwrite would let a later import widen an earlier module's
    permissions invisibly — a hole no test would name."""
    register_action("thing.read", roles={UserRole.OWNER}, is_read=True)
    with pytest.raises(ValueError, match="already registered with different rules"):
        register_action("thing.read", roles={UserRole.OWNER, UserRole.PRACTITIONER}, is_read=True)


@pytest.mark.parametrize(
    "scope",
    [DataScope.PLATFORM, DataScope.TENANT_METADATA, DataScope.AGGREGATE],
)
def test_operator_access_is_permitted_on_non_pii_scopes(scope: DataScope) -> None:
    action = register_action(
        "tenant.read",
        roles={UserRole.PLATFORM_OPERATOR},
        data_scope=scope,
        operator_access=True,
        is_read=True,
    )
    assert action.data_scope is scope


def test_operator_access_to_tenant_pii_fails_at_import_time() -> None:
    """🔒 The operator boundary breaks the build, not a request.

    A 403 at runtime is a boundary nobody notices until someone probes it; a
    refused import is a boundary that cannot ship.
    """
    with pytest.raises(ValueError, match="must not reach tenant PII"):
        register_action(
            "client.read",
            roles={UserRole.PLATFORM_OPERATOR},
            data_scope=DataScope.TENANT_PII,
            operator_access=True,
            is_read=True,
        )


def test_default_data_scope_is_the_most_restrictive() -> None:
    """⚠️ An author who did not think about classification gets TENANT_PII —
    the treatment that fails safe rather than the one that fails open."""
    action = register_action("thing.write", roles={UserRole.OWNER})
    assert action.data_scope is DataScope.TENANT_PII
    assert action.operator_access is False


def test_operator_visible_enumerates_the_platform_surface() -> None:
    """The reviewable answer to "what can the SaaS owner see?" — one call."""
    register_action("client.update", roles={UserRole.OWNER})
    register_action(
        "tenant.read",
        roles={UserRole.PLATFORM_OPERATOR},
        data_scope=DataScope.PLATFORM,
        operator_access=True,
        is_read=True,
    )
    assert REGISTRY.operator_visible() == {"tenant.read"}


def test_tenant_pii_is_not_operator_eligible() -> None:
    """Pinned rather than derived by exclusion: adding a scope should require a
    decision about operator access, not silently inherit one."""
    assert DataScope.TENANT_PII not in OPERATOR_ELIGIBLE_SCOPES


# ─── can(): the gates, in evaluation order ───────────────────────────────


def test_unregistered_action_is_denied() -> None:
    """🔒 FR-M0-019. A forgotten registration means no access, not open access."""
    decision = can(owner(), "never.registered")
    assert not decision
    assert decision.reason == "action_not_registered:never.registered"


def test_anonymous_actor_is_denied() -> None:
    register_action("thing.read", roles={UserRole.OWNER}, is_read=True)
    decision = can(Actor.anonymous(), "thing.read")
    assert not decision
    assert decision.reason == "unauthenticated:thing.read"


def test_authenticated_actor_without_a_role_is_denied() -> None:
    """A token that authenticates but carries no role is malformed, and the safe
    reading of a malformed token is refusal."""
    register_action("thing.read", roles={UserRole.OWNER}, is_read=True)
    roleless = Actor(actor_type=ActorType.PRACTITIONER, subject_id=USER_1, tenant_id=TENANT_A)
    decision = can(roleless, "thing.read")
    assert not decision
    assert decision.reason == "no_role:thing.read"


def test_role_not_permitted_is_denied() -> None:
    register_action("thing.delete", roles={UserRole.OWNER})
    decision = can(practitioner(), "thing.delete")
    assert not decision
    assert decision.reason == "role_not_permitted:practitioner:thing.delete"


def test_cross_tenant_resource_is_denied_before_any_policy_runs() -> None:
    """🔒 Defence in depth with RLS (Arch §6.4).

    A resource can reach a policy from a path that never touched the database —
    a cache, a fixture, a job payload — so the tenant comparison cannot be left
    to PostgreSQL alone. It runs ahead of the policies so that a module's own
    predicate is never the only thing between two tenants.
    """

    def would_allow_anything(_actor: Actor, _resource: object) -> Decision:
        return allow("policy_should_not_have_run")

    register_action(
        "thing.read", roles={UserRole.PRACTITIONER}, policies=[would_allow_anything], is_read=True
    )
    foreign = FakeResource(tenant_id=TENANT_B, owner_user_id=USER_1)

    decision = can(practitioner(tenant=TENANT_A), "thing.read", foreign)
    assert not decision
    assert decision.reason == "cross_tenant:thing.read"


def test_tenant_wide_resource_is_not_treated_as_cross_tenant() -> None:
    """`tenant_id is None` means the resource belongs to no tenant — a catalogue
    row (DDR nullable-tenant), not a foreign one."""
    register_action("food.read", roles={UserRole.PRACTITIONER}, is_read=True)
    catalogue = FakeResource(tenant_id=None, owner_user_id=None)
    assert can(practitioner(), "food.read", catalogue)


def test_first_denying_policy_wins_and_its_reason_survives() -> None:
    """The reason is written to the audit log verbatim, so it must identify
    *which* rule refused, not merely that one did."""

    def refuse(_actor: Actor, _resource: object) -> Decision:
        return deny("state_not_permitted")

    def never_reached(_actor: Actor, _resource: object) -> Decision:  # pragma: no cover
        raise AssertionError("evaluation must stop at the first denial")

    register_action("thing.update", roles={UserRole.OWNER}, policies=[refuse, never_reached])
    decision = can(owner(), "thing.update")
    assert not decision
    assert decision.reason == "state_not_permitted"


def test_all_policies_must_allow() -> None:
    register_action(
        "thing.update",
        roles={UserRole.OWNER},
        policies=[lambda _a, _r: allow(), lambda _a, _r: allow()],
    )
    assert can(owner(), "thing.update")


# ─── The operator boundary at decision time ──────────────────────────────


def test_operator_is_denied_an_action_that_reaches_tenant_pii() -> None:
    """🔒 Belt and braces with the registration check.

    `Action` is a plain dataclass, so a hand-built one can be placed in the
    registry without passing through `register_action`. This is the second line
    that keeps that from becoming a PII path.
    """
    REGISTRY.register(
        Action(
            name="client.read",
            roles=frozenset({UserRole.PLATFORM_OPERATOR}),
            data_scope=DataScope.TENANT_PII,
            operator_access=True,
            is_read=True,
        )
    )
    decision = can(operator(), "client.read", FakeResource(TENANT_A, USER_1))
    assert not decision
    assert decision.reason == "operator_denied_pii:client.read"


def test_operator_is_denied_an_action_that_never_declared_operator_access() -> None:
    """Non-PII is necessary but not sufficient — access is opt-in per action."""
    register_action(
        "tenant.count",
        roles={UserRole.OWNER},
        data_scope=DataScope.TENANT_METADATA,
        is_read=True,
    )
    decision = can(operator(), "tenant.count")
    assert not decision
    assert decision.reason == "operator_access_not_declared:tenant.count"


def test_operator_may_read_platform_data_across_tenants() -> None:
    """Being cross-tenant is the operator's defining property, so no tenant
    comparison applies. Every such access is audited instead (FR-M0-032) — see
    `test_kernel_audit.py::test_operator_reads_of_tenant_data_are_audited`."""
    register_action(
        "subscription.read",
        roles={UserRole.PLATFORM_OPERATOR},
        data_scope=DataScope.PLATFORM,
        operator_access=True,
        is_read=True,
    )
    foreign = FakeResource(tenant_id=TENANT_B, owner_user_id=USER_1)
    assert can(operator(), "subscription.read", foreign)


def test_read_only_policy_refuses_an_operator_mutation() -> None:
    """🔒 FR-M0-016. Write access for an operator is a separately registered
    action, never a side effect of holding an operator token."""
    register_action(
        "tenant.suspend",
        roles={UserRole.PLATFORM_OPERATOR},
        data_scope=DataScope.PLATFORM,
        operator_access=True,
        policies=[read_only],
    )
    decision = can(operator(), "tenant.suspend")
    assert not decision
    assert decision.reason == "operator_is_read_only"


# ─── The ownership matrix (FR-M0-017) ────────────────────────────────────

#: 🔒 The plan's "4 roles x ownership" table, made literal. Each row is
#: (label, actor, resource, expected). Read it as the permission surface: if a
#: future change to `owner_or_assigned` is correct, exactly one row moves.
_OWNERSHIP_MATRIX = [
    ("owner reaches an unowned resource", owner(), FakeResource(TENANT_A, None), True),
    ("owner reaches a colleague's resource", owner(), FakeResource(TENANT_A, USER_2), True),
    ("owner creates", owner(), None, True),
    (
        "practitioner reaches their own",
        practitioner(subject=USER_1),
        FakeResource(TENANT_A, USER_1),
        True,
    ),
    (
        "practitioner reaches a colleague's",
        practitioner(subject=USER_1),
        FakeResource(TENANT_A, USER_2),
        False,
    ),
    (
        "practitioner reaches a tenant-wide resource",
        practitioner(subject=USER_1),
        FakeResource(TENANT_A, None),
        True,
    ),
    ("practitioner creates", practitioner(), None, True),
]


@pytest.mark.parametrize(
    ("label", "actor", "resource", "expected"),
    _OWNERSHIP_MATRIX,
    ids=[row[0] for row in _OWNERSHIP_MATRIX],
)
def test_owner_or_assigned_matrix(
    label: str, actor: Actor, resource: FakeResource | None, expected: bool
) -> None:
    assert bool(owner_or_assigned(actor, resource)) is expected, label


def test_unassigned_access_is_a_denial_not_an_empty_result() -> None:
    """🔒 A practitioner reaching for a colleague's client is either a bug or an
    attempt. Both belong in the audit log; returning nothing hides both."""
    decision = owner_or_assigned(practitioner(subject=USER_1), FakeResource(TENANT_A, USER_2))
    assert not decision
    assert decision.reason == "not_assigned_to_actor"


def test_owner_or_assigned_grants_an_operator_nothing_extra() -> None:
    """🔒 No operator branch, deliberately. An operator reaching this policy has
    already been confined to non-PII scopes by `can()`; treating them as an
    owner here would quietly undo that.

    The operator is judged as any other non-owner subject: the resource belongs
    to someone else, so it is refused for that reason and no other.
    """
    decision = owner_or_assigned(operator(subject=USER_2), FakeResource(TENANT_A, USER_1))
    assert not decision
    assert decision.reason == "not_assigned_to_actor"


# ─── The client realm (FR-M0-018) ────────────────────────────────────────


def test_self_only_confines_a_client_to_their_own_records() -> None:
    assert self_only(client_actor(subject=USER_1), FakeResource(TENANT_A, USER_1))
    refused = self_only(client_actor(subject=USER_1), FakeResource(TENANT_A, USER_2))
    assert not refused
    assert refused.reason == "not_own_record"


def test_self_only_allows_a_create() -> None:
    assert self_only(client_actor(), None)


# ─── Startup validation ──────────────────────────────────────────────────


def test_startup_passes_when_every_route_declares_a_registered_action() -> None:
    register_action("thing.read", roles={UserRole.OWNER}, is_read=True)
    assert_all_routes_declared([("GET /app/things", "thing.read")])


def test_startup_fails_on_a_route_with_no_declared_action() -> None:
    """🔒 A decorator can be forgotten; a process that refuses to boot cannot."""
    with pytest.raises(UndeclaredActionError, match="GET /app/things"):
        assert_all_routes_declared([("GET /app/things", None)])


def test_startup_fails_on_a_route_declaring_an_unregistered_action() -> None:
    """The likeliest cause is a module that was never imported, so the message
    says so rather than only naming the action."""
    with pytest.raises(UndeclaredActionError, match="thing.read"):
        assert_all_routes_declared([("GET /app/things", "thing.read")])


def test_startup_reports_every_offending_route_at_once() -> None:
    """One pass to fix, rather than one restart per route."""
    with pytest.raises(UndeclaredActionError) as raised:
        assert_all_routes_declared([("GET /a", None), ("GET /b", None), ("GET /c", "nope.act")])
    message = str(raised.value)
    assert "GET /a" in message
    assert "GET /b" in message
    assert "GET /c" in message


def test_exempt_routes_are_skipped() -> None:
    """The public surface must be reachable *before* an actor exists — health,
    the OpenAPI schema, sign-in, magic-link redemption."""
    assert_all_routes_declared(
        [("GET /health", None), ("POST /public/auth/login", None)],
        exempt=["GET /health", "POST /public/auth/login"],
    )
