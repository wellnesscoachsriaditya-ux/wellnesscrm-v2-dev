"""Draft is editable, issued is not — DDR-11, EC-M4-03, migration 0020.

🔒 **The point of this file is that the guarantee is enforced twice**, and that
the *database* half is proven independently of the application half.

``kernel.nutrition.assert_draft`` refuses to edit a non-draft version, and that
is what produces a 409 with a message a practitioner can act on. But application
code can be bypassed, refactored or forgotten. Migration 0020 therefore adds
``plan_items__delete_draft_only`` and ``plan_slots__delete_draft_only`` as
``RESTRICTIVE`` DELETE policies, so PostgreSQL itself refuses to delete a row
belonging to an issued version.

⚠️ A ``RESTRICTIVE`` policy that is written wrongly is a **silent no-op** —
PostgreSQL OR-s permissive policies together, so a policy accidentally created as
permissive would *widen* access while reading like a restriction. The tests below
therefore issue the ``DELETE`` directly as ``app_user``, with the service layer
out of the picture entirely. Anything less would be testing ``assert_draft``
twice and calling it defence in depth.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from tests.integration.conftest import scope_to
from tests.integration.nutrition.conftest import Catalogue, PlanApi, practitioner

pytestmark = pytest.mark.asyncio


async def _plan_with_one_item(
    api: PlanApi, client_id: uuid.UUID, catalogue: Catalogue
) -> tuple[str, str, str]:
    """Create a draft, put one food in it, and return (version_id, slot_id, item_id)."""
    created = await api.http.post(
        f"/api/v1/app/clients/{client_id}/plans",
        json={"title": "Immutability", "day_count": 1},
    )
    assert created.status_code == 201, created.text
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
    return version_id, slot_id, added.json()["id"]


async def _issue(api: PlanApi, version_id: str) -> None:
    read = await api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    issued = await api.http.post(
        f"/api/v1/app/plan-versions/{version_id}/issue",
        headers={"If-Match": read.headers["etag"]},
    )
    assert issued.status_code == 200, issued.text


# ─── The draft half ──────────────────────────────────────────────────────


async def test_a_draft_accepts_edits_and_deletes(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """The permitted case, asserted so the refusals below mean something."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))
    version_id, slot_id, item_id = await _plan_with_one_item(plan_api, client_id, catalogue)

    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    edited = await plan_api.http.patch(
        f"/api/v1/app/plan-items/{item_id}",
        headers={"If-Match": read.headers["etag"]},
        json={"quantity": "3"},
    )
    assert edited.status_code == 200

    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    removed = await plan_api.http.delete(
        f"/api/v1/app/plan-items/{item_id}", headers={"If-Match": read.headers["etag"]}
    )
    assert removed.status_code == 204

    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    slot_removed = await plan_api.http.delete(
        f"/api/v1/app/plan-slots/{slot_id}", headers={"If-Match": read.headers["etag"]}
    )
    assert slot_removed.status_code == 204


# ─── The issued half, through the application ────────────────────────────


