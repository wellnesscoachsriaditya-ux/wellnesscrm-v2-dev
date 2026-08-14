"""Shared fixtures for the messaging engine's integration suite — M8, S5.

🔒 **These tests only ever reach the throwaway PostgreSQL.** ``app_engine`` and
``migrator_engine`` are built from ``TEST_DATABASE_URL`` /
``TEST_DATABASE_MIGRATION_URL`` (see ``tests/integration/conftest.py``), which are
deliberately *different variables* from the ``DATABASE_URL`` in ``.env``. The
application's own settings point at Supabase; nothing here reads them, and the
HTTP harness overrides the transaction provider before any request is made — the
same guarantee, and the same reasoning, as the plan-authoring suite.

🔒 **No test here can send anything.** :class:`RecordingTransport` replaces every
adapter, so a suite that accidentally reached a real provider would have to
import one first. The engine under test is unchanged: it still resolves a
transport, writes an attempt row, calls ``send`` and records what came back.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.kernel.context import Actor, ActorType, AuthRealm, UserRole
from app.kernel.jobs import configure_job_enqueuer
from app.kernel.messaging import CheckinFrequency
from app.kernel.models import TransportType
from app.kernel.notifications import (
    DeliveryRecord,
    DeliveryStatus,
    Notification,
    configure_transports,
)
from app.main import create_app
from app.modules.clients import register_subscribers
from app.modules.messaging import configure_link_base_url, register_jobs
from app.platform.http import pipeline
from app.platform.jobs import enqueue
from tests.integration.conftest import scope_to

#: Every messaging table, child-first, for teardown.
#:
#: ⚠️ ``message_dispatches`` before ``scheduled_messages``: the FK points that
#: way, and a teardown that got the order wrong would fail *after* the assertions
#: had passed, which reads as a flaky suite rather than a fixture bug.
_MESSAGING_TABLES: tuple[str, ...] = (
    "message_dispatches",
    "scheduled_messages",
    "checkin_schedules",
    "notification_preferences",
    "timeline_events",
    "jobs",
)

#: ⚠️ Plan rows are purged too, and only because one flow test issues a plan to
#: prove the delivery trigger. `diet_plans` holds a foreign key to `clients`
#: (added by migration 0018), so a teardown that skipped these would fail on the
#: client delete — after the assertions had passed, which reads as a flaky suite
#: rather than a fixture bug.
_PLAN_TABLES: tuple[str, ...] = (
    "plan_items",
    "plan_slots",
    "plan_days",
    "plan_snapshots",
)


@dataclass
class RecordingTransport:
    """A transport that records what it was asked to send and returns a verdict.

    🔒 Deliberately **not** the real ``LoggedTransport``: these tests need to
    drive the *failure* paths — a rejected template, a network error, a provider
    that returns no message id — and a no-op adapter can only ever succeed. The
    contract it satisfies is the same one every adapter satisfies.
    """

    transport_type: TransportType = TransportType.WHATSAPP
    status: DeliveryStatus = DeliveryStatus.SENT
    failure_reason: str | None = None
    provider_message_id: str | None = None
    raises: Exception | None = None
    sent: list[Notification] = field(default_factory=list)

    @property
    def transport(self) -> TransportType:
        return self.transport_type

    async def send(self, notification: Notification) -> DeliveryRecord:
        self.sent.append(notification)
        if self.raises is not None:
            raise self.raises
        return DeliveryRecord(
            tenant_id=notification.tenant_id,
            transport=self.transport_type,
            template_code=notification.template_code,
            recipient_address=notification.recipient.address,
            status=self.status,
            category=notification.category,
            occurred_at=datetime.now(UTC),
            client_id=notification.client_id,
            provider_message_id=self.provider_message_id,
            failure_reason=self.failure_reason,
        )


#: What `messaging_ports` installed, so `messaging_api` can re-install it after
#: `create_app()` has replaced the registry with the real adapters.
_recording_transports: dict[TransportType, Any] = {}


@pytest.fixture
def whatsapp() -> RecordingTransport:
    """The transport every client-facing template resolves to by default."""
    return RecordingTransport(transport_type=TransportType.WHATSAPP)


@pytest.fixture
def email() -> RecordingTransport:
    """The transport the practitioner notification resolves to."""
    return RecordingTransport(transport_type=TransportType.EMAIL)


@pytest.fixture(autouse=True)
def messaging_ports(whatsapp: RecordingTransport, email: RecordingTransport) -> None:
    """🔒 Wire the seams the engine reaches the outside world through.

    ``create_app`` normally does this. The service-level tests here do not build
    an app, so without this the registries would hold whatever the last test to
    build one left behind — and a dispatch would send through a real adapter or
    raise ``TransportNotConfiguredError`` depending on collection order.

    ⚠️ The **job enqueuer is the real one**. The queue is the retry mechanism
    under test (FR-M8-004); a fake would assert that messaging asked for a retry
    rather than that one happens.
    """
    _recording_transports.clear()
    _recording_transports.update({whatsapp.transport: whatsapp, email.transport: email})
    configure_transports(dict(_recording_transports))
    configure_job_enqueuer(enqueue)
    configure_link_base_url("https://portal.example.test")
    register_jobs()
    register_subscribers()


@pytest_asyncio.fixture(autouse=True)
async def approved_templates(migrator_engine: AsyncEngine) -> AsyncIterator[None]:
    """Put the seeded templates into the state Meta approval would produce.

    ⚠️ 🔒 **This is a simulation of an approval that has not happened.** Migration
    0021 seeds every template `provider_template_status = 'pending'`, which is
    the truth: Meta Business Verification is not granted. With WhatsApp
    configured, the engine correctly suppresses every message as
    `template_paused` — so without this fixture the only WhatsApp behaviour the
    suite could observe is that one rule.

    🔒 Flipped through `migrator_engine`, never through the application, and that
    is itself an assertion: `app_user` holds no UPDATE grant on
    `message_templates` (DB §11.1), so a test that tried to do this the "normal"
    way would fail — which is the guarantee.

    ``test_dispatch.test_an_unapproved_template_is_paused_on_whatsapp`` sets one
    back to prove the gate still bites.
    """
    await _set_template_status(migrator_engine, "approved")
    try:
        yield
    finally:
        await _set_template_status(migrator_engine, "pending")


async def _set_template_status(migrator_engine: AsyncEngine, status: str) -> None:
    """Write to `message_templates`, which nothing in the application can do.

    🔒 **`FORCE ROW LEVEL SECURITY` applies to the table owner too**, and the
    only policy on this table is `message_templates__read_all` — a SELECT policy.
    So even `app_migrator` cannot UPDATE it while FORCE is on: the statement
    reports zero rows and changes nothing, silently. That silence is what this
    helper's first version ran into, and it is the guarantee working.

    ⚠️ `NO FORCE` is lifted for the statement and restored immediately. It is the
    narrowest way to express "only a schema migration may change this", which is
    exactly what DB §11.1 intends — and it is confined to a throwaway database
    the suite recreates.
    """
    async with migrator_engine.begin() as connection:
        await connection.execute(text("ALTER TABLE message_templates NO FORCE ROW LEVEL SECURITY"))
        try:
            await connection.execute(
                text(
                    "UPDATE message_templates SET provider_template_status = "
                    "CAST(:s AS provider_template_status)"
                ),
                {"s": status},
            )
        finally:
            await connection.execute(text("ALTER TABLE message_templates FORCE ROW LEVEL SECURITY"))


async def set_provider_template_status(
    migrator_engine: AsyncEngine, *, code: str, status: str
) -> None:
    """Change one template's Meta approval state — EC-M8-03's input."""
    async with migrator_engine.begin() as connection:
        await connection.execute(text("ALTER TABLE message_templates NO FORCE ROW LEVEL SECURITY"))
        try:
            await connection.execute(
                text(
                    "UPDATE message_templates "
                    "SET provider_template_status = CAST(:s AS provider_template_status) "
                    "WHERE code = :c"
                ),
                {"s": status, "c": code},
            )
        finally:
            await connection.execute(text("ALTER TABLE message_templates FORCE ROW LEVEL SECURITY"))


@dataclass(frozen=True, slots=True)
class TenantFixture:
    """One tenant with a practitioner and an active client."""

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    client_id: uuid.UUID


@pytest_asyncio.fixture
async def tenants(
    migrator_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> AsyncIterator[tuple[TenantFixture, TenantFixture]]:
    """Two tenants, each with one active client — the isolation pair.

    ⚠️ Seeded under each tenant's own scope: an unscoped owner connection is
    scoped to *nothing*, so the ``WITH CHECK`` rejects the insert rather than
    waving it through (see ``seeded_tenants``).

    🔒 Clients are created at stage ``active`` because ``receives_engagement`` is
    a dispatch input: a fixture at stage ``lead`` would suppress every message in
    the suite for a reason no test had asked for.
    """
    created: list[TenantFixture] = []

    async with migrator_engine.begin() as connection:
        for tenant_id in seeded_tenants:
            await scope_to(connection, tenant_id)
            user_id = (
                await connection.execute(
                    text("SELECT id FROM users WHERE tenant_id = :t LIMIT 1"), {"t": tenant_id}
                )
            ).scalar_one()
            client_id = uuid.uuid4()
            await connection.execute(
                text(
                    "INSERT INTO clients "
                    "(id, tenant_id, full_name, mobile, email, owner_user_id, stage, "
                    " activated_at) "
                    # ⚠️ `activated_at` is required by
                    # `ck_clients__active_has_activated_at`: a client at stage
                    # `active` must record when they became one, because the
                    # check-in cadence defaults to that weekday (FR-M8-023).
                    "VALUES (:id, :t, 'Anjali Rao', :m, :e, :o, 'active', now())"
                ),
                {
                    "id": client_id,
                    "t": tenant_id,
                    "m": f"+9198{client_id.int % 100000000:08d}",
                    "e": f"msg-{client_id}@example.test",
                    "o": user_id,
                },
            )
            created.append(TenantFixture(tenant_id=tenant_id, user_id=user_id, client_id=client_id))

    try:
        yield created[0], created[1]
    finally:
        async with migrator_engine.begin() as connection:
            for fixture in created:
                await scope_to(connection, fixture.tenant_id)
                for table in _MESSAGING_TABLES:
                    await connection.execute(
                        text(f"DELETE FROM {table} WHERE tenant_id = :t"),
                        {"t": fixture.tenant_id},
                    )
                for table in _PLAN_TABLES:
                    await connection.execute(
                        text(f"DELETE FROM {table} WHERE tenant_id = :t"),
                        {"t": fixture.tenant_id},
                    )
                await connection.execute(
                    text("UPDATE diet_plans SET current_version_id = NULL WHERE tenant_id = :t"),
                    {"t": fixture.tenant_id},
                )
                for table in ("diet_plan_versions", "diet_plans"):
                    await connection.execute(
                        text(f"DELETE FROM {table} WHERE tenant_id = :t"),
                        {"t": fixture.tenant_id},
                    )
                await connection.execute(
                    text("DELETE FROM consent_records WHERE tenant_id = :t"),
                    {"t": fixture.tenant_id},
                )
                await connection.execute(
                    text("DELETE FROM clients WHERE id = :c"), {"c": fixture.client_id}
                )


@pytest.fixture
def tenant_a(tenants: tuple[TenantFixture, TenantFixture]) -> TenantFixture:
    return tenants[0]


@pytest.fixture
def tenant_b(tenants: tuple[TenantFixture, TenantFixture]) -> TenantFixture:
    return tenants[1]


SessionFactory = Callable[..., Any]


@pytest.fixture
def session_for(app_engine: AsyncEngine) -> SessionFactory:
    """Open a tenant-scoped, committing transaction as ``app_user``.

    🔒 As ``app_user``, never the migrator: RLS, the column-level grants and the
    append-only revokes are the properties under test, and a migrator session
    would prove none of them.

    🔒 Commits on success, mirroring ``pipeline._database_transaction``. Services
    never commit (ADR-04), so a helper that only closed the session would roll
    every write back and every later assertion would be vacuous.
    """
    factory = async_sessionmaker(app_engine, expire_on_commit=False)

    @asynccontextmanager
    async def open_session(tenant_id: uuid.UUID | None) -> AsyncIterator[AsyncSession]:
        session = factory()
        try:
            await scope_to(await session.connection(), tenant_id)
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    return open_session


def practitioner(fixture: TenantFixture, *, role: UserRole = UserRole.OWNER) -> Actor:
    """A practitioner-realm actor for this tenant.

    ⚠️ ``role`` defaults to ``OWNER`` because most tests want the practitioner who
    owns the client. 🔒 It matters for the AC-M1-006 assertions: FR-M0-017 gives
    an owner a view of the whole tenant, so a colleague-cannot-see test built on
    an owner actor would pass for the wrong reason — and then keep passing after
    the check it was meant to guard was removed.
    """
    return Actor(
        actor_type=ActorType.PRACTITIONER,
        realm=AuthRealm.PRACTITIONER,
        subject_id=fixture.user_id,
        tenant_id=fixture.tenant_id,
        role=role,
    )


def client_actor(fixture: TenantFixture) -> Actor:
    """A client-realm actor — used to prove the practitioner realm refuses it."""
    return Actor(
        actor_type=ActorType.CLIENT,
        realm=AuthRealm.CLIENT,
        subject_id=fixture.client_id,
        tenant_id=fixture.tenant_id,
        role=None,
    )


def operator_actor() -> Actor:
    """A platform operator — no tenant, and no route here may admit them.

    🔒 FR-M11-003: support sees counts, never content. Every messaging action is
    declared ``TENANT_PII``, which ``register_action`` refuses to combine with
    operator access at import time; this actor is how the *route* is shown to
    refuse as well.
    """
    return Actor(
        actor_type=ActorType.OPERATOR,
        realm=AuthRealm.OPERATOR,
        subject_id=uuid.uuid4(),
        tenant_id=None,
        role=None,
    )


class MessagingApi:
    """An HTTP client for the messaging routes, with a switchable actor."""

    def __init__(self, client: httpx.AsyncClient, set_actor: Callable[[Actor], None]) -> None:
        self.http = client
        self._set_actor = set_actor

    def as_actor(self, actor: Actor) -> None:
        self._set_actor(actor)


@pytest_asyncio.fixture
async def messaging_api(app_engine: AsyncEngine) -> AsyncIterator[MessagingApi]:
    """The real application, over ASGI, against the throwaway database.

    🔒 Two seams are overridden and both restored on teardown — the transaction
    provider (**the safety-critical one**: the default reads ``.env`` and would
    reach Supabase) and the actor resolver. Everything else about the request is
    real: realm check, coarse authorization, the row-bound ``authorize()`` call,
    the transaction, the audit entry and the commit.
    """
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    current: dict[str, Actor] = {}

    @asynccontextmanager
    async def provider(scope: Any) -> Any:
        session = factory()
        try:
            await scope_to(await session.connection(), scope.tenant_id)
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def resolver(_request: httpx.Request) -> Actor:
        # ⚠️ Anonymous by default rather than KeyError: the public webhook has no
        # actor by definition, and a fixture that demanded one would make every
        # unauthenticated route untestable through this harness.
        return current.get("actor", Actor.anonymous())

    original_provider = pipeline.get_transaction_provider()
    app = create_app()
    # 🔒 **After `create_app`, deliberately.** `create_app` calls
    # `configure_messaging`, which installs whatever adapters this machine's
    # settings support — in practice the `logged` transport alone. Re-installing
    # the recording ones here is what stops an HTTP test from silently
    # exercising a different transport than the service tests do, and reporting
    # a send nobody could observe.
    configure_transports(dict(_recording_transports))
    pipeline.configure_transaction_provider(provider)
    pipeline.configure_actor_resolver(resolver)

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://messaging-tests")
    try:
        yield MessagingApi(client, lambda actor: current.__setitem__("actor", actor))
    finally:
        await client.aclose()
        await transport.aclose()
        pipeline.configure_transaction_provider(original_provider)
        pipeline.configure_actor_resolver(_anonymous)


async def _anonymous(_request: httpx.Request) -> Actor:
    return Actor.anonymous()


# ─── Small helpers the tests share ───────────────────────────────────────


async def set_client_stage(
    migrator_engine: AsyncEngine, *, tenant_id: uuid.UUID, client_id: uuid.UUID, stage: str
) -> None:
    """Change a client's stage out of band.

    ⚠️ Deliberately *not* through the transitions service: these tests are about
    what the dispatch engine does when live state has changed underneath a queued
    message, and going through the service would also publish events and drag
    half of M1 into a messaging test.
    """
    async with migrator_engine.begin() as connection:
        await scope_to(connection, tenant_id)
        await connection.execute(
            text("UPDATE clients SET stage = CAST(:s AS client_stage) WHERE id = :c"),
            {"s": stage, "c": client_id},
        )


async def suspend_tenant(migrator_engine: AsyncEngine, *, tenant_id: uuid.UUID) -> None:
    """Suspend a tenant — FR-M8-007's input."""
    async with migrator_engine.begin() as connection:
        await connection.execute(
            text("UPDATE tenants SET status = 'suspended', suspended_at = now() WHERE id = :t"),
            {"t": tenant_id},
        )


