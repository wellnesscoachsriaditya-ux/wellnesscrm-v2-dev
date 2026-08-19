"""Authentication — Arch §5.1 step 2, the seam Slice A left open.

🔒 Resolves the bearer token on an incoming request into an :class:`Actor`. This
is the *only* place a credential becomes an identity; everything downstream —
tenancy, authorization, audit — consumes the actor and never the token.

Three properties, each replacing something a hand-rolled check would get wrong:

1. 🔒 **The realm comes from the path, and the path selects the key** (ADR-A01).
   A practitioner token presented at `/portal` is verified with the *client* key
   and fails on the signature. Realm confusion is not a comparison that can be
   inverted; it is arithmetic that cannot succeed.
2. 🔒 **The session is checked against the database on every request.** A JWT
   alone would keep working until it expired, so logout and reuse-revocation
   would take up to fifteen minutes to bite. "Revoked, but not for another
   quarter of an hour" is not revocation.
3. 🔒 **An unusable token yields an anonymous actor, never an exception.**
   Rejection is authorization's job — deny-by-default already refuses anonymous
   callers on protected routes, and raising here would bypass the audit entry
   the pipeline writes for the denial.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request

from app.kernel.context import Actor, AuthRealm
from app.kernel.identity import TokenRejectedError
from app.kernel.tenancy import realm_for_prefix
from app.platform.db import transaction
from app.platform.identity import repository as repo
from app.platform.identity.tokens import utcnow, verify_for_realm
from app.platform.logging import get_logger

logger = get_logger(__name__)

_BEARER = "bearer"

#: Answers "is this session still live?". A seam for the same reason the
#: transaction provider is one: without it, the only tests covering the step that
#: makes logout and revocation immediate would be the PostgreSQL-gated ones,
#: which are skipped until the D4 gate closes.
SessionValidator = Callable[[uuid.UUID], Awaitable[bool]]


async def _database_session_validator(session_id: uuid.UUID) -> bool:
    """Check the session row.

    ⚠️ Opens its own short transaction rather than using the request's. The
    request transaction is opened by the pipeline *after* tenant scope is
    resolved, and tenant scope comes from the actor this function is producing —
    so the request's transaction does not exist yet. A read-only liveness check
    is the one query that legitimately precedes it.

    Fails **closed**: if the database is unreachable, the actor is anonymous and
    protected routes refuse. The alternative — trusting the token when the
    session store is down — turns a database outage into an authentication
    bypass.
    """
    try:
        async with transaction() as db:
            return await repo.is_session_live(db, session_id=session_id, now=utcnow())
    except Exception:
        logger.exception("Session liveness check failed — treating request as anonymous")
        return False


_session_validator: SessionValidator = _database_session_validator


def configure_session_validator(validator: SessionValidator) -> None:
    """Install the session-liveness check."""
    global _session_validator
    _session_validator = validator


def get_session_validator() -> SessionValidator:
    return _session_validator


def bearer_token(request: Request) -> str | None:
    """Extract the bearer credential.

    ⚠️ Header only — never a query parameter. A token in a URL lands in access
    logs, proxy logs, browser history and `Referer` headers, and a magic-link
    token in a query string is precisely how those leak.
    """
    header = request.headers.get("Authorization", "")
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != _BEARER or not credential.strip():
        return None
    return credential.strip()


def _realm_for(request: Request) -> AuthRealm | None:
    """The realm this path belongs to, or ``None`` for `/public` and unknown."""
    parts = request.url.path.strip("/").split("/")
    if len(parts) >= 3 and parts[0] == "api":
        return realm_for_prefix(parts[2])
    return None


async def resolve_actor(request: Request) -> Actor:
    """🔒 Resolve the request's actor. Anonymous when no usable credential exists.

    Called by ``RequestContextMiddleware`` before routing, so the actor is
    available to the access log, the error handlers and the pipeline alike.
    """
    realm = _realm_for(request)
    if realm is None:
        # `/public` and unrecognised paths. No realm means no key, and a token
        # presented here has nothing to verify against.
        return Actor.anonymous()

    token = bearer_token(request)
    if token is None:
        return Actor.anonymous()

    try:
        claims = verify_for_realm(token, realm=realm)
    except TokenRejectedError as rejected:
        # 🔒 The reason is logged, never returned. The pipeline will deny the
        # request as anonymous and audit that denial (FR-M0-033).
        logger.info(
            "Bearer token rejected",
            extra={"reason": rejected.reason, "realm": realm.value},
        )
        return Actor.anonymous()

    if not await get_session_validator()(claims.session_id):
        logger.info(
            "Token names a session that is no longer live",
            extra={"realm": realm.value},
        )
        return Actor.anonymous()

    return claims.to_actor()
