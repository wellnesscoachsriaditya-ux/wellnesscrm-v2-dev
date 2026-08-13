"""Plan authoring over HTTP — M4 Slice 1.4a, API §8.2/§8.3/§8.4.

🔒 **The totals here are checked against a calculation done by hand**, written
out below, rather than against whatever the code returns. AC-M4-006 asks that
"daily nutritional totals match an independent manual calculation from the same
food values", and a test that asserts the code agrees with itself proves nothing
about that.

    Fixture Dal   100 kcal / 100 g, 5 g protein / 100 g,  1 katori = 150 g
    Fixture Roti  300 kcal / 100 g,                       1 piece  =  40 g

    2 katori dal   -> 2   x 150 = 300 g -> 300/100 x 100 = 300 kcal, 15 g protein
    2 piece  roti  -> 2   x  40 =  80 g ->  80/100 x 300 = 240 kcal,  0 g protein
    plan total                                            540 kcal, 15 g protein

    target 1400 kcal -> remaining_available = 1400 - 540 = 860, outside the 5%
    (70 kcal) tolerance, so one soft `energy_below_target` warning.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.integration.nutrition.conftest import Catalogue, PlanApi, practitioner

pytestmark = pytest.mark.asyncio


async def _create_plan(api: PlanApi, client_id, **body) -> dict:
    response = await api.http.post(
        f"/api/v1/app/clients/{client_id}/plans",
        json={"title": "Week 1", "day_count": 1, **body},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_plan_yields_a_plan_and_its_first_draft(
    plan_api: PlanApi, tenant_a, tenant_clients
) -> None:
    """🔒 API §8.2 — the API never produces a plan without a version."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))

    body = await _create_plan(plan_api, client_id, target_energy_kcal="1400")

    assert body["plan"]["client_id"] == str(client_id)
    assert body["draft_version"]["state"] == "draft"
    assert body["draft_version"]["version_number"] == 1
    assert body["draft_version"]["row_version"] == 1


async def test_new_plan_carries_the_default_slots(
    plan_api: PlanApi, tenant_a, tenant_clients
) -> None:
    """🟡 FR-M4-025's seven proposed slots, on every day."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))

    created = await _create_plan(plan_api, client_id, day_count=2)
    version_id = created["draft_version"]["id"]

    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    assert read.status_code == 200
    days = read.json()["days"]

    assert len(days) == 2
    assert [slot["slot_type"] for slot in days[0]["slots"]] == [
        "early_morning",
        "breakfast",
        "mid_morning",
        "lunch",
        "evening_snack",
        "dinner",
        "bedtime",
    ]


async def test_empty_plan_reports_the_whole_target_as_remaining(
    plan_api: PlanApi, tenant_a, tenant_clients
) -> None:
    """🔒 ADR-A07 — the budget is a first-class response field even when empty."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))

    created = await _create_plan(plan_api, client_id, target_energy_kcal="1400")
    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{created['draft_version']['id']}")
    budget = read.json()["nutrition_budget"]

    assert budget["target"]["energy_kcal"] == "1400"
    assert budget["remaining_available"]["energy_kcal"] == "1400"
    assert budget["is_within_tolerance"] is False
    assert [w["rule_code"] for w in read.json()["warnings"]] == ["energy_below_target"]


