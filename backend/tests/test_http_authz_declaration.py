"""Route declaration — the startup half of ADR-05.

🔒 The pipeline's enforcement is only as good as the guarantee that every route
*runs* it. These tests cover the three ways that guarantee fails, in increasing
order of danger:

1. A route declares nothing — caught, and obviously wrong in review anyway.
2. A route declares an action nobody registered — caught, and a typo away.
3. 🔒 A route declares correctly but sits on a plain router, so the declaration
   is an annotation nobody reads. It passes review, it looks protected, and it
   enforces nothing. This is the one a declaration-only check would miss.

The exemption list is tested as carefully as the declarations, because an
exemption is a hole someone deliberately made and the risk is it quietly
widening.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import APIRouter, FastAPI

from app.kernel.authz import REGISTRY, DataScope, UndeclaredActionError, register_action
from app.kernel.context import UserRole
from app.platform.http.authz import (
    EXEMPT_PATHS,
    declared_action,
    iter_api_routes,
    requires,
    route_label,
)
from app.platform.http.pipeline import (
    AuthorizedRoute,
    UnenforcedRouteError,
    realm_router,
    verify_route_authorization,
)


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    """Snapshot the process-wide registry, as `test_kernel_authz` does."""
    saved = dict(REGISTRY._actions)
    REGISTRY.clear()
    try:
        yield
    finally:
        REGISTRY.clear()
        REGISTRY._actions.update(saved)


@pytest.fixture
def thing_read() -> object:
    return register_action(
        "thing.read",
        roles={UserRole.OWNER},
        data_scope=DataScope.TENANT_METADATA,
        is_read=True,
    )


# ─── The decorator ───────────────────────────────────────────────────────


def test_requires_returns_the_same_function(thing_read: object) -> None:
    """🔒 Not a wrapper.

    FastAPI introspects the endpoint's signature to build its parameters and
    response model. A wrapping decorator would replace the object being
    introspected, and every route would need `functools.wraps` plumbing to keep
    working. Returning the same object also means decorator order cannot matter.
    """

    async def endpoint(thing_id: int) -> dict[str, int]:
        return {"id": thing_id}

    decorated = requires(thing_read)(endpoint)  # type: ignore[arg-type]

    assert decorated is endpoint
    assert decorated.__name__ == "endpoint"


def test_declared_action_reads_back_what_was_declared(thing_read: object) -> None:
    async def endpoint() -> None: ...

    requires(thing_read)(endpoint)  # type: ignore[arg-type]

    assert declared_action(endpoint) is thing_read


def test_declared_action_is_none_when_nothing_was_declared() -> None:
    async def endpoint() -> None: ...

    assert declared_action(endpoint) is None


# ─── Startup validation ──────────────────────────────────────────────────


def _app_with(router: APIRouter) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def test_declared_and_enforced_route_passes(thing_read: object) -> None:
    router = realm_router("/api/v1/app")

    @router.get("/things")
    @requires(thing_read)
    async def list_things() -> list[str]:
        return []

    verify_route_authorization(_app_with(router))


def test_startup_fails_on_a_route_with_no_declaration() -> None:
    router = realm_router("/api/v1/app")

    @router.get("/things")
    async def list_things() -> list[str]:
        return []

    with pytest.raises(UndeclaredActionError) as raised:
        verify_route_authorization(_app_with(router))

    assert "GET /api/v1/app/things" in str(raised.value)


def test_startup_fails_when_the_declared_action_was_never_registered() -> None:
    """A registration in a module that startup never imports is invisible to the
    registry — the failure this catches, and the reason the message says so."""
    unregistered = register_action("ghost.read", roles={UserRole.OWNER})
    REGISTRY.clear()

    router = realm_router("/api/v1/app")

    @router.get("/ghosts")
    @requires(unregistered)
    async def list_ghosts() -> list[str]:
        return []

    with pytest.raises(UndeclaredActionError) as raised:
        verify_route_authorization(_app_with(router))

    assert "ghost.read" in str(raised.value)


def test_startup_fails_when_a_declared_route_bypasses_the_pipeline(
    thing_read: object,
) -> None:
    """🔒 The dangerous case: correctly declared, never enforced.

    A plain `APIRouter` does not use `AuthorizedRoute`, so nothing reads the
    declaration. The endpoint looks protected in the source and is open in
    production. Only a route-class check can see this.
    """
    router = APIRouter(prefix="/api/v1/app")

    @router.get("/things")
    @requires(thing_read)
    async def list_things() -> list[str]:
        return []

    with pytest.raises(UnenforcedRouteError) as raised:
        verify_route_authorization(_app_with(router))

    assert "GET /api/v1/app/things" in str(raised.value)
    assert "route_class=AuthorizedRoute" in str(raised.value)


def test_every_offending_route_is_named_at_once() -> None:
    """One pass to fix them, not one restart per route."""
    router = realm_router("/api/v1/app")

    @router.get("/things")
    async def list_things() -> list[str]:
        return []

    @router.get("/others")
    async def list_others() -> list[str]:
        return []

    with pytest.raises(UndeclaredActionError) as raised:
        verify_route_authorization(_app_with(router))

    message = str(raised.value)
    assert "GET /api/v1/app/things" in message
    assert "GET /api/v1/app/others" in message


# ─── Exemptions ──────────────────────────────────────────────────────────


def test_exempt_paths_need_no_declaration() -> None:
    router = APIRouter()

    @router.get("/api/v1/public/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    verify_route_authorization(_app_with(router))


def test_exemption_is_by_exact_path_not_prefix() -> None:
    """🔒 A prefix exemption would cover every endpoint later added to the realm.

    `/api/v1/public/health/live` is not `/api/v1/public/health`, and a new public
    endpoint must be exempted deliberately rather than inheriting one.
    """
    router = realm_router("")

    @router.get("/api/v1/public/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    with pytest.raises(UndeclaredActionError):
        verify_route_authorization(_app_with(router))


def test_exempt_paths_are_only_health_during_slice_a() -> None:
    """Pins the exemption list so growth is a deliberate, reviewed change.

    ⚠️ Slice B adds `/public/auth/*` — reachable before an actor exists, which is
    the definition of the exemption. Updating this test is the moment to ask
    whether the new entry earns it.
    """
    exempt = set(EXEMPT_PATHS)
    assert exempt == {
        "/api/v1/public/health",
        "/api/v1/public/health/ready",
    }


# ─── Route enumeration ───────────────────────────────────────────────────


def test_synthetic_methods_are_not_reported(thing_read: object) -> None:
    """HEAD is synthesised by the router. Auditing it would report a route
    nobody wrote, and the fix for the resulting failure would be baffling."""
    router = realm_router("/api/v1/app")

    @router.get("/things")
    @requires(thing_read)
    async def list_things() -> list[str]:
        return []

    methods = {method for _route, method, _label in iter_api_routes(_app_with(router))}

    assert methods == {"GET"}


def test_framework_routes_are_excluded_structurally() -> None:
    """`/openapi.json` and `/docs` are Starlette routes, not `APIRoute`s.

    Excluded by type rather than by name, so a FastAPI upgrade that renames or
    adds one cannot silently start failing startup.
    """
    app = FastAPI()

    paths = {route.path for route, _method, _label in iter_api_routes(app)}

    assert paths == set()
    verify_route_authorization(app)


def test_route_label_is_method_and_path() -> None:
    assert route_label("GET", "/api/v1/app/things") == "GET /api/v1/app/things"


# ─── realm_router ────────────────────────────────────────────────────────


def test_realm_router_sets_the_enforcing_route_class() -> None:
    assert realm_router("/api/v1/app").route_class is AuthorizedRoute


def test_realm_router_refuses_a_route_class_override() -> None:
    """🔒 An override would be the unenforced-route failure arriving through the
    door marked safe."""
    with pytest.raises(ValueError, match="does not accept an override"):
        realm_router("/api/v1/app", route_class=APIRouter)


# ─── The real application ────────────────────────────────────────────────


def test_the_application_starts() -> None:
    """🔒 `create_app` calls `verify_route_authorization`, so this failing means
    a route in the real app is undeclared or unenforced."""
    from app.main import create_app

    create_app()
