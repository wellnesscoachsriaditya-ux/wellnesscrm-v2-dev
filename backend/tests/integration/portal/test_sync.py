"""``POST /portal/sync`` — API §12.4's four guarantees, over HTTP.

🔒 The guarantees are the tests. §12.4 states them as prose; each one below is
the same sentence expressed as a request and a database read:

1. a single bad operation never fails the batch;
2. ``duplicate`` is a **success** state;
3. client logs are never discarded;
4. ``plan_changed`` tells the PWA to refresh rather than swap silently.

⚠️ **Dates come from the server, not from the test's clock.** The backdating
window is evaluated against *today in the practice's timezone* (Asia/Kolkata by
default), which is a different date from UTC for several hours a day. A test that
computed its own "today" would pass in the morning and fail in the evening, so
every date here is derived from ``GET /portal/today``'s own ``date`` field.

⚠️ ``client_timestamp`` is a **fixed instant**. Nothing validates it, and a
wall-clock value would make the payload differ between runs for no reason.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.modules.progress import BACKDATING_WINDOW_DAYS
from tests.integration.portal.conftest import (
    SLOT_TYPE,
    PortalApi,
    PortalClient,
    adherence_rows,
    anonymous_actor,
    client_actor,
    measurement_rows,
    operator_actor,
    practitioner_actor,
    set_client_stage,
    suspend_tenant,
)

pytestmark = pytest.mark.asyncio

SYNC = "/api/v1/portal/sync"
TODAY = "/api/v1/portal/today"

#: 🔒 Fixed, so a payload is identical between runs. Deliberately *not* "now":
#: a queued operation's client timestamp is whenever the client acted, which for
#: an offline queue is by definition not the moment of the request.
CLIENT_TIMESTAMP = "2026-08-14T07:30:00+05:30"


async def _server_today(api: PortalApi, fixture: PortalClient) -> date:
    """Today as the *server* reckons it, in the practice's timezone."""
    api.as_actor(client_actor(fixture))
    response = await api.http.get(TODAY)
    assert response.status_code == 200, response.text
    return date.fromisoformat(response.json()["date"])


def adherence_op(
    *,
    op_id: uuid.UUID | None = None,
    logged_for_date: date,
    adherence: str = "followed",
    slot_type: str = SLOT_TYPE,
    slot_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "logged_for_date": logged_for_date.isoformat(),
        "slot_type": slot_type,
        "adherence": adherence,
    }
    if slot_id is not None:
        payload["slot_id"] = str(slot_id)
    return {
        "op_id": str(op_id or uuid.uuid4()),
        "type": "adherence.log",
        "payload": payload,
        "client_timestamp": CLIENT_TIMESTAMP,
    }


def measurement_op(
    *,
    op_id: uuid.UUID | None = None,
    measured_on: date,
    weight_kg: str = "70.5",
) -> dict[str, Any]:
    return {
        "op_id": str(op_id or uuid.uuid4()),
        "type": "measurement.log",
        "payload": {"measured_on": measured_on.isoformat(), "weight_kg": weight_kg},
        "client_timestamp": CLIENT_TIMESTAMP,
    }


async def _sync(
    api: PortalApi,
    fixture: PortalClient,
    operations: list[dict[str, Any]],
    *,
    known_plan_hash: str | None = None,
) -> dict[str, Any]:
    api.as_actor(client_actor(fixture))
    body: dict[str, Any] = {"operations": operations}
    if known_plan_hash is not None:
        body["known_plan_hash"] = known_plan_hash
    response = await api.http.post(SYNC, json=body)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def _status(body: dict[str, Any], op_id: str) -> str:
    for result in body["results"]:
        if result["op_id"] == op_id:
            status: str = result["status"]
            return status
    raise AssertionError(f"no result for {op_id}: {body['results']}")


# ─── One operation ───────────────────────────────────────────────────────


