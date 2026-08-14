"""The real application must start.

🔒 `create_app` runs `verify_route_authorization`, so a failure here means a
route in the shipped application is undeclared, declares an unregistered action,
or bypasses the pipeline. It is the check that makes ADR-05 true of the product
rather than only of the test fixtures.

⚠️ Deliberately in its own module, with **no registry-clearing fixture**. The
tests that exercise the declaration machinery snapshot and clear
`kernel.authz.REGISTRY`; building the real application inside one of those would
find its own import-time registrations missing. See `tests/conftest.py`.
"""

from __future__ import annotations

import inspect

import pytest

from app.kernel.authz import REGISTRY
from app.main import create_app
from app.platform.http.authz import EXEMPT_PATHS, declared_action, iter_api_routes
from app.platform.http.pipeline import AuthorizedRoute, PublicRoute


@pytest.fixture(scope="module")
def application() -> object:
    return create_app()


def test_the_application_starts(application: object) -> None:
    assert application is not None


def test_the_logout_action_is_registered() -> None:
    """The one authorization action authentication owns.

    ⚠️ If this fails while the same test passes in isolation, a fixture
    elsewhere cleared the registry during a first import — the failure mode
    `tests/conftest.py` exists to prevent.
    """
    assert REGISTRY.get("session.end") is not None


def test_every_route_is_declared_or_deliberately_exempt(application: object) -> None:
    """🔒 The permission surface, enumerated.

    Reading this test's failure output is how you find out what the application
    exposes without authorization — which is the property ADR-05 exists to keep
    reviewable.
    """
    undeclared = [
        label
        for route, _method, label in iter_api_routes(application)  # type: ignore[arg-type]
        if route.path not in EXEMPT_PATHS and declared_action(route.endpoint) is None
    ]

    assert undeclared == []


def test_every_authorized_route_runs_the_pipeline(application: object) -> None:
    """🔒 A declaration on a plain router enforces nothing."""
    unenforced = [
        label
        for route, _method, label in iter_api_routes(application)  # type: ignore[arg-type]
        if route.path not in EXEMPT_PATHS and not isinstance(route, AuthorizedRoute)
    ]

    assert unenforced == []


def test_the_authentication_surface_is_the_only_unauthenticated_one(
    application: object,
) -> None:
    """Pins what the running application actually leaves open.

    Distinct from the `EXEMPT_PATHS` test: that one pins the *list*, this pins
    the list's effect on the real route table. A path exempted but never routed
    would pass the first and is invisible to it.
    """
    exempt_and_routed = {
        route.path
        for route, _method, _label in iter_api_routes(application)  # type: ignore[arg-type]
        if route.path in EXEMPT_PATHS
    }

    assert exempt_and_routed == {
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
        # 🔒 Slice F — API §11.1/§11.2. The enquiry form is unauthenticated by
        # necessity: a prospect has no account and getting one is what the form
        # exists to start. Bounded instead by tenant-slug resolution, rate
        # limiting (§14.2), a spam score (FR-M2-008) and explicit consent
        # (EC-M2-04) — see `routers/public_forms.py`.
        "/api/v1/public/forms/{tenant_slug}",
        "/api/v1/public/forms/{tenant_slug}/submit",
        # 🔒 S5 — API §11.3. A provider's delivery-status callback has no actor
        # and no token it could present. Bounded instead by HMAC signature
        # verification over the raw body before it is parsed, a verify-token
        # challenge on the GET, and a tenant resolved from the provider's own
        # message id through a policy that admits exactly one row — see
        # `routers/webhooks.py` and migration 0021 §5.
        "/api/v1/public/webhooks/{provider}",
    }


def test_every_exempt_route_needing_a_database_can_get_one(application: object) -> None:
    """🔒 An exempt route that calls `get_session` must open a transaction.

    **This is the check whose absence let a real defect ship.** `EXEMPT_PATHS`
    exempts a path from *authorization*, and until Slice F `AuthorizedRoute` was
    the only class that opened a transaction. So all six `/public/auth`
    endpoints — register, verify-email, login, refresh and both password-reset
    halves — sat on a bare `APIRouter`, called `get_session()`, hit its
    `RuntimeError` and answered 500 for the whole of S1.

    ⚠️ Nothing noticed because the identity tests exercise
    `platform.identity.service` directly rather than over HTTP, so the routers
    themselves were never called. `PublicRoute` fixes the defect; this fixes the
    blind spot, which is the half that stops it recurring the next time somebody
    adds a public endpoint.

    ⚠️ Source inspection, and only of the endpoint's own body — the same
    heuristic bargain `tests/test_client_access.py` documents. An endpoint that
    reaches a session through a helper is invisible here. That is acceptable: the
    mistake has a consistent shape (a new route on a plain router), and catching
    the shape beats catching nothing.
    """
    stranded: list[str] = []

    for route, _method, label in iter_api_routes(application):  # type: ignore[arg-type]
        if route.path not in EXEMPT_PATHS:
            continue
        try:
            source = inspect.getsource(route.endpoint)
        except OSError:  # pragma: no cover — a C-level or generated endpoint
            continue
        if "get_session(" not in source and "adopt_tenant_scope(" not in source:
            continue
        if not isinstance(route, AuthorizedRoute | PublicRoute):
            stranded.append(label)

    assert stranded == [], (
        "these exempt routes ask for a database session but are not on a route "
        "class that opens a transaction, so every call to them answers 500: "
        f"{', '.join(sorted(stranded))}. Register them on `public_router()`."
    )
