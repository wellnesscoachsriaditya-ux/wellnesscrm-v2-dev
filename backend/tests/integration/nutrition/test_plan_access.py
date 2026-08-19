"""Who may reach a plan — tenant isolation, realm separation, declared actions.

🔒 **AC-M1-006 and API §5.4, exercised through the real pipeline.** A
service-level test can show that a query filtered by ``tenant_id``; only an HTTP
test can show that the route is wired to the pipeline at all, that the realm
check runs before the transaction opens, and that a refusal is shaped as a 404
rather than a 403.

⚠️ **404, never 403, across a tenant boundary.** A 403 confirms the resource
exists. For a plan that leaks the existence of another practice's clinical
record, and it lets a caseload be enumerated one request at a time. The two
answers are indistinguishable on purpose.

Isolation and authorization share a file because they answer the same question —
*may this caller reach this plan* — through the same harness, and separating them
would duplicate the setup without separating any concern.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.kernel.context import Actor, ActorType, AuthRealm
from app.main import create_app
from app.platform.http.authz import declared_action
from app.platform.http.pipeline import AuthorizedRoute
from tests.integration.conftest import scope_to
from tests.integration.nutrition.conftest import Catalogue, PlanApi, practitioner

# ⚠️ Applied per test rather than module-wide: the three route-table tests
# below are synchronous — they need no database and no event loop — and a
# module-level asyncio mark would warn on each of them.
_asyncio = pytest.mark.asyncio

#: The 15 routes Slice 1.4a exposes — API §8.1.
EXPECTED_PLAN_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/v1/app/clients/{client_id}/plans"),
        ("GET", "/api/v1/app/clients/{client_id}/plans"),
        ("GET", "/api/v1/app/plans/{plan_id}"),
        ("GET", "/api/v1/app/plan-versions/{version_id}"),
        ("PATCH", "/api/v1/app/plan-versions/{version_id}"),
        ("POST", "/api/v1/app/plan-versions/{version_id}/days"),
        ("POST", "/api/v1/app/plan-versions/{version_id}/slots"),
        ("POST", "/api/v1/app/plan-versions/{version_id}/discard"),
        ("POST", "/api/v1/app/plan-versions/{version_id}/issue"),
        ("PATCH", "/api/v1/app/plan-days/{day_id}"),
        ("PATCH", "/api/v1/app/plan-slots/{slot_id}"),
        ("DELETE", "/api/v1/app/plan-slots/{slot_id}"),
        ("POST", "/api/v1/app/plan-items"),
        ("PATCH", "/api/v1/app/plan-items/{item_id}"),
        ("DELETE", "/api/v1/app/plan-items/{item_id}"),
    }
)


def _plan_routes(app: object) -> list:
    return [
        route
        for route in app.routes  # type: ignore[attr-defined]
        if hasattr(route, "methods") and ("/plan" in route.path or route.path.endswith("/plans"))
    ]


async def _plan_for(
    api: PlanApi, client_id: uuid.UUID, catalogue: Catalogue
) -> tuple[str, str, str, str]:
    """A draft with one item. Returns (plan_id, version_id, slot_id, item_id)."""
    created = await api.http.post(
        f"/api/v1/app/clients/{client_id}/plans", json={"title": "Access", "day_count": 1}
    )
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan"]["id"]
    version_id = created.json()["draft_version"]["id"]

    read = await api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    slot_id = read.json()["days"][0]["slots"][0]["id"]
    added = await api.http.post(
        "/api/v1/app/plan-items",
        headers={"If-Match": read.headers["etag"]},
        json={
            "slot_id": slot_id,
            "item_type": "food",
            "food_id": str(catalogue.dal_id),
            "quantity": "1",
            "measure_unit_id": str(catalogue.katori_id),
        },
    )
    assert added.status_code == 201, added.text
    return plan_id, version_id, slot_id, added.json()["id"]


# ─── The route table itself ──────────────────────────────────────────────


def test_exactly_fifteen_plan_routes_are_registered() -> None:
    """A route that vanished from the table would fail silently in production."""
    app = create_app()
    registered = {(m, r.path) for r in _plan_routes(app) for m in r.methods if m != "HEAD"}

    assert registered == EXPECTED_PLAN_ROUTES


def test_every_plan_route_runs_the_pipeline_and_declares_an_action() -> None:
    """🔒 ADR-05 — a declaration on a route that bypasses the pipeline enforces nothing.

    ``verify_route_authorization`` aborts startup on either failure, so this is a
    second reading of the same guarantee — cheap, and it names the offending
    route rather than failing the whole process.
    """
    app = create_app()
    routes = _plan_routes(app)

    assert [r.path for r in routes if not isinstance(r, AuthorizedRoute)] == []
    assert [r.path for r in routes if declared_action(r.endpoint) is None] == []
    assert {declared_action(r.endpoint).name for r in routes} == {
        "nutrition.plans.read",
        "nutrition.plans.write",
    }


def test_read_routes_declare_read_and_write_routes_declare_write() -> None:
    """🔒 A GET declaring a write action would over-grant; the reverse under-audits."""
    app = create_app()

    for route in _plan_routes(app):
        action = declared_action(route.endpoint)
        for method in route.methods - {"HEAD", "OPTIONS"}:
            expected = "nutrition.plans.read" if method == "GET" else "nutrition.plans.write"
            assert action.name == expected, f"{method} {route.path} declares {action.name}"


# ─── Tenant isolation ────────────────────────────────────────────────────


@_asyncio
async def test_another_tenant_reading_a_plan_gets_404(
    plan_api: PlanApi, tenant_a, tenant_b, tenant_clients, catalogue: Catalogue
) -> None:
    """🔒 AC-M1-006 / API §5.4 — absent and forbidden are indistinguishable."""
    client_a, user_a = tenant_clients[tenant_a]
    _, user_b = tenant_clients[tenant_b]

    plan_api.as_actor(practitioner(tenant_a, user_a))
    plan_id, version_id, _, _ = await _plan_for(plan_api, client_a, catalogue)

    plan_api.as_actor(practitioner(tenant_b, user_b))

    for url in (
        f"/api/v1/app/plans/{plan_id}",
        f"/api/v1/app/plan-versions/{version_id}",
        f"/api/v1/app/clients/{client_a}/plans",
    ):
        response = await plan_api.http.get(url)
        assert response.status_code == 404, f"{url} -> {response.status_code}"
        assert response.json()["error"]["type"] == "not_found"


@_asyncio
async def test_another_tenant_cannot_mutate_a_plan(
    plan_api: PlanApi, tenant_a, tenant_b, tenant_clients, catalogue: Catalogue
) -> None:
    """Every mutating route, not just the read — a leak needs only one door."""
    client_a, user_a = tenant_clients[tenant_a]
    _, user_b = tenant_clients[tenant_b]

    plan_api.as_actor(practitioner(tenant_a, user_a))
    _, version_id, slot_id, item_id = await _plan_for(plan_api, client_a, catalogue)
    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    etag = read.headers["etag"]
    day_id = read.json()["days"][0]["id"]

    plan_api.as_actor(practitioner(tenant_b, user_b))
    headers = {"If-Match": etag}

    attempts = [
        ("patch", f"/api/v1/app/plan-versions/{version_id}", {"practitioner_notes": "x"}),
        ("post", f"/api/v1/app/plan-versions/{version_id}/discard", None),
        ("post", f"/api/v1/app/plan-versions/{version_id}/days", {"label": "x"}),
        ("patch", f"/api/v1/app/plan-days/{day_id}", {"label": "x"}),
        ("patch", f"/api/v1/app/plan-slots/{slot_id}", {"custom_label": "x"}),
        ("delete", f"/api/v1/app/plan-slots/{slot_id}", None),
        ("patch", f"/api/v1/app/plan-items/{item_id}", {"quantity": "9"}),
        ("delete", f"/api/v1/app/plan-items/{item_id}", None),
    ]
    for method, url, body in attempts:
        kwargs: dict = {"headers": headers}
        if body is not None:
            kwargs["json"] = body
        response = await getattr(plan_api.http, method)(url, **kwargs)
        assert response.status_code == 404, f"{method.upper()} {url} -> {response.status_code}"


@_asyncio
async def test_the_plan_still_exists_after_the_cross_tenant_attempts(
    plan_api: PlanApi, tenant_a, tenant_b, tenant_clients, catalogue: Catalogue
) -> None:
    """A refusal that half-applied would be worse than one that leaked."""
    client_a, user_a = tenant_clients[tenant_a]
    _, user_b = tenant_clients[tenant_b]

    plan_api.as_actor(practitioner(tenant_a, user_a))
    _, version_id, _, item_id = await _plan_for(plan_api, client_a, catalogue)
    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")

    plan_api.as_actor(practitioner(tenant_b, user_b))
    await plan_api.http.delete(
        f"/api/v1/app/plan-items/{item_id}", headers={"If-Match": read.headers["etag"]}
    )

    plan_api.as_actor(practitioner(tenant_a, user_a))
    after = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    assert after.status_code == 200
    assert [i["id"] for i in after.json()["days"][0]["slots"][0]["items"]] == [item_id]


@_asyncio
async def test_cross_tenant_delete_affects_zero_rows_at_the_database(
    plan_api: PlanApi,
    tenant_a,
    tenant_b,
    tenant_clients,
    catalogue: Catalogue,
    app_engine: AsyncEngine,
) -> None:
    """🔒 RLS, with the application out of the picture.

    PostgreSQL applies a policy's ``USING`` clause to ``DELETE`` as a visibility
    filter, so another tenant's row is not errored on — it is *not there*. Zero
    rows is the correct outcome and the one the 404 mapping is built on.
    """
    client_a, user_a = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_a))
    _, _, _, item_id = await _plan_for(plan_api, client_a, catalogue)

    async with async_sessionmaker(app_engine)() as session:
        await scope_to(await session.connection(), tenant_b)
        result = await session.execute(
            text("DELETE FROM plan_items WHERE id = :id"), {"id": item_id}
        )
        await session.rollback()

    assert result.rowcount == 0


# ─── Realm separation ────────────────────────────────────────────────────


def _client_realm_actor(tenant_id: uuid.UUID) -> Actor:
    return Actor(
        actor_type=ActorType.CLIENT,
        realm=AuthRealm.CLIENT,
        subject_id=uuid.uuid4(),
        tenant_id=tenant_id,
        role=None,
    )


def _operator_actor() -> Actor:
    return Actor(
        actor_type=ActorType.OPERATOR,
        realm=AuthRealm.OPERATOR,
        subject_id=uuid.uuid4(),
        tenant_id=None,
        role=None,
    )


@_asyncio
async def test_the_client_realm_cannot_reach_plan_routes(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """🔒 ADR-A01 — `/app` is the practitioner surface.

    A client reads their plan through the portal's own snapshot (M7), never
    through the authoring API. The realm check runs *before* the transaction
    opens, so this is refused without ever consuming a connection.
    """
    client_a, user_a = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_a))
    plan_id, version_id, _, _ = await _plan_for(plan_api, client_a, catalogue)

    plan_api.as_actor(_client_realm_actor(tenant_a))

    for url in (f"/api/v1/app/plans/{plan_id}", f"/api/v1/app/plan-versions/{version_id}"):
        response = await plan_api.http.get(url)
        assert response.status_code in (401, 403, 404), f"{url} -> {response.status_code}"
        assert response.status_code != 200


@_asyncio
async def test_the_operator_realm_cannot_reach_plan_routes(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """🔒 An operator administers tenants; they do not read clinical records."""
    client_a, user_a = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_a))
    plan_id, version_id, _, _ = await _plan_for(plan_api, client_a, catalogue)

    plan_api.as_actor(_operator_actor())

    for url in (f"/api/v1/app/plans/{plan_id}", f"/api/v1/app/plan-versions/{version_id}"):
        response = await plan_api.http.get(url)
        assert response.status_code in (401, 403, 404), f"{url} -> {response.status_code}"
        assert response.status_code != 200


@_asyncio
async def test_an_anonymous_caller_is_refused(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """🔒 Deny by default — no credential means no plan."""
    client_a, user_a = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_a))
    _, version_id, _, _ = await _plan_for(plan_api, client_a, catalogue)

    plan_api.as_actor(Actor.anonymous())

    response = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    assert response.status_code == 401


@_asyncio
async def test_a_practitioner_of_the_owning_tenant_is_allowed(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """The permitted case — without it, every refusal above could be a false pass."""
    client_a, user_a = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_a))
    plan_id, version_id, _, _ = await _plan_for(plan_api, client_a, catalogue)

    assert (await plan_api.http.get(f"/api/v1/app/plans/{plan_id}")).status_code == 200
    assert (await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")).status_code == 200
    assert (await plan_api.http.get(f"/api/v1/app/clients/{client_a}/plans")).status_code == 200