async def test_a_single_adherence_operation_is_applied(
    portal_api: PortalApi, client_a: PortalClient, migrator_engine: AsyncEngine
) -> None:
    """The base case, and the one every guarantee below is measured against."""
    today = await _server_today(portal_api, client_a)
    operation = adherence_op(logged_for_date=today)

    body = await _sync(portal_api, client_a, [operation])

    assert body["results"] == [{"op_id": operation["op_id"], "status": "applied", "error": None}]

    rows = await adherence_rows(migrator_engine, fixture=client_a)
    assert len(rows) == 1
    assert rows[0]["adherence"] == "followed"
    assert rows[0]["slot_type"] == SLOT_TYPE
    assert rows[0]["logged_for_date"] == today
    # 🔒 op_id *is* the idempotency key (API §13.1).
    assert rows[0]["idempotency_key"] == operation["op_id"]
    # 🔒 Server-resolved, never taken from the payload.
    assert rows[0]["plan_version_id"] == client_a.version_id


async def test_both_timestamps_are_kept(
    portal_api: PortalApi, client_a: PortalClient, migrator_engine: AsyncEngine
) -> None:
    """🔒 API §12.3 — a log queued Tuesday and synced Thursday is *dated*
    Tuesday and *recorded* Thursday. Collapsing them corrupts adherence history.
    """
    today = await _server_today(portal_api, client_a)
    await _sync(portal_api, client_a, [adherence_op(logged_for_date=today)])

    row = (await adherence_rows(migrator_engine, fixture=client_a))[0]

    # ⚠️ Compared as an *instant*, not as a string. `timestamptz` normalises to
    # UTC on read, so the +05:30 the client sent comes back as +00:00 — the same
    # moment, spelled differently. Asserting on the text would be asserting on
    # PostgreSQL's output format rather than on what was stored.
    assert row["client_timestamp"] == datetime.fromisoformat(CLIENT_TIMESTAMP)
    assert row["server_recorded_at"] > row["client_timestamp"]


async def test_a_measurement_operation_is_applied_as_the_client(
    portal_api: PortalApi, client_a: PortalClient, migrator_engine: AsyncEngine
) -> None:
    """🔒 EC-M3-05 — the source column is what keeps a client's weight and a
    practitioner's for the same date apart. A queue must never claim to be the
    practitioner's reading."""
    today = await _server_today(portal_api, client_a)
    operation = measurement_op(measured_on=today, weight_kg="70.5")

    body = await _sync(portal_api, client_a, [operation])
    assert _status(body, operation["op_id"]) == "applied"

    rows = await measurement_rows(migrator_engine, fixture=client_a)
    assert len(rows) == 1
    assert rows[0]["source"] == "client"
    assert rows[0]["recorded_by_user_id"] is None
    assert rows[0]["weight_kg"] == Decimal("70.5")
    assert rows[0]["idempotency_key"] == operation["op_id"]


# ─── Batches ─────────────────────────────────────────────────────────────


async def test_multiple_valid_operations_all_apply(
    portal_api: PortalApi, client_a: PortalClient, migrator_engine: AsyncEngine
) -> None:
    today = await _server_today(portal_api, client_a)
    operations = [
        adherence_op(logged_for_date=today, slot_type="breakfast"),
        adherence_op(logged_for_date=today, slot_type="lunch"),
        adherence_op(logged_for_date=today - timedelta(days=1), slot_type="dinner"),
        measurement_op(measured_on=today),
    ]

    body = await _sync(portal_api, client_a, operations)

    assert [result["status"] for result in body["results"]] == ["applied"] * 4
    assert len(await adherence_rows(migrator_engine, fixture=client_a)) == 3
    assert len(await measurement_rows(migrator_engine, fixture=client_a)) == 1


async def test_an_empty_batch_is_valid_and_still_answers_the_plan_question(
    portal_api: PortalApi, client_a: PortalClient
) -> None:
    """A PWA coming online with nothing queued still needs to know whether its
    cached plan is current. Refusing that would cost a second round trip on the
    connection §12.2 exists to protect."""
    body = await _sync(portal_api, client_a, [])

    assert body["results"] == []
    assert body["current_plan_hash"] == client_a.content_hash


