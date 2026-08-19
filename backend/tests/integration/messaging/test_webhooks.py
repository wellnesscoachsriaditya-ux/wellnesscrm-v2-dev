"""The delivery-receipt webhook, end to end — API §11.3, EC-M8-07.

🔒 Two properties can only be shown here, against a real database:

* the **tenant-less provider lookup** returns exactly one row and only to a
  session with no tenant (migration 0021 §5), and
* a **duplicate or out-of-order receipt** changes nothing, which is what
  ``uq_message_dispatches__provider_id`` plus the forward-only rule buy.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.kernel.messaging import DispatchStatus
from app.modules.messaging import (
    MessageDispatch,
    StatusUpdate,
    apply_status,
    tenant_for_provider_message,
)
from app.platform.config import get_settings
from tests.integration.messaging.conftest import (
    MessagingApi,
    SessionFactory,
    TenantFixture,
)

pytestmark = pytest.mark.asyncio

WEBHOOK_PATH = "/api/v1/public/webhooks/whatsapp"
SECRET = "webhook-app-secret"


async def _seed_dispatch(
    session_for: SessionFactory,
    tenant: TenantFixture,
    *,
    provider_message_id: str,
    status: str = "sent",
) -> uuid.UUID:
    """One sent dispatch, the row a receipt will refer to."""
    from sqlalchemy import text

    async with session_for(tenant.tenant_id) as session:
        return (
            await session.execute(
                text(
                    "INSERT INTO message_dispatches "
                    "(tenant_id, client_id, template_id, template_version, transport, "
                    " recipient_address, status, provider_message_id, sent_at) "
                    "SELECT :t, :c, id, 1, 'whatsapp', '+919000000000', "
                    "CAST(:s AS dispatch_status), :p, now() "
                    "FROM message_templates WHERE code = 'plan_delivered' RETURNING id"
                ),
                {
                    "t": tenant.tenant_id,
                    "c": tenant.client_id,
                    "s": status,
                    "p": provider_message_id,
                },
            )
        ).scalar_one()


def _payload(provider_message_id: str, status: str, *, at: int = 1755000000) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "102290129340398",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "statuses": [
                                {
                                    "id": provider_message_id,
                                    "status": status,
                                    "timestamp": str(at),
                                    "recipient_id": "919000000000",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _signed(body: bytes) -> dict[str, str]:
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def webhook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the app secret the signature is verified against.

    ⚠️ Set on the cached settings object rather than the environment: the app is
    built by a fixture that has already read them, and a webhook that silently
    fell back to "no secret configured" would refuse every request and the tests
    would pass for the wrong reason.
    """
    from pydantic import SecretStr

    monkeypatch.setattr(get_settings(), "whatsapp_webhook_secret", SecretStr(SECRET), raising=False)
    monkeypatch.setattr(
        get_settings(), "whatsapp_verify_token", SecretStr("verify-me"), raising=False
    )


async def _dispatch_row(
    session_for: SessionFactory, tenant: TenantFixture, dispatch_id: uuid.UUID
) -> MessageDispatch:
    async with session_for(tenant.tenant_id) as session:
        row = await session.get(MessageDispatch, dispatch_id)
        assert row is not None
        return row


# ─── Signature (API §11.3 rule 1) ────────────────────────────────────────


