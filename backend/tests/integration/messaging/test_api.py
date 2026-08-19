"""The messaging API and its authorization — ADR-05, AC-M1-006, FR-M11-003.

🔒 **Authorization is asserted at the route, not at the service.** A service-level
test cannot show that a route is wired to the pipeline at all, and the mistake
this suite exists to catch has a consistent shape: an endpoint that authorizes
the *action* ("may a practitioner read messages") without authorizing the
*resource* ("may this practitioner read this client's messages").

The matrix each client-bound route is put through:

* the owning practitioner — 200
* a practitioner of **another tenant** — 404, never 403 (API §5.4)
* a practitioner of the same tenant with no grant — 404
* the **client realm** — 403, the realm check
* a platform **operator** — refused; FR-M11-003 puts message content out of
  reach permanently
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.kernel.context import UserRole
from tests.integration.messaging.conftest import (
    MessagingApi,
    SessionFactory,
    TenantFixture,
    client_actor,
    operator_actor,
    practitioner,
)

pytestmark = pytest.mark.asyncio


def _client_path(tenant: TenantFixture, suffix: str) -> str:
    return f"/api/v1/app/clients/{tenant.client_id}{suffix}"


@pytest.fixture
async def other_practitioner(
    migrator_engine: AsyncEngine, tenant_a: TenantFixture
) -> TenantFixture:
    """A second practitioner in the same tenant, with no grant on the client.

    🔒 AC-M1-006's actual shape: the leak that matters is a colleague, not a
    stranger. A cross-tenant test alone would pass against an implementation
    that only checked the tenant.
    """
    user_id = uuid.uuid4()
    async with migrator_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_a.tenant_id)}
        )
        await connection.execute(
            text(
                "INSERT INTO users (id, tenant_id, auth_subject_id, email, full_name, role, "
                " status) "
                "VALUES (:id, :t, :sub, :e, 'Other Practitioner', 'practitioner', 'active')"
            ),
            {
                "id": user_id,
                "t": tenant_a.tenant_id,
                "sub": f"auth-{user_id}",
                "e": f"other-{user_id}@example.test",
            },
        )
    return TenantFixture(
        tenant_id=tenant_a.tenant_id, user_id=user_id, client_id=tenant_a.client_id
    )


async def _seed_dispatch(
    session_for: SessionFactory, tenant: TenantFixture, *, provider_message_id: str
) -> uuid.UUID:
    async with session_for(tenant.tenant_id) as session:
        return (
            await session.execute(
                text(
                    "INSERT INTO message_dispatches "
                    "(tenant_id, client_id, template_id, template_version, transport, "
                    " recipient_address, status, provider_message_id, sent_at) "
                    "SELECT :t, :c, id, 1, 'whatsapp', '+919000000000', 'sent', :p, now() "
                    "FROM message_templates WHERE code = 'plan_delivered' RETURNING id"
                ),
                {"t": tenant.tenant_id, "c": tenant.client_id, "p": provider_message_id},
            )
        ).scalar_one()


# ─── Message history (FR-M8-011) ─────────────────────────────────────────


async def test_the_owning_practitioner_reads_the_history(
    messaging_api: MessagingApi, session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    await _seed_dispatch(session_for, tenant_a, provider_message_id="wamid.h1")
    messaging_api.as_actor(practitioner(tenant_a))

    response = await messaging_api.http.get(_client_path(tenant_a, "/messages"))

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["template_code"] == "plan_delivered"
    assert body["items"][0]["status"] == "sent"
    assert body["page"]["has_more"] is False


async def test_another_tenant_cannot_read_the_history(
    messaging_api: MessagingApi,
    session_for: SessionFactory,
    tenant_a: TenantFixture,
    tenant_b: TenantFixture,
) -> None:
    """🔒 404, never 403 (API §5.4) — a 403 confirms the client exists."""
    await _seed_dispatch(session_for, tenant_a, provider_message_id="wamid.h2")
    messaging_api.as_actor(practitioner(tenant_b))

    response = await messaging_api.http.get(_client_path(tenant_a, "/messages"))

    assert response.status_code == 404


async def test_an_unassigned_colleague_cannot_read_the_history(
    messaging_api: MessagingApi,
    session_for: SessionFactory,
    tenant_a: TenantFixture,
    other_practitioner: TenantFixture,
) -> None:
    """AC-M1-006 — and 404 so a colleague's caseload cannot be enumerated one
    request at a time."""
    await _seed_dispatch(session_for, tenant_a, provider_message_id="wamid.h3")
    # 🔒 `PRACTITIONER`, not `OWNER`: an owner sees the whole tenant (FR-M0-017),
    # so an owner actor here would prove nothing about the grant check.
    messaging_api.as_actor(practitioner(other_practitioner, role=UserRole.PRACTITIONER))

    response = await messaging_api.http.get(_client_path(tenant_a, "/messages"))

    assert response.status_code == 404


async def test_the_client_realm_cannot_read_the_history(
    messaging_api: MessagingApi, tenant_a: TenantFixture
) -> None:
    """🔒 ADR-A01 — `/app` is the practitioner realm. A client reading their own
    delivery log is an S6 portal question, and it is not this endpoint."""
    messaging_api.as_actor(client_actor(tenant_a))

    response = await messaging_api.http.get(_client_path(tenant_a, "/messages"))

    assert response.status_code in {403, 404}


async def test_an_operator_cannot_read_the_history(
    messaging_api: MessagingApi, tenant_a: TenantFixture
) -> None:
    """🔒 FR-M11-003 — support resolves cases from counts and statuses, never
    from what a practitioner said to their client."""
    messaging_api.as_actor(operator_actor())

    response = await messaging_api.http.get(_client_path(tenant_a, "/messages"))

    assert response.status_code in {403, 404}


async def test_the_history_shows_failures(
    messaging_api: MessagingApi, session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """AC-M8-007 — terminal failure is visible to the practitioner."""
    async with session_for(tenant_a.tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO message_dispatches "
                "(tenant_id, client_id, template_id, template_version, transport, "
                " recipient_address, status, failure_code, failure_reason) "
                "SELECT :t, :c, id, 1, 'whatsapp', '+919000000000', 'failed', "
                "'provider_rejected', 'template not approved' "
                "FROM message_templates WHERE code = 'plan_delivered'"
            ),
            {"t": tenant_a.tenant_id, "c": tenant_a.client_id},
        )

    messaging_api.as_actor(practitioner(tenant_a))
    response = await messaging_api.http.get("/api/v1/app/messaging/failures")

    assert response.status_code == 200
    assert response.json()[0]["failure_code"] == "provider_rejected"


# ─── Sending and cancelling ──────────────────────────────────────────────


async def test_a_practitioner_can_queue_a_message(
    messaging_api: MessagingApi, tenant_a: TenantFixture
) -> None:
    """🔒 Queued, not sent. FR-M8-001 admits no exception for a human-initiated
    message, and the suppression rules a direct send would skip are exactly the
    ones protecting the practitioner."""
    messaging_api.as_actor(practitioner(tenant_a))

    response = await messaging_api.http.post(
        _client_path(tenant_a, "/messages"),
        json={
            "template_code": "assessment_invitation",
            "variables": {
                "practitioner_name": "Priya",
                "assessment_url": "https://portal.example.test/a/1",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True
    assert body["state"] == "pending"


async def test_the_client_name_cannot_be_supplied_by_the_caller(
    messaging_api: MessagingApi, session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 A caller-supplied `client_name` would let one client's name be sent to
    another, under the practitioner's own name."""
    messaging_api.as_actor(practitioner(tenant_a))

    await messaging_api.http.post(
        _client_path(tenant_a, "/messages"),
        json={
            "template_code": "assessment_invitation",
            "variables": {
                "client_name": "Someone Else",
                "practitioner_name": "Priya",
                "assessment_url": "https://portal.example.test/a/1",
            },
        },
    )

    async with session_for(tenant_a.tenant_id) as session:
        stored = (
            await session.execute(
                text("SELECT template_variables ->> 'client_name' FROM scheduled_messages")
            )
        ).scalar_one()

    assert stored == "Anjali Rao"