async def test_an_oversized_batch_is_refused_as_a_whole(
    portal_api: PortalApi, client_a: PortalClient
) -> None:
    """🟡 API §12.4's proposed cap of 100 — "a larger queue syncs in pages".

    ⚠️ The one failure that legitimately fails the batch: there is nothing to
    apply until the client splits it, so this is a 422 on the envelope rather
    than 101 rejections.
    """
    today = await _server_today(portal_api, client_a)
    operations = [adherence_op(logged_for_date=today) for _ in range(101)]

    portal_api.as_actor(client_actor(client_a))
    response = await portal_api.http.post(SYNC, json={"operations": operations})

    assert response.status_code == 422


# ─── Guarantee 2 — duplicate is success ──────────────────────────────────


async def test_a_replayed_operation_is_a_duplicate_not_an_error(
    portal_api: PortalApi, client_a: PortalClient, migrator_engine: AsyncEngine
) -> None:
    """🔒 Guarantee 2. Replaying a queue is expected behaviour for a PWA that
    reconnects; reporting it as a failure would make the client retry forever."""
    today = await _server_today(portal_api, client_a)
    operation = adherence_op(logged_for_date=today)

    first = await _sync(portal_api, client_a, [operation])
    second = await _sync(portal_api, client_a, [operation])

    assert _status(first, operation["op_id"]) == "applied"
    assert _status(second, operation["op_id"]) == "duplicate"
    # 🔒 And no second row. The unique index is the arbiter, not a prior read.
    assert len(await adherence_rows(migrator_engine, fixture=client_a)) == 1


async def test_a_replayed_measurement_creates_no_second_row(
    portal_api: PortalApi, client_a: PortalClient, migrator_engine: AsyncEngine
) -> None:
    """🔒 The constraint added in ef3c636, doing its job.

    ⚠️ Worth its own test rather than folding into the adherence case: the two
    tables enforce uniqueness differently — a UNIQUE constraint on
    ``adherence_logs`` and a *partial* unique index on ``measurements``, which
    only covers rows that carry a key at all.
    """
    today = await _server_today(portal_api, client_a)
    operation = measurement_op(measured_on=today)

    await _sync(portal_api, client_a, [operation])
    second = await _sync(portal_api, client_a, [operation])

    assert _status(second, operation["op_id"]) == "duplicate"
    assert len(await measurement_rows(migrator_engine, fixture=client_a)) == 1


async def test_a_duplicate_inside_one_batch_is_reported_per_operation(
    portal_api: PortalApi, client_a: PortalClient, migrator_engine: AsyncEngine
) -> None:
    """The same op_id twice in one request — a queue that double-enqueued."""
    today = await _server_today(portal_api, client_a)
    op_id = uuid.uuid4()
    operations = [
        adherence_op(op_id=op_id, logged_for_date=today),
        adherence_op(op_id=op_id, logged_for_date=today),
    ]

    body = await _sync(portal_api, client_a, operations)

    assert [result["status"] for result in body["results"]] == ["applied", "duplicate"]
    assert len(await adherence_rows(migrator_engine, fixture=client_a)) == 1


async def test_a_new_operation_alongside_a_duplicate_still_applies(
    portal_api: PortalApi, client_a: PortalClient, migrator_engine: AsyncEngine
) -> None:
    """🔒 The realistic replay: a queue resent with one new entry appended."""
    today = await _server_today(portal_api, client_a)
    already = adherence_op(logged_for_date=today, slot_type="breakfast")
    await _sync(portal_api, client_a, [already])

    fresh = adherence_op(logged_for_date=today, slot_type="lunch")
    body = await _sync(portal_api, client_a, [already, fresh])

    assert _status(body, already["op_id"]) == "duplicate"
    assert _status(body, fresh["op_id"]) == "applied"
    assert len(await adherence_rows(migrator_engine, fixture=client_a)) == 2


# ─── Guarantee 1 — one bad operation never fails the batch ───────────────


