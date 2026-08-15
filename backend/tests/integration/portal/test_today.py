"""``GET /portal/today`` — API §12.2, §12.6.

Every test here goes over HTTP against the real application and a real
PostgreSQL, because the three things most worth proving about this endpoint are
all properties of the whole request:

* 🔒 that a client sees **their own** plan and no one else's, including another
  client of the same practitioner (Pattern C, migration 0025);
* 🔒 that nutrition figures are **absent from the payload** unless the
  practitioner enabled them, rather than present and null;
* 🔒 that degradation answers **200 with a neutral message** and never leaks the
  practitioner's account state (EC-M7-08).

A service-level test could not show any of them: the first is enforced by the
database, the second by serialisation, and the third by the status code.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.conftest import scope_to
from tests.integration.portal.conftest import (
    CLIENT_NOTE,
    EXPECTED_QUANTITY_DISPLAY_SUFFIX,
    PRACTITIONER_NOTE,
    SLOT_TYPE,
    PortalApi,
    PortalClient,
    archive_client,
    client_actor,
    operator_actor,
    practitioner_actor,
    record_weight,
    set_client_stage,
    set_nutrition_visibility,
    suspend_tenant,
)

pytestmark = pytest.mark.asyncio

TODAY = "/api/v1/portal/today"


async def _today(api: PortalApi, fixture: PortalClient) -> dict[str, Any]:
    api.as_actor(client_actor(fixture))
    response = await api.http.get(TODAY)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# ─── The aggregate ───────────────────────────────────────────────────────


async def test_a_client_sees_their_own_plan(portal_api: PortalApi, client_a: PortalClient) -> None:
    """The landing view, in one round trip — ADR-A09."""
    body = await _today(portal_api, client_a)

    assert body["plan"]["version_id"] == str(client_a.version_id)
    assert body["plan"]["content_hash"] == client_a.content_hash
    assert body["plan"]["day_number"] == 1

    slot = body["plan"]["slots"][0]
    assert slot["slot_id"] == str(client_a.slot_id)
    assert slot["slot_type"] == SLOT_TYPE
    assert slot["label"] == "Breakfast"
    assert slot["adherence"] == {"logged": False, "value": None}

    item = slot["items"][0]
    assert item["display_name"] == client_a.food_name
    assert item["quantity_display"].endswith(EXPECTED_QUANTITY_DISPLAY_SUFFIX)
    assert item["note"] == f"{CLIENT_NOTE} {client_a.client_id}"

    assert body["practitioner"]["name"]
    assert body["capabilities"] == {
        "can_log_adherence": True,
        "can_log_measurements": True,
        "can_upload": True,
        "can_view_plan": True,
    }
    assert body["notice"] is None
    assert body["next_appointment"] is None


async def test_the_practitioners_own_note_is_never_sent(
    portal_api: PortalApi, client_a: PortalClient
) -> None:
    """🔒 API §8.3's ``notes`` is the practitioner's working annotation.

    The portal carries ``client_note`` and nothing else. A payload containing the
    practitioner's private text would be a disclosure no UI decision could undo,
    because the bytes have already left the server.
    """
    body = await _today(portal_api, client_a)
    assert PRACTITIONER_NOTE not in str(body)


async def test_no_issued_plan_is_an_empty_state_not_an_error(
    portal_api: PortalApi, client_a: PortalClient, migrator_engine: AsyncEngine
) -> None:
    """🔒 EC-M7-02 — 200 with ``plan: null``, and the key is present."""
    async with migrator_engine.begin() as connection:
        await scope_to(connection, client_a.tenant_id)
        await connection.execute(
            text("UPDATE diet_plan_versions SET state = 'draft' WHERE id = :v"),
            {"v": client_a.version_id},
        )

    body = await _today(portal_api, client_a)
    assert "plan" in body
    assert body["plan"] is None
    assert body["capabilities"]["can_view_plan"] is True


# ─── Nutrition visibility ────────────────────────────────────────────────


async def test_nutrition_is_absent_from_the_payload_by_default(
    portal_api: PortalApi, client_a: PortalClient
) -> None:
    """🔒 **Absent, not null.** S6's backend tasks: "nutrition fields omitted
    entirely unless the per-client flag is set".

    ⚠️ Asserted on the key rather than on the value. ``"nutrition": null`` would
    satisfy a value assertion while still telling a reader of the payload that
    figures exist and are being withheld — and a future serialiser change could
    turn the null back into a number without this test noticing.
    """
    body = await _today(portal_api, client_a)
    item = body["plan"]["slots"][0]["items"][0]
    assert "nutrition" not in item
    assert "energy_kcal" not in str(body)


async def test_nutrition_appears_once_the_practitioner_enables_it(
    portal_api: PortalApi, client_a: PortalClient, migrator_engine: AsyncEngine
) -> None:
    """🟡 The practitioner's call, per client (approved refinement)."""
    await set_nutrition_visibility(migrator_engine, fixture=client_a, visible=True)

    body = await _today(portal_api, client_a)
    nutrition = body["plan"]["slots"][0]["items"][0]["nutrition"]

    # 🔒 300 g of a 100 kcal/100 g food. Carried as a *string* (API §4) — a JSON
    # number would be an IEEE-754 double, and a silently rounded clinical figure
    # is a defect rather than a rounding artefact.
    #
    # ⚠️ Compared as a Decimal rather than byte-for-byte: PostgreSQL preserves an
    # unconstrained `numeric`'s scale, so the same value can arrive as "300" or
    # "300.00" depending on how the fixture wrote it. The type is what this test
    # is about, and the type is asserted separately.
    assert isinstance(nutrition["energy_kcal"], str)
    assert isinstance(nutrition["protein_g"], str)
    assert Decimal(nutrition["energy_kcal"]) == Decimal("300")
    assert Decimal(nutrition["protein_g"]) == Decimal("18")


