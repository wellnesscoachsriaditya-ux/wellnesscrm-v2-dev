"""Optimistic concurrency on a plan — ADR-14, EC-M4-07, API §4.4.

🔒 **EC-M4-07: "Two practitioners in a clinic edit the same plan concurrently —
last write must not silently discard the other's work."** Two people editing one
client's plan is routine in a clinic. Without a precondition the second save
overwrites the first with no error and no trace, and nobody finds out until a
client is given a plan nobody wrote.

⚠️ **The aggregate is the unit of concurrency, not the row.** A slot and an item
have no version of their own; editing either bumps ``diet_plan_versions.row_version``.
That is what makes a stale plan-builder screen detectable at all — otherwise two
practitioners editing different slots would both succeed while each held a view
of the plan that no longer matched the database.
"""

from __future__ import annotations

import uuid

import pytest

from tests.integration.nutrition.conftest import Catalogue, PlanApi, practitioner

pytestmark = pytest.mark.asyncio


async def _draft_with_item(
    api: PlanApi, client_id: uuid.UUID, catalogue: Catalogue
) -> tuple[str, str, str, str]:
    """Returns (version_id, day_id, slot_id, item_id) for a fresh draft."""
    created = await api.http.post(
        f"/api/v1/app/clients/{client_id}/plans",
        json={"title": "Concurrency", "day_count": 1},
    )
    assert created.status_code == 201, created.text
    version_id = created.json()["draft_version"]["id"]

    read = await api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    day_id = read.json()["days"][0]["id"]
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
    return version_id, day_id, slot_id, added.json()["id"]


async def test_the_read_returns_an_etag(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """A caller cannot present a precondition it was never given."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))
    version_id, _, _, _ = await _draft_with_item(plan_api, client_id, catalogue)

    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")

    assert read.headers["etag"] == f'W/"{read.json()["row_version"]}"'


async def test_a_missing_if_match_is_refused_with_428(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """🔒 Absent is refused, not treated as "no precondition".

    Silently accepting a mutation without a token would downgrade every write to
    last-write-wins — the exact data loss ADR-14 exists to prevent.
    """
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))
    version_id, day_id, slot_id, item_id = await _draft_with_item(plan_api, client_id, catalogue)

    attempts = [
        ("patch", f"/api/v1/app/plan-versions/{version_id}", {"practitioner_notes": "x"}),
        ("post", f"/api/v1/app/plan-versions/{version_id}/discard", None),
        ("patch", f"/api/v1/app/plan-days/{day_id}", {"label": "x"}),
        ("patch", f"/api/v1/app/plan-slots/{slot_id}", {"custom_label": "x"}),
        ("delete", f"/api/v1/app/plan-slots/{slot_id}", None),
        ("patch", f"/api/v1/app/plan-items/{item_id}", {"quantity": "2"}),
        ("delete", f"/api/v1/app/plan-items/{item_id}", None),
    ]
    for method, url, body in attempts:
        kwargs: dict = {}
        if body is not None:
            kwargs["json"] = body
        response = await getattr(plan_api.http, method)(url, **kwargs)
        assert response.status_code == 428, f"{method.upper()} {url} -> {response.status_code}"
        assert response.json()["error"]["type"] == "precondition_required"


@pytest.mark.parametrize("token", ["not-a-number", 'W/"abc"', "", 'W/""'])
async def test_a_malformed_if_match_is_refused_with_428(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue, token: str
) -> None:
    """🔒 Unparseable is refused too — the safe direction.

    An unreadable token treated as "no precondition" is the same silent
    downgrade as omitting one.
    """
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))
    version_id, _, _, item_id = await _draft_with_item(plan_api, client_id, catalogue)

    response = await plan_api.http.patch(
        f"/api/v1/app/plan-items/{item_id}",
        headers={"If-Match": token},
        json={"quantity": "2"},
    )

    assert response.status_code == 428
    assert response.json()["error"]["type"] == "precondition_required"


async def test_a_stale_token_is_refused_with_409(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """🔒 EC-M4-07 — the second writer is told, not silently overruled."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))
    version_id, _, _, item_id = await _draft_with_item(plan_api, client_id, catalogue)

    # Both practitioners open the plan and hold the same token.
    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    shared = read.headers["etag"]

    first = await plan_api.http.patch(
        f"/api/v1/app/plan-items/{item_id}",
        headers={"If-Match": shared},
        json={"quantity": "2"},
    )
    assert first.status_code == 200

    second = await plan_api.http.patch(
        f"/api/v1/app/plan-items/{item_id}",
        headers={"If-Match": shared},
        json={"quantity": "3"},
    )

    assert second.status_code == 409
    assert second.json()["error"]["type"] == "conflict"

    # 🔒 And the first writer's work survived — a conflict that still applied the
    # second write would be a worse failure than no check at all.
    after = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    assert after.json()["days"][0]["slots"][0]["items"][0]["quantity"] == "2"


async def test_a_child_edit_bumps_the_parent_version(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """🔒 The aggregate is the unit of concurrency.

    An item edit must invalidate a token held over the *plan*, or two
    practitioners working on different slots would never discover they had
    diverged.
    """
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))
    version_id, _, _, item_id = await _draft_with_item(plan_api, client_id, catalogue)

    before = (await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")).json()[
        "row_version"
    ]

    edited = await plan_api.http.patch(
        f"/api/v1/app/plan-items/{item_id}",
        headers={"If-Match": f'W/"{before}"'},
        json={"quantity": "4"},
    )
    assert edited.status_code == 200

    after = (await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")).json()[
        "row_version"
    ]

    assert after == before + 1
    assert edited.headers["etag"] == f'W/"{after}"'