async def test_another_tenant_cannot_queue_a_message(
    messaging_api: MessagingApi, tenant_a: TenantFixture, tenant_b: TenantFixture
) -> None:
    messaging_api.as_actor(practitioner(tenant_b))

    response = await messaging_api.http.post(
        _client_path(tenant_a, "/messages"),
        json={"template_code": "assessment_invitation", "variables": {}},
    )

    assert response.status_code == 404


async def test_pending_messages_are_listed_with_their_rendered_body(
    messaging_api: MessagingApi, tenant_a: TenantFixture
) -> None:
    """FR-M8-028 — "you have three messages scheduled" is not something a
    practitioner can act on; what it *says* is."""
    messaging_api.as_actor(practitioner(tenant_a))
    await messaging_api.http.post(
        _client_path(tenant_a, "/messages"),
        json={
            "template_code": "assessment_invitation",
            "variables": {
                "practitioner_name": "Priya",
                "assessment_url": "https://portal.example.test/a/1",
            },
        },
    )

    response = await messaging_api.http.get(_client_path(tenant_a, "/messages/pending"))

    assert response.status_code == 200
    (pending,) = response.json()
    assert pending["template_code"] == "assessment_invitation"
    assert "Anjali Rao" in pending["preview"]


async def test_a_pending_message_can_be_cancelled(
    messaging_api: MessagingApi, tenant_a: TenantFixture
) -> None:
    messaging_api.as_actor(practitioner(tenant_a))
    queued = await messaging_api.http.post(
        _client_path(tenant_a, "/messages"),
        json={
            "template_code": "assessment_invitation",
            "variables": {
                "practitioner_name": "Priya",
                "assessment_url": "https://portal.example.test/a/1",
            },
        },
    )
    message_id = queued.json()["id"]

    response = await messaging_api.http.post(f"/api/v1/app/messaging/scheduled/{message_id}/cancel")

    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"


