"""The request pipeline — Arch §5.1, steps 3, 4, 5, 9 and 11.

🔒 **There is no bypass.** Tenant resolution, transaction, authorization and
audit are properties of the *route class*, not lines an endpoint author
remembers to write. This is the structural answer to V1's "authentication,
routing and permissions became difficult to maintain": there is nothing to
remember, because there is nothing to write.

One request, in order::

    2. Authentication      → app.platform.http.middleware (the seam below)
    3. Tenant resolution   → kernel.tenancy: realm check, then TenantScope
    4. Transaction begin   → SET LOCAL app.tenant_id, read by every RLS policy
    5. Authorization       → kernel.authz.can(), deny by default
    8. Module service      → the endpoint
    9. Audit               → allow in-transaction, deny/fail out-of-band
    11. Commit             → or roll back, taking the audit row with it

Two seams exist because two things are not built yet, and both are named rather
than implied:

* :func:`resolve_actor` — 🔒 **Slice B replaces this.** Every request is
  currently anonymous. It lives here, called from the middleware, because
  authentication needs the token and not the route; when identity lands it
  becomes a token verification and nothing else in the pipeline moves.
* :func:`configure_transaction_provider` — lets the pipeline be exercised
  without PostgreSQL. The alternative is that the only tests proving the
  pipeline works are the ones skipped until the database gate closes, which
  would leave the most security-relevant code in the sprint unverified.

⚠️ **What this does not do.** Step 5 here is the *coarse* decision: is the action
registered, is the actor authenticated, in the right realm, holding a permitted
role. A decision that depends on the specific row — "may read *this* client" —
needs the row, and the row is loaded by the service. Those call :func:`authorize`
once they have it. The pipeline cannot make that call on their behalf, and
pretending otherwise would be worse than saying so.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.audit import (
    AuditEntry,
    AuditOutcome,
    build_denial_entry,
    build_entry,
    hash_ip,
    should_audit,
)
from app.kernel.authz import (
    Action,
    Decision,
    Resource,
    assert_all_routes_declared,
    can,
    deny,
)
from app.kernel.context import Actor, get_context
from app.kernel.errors import (
    AppError,
    AuthenticationError,
    AuthorizationError,
    NotFoundError,
)
from app.kernel.tenancy import TenantScope, assert_realm_permits, resolve_scope
from app.platform.audit import record_out_of_band, write_entry
from app.platform.config import get_settings
from app.platform.db import transaction
from app.platform.http.authz import EXEMPT_PATHS, declared_action, iter_api_routes
from app.platform.logging import get_logger

logger = get_logger(__name__)

#: The action name recorded when a route reaches the pipeline without declaring
#: one. Startup validation makes this unreachable in a running application; it
#: exists so that if it ever *is* reached, `can()` denies it and the attempt is
#: auditable under a name rather than under `None`.
UNDECLARED_ACTION = "route.undeclared"


# ─── Step 2 seam — authentication (Slice B) ──────────────────────────────

#: Resolves the actor for a request. Returns an :class:`Actor`; never raises for
#: an absent credential — an anonymous actor is a valid answer, and refusing
#: anonymity is the realm check's job, not authentication's.
ActorResolver = Callable[[Request], Awaitable[Actor]]


async def _anonymous_resolver(_request: Request) -> Actor:
    """🔒 Slice A placeholder: every request is anonymous.

    Not a stub that returns something convenient — anonymous is the *correct*
    answer while no credential can be verified, and it means the pipeline
    currently denies every non-exempt route. That is the intended behaviour of a
    deny-by-default system with no identity: closed, not open.
    """
    return Actor.anonymous()


_actor_resolver: ActorResolver = _anonymous_resolver


def configure_actor_resolver(resolver: ActorResolver) -> None:
    """Install the actor resolver. 🔒 Slice B calls this with token verification."""
    global _actor_resolver
    _actor_resolver = resolver


async def resolve_actor(request: Request) -> Actor:
    """Resolve the acting principal — Arch §5.1 step 2."""
    return await _actor_resolver(request)


# ─── Step 4 seam — the transaction ───────────────────────────────────────

#: Opens the request's transaction with tenant scope applied. Yields ``None``
#: when no database is configured, which the audit path treats as "write out of
#: band" rather than "skip the audit".
TransactionProvider = Callable[[TenantScope], AbstractAsyncContextManager[AsyncSession | None]]


@asynccontextmanager
async def _database_transaction(scope: TenantScope) -> Any:
    """The real provider: one transaction per request, tenant scope applied.

    🔒 ADR-04 — the transaction is owned here, at the HTTP layer. Services never
    commit, which is what makes a multi-step operation atomic without any
    service knowing another exists.
    """
    async with transaction(
        tenant_id=scope.tenant_id,
        actor_id=scope.actor_id,
        actor_role=scope.actor_role,
    ) as session:
        yield session


_transaction_provider: TransactionProvider = _database_transaction


def configure_transaction_provider(provider: TransactionProvider) -> None:
    """Install the transaction provider. Used by tests running without PostgreSQL."""
    global _transaction_provider
    _transaction_provider = provider


def get_transaction_provider() -> TransactionProvider:
    return _transaction_provider


# ─── Request-scoped state ────────────────────────────────────────────────

_STATE_SESSION = "wellness_db_session"
_STATE_ACTION = "wellness_authz_action"
_STATE_RESOURCE_ID = "wellness_audit_resource_id"
_STATE_CHANGED_FIELDS = "wellness_audit_changed_fields"
_STATE_METADATA = "wellness_audit_metadata"


def get_session(request: Request) -> AsyncSession:
    """FastAPI dependency: the request's transaction.

    Raises:
        RuntimeError: If the route is not on :class:`AuthorizedRoute`, or no
            database is configured. Both are programming or deployment errors —
            failing loudly beats handing back a session that commits nothing.
    """
    session = getattr(request.state, _STATE_SESSION, None)
    if not isinstance(session, AsyncSession):
        raise RuntimeError(
            "No database session on this request. A route needing one must be "
            "registered on `AuthorizedRoute` (see `realm_router`), which opens "
            "the transaction the whole request shares."
        )
    return session


def record_audit(
    request: Request,
    *,
    resource_id: uuid.UUID | None = None,
    changed_fields: Iterable[str] = (),
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Enrich the audit entry the pipeline will write.

    The pipeline knows *who*, *what action* and *what outcome* without being
    told. It cannot know which row was touched or which fields changed, so a
    service says so here rather than writing its own audit entry — the entry
    stays framework-written and therefore cannot be forgotten, skipped or shaped.

    🔒 ``changed_fields`` takes field *names* (FR-M0-035). ``kernel.audit``
    rejects anything else rather than trusting the caller.
    """
    if resource_id is not None:
        setattr(request.state, _STATE_RESOURCE_ID, resource_id)
    if changed_fields:
        existing: tuple[str, ...] = getattr(request.state, _STATE_CHANGED_FIELDS, ())
        setattr(request.state, _STATE_CHANGED_FIELDS, (*existing, *changed_fields))
    if metadata:
        merged = {**getattr(request.state, _STATE_METADATA, {}), **metadata}
        setattr(request.state, _STATE_METADATA, merged)