async def test_an_operation_outside_the_backdating_window_is_rejected_alone(
    portal_api: PortalApi, client_a: PortalClient, migrator_engine: AsyncEngine
) -> None:
    """🔒 EC-M7-04, and 🔒 guarantee 1 in the same request."""
    today = await _server_today(portal_api, client_a)
    stale = adherence_op(logged_for_date=today - timedelta(days=BACKDATING_WINDOW_DAYS + 1))
    good = adherence_op(logged_for_date=today, slot_type="lunch")

    body = await _sync(portal_api, client_a, [stale, good])

    assert _status(body, stale["op_id"]) == "rejected"
    assert _status(body, good["op_id"]) == "applied"
    assert len(await adherence_rows(migrator_engine, fixture=client_a)) == 1


async def test_a_malformed_payload_never_fails_the_batch(
    portal_api: PortalApi, client_a: PortalClient, migrator_engine: AsyncEngine
) -> None:
    """🔒 Guarantee 1, at the parsing layer.

    ⚠️ This is why ``SyncOperation.payload`` is an untyped object. A typed union
    would make FastAPI answer 422 for the whole request, and a week of valid
    queued logs would be discarded because one of them was garbage.
    """
    today = await _server_today(portal_api, client_a)
    malformed = {
        "op_id": str(uuid.uuid4()),
        "type": "adherence.log",
        "payload": {"logged_for_date": "not-a-date", "slot_type": "breakfast"},
        "client_timestamp": CLIENT_TIMESTAMP,
    }
    good = adherence_op(logged_for_date=today)

    body = await _sync(portal_api, client_a, [malformed, good])

    assert _status(body, malformed["op_id"]) == "rejected"
    assert _status(body, good["op_id"]) == "applied"
    assert len(await adherence_rows(migrator_engine, fixture=client_a)) == 1


async def test_an_unknown_slot_type_is_rejected_alone(
    portal_api: PortalApi, client_a: PortalClient
) -> None:
    """The vocabulary is FR-M4-025's, validated against the same enum the plan
    builder uses — a client cannot invent a slot."""
    today = await _server_today(portal_api, client_a)
    invented = adherence_op(logged_for_date=today, slot_type="second_breakfast")

    body = await _sync(portal_api, client_a, [invented])

    assert _status(body, invented["op_id"]) == "rejected"


async def test_an_unsupported_operation_type_is_rejected_alone(
    portal_api: PortalApi, client_a: PortalClient
) -> None:
    """🔒 A newer PWA queuing a type this deployment has never heard of must get
    a per-operation answer, not a 500 that discards the batch around it."""
    today = await _server_today(portal_api, client_a)
    unknown = {
        "op_id": str(uuid.uuid4()),
        "type": "assessment.answer",
        "payload": {},
        "client_timestamp": CLIENT_TIMESTAMP,
    }
    good = adherence_op(logged_for_date=today)

    body = await _sync(portal_api, client_a, [unknown, good])

    assert _status(body, unknown["op_id"]) == "rejected"
    assert _status(body, good["op_id"]) == "applied"


async def test_a_rejected_operation_leaves_the_transaction_usable(
    portal_api: PortalApi, client_a: PortalClient, migrator_engine: AsyncEngine
) -> None:
    """🔒 **The savepoint test.** A constraint violation aborts a PostgreSQL
    transaction, and catching the Python exception does not clear it — every
    later statement then fails with "current transaction is aborted".

    So the batch below puts a *replay* (which reaches the database and fails)
    ahead of three valid operations. Without a per-operation ``SAVEPOINT`` they
    would all be lost, and the assertion that they applied is what proves one is
    in place.
    """
    today = await _server_today(portal_api, client_a)
    already = adherence_op(logged_for_date=today, slot_type="breakfast")
    await _sync(portal_api, client_a, [already])

    body = await _sync(
        portal_api,
        client_a,
        [
            already,
            adherence_op(logged_for_date=today, slot_type="lunch"),
            adherence_op(logged_for_date=today, slot_type="dinner"),
            measurement_op(measured_on=today),
        ],
    )

    assert [result["status"] for result in body["results"]] == [
        "duplicate",
        "applied",
        "applied",
        "applied",
    ]
    assert len(await adherence_rows(migrator_engine, fixture=client_a)) == 3
    assert len(await measurement_rows(migrator_engine, fixture=client_a)) == 1