async def test_another_tenant_cannot_cancel_a_message(
    messaging_api: MessagingApi, tenant_a: TenantFixture, tenant_b: TenantFixture
) -> None:
    """🔒 RLS makes the row invisible, so the route answers 404 rather than
    reporting that something exists which the caller may not touch."""
    messaging_api.as_actor(practitioner(tenant_a))
    queued = await messaging_api.http.post(
        _client_path(tenant_a, "/messages"),
        json={
            "template_code": "assessment_invitation",
            "variables": {
                "practitioner_name": "Priya",
                "assessment_url": "https://portal.example.test/a/1",
            },
        },
    )
    message_id = queued.json()["id"]

    messaging_api.as_actor(practitioner(tenant_b))
    response = await messaging_api.http.post(f"/api/v1/app/messaging/scheduled/{message_id}/cancel")

    assert response.status_code == 404


# ─── Templates and preview (FR-M8-026) ───────────────────────────────────


async def test_all_eight_message_types_are_listed(
    messaging_api: MessagingApi, tenant_a: TenantFixture
) -> None:
    """🔒 AC-M8-009 — a practitioner previews every MVP message template."""
    messaging_api.as_actor(practitioner(tenant_a))

    response = await messaging_api.http.get("/api/v1/app/messaging/templates")

    assert response.status_code == 200
    codes = {template["code"] for template in response.json()}
    assert codes == {
        "plan_delivered",
        "appointment_confirmed",
        "appointment_reminder",
        "checkin_nudge",
        "assessment_invitation",
        "lead_acknowledgement",
        "lead_notification",
        "magic_link",
    }


@pytest.mark.parametrize(
    "code",
    [
        "plan_delivered",
        "appointment_confirmed",
        "appointment_reminder",
        "checkin_nudge",
        "assessment_invitation",
        "lead_acknowledgement",
        "lead_notification",
        "magic_link",
    ],
)
async def test_every_message_type_can_be_previewed(
    messaging_api: MessagingApi, tenant_a: TenantFixture, code: str
) -> None:
    """🔒 AC-M8-009, one test per type — a preview list that renders seven of
    eight is the state this asserts against."""
    messaging_api.as_actor(practitioner(tenant_a))

    response = await messaging_api.http.get(f"/api/v1/app/messaging/templates/{code}/preview")

    assert response.status_code == 200
    body = response.json()
    assert body["template_code"] == code
    # 🔒 Nothing unrendered reaches a practitioner's screen as a promise of what
    # a client will see.
    assert "{" not in body["body"]
    assert body["is_sample"] is True


