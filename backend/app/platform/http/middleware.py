"""HTTP middleware — the front of the request pipeline.

Arch §5.1 steps 1–2. Establishes request context, resolves the acting principal,
and applies security headers for every request, including those that never reach
a route.

🔒 The pipeline is framework-level, not per-endpoint. A developer cannot forget
to establish context, because no endpoint establishes it — this is the
structural answer to V1's *"authentication, routing and permissions became
difficult to maintain"*.

Authentication resolves here rather than in the route class because it needs the
credential and not the route: the actor must be known before routing so that the
access log, error handlers and rate limiting all see the same principal. Steps
3–5 and 9 — tenancy, authorization, audit — need the route's declared action and
therefore live in ``app.platform.http.pipeline``.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.kernel.context import RequestContext, context_scope, new_request_id
from app.platform.http.pipeline import resolve_actor
from app.platform.logging import get_logger

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"

#: Paths excluded from access logging. Health checks are polled continuously by
#: the platform and would otherwise dominate the logs.
_QUIET_PATHS = frozenset({"/api/v1/public/health", "/api/v1/public/health/ready"})


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Establish request context and emit an access log line.

    🔒 Runs first, so every downstream component — including error handlers —
    has a ``request_id`` to correlate on. The identifier is echoed in the
    response header and in every error body (API §5.1), which is how a
    user-reported problem is traced without the logs containing anything
    about the user.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Accept a caller-supplied id for cross-service correlation, but bound
        # its length — an unbounded header value would end up in every log line.
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = incoming[:64] if incoming else new_request_id()

        ctx = RequestContext(
            request_id=request_id,
            # 🔒 Arch §5.1 step 2. Anonymous throughout Slice A — the resolver is
            # the seam Slice B replaces with token verification. Resolved before
            # `context_scope` so the actor is visible to the access log and to
            # every downstream component, not only to the endpoint.
            actor=await resolve_actor(request),
            method=request.method,
            path=request.url.path,
            realm_prefix=_realm_prefix(request.url.path),
        )

        started = time.perf_counter()
        with context_scope(ctx):
            try:
                response = await call_next(request)
            except Exception:
                # Logged by the exception handler; re-raised so it reaches it.
                # Timing is still recorded, because a slow failure is a signal.
                duration_ms = (time.perf_counter() - started) * 1000
                logger.warning(
                    "Request raised",
                    extra={
                        "endpoint": f"{request.method} {request.url.path}",
                        "duration_ms": round(duration_ms, 1),
                    },
                )
                raise

            duration_ms = (time.perf_counter() - started) * 1000
            response.headers[REQUEST_ID_HEADER] = request_id

            if request.url.path not in _QUIET_PATHS:
                logger.info(
                    "Request completed",
                    extra={
                        "endpoint": f"{request.method} {request.url.path}",
                        "status_code": response.status_code,
                        "duration_ms": round(duration_ms, 1),
                    },
                )

            return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply security headers to every response.

    NFR-027 (transport security) and defence against common browser-side
    attacks. Applied centrally so no endpoint can omit them.
    """

    def __init__(self, app: ASGIApp, *, is_production: bool) -> None:
        super().__init__(app)
        self._is_production = is_production

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)

        # No MIME sniffing: an uploaded file must never be reinterpreted as a
        # script by the browser (NFR-036).
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=(), payment=()",
        )

        if self._is_production:
            # NFR-027 — HSTS only in production; it would break local http://.
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )

        return response


def _realm_prefix(path: str) -> str | None:
    """Extract the realm segment from a path.

    🔒 ADR-A01 — realm is a path segment (``/api/v1/{realm}/...``), which is what
    makes realm-specific rate limits, audit rules and middleware declarative
    rather than conditional.
    """
    parts = path.strip("/").split("/")
    # ["api", "v1", "<realm>", ...]
    if len(parts) >= 3 and parts[0] == "api":
        realm = parts[2]
        if realm in {"app", "portal", "admin", "public"}:
            return realm
    return None