async def test_visibility_is_per_client_not_per_tenant(
    portal_api: PortalApi,
    client_a: PortalClient,
    client_a2: PortalClient,
    migrator_engine: AsyncEngine,
) -> None:
    """Enabling figures for one client must not enable them for their neighbour."""
    await set_nutrition_visibility(migrator_engine, fixture=client_a, visible=True)

    assert "nutrition" in (await _today(portal_api, client_a))["plan"]["slots"][0]["items"][0]
    assert "nutrition" not in (await _today(portal_api, client_a2))["plan"]["slots"][0]["items"][0]


# ─── Isolation ───────────────────────────────────────────────────────────


async def test_two_clients_of_one_practitioner_see_different_plans(
    portal_api: PortalApi, client_a: PortalClient, client_a2: PortalClient
) -> None:
    """🔒 **The Pattern C boundary.** Same tenant, same practitioner, two clients.

    The tenant predicate is satisfied for both, so nothing Pattern A does
    separates them. What separates them is ``client_id = portal_client_id()``.
    """
    first = await _today(portal_api, client_a)
    second = await _today(portal_api, client_a2)

    assert first["plan"]["version_id"] == str(client_a.version_id)
    assert second["plan"]["version_id"] == str(client_a2.version_id)

    # 🔒 The neighbour's note is the visible evidence a leak would leave.
    assert str(client_a2.client_id) not in str(first)
    assert str(client_a.client_id) not in str(second)


async def test_a_client_cannot_reach_another_tenants_data(
    portal_api: PortalApi, client_a: PortalClient, client_b: PortalClient
) -> None:
    """🔒 The Pattern A boundary, re-asserted on the client realm."""
    body = await _today(portal_api, client_a)

    assert body["plan"]["version_id"] != str(client_b.version_id)
    assert str(client_b.client_id) not in str(body)
    assert body["practitioner"]["name"] != ""


async def test_a_practitioner_token_is_refused_at_the_portal(
    portal_api: PortalApi, client_a: PortalClient
) -> None:
    """🔒 FR-M0-004 — a valid token at the wrong realm's surface.

    ⚠️ 403 rather than 401: the caller *is* signed in, just not here.

    🔒 The refusal must be **indistinguishable across realms** (NFR-043), so the
    assertion compares a practitioner's answer with an operator's rather than
    grepping for a word. A body that differed by realm would tell a prober which
    token they are holding, and grepping would not catch a difference in the
    error *type* or the action text.
    """
    portal_api.as_actor(practitioner_actor(client_a))
    as_practitioner = await portal_api.http.get(TODAY)

    portal_api.as_actor(operator_actor())
    as_operator = await portal_api.http.get(TODAY)

    assert as_practitioner.status_code == 403
    assert as_operator.status_code == 403
    assert _without_request_id(as_practitioner.json()) == _without_request_id(as_operator.json())
    assert "realm" not in as_practitioner.text.lower()


def _without_request_id(body: dict[str, Any]) -> dict[str, Any]:
    """The error envelope minus the one field that is unique per request."""
    error = {key: value for key, value in body["error"].items() if key != "request_id"}
    return {"error": error}