# ─── Guarantee 4 — plan_changed ──────────────────────────────────────────


async def test_a_matching_hash_reports_no_change(
    portal_api: PortalApi, client_a: PortalClient
) -> None:
    body = await _sync(portal_api, client_a, [], known_plan_hash=client_a.content_hash)

    assert body["plan_changed"] is False
    assert body["current_plan_hash"] == client_a.content_hash


async def test_a_stale_hash_asks_the_client_to_refresh(
    portal_api: PortalApi, client_a: PortalClient
) -> None:
    """🔒 EC-M7-03 — the PWA is told to refresh rather than having content
    swapped under the reader."""
    body = await _sync(portal_api, client_a, [], known_plan_hash="sha256:something-older")

    assert body["plan_changed"] is True
    assert body["current_plan_hash"] == client_a.content_hash


async def test_a_client_with_no_cached_plan_is_not_told_it_changed(
    portal_api: PortalApi, client_a: PortalClient
) -> None:
    """A fresh install has nothing to invalidate; a spurious ``true`` would make
    it refetch a plan it is already about to fetch."""
    body = await _sync(portal_api, client_a, [])

    assert body["plan_changed"] is False


# ─── Degradation — API §12.6 ─────────────────────────────────────────────


async def test_a_paused_client_has_every_operation_rejected(
    portal_api: PortalApi, client_a: PortalClient, migrator_engine: AsyncEngine
) -> None:
    """🔒 EC-M7-06 — the capabilities object said ``can_log_adherence: false``,
    and the endpoint agrees rather than trusting the UI to have read it."""
    today = await _server_today(portal_api, client_a)
    await set_client_stage(migrator_engine, fixture=client_a, stage="paused")

    body = await _sync(portal_api, client_a, [adherence_op(logged_for_date=today)])

    assert body["results"][0]["status"] == "rejected"
    assert len(await adherence_rows(migrator_engine, fixture=client_a)) == 0


async def test_a_suspended_tenant_rejects_without_naming_a_reason(
    portal_api: PortalApi, client_a: PortalClient, migrator_engine: AsyncEngine
) -> None:
    """🔒 EC-M7-08 — **200, not 403.** A different status code for a suspended
    practice would let a client infer their practitioner's account state from
    the response line alone, which is what the neutral message exists to
    prevent. The error code is the same one a paused client gets.
    """
    today = await _server_today(portal_api, client_a)
    await suspend_tenant(migrator_engine, tenant_id=client_a.tenant_id)

    portal_api.as_actor(client_actor(client_a))
    response = await portal_api.http.post(
        SYNC, json={"operations": [adherence_op(logged_for_date=today)]}
    )

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["status"] == "rejected"
    assert result["error"] == "portal_read_only"
    for forbidden in ("suspend", "billing", "payment", "overdue", "subscription"):
        assert forbidden not in response.text.lower()


# ─── Realm ───────────────────────────────────────────────────────────────


async def test_an_anonymous_caller_cannot_sync(portal_api: PortalApi) -> None:
    """⚠️ The actor is set explicitly rather than left unset. Relying on the
    harness's default would make this assertion depend on nothing else in the
    test having signed in first."""
    portal_api.as_actor(anonymous_actor())
    response = await portal_api.http.post(SYNC, json={"operations": []})
    assert response.status_code == 401


async def test_practitioner_and_operator_tokens_are_refused_identically(
    portal_api: PortalApi, client_a: PortalClient
) -> None:
    """🔒 FR-M0-004 and NFR-043 together — refused, and indistinguishably so."""
    portal_api.as_actor(practitioner_actor(client_a))
    as_practitioner = await portal_api.http.post(SYNC, json={"operations": []})

    portal_api.as_actor(operator_actor())
    as_operator = await portal_api.http.post(SYNC, json={"operations": []})

    assert as_practitioner.status_code == 403
    assert as_operator.status_code == 403
    assert as_practitioner.json()["error"]["type"] == as_operator.json()["error"]["type"]