async def test_a_slot_edit_invalidates_a_token_held_over_the_plan(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """Two practitioners, two different slots, one plan — the EC-M4-07 scenario."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))
    version_id, _, slot_id, item_id = await _draft_with_item(plan_api, client_id, catalogue)

    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    shared = read.headers["etag"]
    other_slot = read.json()["days"][0]["slots"][1]["id"]

    # Practitioner one renames a different slot.
    renamed = await plan_api.http.patch(
        f"/api/v1/app/plan-slots/{other_slot}",
        headers={"If-Match": shared},
        json={"custom_label": "Mid-morning shake"},
    )
    assert renamed.status_code == 200

    # Practitioner two, still holding the token from before that rename, is refused.
    for method, url, body in (
        ("patch", f"/api/v1/app/plan-items/{item_id}", {"quantity": "5"}),
        ("delete", f"/api/v1/app/plan-slots/{slot_id}", None),
    ):
        kwargs: dict = {"headers": {"If-Match": shared}}
        if body is not None:
            kwargs["json"] = body
        response = await getattr(plan_api.http, method)(url, **kwargs)
        assert response.status_code == 409, f"{method.upper()} {url} -> {response.status_code}"


async def test_the_conflict_names_the_current_version(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """NFR-063 — a refusal a client can act on, rather than one it must guess at."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))
    version_id, _, _, item_id = await _draft_with_item(plan_api, client_id, catalogue)

    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    stale = read.headers["etag"]
    await plan_api.http.patch(
        f"/api/v1/app/plan-items/{item_id}", headers={"If-Match": stale}, json={"quantity": "2"}
    )

    conflict = await plan_api.http.patch(
        f"/api/v1/app/plan-items/{item_id}", headers={"If-Match": stale}, json={"quantity": "3"}
    )

    assert conflict.status_code == 409
    body = conflict.json()["error"]
    assert body["details"]["current_row_version"] == read.json()["row_version"] + 1
    assert body["action"]


async def test_a_fresh_token_after_a_conflict_succeeds(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """The recovery path: reload, then retry. Otherwise the check is a dead end."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))
    version_id, _, _, item_id = await _draft_with_item(plan_api, client_id, catalogue)

    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    stale = read.headers["etag"]
    await plan_api.http.patch(
        f"/api/v1/app/plan-items/{item_id}", headers={"If-Match": stale}, json={"quantity": "2"}
    )
    refused = await plan_api.http.patch(
        f"/api/v1/app/plan-items/{item_id}", headers={"If-Match": stale}, json={"quantity": "3"}
    )
    assert refused.status_code == 409

    reloaded = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    retried = await plan_api.http.patch(
        f"/api/v1/app/plan-items/{item_id}",
        headers={"If-Match": reloaded.headers["etag"]},
        json={"quantity": "3"},
    )

    assert retried.status_code == 200

    # ⚠️ Asserted against a re-read, not against the PATCH body. A leaf route
    # returns the leaf it changed (`ItemResponse`), while the version routes
    # return the whole aggregate — so only the aggregate read proves the change
    # is what a practitioner would next see.
    final = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    assert final.json()["days"][0]["slots"][0]["items"][0]["quantity"] == "3"