async def withdraw_consent(
    migrator_engine: AsyncEngine,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    purpose_code: str,
) -> None:
    """Record a consent withdrawal in the ledger — FR-M8-006's input.

    🔒 Written as an append-only ledger row, which is what the engine reads.
    ``consent_records`` has no RLS (DB §16.4), so the tenant filter in the read
    *is* the isolation boundary — and the fixture supplies a real tenant id so
    that boundary is exercised rather than bypassed.
    """
    async with migrator_engine.begin() as connection:
        purpose_id = (
            await connection.execute(
                text("SELECT id FROM consent_purposes WHERE code = :c"), {"c": purpose_code}
            )
        ).scalar_one()
        notice_id = (
            await connection.execute(
                text("SELECT id FROM consent_notices ORDER BY effective_from DESC LIMIT 1")
            )
        ).scalar_one_or_none()
        if notice_id is None:
            # ⚠️ No notice is seeded by migration 0006 — the catalogue of
            # *purposes* is platform data, while a notice is a published document
            # with a version and a locale. One is created here rather than in a
            # migration because a fabricated privacy notice must not exist in a
            # real deployment.
            notice_id = uuid.uuid4()
            await connection.execute(
                text(
                    "INSERT INTO consent_notices "
                    "(id, purpose_ids, version, title, body, effective_from) "
                    "VALUES (:id, ARRAY[:p]::uuid[], 'test-1', 'Test notice', "
                    "'For the messaging suite only.', now())"
                ),
                {"id": notice_id, "p": purpose_id},
            )
        await connection.execute(
            text(
                "INSERT INTO consent_records "
                "(tenant_id, subject_type, subject_id, purpose_id, notice_id, action, "
                " captured_via, captured_by_actor_type, occurred_at) "
                "VALUES (:t, 'client', :s, :p, :n, 'withdrawn', 'portal', 'client', now())"
            ),
            {"t": tenant_id, "s": client_id, "p": purpose_id, "n": notice_id},
        )


DEFAULT_CHECKIN_FREQUENCY = CheckinFrequency.WEEKLY
