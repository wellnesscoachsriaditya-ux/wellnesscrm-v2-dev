"""Tenancy resolution and realm enforcement — ADR-06 / ADR-A01 / FR-M0-004.

`app.kernel.tenancy` is pure by design: it decides *what* `app.tenant_id` should
be, and `app.platform.db` applies it. That split is what makes this file
possible — the rules that draw a tenant boundary are exercised as a table of
inputs, with no PostgreSQL instance and no session.

⚠️ These tests prove the *decision*. They cannot prove enforcement; that is
`tests/integration/test_tenant_isolation.py` (AC-M0-003), which needs a live
database and is the sprint gate. Both halves are required: a correct scope
applied to an unpoliced table protects nothing, and a policed table handed the
wrong scope protects the wrong tenant.
"""

from __future__ import annotations

import uuid

import pytest

from app.kernel.context import Actor, ActorType, AuthRealm, UserRole
from app.kernel.errors import AuthenticationError, AuthorizationError
from app.kernel.tenancy import (
    ANONYMOUS_SCOPE,
    PUBLIC_PREFIX,
    REALM_BY_PATH_PREFIX,
    assert_realm_permits,
    realm_for_prefix,
    resolve_scope,
)

TENANT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
USER_1 = uuid.UUID("11111111-0000-4000-8000-000000000001")


# ─── Realm <-> path ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("app", AuthRealm.PRACTITIONER),
        ("portal", AuthRealm.CLIENT),
        ("admin", AuthRealm.OPERATOR),
    ],
)
def test_realm_is_derived_from_the_path_segment(prefix: str, expected: AuthRealm) -> None:
    """🔒 ADR-A01 — realm is structural, so realm rules are declarative rather
    than a condition somebody has to remember to write."""
    assert realm_for_prefix(prefix) is expected


@pytest.mark.parametrize("prefix", [None, "public", "unknown", "APP", ""])
def test_unmapped_prefixes_resolve_to_no_realm(prefix: str | None) -> None:
    """Including `APP` — the mapping is case-sensitive, and a path that differs
    in case is not the realm it resembles."""
    assert realm_for_prefix(prefix) is None


def test_every_realm_has_exactly_one_path_prefix() -> None:
    """⚠️ A realm without a prefix is unreachable; two prefixes for one realm
    means the realm boundary has a second, unreviewed door."""
    assert set(REALM_BY_PATH_PREFIX.values()) == set(AuthRealm)
    assert len(REALM_BY_PATH_PREFIX) == len(AuthRealm)


def test_the_public_prefix_is_not_a_realm() -> None:
    """`/public` is the only unauthenticated surface — no actor exists yet, so
    there is nothing to confine."""
    assert PUBLIC_PREFIX not in REALM_BY_PATH_PREFIX


# ─── Scope resolution ────────────────────────────────────────────────────


def test_anonymous_resolves_to_the_named_empty_scope() -> None:
    """One reviewable object rather than an ad-hoc construction at each site."""
    scope = resolve_scope(Actor.anonymous())
    assert scope is ANONYMOUS_SCOPE
    assert scope.tenant_id is None
    assert scope.sees_tenant_rows is False
    assert scope.is_platform_scope is False


def test_practitioner_scope_carries_the_tenant_from_the_token() -> None:
    """🔒 The tenant comes from the verified token, never from a header, query
    parameter or body. There is no path by which a caller-supplied tenant can
    arrive — that is tenant switching wearing a convenience feature's clothes."""
    actor = Actor(
        actor_type=ActorType.PRACTITIONER,
        realm=AuthRealm.PRACTITIONER,
        subject_id=USER_1,
        tenant_id=TENANT_A,
        role=UserRole.PRACTITIONER,
    )
    scope = resolve_scope(actor)
    assert scope.tenant_id == TENANT_A
    assert scope.actor_id == USER_1
    assert scope.actor_role == UserRole.PRACTITIONER.value
    assert scope.is_platform_scope is False
    assert scope.sees_tenant_rows is True


def test_client_scope_is_tenant_bound_like_a_practitioner() -> None:
    """A client belongs to the practitioner's tenant. RLS confines them to it;
    `self_only` in `kernel.authz` confines them further, to their own rows."""
    actor = Actor(
        actor_type=ActorType.CLIENT,
        realm=AuthRealm.CLIENT,
        subject_id=USER_1,
        tenant_id=TENANT_A,
        role=UserRole.CLIENT,
    )
    scope = resolve_scope(actor)
    assert scope.tenant_id == TENANT_A
    assert scope.is_platform_scope is False


@pytest.mark.parametrize("actor_type", [ActorType.PRACTITIONER, ActorType.CLIENT])
def test_a_tenant_bound_actor_without_a_tenant_is_a_401(actor_type: ActorType) -> None:
    """🔒 A stale or malformed token, not a permission problem.

    The distinction matters operationally: 403 sends the user to support, 401
    sends them to a sign-in that actually fixes it.
    """
    actor = Actor(actor_type=actor_type, subject_id=USER_1, tenant_id=None, role=UserRole.CLIENT)
    with pytest.raises(AuthenticationError):
        resolve_scope(actor)


