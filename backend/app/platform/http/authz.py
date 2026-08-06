"""Declaring the authorization action a route performs — Arch §5.1 step 5.

🔒 ADR-05. ``kernel.authz`` decides permissions; this module is how an HTTP route
*says which decision applies to it*. The split matters: the decision logic knows
nothing about FastAPI, and FastAPI knows nothing about roles or policies.

Declaration is an attribute on the endpoint function rather than a wrapper::

    @router.get("/things/{thing_id}")
    @requires(THING_READ)
    async def read_thing(thing_id: UUID) -> ThingResponse:
        ...

⚠️ Deliberately **not** a wrapping decorator. Wrapping would replace the function
FastAPI introspects for its signature, and every parameter, response model and
dependency would have to be re-plumbed through ``functools.wraps``. Setting an
attribute and returning the same object means decorator order cannot matter and
FastAPI sees exactly the function the author wrote.

🔒 A declaration on its own enforces nothing — it is read by
``pipeline.AuthorizedRoute`` at request time and by
``pipeline.verify_route_authorization`` at startup. That startup check is the
load-bearing part: a route that declares nothing, or declares an action nobody
registered, or declares correctly but is not on the enforcing route class,
aborts the process. A decorator can be forgotten; a process that refuses to boot
cannot be.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, TypeVar

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.kernel.authz import Action

#: Where the declared action is stashed on the endpoint function. Name-mangled
#: with a project prefix because it shares a namespace with whatever else
#: decorates an endpoint.
DECLARED_ACTION_ATTR = "__wellness_authz_action__"

#: 🔒 Routes that are legitimately outside authorization, by exact path.
#:
#: Exact paths, never prefixes. A prefix such as ``/api/v1/public`` would exempt
#: every endpoint added to that realm from then on — including, eventually, one
#: that should not have been. Adding a path here is a decision someone makes and
#: a reviewer sees; inheriting an exemption is neither.
#:
#: The two health probes are unauthenticated by design (NFR-086) and disclose
#: nothing about tenants or configuration.
#:
#: 🔒 The authentication surface is exempt because it is what *establishes* an
#: actor — requiring one would make sign-in unreachable. Each path earns it
#: individually:
#:
#: * `register`, `login`, `verify-email`, `password-reset/*` — no actor exists.
#: * `refresh` — the caller's access token has expired by definition; the
#:   refresh token is the credential it presents instead.
#: * `portal/access/*` — the client realm is passwordless (FR-M0-005); the magic
#:   link is the credential.
#:
#: ⚠️ Logout is **not** here. It acts on an existing session, so it is authorized
#: like any other action (`session.end`) and lives under `/app`.
#:
#: These are literals rather than an import from the router, which would be a
#: cycle. `tests/test_http_authz_declaration.py` asserts the two agree, and a
#: public route added without its exemption fails startup — so drift is caught
#: twice over.
EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/public/health",
        "/api/v1/public/health/ready",
        "/api/v1/public/auth/register",
        "/api/v1/public/auth/verify-email",
        "/api/v1/public/auth/login",
        "/api/v1/public/auth/refresh",
        "/api/v1/public/auth/password-reset/request",
        "/api/v1/public/auth/password-reset/confirm",
        "/api/v1/public/portal/access/request",
        "/api/v1/public/portal/access/redeem",
    }
)

#: Methods a router synthesises rather than the author declaring them. Auditing
#: them would report phantom routes nobody wrote.
_SYNTHETIC_METHODS = frozenset({"HEAD", "OPTIONS"})

F = TypeVar("F", bound=Callable[..., Any])


def requires(action: Action) -> Callable[[F], F]:
    """Declare the authorization action this endpoint performs.

    Args:
        action: The registered action, as returned by
            ``kernel.authz.register_action``. 🔒 The :class:`Action` object
            rather than its name — a name is a string that can be misspelled
            into an action that does not exist, and the mistake would surface at
            startup instead of at import.

    Returns:
        The same function, annotated. Not a wrapper.
    """

    def declare(endpoint: F) -> F:
        setattr(endpoint, DECLARED_ACTION_ATTR, action)
        return endpoint

    return declare


def declared_action(endpoint: Callable[..., Any]) -> Action | None:
    """Return the action declared on an endpoint, or ``None``."""
    action = getattr(endpoint, DECLARED_ACTION_ATTR, None)
    return action if isinstance(action, Action) else None


def route_label(method: str, path: str) -> str:
    """The human-readable identity of a route, used in every failure message."""
    return f"{method} {path}"


def iter_api_routes(app: FastAPI) -> Iterator[tuple[APIRoute, str, str]]:
    """Yield ``(route, method, label)`` for every author-written route.

    Only :class:`~fastapi.routing.APIRoute` instances are considered. FastAPI
    registers ``/openapi.json``, ``/docs`` and ``/redoc`` as plain Starlette
    routes, so they are excluded structurally rather than by an exemption list
    that would have to be kept in step with the framework.
    """
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted((route.methods or set()) - _SYNTHETIC_METHODS):
            yield route, method, route_label(method, route.path)
