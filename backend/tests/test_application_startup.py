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

import pytest

from app.kernel.authz import REGISTRY
from app.main import create_app
from app.platform.http.authz import EXEMPT_PATHS, declared_action, iter_api_routes
from app.platform.http.pipeline import AuthorizedRoute


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
    }
