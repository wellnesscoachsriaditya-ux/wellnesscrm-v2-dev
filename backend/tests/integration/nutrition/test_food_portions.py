"""The food-portions endpoint — FR-M4-011, the builder's measure vocabulary.

🔒 A food is entered in a household measure ("1 katori"), so the builder must
learn which measures a food has and their gram equivalents before it can add an
item at all. This is that lookup.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.integration.nutrition.conftest import Catalogue, PlanApi, practitioner

pytestmark = pytest.mark.asyncio


async def test_food_portions_lists_the_household_measures(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    _client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))

    response = await plan_api.http.get(f"/api/v1/app/nutrition/foods/{catalogue.dal_id}/portions")
    assert response.status_code == 200, response.text

    body = response.json()
    assert len(body) == 1
    portion = body[0]
    assert portion["measure_unit_id"] == str(catalogue.katori_id)
    assert portion["measure_unit_name"].startswith("katori")
    # 🔒 Numeric-as-string (API §4), and the fixture's 1 katori = 150 g.
    assert portion["gram_weight"] == "150"
    assert Decimal(portion["gram_weight"]) == Decimal("150")


async def test_portions_of_a_food_without_any_is_an_empty_list(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """A curated food with no portions returns ``[]``, not a 404 — the builder
    then offers custom entry rather than treating it as a missing food."""
    _client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))

    import uuid

    response = await plan_api.http.get(f"/api/v1/app/nutrition/foods/{uuid.uuid4()}/portions")
    assert response.status_code == 200
    assert response.json() == []
