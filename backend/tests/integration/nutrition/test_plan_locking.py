"""Locking as a nutritional constraint over HTTP — M4, API §8.4 (Approved).

🔒 The guarantee under test: locking moves an item's nutrition from
``unlocked_current`` into ``locked_consumed`` **without changing what the plan
contains** — ``remaining_available`` and ``plan_totals`` are invariant across a
lock, because a lock fixes a portion, it does not add or remove food. A locked
*slot* makes every item inside it read as effectively locked. And a
practitioner can always unlock: there is no super-lock.

    Fixture Dal  100 kcal / 100 g, 5 g protein / 100 g,  1 katori = 150 g
    1 katori dal -> 150 g -> 150 kcal, 7.5 g protein

    target 1400 kcal -> remaining_available = 1400 - 150 = 1250, locked or not.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.integration.nutrition.conftest import Catalogue, PlanApi, practitioner

pytestmark = pytest.mark.asyncio


async def _plan_with_one_dal(
    plan_api: PlanApi, client_id, catalogue: Catalogue
) -> tuple[str, str, str]:
    """A draft with a single katori of dal in its first slot.

    Returns ``(version_id, slot_id, item_id)``.
    """
    created = await plan_api.http.post(
        f"/api/v1/app/clients/{client_id}/plans",
        json={"title": "Week 1", "day_count": 1, "target_energy_kcal": "1400"},
    )
    assert created.status_code == 201, created.text
    version_id = created.json()["draft_version"]["id"]

    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    slot_id = read.json()["days"][0]["slots"][0]["id"]

    added = await plan_api.http.post(
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
    return version_id, slot_id, added.json()["id"]


async def test_locking_an_item_moves_its_nutrition_into_locked_consumed(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """🔒 The budget shifts; the plan does not. Locking is a constraint, not an edit."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))

    version_id, _slot_id, item_id = await _plan_with_one_dal(plan_api, client_id, catalogue)

    before = (await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")).json()
    budget = before["nutrition_budget"]
    assert Decimal(budget["unlocked_current"]["energy_kcal"]) == Decimal("150")
    assert Decimal(budget["locked_consumed"]["energy_kcal"]) == Decimal("0")
    assert Decimal(budget["remaining_available"]["energy_kcal"]) == Decimal("1250")

    etag = (await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")).headers["etag"]
    locked = await plan_api.http.patch(
        f"/api/v1/app/plan-items/{item_id}",
        headers={"If-Match": etag},
        json={"is_locked": True},
    )
    assert locked.status_code == 200, locked.text
    assert locked.json()["is_locked"] is True

    after = (await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")).json()
    budget = after["nutrition_budget"]
    # 🔒 The nutrition moved from unlocked to locked …
    assert Decimal(budget["locked_consumed"]["energy_kcal"]) == Decimal("150")
    assert Decimal(budget["locked_consumed"]["protein_g"]) == Decimal("7.5")
    assert Decimal(budget["unlocked_current"]["energy_kcal"]) == Decimal("0")
    assert budget["locked_item_count"] == 1
    # 🔒 … but nothing about what the plan contains changed.
    assert Decimal(budget["remaining_available"]["energy_kcal"]) == Decimal("1250")
    assert Decimal(after["plan_totals"]["energy_kcal"]) == Decimal("150")

    item = after["days"][0]["slots"][0]["items"][0]
    assert item["item_is_locked"] is True
    assert item["is_locked"] is True


async def test_a_locked_item_can_always_be_unlocked(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """🔒 Approved — there is no super-lock; unlocking is always available."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))

    version_id, _slot_id, item_id = await _plan_with_one_dal(plan_api, client_id, catalogue)

    etag = (await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")).headers["etag"]
    await plan_api.http.patch(
        f"/api/v1/app/plan-items/{item_id}",
        headers={"If-Match": etag},
        json={"is_locked": True},
    )

    etag = (await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")).headers["etag"]
    unlocked = await plan_api.http.patch(
        f"/api/v1/app/plan-items/{item_id}",
        headers={"If-Match": etag},
        json={"is_locked": False},
    )
    assert unlocked.status_code == 200, unlocked.text
    assert unlocked.json()["is_locked"] is False

    budget = (await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")).json()[
        "nutrition_budget"
    ]
    assert Decimal(budget["unlocked_current"]["energy_kcal"]) == Decimal("150")
    assert Decimal(budget["locked_consumed"]["energy_kcal"]) == Decimal("0")
    assert budget["locked_item_count"] == 0


async def test_locking_a_slot_locks_every_item_within_it(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """🔒 A slot's lock is effective on its items, even when they carry none of their own."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))

    version_id, slot_id, _item_id = await _plan_with_one_dal(plan_api, client_id, catalogue)

    etag = (await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")).headers["etag"]
    locked = await plan_api.http.patch(
        f"/api/v1/app/plan-slots/{slot_id}",
        headers={"If-Match": etag},
        json={"is_locked": True},
    )
    assert locked.status_code == 200, locked.text
    assert locked.json()["is_locked"] is True

    body = (await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")).json()
    slot = body["days"][0]["slots"][0]
    item = slot["items"][0]

    assert slot["is_locked"] is True
    # The item declares no lock of its own, but reads as effectively locked.
    assert item["item_is_locked"] is False
    assert item["is_locked"] is True

    budget = body["nutrition_budget"]
    assert budget["locked_slot_count"] == 1
    assert Decimal(budget["locked_consumed"]["energy_kcal"]) == Decimal("150")
    assert Decimal(budget["unlocked_current"]["energy_kcal"]) == Decimal("0")


async def test_a_stale_if_match_refuses_the_lock(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """🔒 ADR-14 / EC-M4-07 — locking is a mutation, so a stale token is a 409."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))

    version_id, _slot_id, item_id = await _plan_with_one_dal(plan_api, client_id, catalogue)

    stale = (await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")).headers["etag"]

    # A concurrent edit lands first, moving the version on.
    moved = await plan_api.http.patch(
        f"/api/v1/app/plan-items/{item_id}",
        headers={"If-Match": stale},
        json={"quantity": "2"},
    )
    assert moved.status_code == 200

    conflict = await plan_api.http.patch(
        f"/api/v1/app/plan-items/{item_id}",
        headers={"If-Match": stale},
        json={"is_locked": True},
    )
    assert conflict.status_code == 409


async def test_cannot_lock_an_item_in_another_tenants_plan(
    plan_api: PlanApi, tenant_a, tenant_b, tenant_clients, catalogue: Catalogue
) -> None:
    """🔒 AC-M1-006 — the lock authorizes against the client, so a stranger gets 404."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))

    version_id, _slot_id, item_id = await _plan_with_one_dal(plan_api, client_id, catalogue)
    etag = (await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")).headers["etag"]

    # A practitioner in the other tenant tries to reach the item by its id.
    _other_client, other_user = tenant_clients[tenant_b]
    plan_api.as_actor(practitioner(tenant_b, other_user))

    refused = await plan_api.http.patch(
        f"/api/v1/app/plan-items/{item_id}",
        headers={"If-Match": etag},
        json={"is_locked": True},
    )
    assert refused.status_code == 404, refused.text