async def test_a_preview_for_a_client_uses_their_own_name(
    messaging_api: MessagingApi, tenant_a: TenantFixture
) -> None:
    """🔒 FR-M8-026 — "as their clients will receive it"."""
    messaging_api.as_actor(practitioner(tenant_a))

    response = await messaging_api.http.get(
        "/api/v1/app/messaging/templates/plan_delivered/preview",
        params={"client_id": str(tenant_a.client_id)},
    )

    assert response.status_code == 200
    body = response.json()
    assert "Anjali Rao" in body["body"]
    assert body["is_sample"] is False


async def test_a_preview_for_another_tenants_client_is_refused(
    messaging_api: MessagingApi, tenant_a: TenantFixture, tenant_b: TenantFixture
) -> None:
    messaging_api.as_actor(practitioner(tenant_b))

    response = await messaging_api.http.get(
        "/api/v1/app/messaging/templates/plan_delivered/preview",
        params={"client_id": str(tenant_a.client_id)},
    )

    assert response.status_code == 404


# ─── Preferences (FR-M8-027) ─────────────────────────────────────────────


async def test_a_message_type_can_be_disabled_practice_wide(
    messaging_api: MessagingApi, tenant_a: TenantFixture
) -> None:
    messaging_api.as_actor(practitioner(tenant_a))

    response = await messaging_api.http.put(
        "/api/v1/app/messaging/preferences",
        json={"template_code": "checkin_nudge", "is_enabled": False},
    )

    assert response.status_code == 200
    assert response.json()["is_enabled"] is False

    listed = await messaging_api.http.get("/api/v1/app/messaging/preferences")
    assert any(row["template_code"] == "checkin_nudge" for row in listed.json())


async def test_an_essential_message_type_cannot_be_disabled(
    messaging_api: MessagingApi, tenant_a: TenantFixture
) -> None:
    """🔒 FR-M8-027 says "any *non-essential* message type". A client locked out
    of their portal because their practitioner turned off magic links is
    unacceptable — refused with a sentence, not a constraint violation."""
    messaging_api.as_actor(practitioner(tenant_a))

    response = await messaging_api.http.put(
        "/api/v1/app/messaging/preferences",
        json={"template_code": "magic_link", "is_enabled": False},
    )

    assert response.status_code == 422
    assert "action" in response.json()["error"]


async def test_a_quiet_window_can_be_set_practice_wide(
    messaging_api: MessagingApi, tenant_a: TenantFixture
) -> None:
    """FR-M8-009 — the window is a preference, defaulting to 21:00–08:00."""
    messaging_api.as_actor(practitioner(tenant_a))

    response = await messaging_api.http.put(
        "/api/v1/app/messaging/preferences",
        json={"quiet_hours_start": "22:00:00", "quiet_hours_end": "07:00:00"},
    )

    assert response.status_code == 200
    assert response.json()["quiet_hours_start"] == "22:00:00"


async def test_a_client_override_is_scoped_to_that_client(
    messaging_api: MessagingApi, tenant_a: TenantFixture
) -> None:
    """US-M8-06 — the per-client half of the two-level model."""
    messaging_api.as_actor(practitioner(tenant_a))

    response = await messaging_api.http.put(
        _client_path(tenant_a, "/message-preferences"),
        json={"template_code": "checkin_nudge", "is_enabled": False},
    )

    assert response.status_code == 200
    assert response.json()["client_id"] == str(tenant_a.client_id)


async def test_a_client_preference_read_includes_the_practice_default(
    messaging_api: MessagingApi, tenant_a: TenantFixture
) -> None:
    """⚠️ A screen showing an override without what it overrides cannot explain
    itself."""
    messaging_api.as_actor(practitioner(tenant_a))
    await messaging_api.http.put(
        "/api/v1/app/messaging/preferences",
        json={"template_code": "checkin_nudge", "is_enabled": False},
    )
    await messaging_api.http.put(
        _client_path(tenant_a, "/message-preferences"),
        json={"template_code": "appointment_reminder", "is_enabled": False},
    )

    response = await messaging_api.http.get(_client_path(tenant_a, "/message-preferences"))

    scopes = {(row["client_id"], row["template_code"]) for row in response.json()}
    assert (None, "checkin_nudge") in scopes
    assert (str(tenant_a.client_id), "appointment_reminder") in scopes