async def test_an_issued_version_refuses_metadata_edits(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """🔒 EC-M4-03 — an issued plan is a clinical record, not an editable row."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))
    version_id, _, _ = await _plan_with_one_item(plan_api, client_id, catalogue)

    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    etag = read.headers["etag"]
    await _issue(plan_api, version_id)

    refused = await plan_api.http.patch(
        f"/api/v1/app/plan-versions/{version_id}",
        headers={"If-Match": etag},
        json={"practitioner_notes": "after the fact"},
    )
    assert refused.status_code == 409
    # API §5.1 — one envelope, and the machine-readable `type` is what a client
    # branches on. Asserted here so a change to the taxonomy is caught by a test
    # rather than by a frontend that silently stops recognising the conflict.
    assert refused.json()["error"]["type"] == "conflict"


async def test_an_issued_version_refuses_item_edits_and_deletes(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))
    version_id, slot_id, item_id = await _plan_with_one_item(plan_api, client_id, catalogue)

    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    etag = read.headers["etag"]
    await _issue(plan_api, version_id)

    for method, url in (
        ("patch", f"/api/v1/app/plan-items/{item_id}"),
        ("delete", f"/api/v1/app/plan-items/{item_id}"),
        ("delete", f"/api/v1/app/plan-slots/{slot_id}"),
    ):
        kwargs = {"headers": {"If-Match": etag}}
        if method == "patch":
            kwargs["json"] = {"quantity": "9"}
        response = await getattr(plan_api.http, method)(url, **kwargs)
        assert response.status_code == 409, f"{method.upper()} {url} -> {response.status_code}"


async def test_an_issued_version_cannot_be_discarded(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """Discard is a draft transition. An issued plan is superseded, never unsent."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))
    version_id, _, _ = await _plan_with_one_item(plan_api, client_id, catalogue)

    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    etag = read.headers["etag"]
    await _issue(plan_api, version_id)

    refused = await plan_api.http.post(
        f"/api/v1/app/plan-versions/{version_id}/discard", headers={"If-Match": etag}
    )
    assert refused.status_code == 409


async def test_an_issued_version_is_still_readable(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue
) -> None:
    """🔒 AC-M4-008 — immutable is not the same as inaccessible."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))
    version_id, _, _ = await _plan_with_one_item(plan_api, client_id, catalogue)
    await _issue(plan_api, version_id)

    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")

    assert read.status_code == 200
    assert read.json()["state"] == "issued"
    assert read.json()["days"][0]["slots"][0]["items"][0]["resolved_grams"] is not None


# ─── The issued half, at the database, with the service bypassed ─────────


async def _delete_directly(
    engine: AsyncEngine, tenant_id: uuid.UUID, table: str, row_id: str
) -> int:
    """Issue a raw DELETE as ``app_user``, with no application code involved.

    🔒 This is the whole point of the file. It proves the ``RESTRICTIVE`` policy
    refuses the row, rather than proving ``assert_draft`` was called.

    Returns:
        The number of rows deleted. ⚠️ Zero rather than an error is the correct
        outcome under RLS: the row is simply not visible to the statement.
    """
    async with async_sessionmaker(engine)() as session:
        await scope_to(await session.connection(), tenant_id)
        result = await session.execute(
            text(f"DELETE FROM {table} WHERE id = :id"),
            {"id": row_id},
        )
        await session.rollback()
        return int(result.rowcount)


async def test_database_refuses_to_delete_an_issued_plans_item(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue, app_engine: AsyncEngine
) -> None:
    """🔒 `plan_items__delete_draft_only` bites with `assert_draft` bypassed."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))
    version_id, _, item_id = await _plan_with_one_item(plan_api, client_id, catalogue)
    await _issue(plan_api, version_id)

    deleted = await _delete_directly(app_engine, tenant_a, "plan_items", item_id)

    assert deleted == 0, "the restrictive DELETE policy did not refuse an issued plan's item"


async def test_database_refuses_to_delete_an_issued_plans_slot(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue, app_engine: AsyncEngine
) -> None:
    """🔒 `plan_slots__delete_draft_only`, same argument."""
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))
    version_id, slot_id, _ = await _plan_with_one_item(plan_api, client_id, catalogue)
    await _issue(plan_api, version_id)

    deleted = await _delete_directly(app_engine, tenant_a, "plan_slots", slot_id)

    assert deleted == 0, "the restrictive DELETE policy did not refuse an issued plan's slot"


async def test_database_allows_deleting_a_draft_item(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue, app_engine: AsyncEngine
) -> None:
    """🔒 The negative control.

    Without this, the two tests above would pass just as happily if the grant
    were missing altogether — "zero rows deleted" would then mean "no privilege"
    rather than "policy refused", and the policy could be inert without anyone
    noticing.
    """
    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))
    _, _, item_id = await _plan_with_one_item(plan_api, client_id, catalogue)

    deleted = await _delete_directly(app_engine, tenant_a, "plan_items", item_id)

    assert deleted == 1, "app_user cannot delete a draft item — the DELETE grant is missing"


async def test_plan_days_remain_undeletable_by_the_application(
    plan_api: PlanApi, tenant_a, tenant_clients, catalogue: Catalogue, app_engine: AsyncEngine
) -> None:
    """🔒 Migration 0020 grants DELETE on `plan_items` and `plan_slots` only.

    ``plan_days`` keeps 0018's revoke because no requirement asks for day
    removal. A grant nobody needs is a privilege nobody audited, so its absence
    is asserted rather than assumed.
    """
    from sqlalchemy.exc import ProgrammingError

    client_id, user_id = tenant_clients[tenant_a]
    plan_api.as_actor(practitioner(tenant_a, user_id))
    version_id, _, _ = await _plan_with_one_item(plan_api, client_id, catalogue)

    read = await plan_api.http.get(f"/api/v1/app/plan-versions/{version_id}")
    day_id = read.json()["days"][0]["id"]

    with pytest.raises(ProgrammingError, match="permission denied"):
        await _delete_directly(app_engine, tenant_a, "plan_days", day_id)