async def authorize(request: Request, resource: Resource | None) -> None:
    """🔒 The resource-bound authorization decision — Arch §6.3.

    Called by a service once it has loaded the row. Re-enters the same
    :func:`~app.kernel.authz.can` the pipeline used, now with the resource, so
    ownership is part of the decision rather than a filter applied afterwards.

    Raises:
        NotFoundError: If the resource belongs to another tenant. 🔒 Never 403 —
            confirming existence is itself the leak (API §5.4).
        AuthorizationError: If a policy refuses.
    """
    action = _action_on(request)
    action_name = action.name if action is not None else UNDECLARED_ACTION
    actor = get_context().actor

    decision = can(actor, action_name, resource)
    if decision:
        return

    await _record_denial(
        request,
        decision=decision,
        action=action,
        action_name=action_name,
        resource_id=_resource_id_of(resource),
    )
    raise _error_for(decision, actor)


# ─── The route class ─────────────────────────────────────────────────────


class AuthorizedRoute(APIRoute):
    """A route that runs the pipeline. 🔒 The only way an endpoint is reachable.

    Applied per router rather than per endpoint (see :func:`realm_router`), so
    adding an endpoint to a realm cannot omit it. Startup validation refuses to
    boot if a route declaring an action is not on this class — which is the case
    a per-endpoint decorator would silently allow.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        run_endpoint = super().get_route_handler()
        action = declared_action(self.endpoint)
        action_name = action.name if action is not None else UNDECLARED_ACTION
        resource_type = action_name.split(".", 1)[0]

        async def pipeline(request: Request) -> Response:
            context = get_context()
            actor = context.actor
            setattr(request.state, _STATE_ACTION, action)

            # ─ Step 3: tenant resolution ─────────────────────────────────
            # 🔒 Before the transaction opens. An actor at the wrong realm's
            # surface should never reach a database connection at all.
            try:
                assert_realm_permits(context.realm_prefix, actor)
            except AppError:
                await _record_denial(
                    request,
                    decision=deny(f"realm_mismatch:{context.realm_prefix}"),
                    action=action,
                    action_name=action_name,
                    resource_type=resource_type,
                )
                raise

            try:
                scope = resolve_scope(actor)
            except AppError:
                await _record_denial(
                    request,
                    decision=deny("tenant_unresolved"),
                    action=action,
                    action_name=action_name,
                    resource_type=resource_type,
                )
                raise

            # ─ Step 5: authorization ─────────────────────────────────────
            # 🔒 Also before the transaction. A denied request must not consume
            # a connection from a pool sized for legitimate work.
            decision = can(actor, action_name)
            if not decision:
                await _record_denial(
                    request,
                    decision=decision,
                    action=action,
                    action_name=action_name,
                    resource_type=resource_type,
                )
                raise _error_for(decision, actor)

            # ─ Steps 4, 8, 9, 11 ─────────────────────────────────────────
            try:
                async with get_transaction_provider()(scope) as session:
                    setattr(request.state, _STATE_SESSION, session)
                    response = await run_endpoint(request)

                    # 🔒 Written inside the transaction, before the commit. The
                    # record and the change it describes commit together or not
                    # at all — an audit row for a rolled-back change would be
                    # evidence of something that never happened.
                    if should_audit(action, AuditOutcome.ALLOWED, actor):
                        await _write_success(
                            request,
                            session=session,
                            action=action,
                            action_name=action_name,
                            resource_type=resource_type,
                        )
            except Exception as exc:
                # ⚠️ The transaction has rolled back by the time we are here, so
                # this entry cannot join it (FR-M0-033) — a failure that erased
                # its own record is the one you most need.
                await _record_failure(
                    request,
                    exc=exc,
                    action=action,
                    action_name=action_name,
                    resource_type=resource_type,
                )
                raise

            return response

        return pipeline


def realm_router(prefix: str, **kwargs: Any) -> APIRouter:
    """Build a router whose every route runs the pipeline.

    🔒 The attachment point for ADR-05. Realm routers are created here rather
    than with a bare ``APIRouter`` so that enforcement is a property of joining
    the realm, not of remembering a keyword argument per router::

        router = realm_router("/app/clients", tags=["clients"])

    Passing ``route_class`` is refused: a router created by this function that
    silently used a different class would be the exact failure
    :class:`UnenforcedRouteError` exists to catch, arriving through the door
    marked safe.
    """
    if "route_class" in kwargs:
        raise ValueError(
            "realm_router() sets route_class=AuthorizedRoute and does not accept an "
            "override. If a route genuinely sits outside authorization, register it "
            "on a plain APIRouter and add its path to EXEMPT_PATHS, where the "
            "exemption is visible."
        )
    return APIRouter(prefix=prefix, route_class=AuthorizedRoute, **kwargs)


# ─── Startup validation ──────────────────────────────────────────────────


class UnenforcedRouteError(RuntimeError):
    """🔒 A route declares an action but is not on :class:`AuthorizedRoute`.

    The dangerous half of ADR-05's failure space, and the half a declaration
    check alone would miss: the endpoint looks protected in the source, passes
    review, and enforces nothing.
    """


def verify_route_authorization(app: FastAPI) -> None:
    """🔒 Abort startup unless every route is declared *and* enforced.

    Two questions, because passing one and failing the other is worse than
    failing both:

    1. Does every non-exempt route declare a registered action? (kernel's
       :func:`~app.kernel.authz.assert_all_routes_declared`)
    2. Is every such route on the class that actually runs the check?

    Raises:
        UndeclaredActionError: On a missing or unregistered declaration.
        UnenforcedRouteError: On a declared route that bypasses the pipeline.
    """
    declarations: list[tuple[str, str | None]] = []
    exempt_labels: list[str] = []
    unenforced: list[str] = []

    for route, _method, label in iter_api_routes(app):
        if route.path in EXEMPT_PATHS:
            exempt_labels.append(label)
            continue

        action = declared_action(route.endpoint)
        declarations.append((label, action.name if action is not None else None))
        if not isinstance(route, AuthorizedRoute):
            unenforced.append(label)

    assert_all_routes_declared(declarations, exempt=exempt_labels)

    if unenforced:
        raise UnenforcedRouteError(
            "Routes declare an authorization action but do not run the pipeline "
            "(ADR-05):\n"
            + "\n".join(f"  - {label}" for label in sorted(unenforced))
            + "\n\nThe declaration is read by `AuthorizedRoute`. On any other route "
            "class it is an annotation nobody consults, so the endpoint is "
            "unprotected while appearing protected.\n\n"
            "Register the router with `route_class=AuthorizedRoute`."
        )

    logger.info(
        "Route authorization verified",
        extra={"declared": len(declarations), "exempt": len(exempt_labels)},
    )


# ─── Internals ───────────────────────────────────────────────────────────


def _action_on(request: Request) -> Action | None:
    action = getattr(request.state, _STATE_ACTION, None)
    return action if isinstance(action, Action) else None


def _resource_id_of(resource: Resource | None) -> uuid.UUID | None:
    """Best-effort resource identifier for the audit row.

    ``Resource`` is a structural protocol and deliberately does not require an
    ``id``: the kernel must work with whatever a module's model looks like. An
    absent or non-UUID identifier is recorded as ``None`` rather than coerced —
    a wrong id in an audit row is worse than no id.
    """
    candidate = getattr(resource, "id", None)
    return candidate if isinstance(candidate, uuid.UUID) else None


def _client_ip_hash(request: Request) -> str | None:
    """🔒 Hash the caller's IP (NFR-033). The raw value is never stored."""
    client = request.client
    return hash_ip(
        client.host if client else None,
        salt=get_settings().audit_ip_salt.get_secret_value(),
    )