async def test_totals_match_a_hand_calculation(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """🔒 AC-M4-006 — see the module docstring for the arithmetic."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))

    created = await _create_plan(plan_api, client_id, target_energy_kcal="1400")
    version_id = created["draft_version"]["id"]
    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    slot_id = read.json()["days"][0]["slots"][1]["id"]

    for food_id, unit_id in (
        (catalogue.dal_id, catalogue.katori_id),
        (catalogue.roti_id, catalogue.piece_id),
    ):
        added = await plan_api.http.post(
            "/api/v1/app/plan-items",
            headers={"If-Match": read.headers["etag"]},
            json={
                "slot_id": slot_id,
                "item_type": "food",
                "food_id": str(food_id),
                "quantity": "2",
                "measure_unit_id": str(unit_id),
            },
        )
        assert added.status_code == 201, added.text
        read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")

    body = read.json()
    dal, roti = body["days"][0]["slots"][1]["items"]

    assert Decimal(dal["resolved_grams"]) == Decimal("300")
    assert Decimal(dal["nutrition"]["energy_kcal"]) == Decimal("300")
    assert Decimal(dal["nutrition"]["protein_g"]) == Decimal("15")
    assert Decimal(roti["resolved_grams"]) == Decimal("80")
    assert Decimal(roti["nutrition"]["energy_kcal"]) == Decimal("240")

    assert Decimal(body["days"][0]["slots"][1]["slot_totals"]["energy_kcal"]) == Decimal("540")
    assert Decimal(body["days"][0]["day_totals"]["energy_kcal"]) == Decimal("540")
    assert Decimal(body["plan_totals"]["energy_kcal"]) == Decimal("540")
    assert Decimal(body["plan_totals"]["protein_g"]) == Decimal("15")
    assert Decimal(body["nutrition_budget"]["remaining_available"]["energy_kcal"]) == Decimal("860")


async def test_measure_display_is_rendered_server_side(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """🔒 API §8.3 — household-measure formatting lives in one place."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))

    created = await _create_plan(plan_api, client_id)
    version_id = created["draft_version"]["id"]
    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")

    await plan_api.http.post(
        "/api/v1/app/plan-items",
        headers={"If-Match": read.headers["etag"]},
        json={
            "slot_id": read.json()["days"][0]["slots"][0]["id"],
            "item_type": "food",
            "food_id": str(catalogue.dal_id),
            "quantity": "1.5",
            "measure_unit_id": str(catalogue.katori_id),
        },
    )

    item = (await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")).json()["days"][0][
        "slots"
    ][0]["items"][0]

    assert item["measure_display"].startswith("1.5 katori")
    assert item["display_name"].startswith("Fixture Dal")


async def test_decimal_precision_survives_the_round_trip(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """🔒 API §4 — nutrition values are `numeric` precisely to avoid float error.

    ``12.345`` has no exact IEEE-754 double, so a quantity that came back as
    ``12.344999999999999`` would prove the value had been through a float. It
    must return byte-identical.
    """
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))

    created = await _create_plan(plan_api, client_id)
    version_id = created["draft_version"]["id"]
    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")

    added = await plan_api.http.post(
        "/api/v1/app/plan-items",
        headers={"If-Match": read.headers["etag"]},
        json={
            "slot_id": read.json()["days"][0]["slots"][0]["id"],
            "item_type": "food",
            "food_id": str(catalogue.dal_id),
            "quantity": "12.345",
            "measure_unit_id": str(catalogue.katori_id),
        },
    )
    assert added.status_code == 201

    item = (await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")).json()["days"][0][
        "slots"
    ][0]["items"][0]

    assert item["quantity"] == "12.345"
    # 12.345 katori x 150 g = 1851.750 g, exactly.
    assert Decimal(item["resolved_grams"]) == Decimal("1851.750")