def test_operator_scope_sees_no_tenant_rows() -> None:
    """🔒 FR-M0-016. An operator's tenant is None, so every RLS policy comparing
    `tenant_id = current_tenant_id()` matches nothing. What an operator may
    reach is decided by `DataScope` in `kernel.authz`, never by RLS."""
    actor = Actor(
        actor_type=ActorType.OPERATOR,
        realm=AuthRealm.OPERATOR,
        subject_id=USER_1,
        role=UserRole.PLATFORM_OPERATOR,
    )
    scope = resolve_scope(actor)
    assert scope.tenant_id is None
    assert scope.sees_tenant_rows is False
    assert scope.is_platform_scope is True
    assert scope.actor_id == USER_1


def test_a_system_job_carrying_a_tenant_runs_inside_it() -> None:
    scope = resolve_scope(Actor.system(tenant_id=TENANT_A))
    assert scope.tenant_id == TENANT_A
    assert scope.actor_role == "system"
    assert scope.is_platform_scope is False
    # 🔒 No actor id: "the system did it" is attributable by job, not by user.
    assert scope.actor_id is None


def test_a_platform_wide_job_runs_outside_every_tenant() -> None:
    scope = resolve_scope(Actor.system())
    assert scope.tenant_id is None
    assert scope.is_platform_scope is True


def test_scope_is_frozen() -> None:
    """A scope that could be mutated mid-request is a scope that could be
    widened mid-request."""
    scope = resolve_scope(Actor.system(tenant_id=TENANT_A))
    with pytest.raises((AttributeError, TypeError)):
        scope.tenant_id = None  # type: ignore[misc]


# ─── Realm enforcement (FR-M0-004) ───────────────────────────────────────


def _actor_in(realm: AuthRealm) -> Actor:
    by_realm = {
        AuthRealm.PRACTITIONER: ActorType.PRACTITIONER,
        AuthRealm.CLIENT: ActorType.CLIENT,
        AuthRealm.OPERATOR: ActorType.OPERATOR,
    }
    return Actor(
        actor_type=by_realm[realm],
        realm=realm,
        subject_id=USER_1,
        tenant_id=None if realm is AuthRealm.OPERATOR else TENANT_A,
        role=UserRole.PRACTITIONER,
    )


@pytest.mark.parametrize(("prefix", "realm"), sorted(REALM_BY_PATH_PREFIX.items()))
def test_an_actor_at_their_own_realm_is_permitted(prefix: str, realm: AuthRealm) -> None:
    assert_realm_permits(prefix, _actor_in(realm))


#: 🔒 Every crossing, not a sample. Separate signing keys make a forged
#: cross-realm token impossible; this is the second line, catching a *validly
#: signed* token presented to the wrong surface — a stale mobile client, or a
#: captured token replayed against a more privileged area.
_CROSSINGS = [
    (prefix, realm)
    for prefix, expected in REALM_BY_PATH_PREFIX.items()
    for realm in AuthRealm
    if realm is not expected
]


@pytest.mark.parametrize(("prefix", "realm"), _CROSSINGS, ids=[f"{p}<-{r}" for p, r in _CROSSINGS])
def test_every_cross_realm_presentation_is_refused(prefix: str, realm: AuthRealm) -> None:
    with pytest.raises(AuthorizationError):
        assert_realm_permits(prefix, _actor_in(realm))


def test_the_refusal_names_neither_realm() -> None:
    """🔒 NFR-043. "You are signed in, just to the wrong thing" tells a prober
    which token they hold, so the refusal must not disclose either realm.

    ⚠️ Asserted against the two halves separately, because they say different
    things. `message` states what happened and must stay generic. `action` is
    the next step, and its client-facing wording ("contact your practitioner")
    names a *person*, not the actor's realm — a client reading it learns who to
    ask, not what kind of token they hold.
    """
    with pytest.raises(AuthorizationError) as raised:
        assert_realm_permits("admin", _actor_in(AuthRealm.PRACTITIONER))

    message = raised.value.message.lower()
    for realm_word in ("practitioner", "client", "operator", "admin", "portal"):
        assert realm_word not in message, f"the refusal message discloses {realm_word!r}"

    # The privileged realm is never named anywhere, even in the next step.
    whole = f"{raised.value.message} {raised.value.action}".lower()
    assert "operator" not in whole
    assert "admin" not in whole


def test_an_anonymous_actor_at_a_realm_surface_is_a_401() -> None:
    """Not a 403: there is nothing to forbid yet, and the fix is signing in."""
    with pytest.raises(AuthenticationError):
        assert_realm_permits("app", Actor.anonymous())


@pytest.mark.parametrize("prefix", [None, PUBLIC_PREFIX])
def test_the_public_surface_admits_anonymous_callers(prefix: str | None) -> None:
    assert_realm_permits(prefix, Actor.anonymous())


def test_an_unrecognised_prefix_is_not_enforced_here() -> None:
    """⚠️ Deliberate, and safe only because of what follows it: a path outside
    the three realms has no realm rule to apply, and `kernel.authz` denies by
    default regardless. Enforcement is not skipped — it moves."""
    assert_realm_permits("unknown", Actor.anonymous())