def _error_for(decision: Decision, actor: Actor) -> AppError:
    """Map a denial onto the response the caller should see.

    🔒 The decision's reason is written to the audit log and never returned
    (API §5.4). Three outcomes, each chosen to reveal nothing:

    * **401** when nobody is signed in — the honest answer, and the one that
      makes the client re-authenticate rather than give up.
    * **404** across a tenant boundary — 🔒 a 403 would confirm the resource
      exists, which is the leak itself.
    * **403** otherwise.
    """
    if not actor.is_authenticated:
        return AuthenticationError(
            message="You need to sign in to continue.",
            action="Sign in and try again.",
        )

    if decision.reason.startswith("cross_tenant"):
        return NotFoundError(
            message="That record doesn't exist.",
            action="Check the link, or go back and try again.",
        )

    return AuthorizationError(
        message="You don't have access to this.",
        action="If you think you should, contact the account owner.",
    )


def _entry_extras(request: Request) -> tuple[uuid.UUID | None, tuple[str, ...], dict[str, Any]]:
    """Whatever the endpoint contributed through :func:`record_audit`."""
    resource_id = getattr(request.state, _STATE_RESOURCE_ID, None)
    changed: tuple[str, ...] = getattr(request.state, _STATE_CHANGED_FIELDS, ())
    metadata: dict[str, Any] = dict(getattr(request.state, _STATE_METADATA, {}))
    return (resource_id if isinstance(resource_id, uuid.UUID) else None, changed, metadata)