async def test_a_signed_receipt_is_accepted(
    messaging_api: MessagingApi, session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    dispatch_id = await _seed_dispatch(session_for, tenant_a, provider_message_id="wamid.ok")
    body = json.dumps(_payload("wamid.ok", "delivered")).encode()

    response = await messaging_api.http.post(WEBHOOK_PATH, content=body, headers=_signed(body))

    assert response.status_code == 200
    assert response.json() == {"received": True}
    assert dispatch_id is not None


async def test_an_unsigned_receipt_is_refused(messaging_api: MessagingApi) -> None:
    """🔒 An unverified webhook is an unauthenticated write endpoint."""
    body = json.dumps(_payload("wamid.x", "delivered")).encode()

    response = await messaging_api.http.post(
        WEBHOOK_PATH, content=body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 401
    assert response.content == b""


async def test_a_tampered_body_is_refused(messaging_api: MessagingApi) -> None:
    """The signature is over the bytes the provider sent, so a modified body no
    longer matches — which is the whole point of verifying before parsing."""
    body = json.dumps(_payload("wamid.x", "delivered")).encode()
    headers = _signed(body)

    response = await messaging_api.http.post(WEBHOOK_PATH, content=body + b" ", headers=headers)

    assert response.status_code == 401


async def test_a_signed_but_unparseable_body_is_acknowledged(
    messaging_api: MessagingApi,
) -> None:
    """🔒 The provider cannot fix it by retrying, and a 4xx would count against
    the endpoint's health — which would eventually stop the receipts we do use."""
    body = b"{not json"

    response = await messaging_api.http.post(WEBHOOK_PATH, content=body, headers=_signed(body))

    assert response.status_code == 200


async def test_an_unknown_provider_is_acknowledged_not_disclosed(
    messaging_api: MessagingApi,
) -> None:
    """A 404 would tell an unauthenticated caller which integrations exist."""
    response = await messaging_api.http.post(
        "/api/v1/public/webhooks/razorpay", content=b"{}", headers=_signed(b"{}")
    )
    assert response.status_code == 200


# ─── The verification challenge ──────────────────────────────────────────


async def test_the_challenge_is_echoed_for_the_right_token(
    messaging_api: MessagingApi,
) -> None:
    response = await messaging_api.http.get(
        WEBHOOK_PATH,
        params={"hub.mode": "subscribe", "hub.challenge": "12345", "hub.verify_token": "verify-me"},
    )

    assert response.status_code == 200
    assert response.text == "12345"


async def test_the_challenge_is_refused_for_a_wrong_token(
    messaging_api: MessagingApi,
) -> None:
    """🔒 Echoing unconditionally would let anyone register our endpoint against
    their own app and start feeding it callbacks."""
    response = await messaging_api.http.get(
        WEBHOOK_PATH,
        params={"hub.mode": "subscribe", "hub.challenge": "12345", "hub.verify_token": "wrong"},
    )

    assert response.status_code == 403


# ─── The tenant-less lookup (migration 0021 §5) ──────────────────────────


async def test_the_provider_lookup_resolves_a_tenant_without_a_scope(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 The one read that crosses a tenant-less boundary, bounded by a policy
    that admits exactly the row whose opaque provider id the caller already has.
    """
    await _seed_dispatch(session_for, tenant_a, provider_message_id="wamid.lookup")

    async with session_for(None) as session:
        resolved = await tenant_for_provider_message(session, provider_message_id="wamid.lookup")

    assert resolved == tenant_a.tenant_id


async def test_the_provider_lookup_finds_nothing_for_an_unknown_id(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """The common, harmless case: a receipt for a message another system sent,
    or one whose row we have purged."""
    await _seed_dispatch(session_for, tenant_a, provider_message_id="wamid.known")

    async with session_for(None) as session:
        assert await tenant_for_provider_message(session, provider_message_id="wamid.other") is None


async def test_a_tenant_scoped_session_cannot_use_the_provider_lookup(
    session_for: SessionFactory, tenant_a: TenantFixture, tenant_b: TenantFixture
) -> None:
    """🔒 **The condition that keeps this from being an RLS hole.** Permissive
    policies are OR-ed, so without `current_tenant_id() IS NULL` inside it, a
    tenant holding another tenant's provider id could read their delivery row —
    recipient address included."""
    await _seed_dispatch(session_for, tenant_a, provider_message_id="wamid.private")

    async with session_for(tenant_b.tenant_id) as session:
        assert (
            await tenant_for_provider_message(session, provider_message_id="wamid.private") is None
        )


# ─── Applying a status (DB §11.3) ────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("delivered", DispatchStatus.DELIVERED),
        ("read", DispatchStatus.READ),
        ("failed", DispatchStatus.FAILED),
    ],
)
async def test_each_delivery_status_is_applied(
    session_for: SessionFactory,
    tenant_a: TenantFixture,
    status: str,
    expected: DispatchStatus,
) -> None:
    dispatch_id = await _seed_dispatch(session_for, tenant_a, provider_message_id=f"wamid.{status}")

    async with session_for(tenant_a.tenant_id) as session:
        changed = await apply_status(
            session,
            tenant_id=tenant_a.tenant_id,
            update=StatusUpdate(
                provider_message_id=f"wamid.{status}",
                status=expected,
                occurred_at=datetime.now(UTC),
                failure_code="131026" if status == "failed" else None,
            ),
        )

    assert changed
    row = await _dispatch_row(session_for, tenant_a, dispatch_id)
    assert row.status is expected


async def test_a_duplicate_receipt_changes_nothing(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 Providers retry aggressively (API §11.3), so a duplicate is the normal
    case rather than an anomaly."""
    dispatch_id = await _seed_dispatch(session_for, tenant_a, provider_message_id="wamid.dup")
    update = StatusUpdate(
        provider_message_id="wamid.dup",
        status=DispatchStatus.DELIVERED,
        occurred_at=datetime.now(UTC),
    )

    async with session_for(tenant_a.tenant_id) as session:
        assert await apply_status(session, tenant_id=tenant_a.tenant_id, update=update)
    async with session_for(tenant_a.tenant_id) as session:
        assert not await apply_status(session, tenant_id=tenant_a.tenant_id, update=update)

    row = await _dispatch_row(session_for, tenant_a, dispatch_id)
    assert row.status is DispatchStatus.DELIVERED


async def test_a_late_delivered_receipt_does_not_undo_a_read(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 Forward-only. A provider's callbacks are not ordered, and a row walked
    backwards would report that a message the client had already opened was
    merely delivered."""
    dispatch_id = await _seed_dispatch(session_for, tenant_a, provider_message_id="wamid.order")
    now = datetime.now(UTC)

    async with session_for(tenant_a.tenant_id) as session:
        await apply_status(
            session,
            tenant_id=tenant_a.tenant_id,
            update=StatusUpdate(
                provider_message_id="wamid.order",
                status=DispatchStatus.READ,
                occurred_at=now,
            ),
        )
    async with session_for(tenant_a.tenant_id) as session:
        changed = await apply_status(
            session,
            tenant_id=tenant_a.tenant_id,
            update=StatusUpdate(
                provider_message_id="wamid.order",
                status=DispatchStatus.DELIVERED,
                occurred_at=now - timedelta(minutes=1),
            ),
        )

    assert not changed
    row = await _dispatch_row(session_for, tenant_a, dispatch_id)
    assert row.status is DispatchStatus.READ


async def test_a_read_receipt_implies_delivery(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """⚠️ A `delivered` callback does not always arrive. Leaving `delivered_at`
    NULL would make the delivery report understate what reached clients."""
    dispatch_id = await _seed_dispatch(session_for, tenant_a, provider_message_id="wamid.read")

    async with session_for(tenant_a.tenant_id) as session:
        await apply_status(
            session,
            tenant_id=tenant_a.tenant_id,
            update=StatusUpdate(
                provider_message_id="wamid.read",
                status=DispatchStatus.READ,
                occurred_at=datetime.now(UTC),
            ),
        )

    row = await _dispatch_row(session_for, tenant_a, dispatch_id)
    assert row.delivered_at is not None and row.read_at is not None


async def test_a_failure_without_a_code_still_satisfies_the_constraint(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """⚠️ `ck_message_dispatches__failure_coded` requires a code, so a provider
    that reports a failure without one must get a generic one rather than lose
    the whole receipt to a constraint violation."""
    dispatch_id = await _seed_dispatch(session_for, tenant_a, provider_message_id="wamid.nocode")

    async with session_for(tenant_a.tenant_id) as session:
        await apply_status(
            session,
            tenant_id=tenant_a.tenant_id,
            update=StatusUpdate(
                provider_message_id="wamid.nocode",
                status=DispatchStatus.FAILED,
                occurred_at=datetime.now(UTC),
            ),
        )

    row = await _dispatch_row(session_for, tenant_a, dispatch_id)
    assert row.failure_code == "provider_reported_failure"


async def test_a_status_for_another_tenant_is_not_applied(
    session_for: SessionFactory, tenant_a: TenantFixture, tenant_b: TenantFixture
) -> None:
    """The job runs under the resolved tenant's own scope, through the ordinary
    isolation policy — so a mis-routed job finds nothing rather than writing to
    the wrong tenant."""
    dispatch_id = await _seed_dispatch(session_for, tenant_a, provider_message_id="wamid.iso")

    async with session_for(tenant_b.tenant_id) as session:
        changed = await apply_status(
            session,
            tenant_id=tenant_b.tenant_id,
            update=StatusUpdate(
                provider_message_id="wamid.iso",
                status=DispatchStatus.DELIVERED,
                occurred_at=datetime.now(UTC),
            ),
        )

    assert not changed
    row = await _dispatch_row(session_for, tenant_a, dispatch_id)
    assert row.status is DispatchStatus.SENT


# ─── The whole path (route → queue → handler) ────────────────────────────


async def test_a_receipt_is_queued_and_applied_by_the_worker(
    messaging_api: MessagingApi, session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 API §11.3 steps 2 and 3: the route acknowledges immediately and the
    work happens in a job — under the tenant scope the job row carries."""
    from app.kernel.context import RequestContext, context_scope
    from app.modules.messaging import STATUS_JOB_TYPE
    from app.modules.messaging.jobs import apply_message_status
    from app.platform.jobs import get_job

    dispatch_id = await _seed_dispatch(session_for, tenant_a, provider_message_id="wamid.flow")
    body = json.dumps(_payload("wamid.flow", "delivered")).encode()

    response = await messaging_api.http.post(WEBHOOK_PATH, content=body, headers=_signed(body))
    assert response.status_code == 200

    async with session_for(tenant_a.tenant_id) as session:
        job_id = (
            await session.execute(
                select(MessageDispatch.id).where(MessageDispatch.id == dispatch_id)
            )
        ).scalar_one()
        assert job_id == dispatch_id

    async with session_for(None) as session:
        from sqlalchemy import text

        row = (
            await session.execute(
                text(
                    "SELECT id, payload, tenant_id FROM jobs WHERE job_type = :j "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"j": STATUS_JOB_TYPE},
            )
        ).one()

    assert row.tenant_id == tenant_a.tenant_id

    with context_scope(RequestContext.for_worker(STATUS_JOB_TYPE, tenant_id=row.tenant_id)):
        async with session_for(tenant_a.tenant_id) as session:
            await apply_message_status(row.payload, session)

    updated = await _dispatch_row(session_for, tenant_a, dispatch_id)
    assert updated.status is DispatchStatus.DELIVERED
    assert get_job is not None


async def test_a_receipt_for_an_unknown_message_queues_nothing(
    messaging_api: MessagingApi, session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """⚠️ Acknowledged, not errored — and no job, because there is no tenant to
    run one under."""
    from sqlalchemy import text

    body = json.dumps(_payload("wamid.nobody", "delivered")).encode()
    response = await messaging_api.http.post(WEBHOOK_PATH, content=body, headers=_signed(body))

    assert response.status_code == 200
    async with session_for(None) as session:
        queued = (
            await session.execute(
                text("SELECT count(*) FROM jobs WHERE job_type = 'apply_message_status'")
            )
        ).scalar_one()
    assert queued == 0
    assert tenant_a is not None


async def test_an_inbound_reply_queues_nothing(
    messaging_api: MessagingApi, session_for: SessionFactory
) -> None:
    """🔒 EC-M8-07 — replies reach the practitioner's own WhatsApp. Accepted and
    discarded so Meta does not mark the webhook unhealthy."""
    from sqlalchemy import text

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [{"from": "919000000000", "id": "wamid.in", "type": "text"}]
                        }
                    }
                ]
            }
        ]
    }
    body = json.dumps(payload).encode()
    response = await messaging_api.http.post(WEBHOOK_PATH, content=body, headers=_signed(body))

    assert response.status_code == 200
    async with session_for(None) as session:
        queued = (
            await session.execute(
                text("SELECT count(*) FROM jobs WHERE job_type = 'apply_message_status'")
            )
        ).scalar_one()
    assert queued == 0