async def test_an_anonymous_caller_is_refused(portal_api: PortalApi) -> None:
    """Deny by default — the portal is not a public surface."""
    response = await portal_api.http.get(TODAY)
    assert response.status_code == 401


# ─── Prompts and the teaser ──────────────────────────────────────────────


async def test_weight_is_due_when_none_has_ever_been_recorded(
    portal_api: PortalApi, client_a: PortalClient
) -> None:
    body = await _today(portal_api, client_a)
    assert body["prompts"]["weight_due"] is True
    assert body["prompts"]["assessment_pending_id"] is None
    assert body["progress_teaser"] is None


async def test_a_recent_weight_clears_the_prompt(
    portal_api: PortalApi, client_a: PortalClient, migrator_engine: AsyncEngine
) -> None:
    await record_weight(
        migrator_engine,
        fixture=client_a,
        weight_kg=Decimal("70.0"),
        measured_on=_days_ago(1),
    )

    body = await _today(portal_api, client_a)
    assert body["prompts"]["weight_due"] is False


async def test_the_teaser_reports_the_change_between_two_readings(
    portal_api: PortalApi, client_a: PortalClient, migrator_engine: AsyncEngine
) -> None:
    """A loss is negative, and the figure is a string (API §4)."""
    await record_weight(
        migrator_engine, fixture=client_a, weight_kg=Decimal("72.0"), measured_on=_days_ago(10)
    )
    await record_weight(
        migrator_engine, fixture=client_a, weight_kg=Decimal("70.8"), measured_on=_days_ago(0)
    )

    teaser = (await _today(portal_api, client_a))["progress_teaser"]
    assert Decimal(teaser["weight_change_kg"]) == Decimal("-1.2")
    assert teaser["period_days"] == 10


async def test_another_clients_weight_never_reaches_the_teaser(
    portal_api: PortalApi,
    client_a: PortalClient,
    client_a2: PortalClient,
    migrator_engine: AsyncEngine,
) -> None:
    """🔒 Pattern C on ``measurements`` — the same boundary, a different table."""
    await record_weight(
        migrator_engine, fixture=client_a2, weight_kg=Decimal("95.0"), measured_on=_days_ago(5)
    )
    await record_weight(
        migrator_engine, fixture=client_a2, weight_kg=Decimal("90.0"), measured_on=_days_ago(0)
    )

    body = await _today(portal_api, client_a)
    assert body["progress_teaser"] is None
    assert body["prompts"]["weight_due"] is True


# ─── Degradation — API §12.6 ─────────────────────────────────────────────


async def test_a_paused_client_is_read_only(
    portal_api: PortalApi, client_a: PortalClient, migrator_engine: AsyncEngine
) -> None:
    """🔒 EC-M7-06 — 200, read-only, neutral message, plan still visible."""
    await set_client_stage(migrator_engine, fixture=client_a, stage="paused")

    body = await _today(portal_api, client_a)
    assert body["capabilities"] == {
        "can_log_adherence": False,
        "can_log_measurements": False,
        "can_upload": False,
        "can_view_plan": True,
    }
    assert body["notice"]
    assert body["plan"] is not None


async def test_a_suspended_tenant_never_explains_itself(
    portal_api: PortalApi, client_a: PortalClient, migrator_engine: AsyncEngine
) -> None:
    """🔒 EC-M7-08 — the client must not be able to infer a billing state.

    ⚠️ The assertion is on the *absence* of vocabulary, not only on the status
    code. A neutral 200 carrying the word "payment" would leak exactly what the
    neutral status code exists to hide.
    """
    await suspend_tenant(migrator_engine, tenant_id=client_a.tenant_id)

    body = await _today(portal_api, client_a)
    assert body["capabilities"]["can_log_adherence"] is False
    assert body["capabilities"]["can_view_plan"] is True

    lowered = body["notice"].lower()
    for forbidden in ("suspend", "billing", "payment", "invoice", "overdue", "subscription"):
        assert forbidden not in lowered


async def test_an_archived_client_loses_the_portal(
    portal_api: PortalApi, client_a: PortalClient, migrator_engine: AsyncEngine
) -> None:
    """A soft-deleted client is no longer engaged; their session ends with a
    self-service next step rather than a permission error."""
    await archive_client(migrator_engine, fixture=client_a)

    portal_api.as_actor(client_actor(client_a))
    response = await portal_api.http.get(TODAY)

    assert response.status_code == 401


def _days_ago(count: int) -> date:
    return (datetime.now(UTC) - timedelta(days=count)).date()
