"""Tenancy — resolving which tenant a request acts within.

🔒 ADR-06 / DB §2.3. Isolation is enforced by PostgreSQL Row Level Security
against a transaction-scoped ``app.tenant_id``. This module decides *what that
value should be*; ``app.platform.db`` applies it.

The split is deliberate and load-bearing:

* **Policy here, mechanism there.** This module is pure — no database, no
  session, no I/O — so the rules that decide a tenant boundary can be tested
  exhaustively as a table of inputs, without a PostgreSQL instance. The rules
  are the part that has to be right.
* 🔒 **R5** (``tools/check_boundaries.py``): the kernel must not import
  ``platform``. A ``kernel`` that reached for a session would make the kernel
  unreusable and the boundary check would fail the build.

⚠️ Resolution failing *closed* is the whole design. An unresolved tenant yields
an empty ``app.tenant_id``, and every RLS policy compares ``tenant_id =
current_tenant_id()`` against NULL — which matches nothing. A bug in this module
produces "no data", never "another tenant's data".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.kernel.context import Actor, ActorType, AuthRealm
from app.kernel.errors import AuthenticationError, AuthorizationError

# ─── Realm ↔ path ────────────────────────────────────────────────────────

#: 🔒 ADR-A01 — realm is a path segment, so realm rules are declarative rather
#: than conditional. Each realm is served by its own token signing key
#: (AC-M11-005), which means a cross-realm token fails signature verification
#: before it ever reaches this check. This mapping is the second line: it
#: catches a *validly signed* token presented to the wrong surface.
REALM_BY_PATH_PREFIX: dict[str, AuthRealm] = {
    "app": AuthRealm.PRACTITIONER,
    "portal": AuthRealm.CLIENT,
    "admin": AuthRealm.OPERATOR,
}

#: The only unauthenticated surface: health, sign-in, magic-link redemption,
#: public enquiry forms, provider webhooks. No actor exists yet by definition.
PUBLIC_PREFIX = "public"


def realm_for_prefix(prefix: str | None) -> AuthRealm | None:
    """Map a path prefix to its realm. ``None`` for ``/public`` and unknown."""
    if prefix is None:
        return None
    return REALM_BY_PATH_PREFIX.get(prefix)


# ─── The resolved scope ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TenantScope:
    """The tenant boundary for one transaction.

    Passed to ``app.platform.db.transaction()``, which turns it into the
    ``SET LOCAL`` variables that RLS policies read. Frozen, because a scope that
    could be mutated mid-request is a scope that could be widened mid-request.
    """

    #: The tenant whose rows are visible. ``None`` means *no tenant rows at
    #: all* — either an anonymous request or platform-level work.
    tenant_id: uuid.UUID | None
    #: The acting subject, exposed to policies and written to audit rows.
    actor_id: uuid.UUID | None
    #: The actor's role, as a plain string for the session variable.
    actor_role: str | None
    #: 🔒 True for operators and system jobs: work that legitimately sits
    #: outside any single tenant. RLS is not the control in this case —
    #: authorization is (``DataScope`` in ``kernel.authz``), and every access is
    #: audited. Never set from a request-supplied value.
    is_platform_scope: bool = False

    @property
    def sees_tenant_rows(self) -> bool:
        return self.tenant_id is not None


#: The scope for a request with no established identity. Explicitly named
#: rather than constructed ad hoc, so "anonymous" is one reviewable object.
ANONYMOUS_SCOPE = TenantScope(tenant_id=None, actor_id=None, actor_role=None)


def resolve_scope(actor: Actor) -> TenantScope:
    """Derive the tenant scope for an actor.

    🔒 The tenant comes from the **verified token**, never from a header, query
    parameter or request body. A caller-supplied tenant identifier is a
    tenant-switching vulnerability wearing a convenience feature's clothes, and
    there is no path in this codebase by which one can arrive.

    Raises:
        AuthenticationError: If a tenant-bound actor carries no tenant. This is
            a malformed or stale token, not a permission problem — the correct
            response is 401 and re-authentication, not 403.
    """
    match actor.actor_type:
        case ActorType.ANONYMOUS:
            return ANONYMOUS_SCOPE

        case ActorType.OPERATOR:
            # 🔒 No tenant. An operator sees no tenant rows through RLS; what
            # they may reach is decided by `kernel.authz` and confined to
            # non-PII scopes (FR-M0-016, FR-M0-032).
            return TenantScope(
                tenant_id=None,
                actor_id=actor.subject_id,
                actor_role=actor.role.value if actor.role else None,
                is_platform_scope=True,
            )

        case ActorType.SYSTEM:
            # Background workers and migrations. A job carrying a tenant runs
            # inside it; a platform-wide job runs outside every tenant.
            return TenantScope(
                tenant_id=actor.tenant_id,
                actor_id=None,
                actor_role="system",
                is_platform_scope=actor.tenant_id is None,
            )

        case ActorType.PRACTITIONER | ActorType.CLIENT:
            if actor.tenant_id is None:
                raise AuthenticationError(
                    message="Your session is no longer valid.",
                    action="Sign in again to continue.",
                )
            return TenantScope(
                tenant_id=actor.tenant_id,
                actor_id=actor.subject_id,
                actor_role=actor.role.value if actor.role else None,
            )

    # Unreachable while ActorType is exhaustive; kept so that adding a member
    # without handling it here fails closed rather than falling through.
    raise AuthenticationError(
        message="Your session is no longer valid.",
        action="Sign in again to continue.",
    )


# ─── Realm enforcement ───────────────────────────────────────────────────


def assert_realm_permits(realm_prefix: str | None, actor: Actor) -> None:
    """🔒 Refuse an actor presenting at the wrong realm's surface (FR-M0-004).

    A client token must not work on ``/app``, and a practitioner token must not
    work on ``/admin``. Separate signing keys make forgery impossible; this
    makes *misuse of a genuine token* impossible too — the case that arises from
    a mobile client with a stale realm, or from an attacker replaying a captured
    token against a more privileged surface.

    Args:
        realm_prefix: The path's realm segment, or ``None`` for ``/public`` and
            unrecognised paths.
        actor: The actor resolved from the token.

    Raises:
        AuthorizationError: On mismatch. Auditable (FR-M0-033), because a
            cross-realm attempt is a signal worth keeping.
    """
    if realm_prefix is None or realm_prefix == PUBLIC_PREFIX:
        return

    expected = realm_for_prefix(realm_prefix)
    if expected is None:
        return

    if not actor.is_authenticated:
        raise AuthenticationError(
            message="You need to sign in to view this.",
            action="Sign in and try again.",
        )

    if actor.realm is not expected:
        # 🔒 The message names neither the actor's realm nor the expected one.
        # Confirming "you are signed in, just to the wrong thing" tells a prober
        # which token they hold (NFR-043).
        raise AuthorizationError(
            message="You do not have access to this area.",
            action="Sign in with the correct account, or contact your practitioner.",
        )