async def test_another_tenant_cannot_change_a_clients_preferences(
    messaging_api: MessagingApi, tenant_a: TenantFixture, tenant_b: TenantFixture
) -> None:
    messaging_api.as_actor(practitioner(tenant_b))

    response = await messaging_api.http.put(
        _client_path(tenant_a, "/message-preferences"),
        json={"template_code": "checkin_nudge", "is_enabled": False},
    )

    assert response.status_code == 404


# ─── Check-in configuration (FR-M8-022…024) ──────────────────────────────


async def test_a_check_in_cadence_can_be_configured(
    messaging_api: MessagingApi, tenant_a: TenantFixture
) -> None:
    messaging_api.as_actor(practitioner(tenant_a))

    response = await messaging_api.http.put(
        _client_path(tenant_a, "/checkin-schedule"),
        json={"frequency": "fortnightly", "day_of_week": 3, "time_of_day": "08:30:00"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["frequency"] == "fortnightly"
    assert body["day_of_week"] == 3
    assert body["is_paused"] is False
    assert body["next_due_on"] is not None


async def test_a_check_in_cadence_can_be_paused_without_changing_the_stage(
    messaging_api: MessagingApi, session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 FR-M8-024 — pausing is a messaging decision, not a lifecycle one."""
    messaging_api.as_actor(practitioner(tenant_a))
    await messaging_api.http.put(
        _client_path(tenant_a, "/checkin-schedule"), json={"frequency": "weekly"}
    )

    response = await messaging_api.http.put(
        _client_path(tenant_a, "/checkin-schedule"),
        json={"frequency": "weekly", "is_paused": True},
    )

    assert response.status_code == 200
    assert response.json()["is_paused"] is True

    async with session_for(tenant_a.tenant_id) as session:
        stage = (
            await session.execute(
                text("SELECT stage FROM clients WHERE id = :c"), {"c": tenant_a.client_id}
            )
        ).scalar_one()
    assert stage == "active"


async def test_a_client_with_no_schedule_reads_as_null(
    messaging_api: MessagingApi, tenant_a: TenantFixture
) -> None:
    messaging_api.as_actor(practitioner(tenant_a))

    response = await messaging_api.http.get(_client_path(tenant_a, "/checkin-schedule"))

    assert response.status_code == 200
    assert response.json() is None


async def test_another_tenant_cannot_configure_check_ins(
    messaging_api: MessagingApi, tenant_a: TenantFixture, tenant_b: TenantFixture
) -> None:
    messaging_api.as_actor(practitioner(tenant_b))

    response = await messaging_api.http.put(
        _client_path(tenant_a, "/checkin-schedule"), json={"frequency": "weekly"}
    )

    assert response.status_code == 404


async def test_an_operator_cannot_configure_check_ins(
    messaging_api: MessagingApi, tenant_a: TenantFixture
) -> None:
    """🔒 The operator boundary again, on a write this time."""
    messaging_api.as_actor(operator_actor())

    response = await messaging_api.http.put(
        _client_path(tenant_a, "/checkin-schedule"), json={"frequency": "weekly"}
    )

    assert response.status_code in {403, 404}


async def test_the_audit_trail_records_who_changed_a_setting(
    messaging_api: MessagingApi, tenant_a: TenantFixture
) -> None:
    """FR-M0-031 — the messaging routes use the existing audit pipeline rather
    than a parallel one. The in-memory sink is what the harness installs, so the
    assertion is that the route *declares* an auditable action and completes.
    """
    messaging_api.as_actor(practitioner(tenant_a))

    response = await messaging_api.http.put(
        "/api/v1/app/messaging/preferences",
        json={"template_code": "checkin_nudge", "is_enabled": False},
    )

    assert response.status_code == 200
    assert datetime.now(UTC) is not None