async def _write_success(
    request: Request,
    *,
    session: AsyncSession | None,
    action: Action | None,
    action_name: str,
    resource_type: str,
) -> None:
    resource_id, changed, metadata = _entry_extras(request)
    entry = build_entry(
        action_name=action_name,
        resource_type=resource_type,
        outcome=AuditOutcome.ALLOWED,
        context=get_context(),
        action=action,
        resource_id=resource_id,
        changed_fields=changed,
        metadata=metadata,
        ip_hash=_client_ip_hash(request),
    )
    if session is None:
        # No database configured. The entry still exists — degrading to the
        # out-of-band sink keeps the record, where skipping it would lose one.
        await record_out_of_band(entry)
        return
    await write_entry(session, entry)


async def _record_denial(
    request: Request,
    *,
    decision: Decision,
    action: Action | None,
    action_name: str,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
) -> None:
    """🔒 Record a refusal (FR-M0-033), out of band.

    There is no transaction to join — the request is about to fail — and a
    refusal recorded inside a rolling-back transaction would erase exactly the
    evidence the log exists to keep.
    """
    entry = build_denial_entry(
        action_name=action_name,
        resource_type=resource_type or action_name.split(".", 1)[0],
        context=get_context(),
        decision=decision,
        action=action,
        resource_id=resource_id or _entry_extras(request)[0],
        ip_hash=_client_ip_hash(request),
    )
    await record_out_of_band(entry)


async def _record_failure(
    request: Request,
    *,
    exc: BaseException,
    action: Action | None,
    action_name: str,
    resource_type: str,
) -> None:
    """Record a request that failed after passing authorization.

    🔒 ``FAILED`` is distinct from ``DENIED``: "it broke" and "you may not" lead
    to different investigations, and collapsing them turns a security signal
    into an error-rate statistic.
    """
    resource_id, changed, metadata = _entry_extras(request)
    # 🔒 The exception's own message is never recorded — it may echo a submitted
    # clinical value. The taxonomy's stable error type is the whole payload.
    metadata["error_type"] = exc.error_type.value if isinstance(exc, AppError) else "internal_error"

    entry: AuditEntry = build_entry(
        action_name=action_name,
        resource_type=resource_type,
        outcome=AuditOutcome.FAILED,
        context=get_context(),
        action=action,
        resource_id=resource_id,
        changed_fields=changed,
        metadata=metadata,
        ip_hash=_client_ip_hash(request),
    )
    await record_out_of_band(entry)