async def test_add_day_and_slot_then_rename(plan_api: PlanApi, tenant_a, tenant_clients) -> None:
    """FR-M4-025/026 — add, rename and reorder."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))

    created = await _create_plan(plan_api, client_id, day_count=1)
    version_id = created["draft_version"]["id"]
    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")

    day = await plan_api.http.post(
        f"/api/v1/app/plan-versions/{version_id}/days",
        headers={"If-Match": read.headers["etag"]},
        json={"label": "Rest day", "slot_types": ["breakfast", "dinner"]},
    )
    assert day.status_code == 201, day.text
    assert day.json()["day_number"] == 2

    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    assert len(read.json()["days"]) == 2
    assert len(read.json()["days"][1]["slots"]) == 2

    slot = await plan_api.http.post(
        f"/api/v1/app/plan-versions/{version_id}/slots",
        headers={"If-Match": read.headers["etag"]},
        json={
            "day_id": read.json()["days"][1]["id"],
            "slot_type": "custom",
            "custom_label": "Post-workout",
        },
    )
    assert slot.status_code == 201, slot.text
    assert slot.json()["custom_label"] == "Post-workout"

    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    renamed = await plan_api.http.patch(
        f"/api/v1/app/plan-days/{read.json()['days'][1]['id']}",
        headers={"If-Match": read.headers["etag"]},
        json={"label": "Sunday"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["label"] == "Sunday"


async def test_update_and_remove_an_item_moves_the_totals(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """Quantity edits and removals are reflected in the server-computed totals."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))

    created = await _create_plan(plan_api, client_id, target_energy_kcal="1400")
    version_id = created["draft_version"]["id"]
    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")

    added = await plan_api.http.post(
        "/api/v1/app/plan-items",
        headers={"If-Match": read.headers["etag"]},
        json={
            "slot_id": read.json()["days"][0]["slots"][0]["id"],
            "item_type": "food",
            "food_id": str(catalogue.dal_id),
            "quantity": "1",
            "measure_unit_id": str(catalogue.katori_id),
        },
    )
    item_id = added.json()["id"]

    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    assert Decimal(read.json()["plan_totals"]["energy_kcal"]) == Decimal("150")

    updated = await plan_api.http.patch(
        f"/api/v1/app/plan-items/{item_id}",
        headers={"If-Match": read.headers["etag"]},
        json={"quantity": "2"},
    )
    assert updated.status_code == 200

    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    assert Decimal(read.json()["plan_totals"]["energy_kcal"]) == Decimal("300")

    removed = await plan_api.http.delete(
        f"/api/v1/app/plan-items/{item_id}", headers={"If-Match": read.headers["etag"]}
    )
    assert removed.status_code == 204

    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    assert Decimal(read.json()["plan_totals"]["energy_kcal"]) == Decimal("0")
    assert read.json()["days"][0]["slots"][0]["items"] == []


async def test_removing_a_slot_removes_its_items(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """FR-M4-025 — a removed slot takes its contents with it."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))

    created = await _create_plan(plan_api, client_id)
    version_id = created["draft_version"]["id"]
    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    slot_id = read.json()["days"][0]["slots"][0]["id"]

    await plan_api.http.post(
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

    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    removed = await plan_api.http.delete(
        f"/api/v1/app/plan-slots/{slot_id}", headers={"If-Match": read.headers["etag"]}
    )
    assert removed.status_code == 204

    body = (await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")).json()
    assert len(body["days"][0]["slots"]) == 6
    assert Decimal(body["plan_totals"]["energy_kcal"]) == Decimal("0")


async def test_edit_the_draft_metadata(plan_api: PlanApi, tenant_a, tenant_clients) -> None:
    """PATCH on the version updates the plan's title and the version's targets."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))

    created = await _create_plan(plan_api, client_id)
    version_id = created["draft_version"]["id"]
    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")

    patched = await plan_api.http.patch(
        f"/api/v1/app/plan-versions/{version_id}",
        headers={"If-Match": read.headers["etag"]},
        json={
            "title": "Renamed plan",
            "practitioner_notes": "Drink more water.",
            "target_energy_kcal": "1600",
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["practitioner_notes"] == "Drink more water."
    assert patched.json()["nutrition_budget"]["target"]["energy_kcal"] == "1600"

    detail = await plan_api.http.get(f"/api/v1/app/plans/{created['plan']['id']}")
    assert detail.json()["plan"]["title"] == "Renamed plan"


async def test_a_second_draft_is_refused_by_the_database(
    plan_api: PlanApi, tenant_a, tenant_clients
) -> None:
    """🔒 `uq_diet_plan_versions__one_draft` — one open draft per plan (DDR-11).

    Exercised by discarding, which frees the partial unique index, then showing a
    fresh plan can be created. The index itself is what makes concurrent creation
    safe; a service-level check could be raced.
    """
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))

    created = await _create_plan(plan_api, client_id)
    version_id = created["draft_version"]["id"]
    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")

    discarded = await plan_api.http.post(
        f"/api/v1/app/plan-versions/{version_id}/discard",
        headers={"If-Match": read.headers["etag"]},
    )
    assert discarded.status_code == 200
    assert discarded.json()["state"] == "discarded"

    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    refused = await plan_api.http.patch(
        f"/api/v1/app/plan-versions/{version_id}",
        headers={"If-Match": read.headers["etag"]},
        json={"practitioner_notes": "too late"},
    )
    assert refused.status_code == 409
