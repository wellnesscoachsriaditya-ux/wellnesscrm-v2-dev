"""The messaging schema's guarantees, against a real PostgreSQL — DB §11, §17.

🔒 These are the properties that only a live database can prove: that RLS
actually isolates, that the append-only grant actually refuses, that the
idempotency constraint actually loses the race for you. Every one of them is
asserted as ``app_user`` — the role the application runs as — because a migrator
session would prove none of them.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.kernel.messaging import utc_now
from app.modules.messaging import MessageRequest, schedule
from tests.integration.messaging.conftest import SessionFactory, TenantFixture

pytestmark = pytest.mark.asyncio

_TENANT_SCOPED = (
    "scheduled_messages",
    "message_dispatches",
    "checkin_schedules",
    "notification_preferences",
)


async def _queue(
    session_for: SessionFactory, tenant: TenantFixture, *, occasion: str = "o1"
) -> uuid.UUID:
    async with session_for(tenant.tenant_id) as session:
        message = await schedule(
            session,
            tenant_id=tenant.tenant_id,
            request=MessageRequest(
                template_code="plan_delivered",
                occasion=occasion,
                scheduled_for=utc_now(),
                source_module="tests",
                client_id=tenant.client_id,
                variables={
                    "client_name": "Anjali Rao",
                    "practitioner_name": "Priya",
                    "plan_url": "https://portal.example.test/p/1",
                },
            ),
        )
        assert message is not None
        return message.id


# ─── RLS (DB §17.1) ──────────────────────────────────────────────────────


@pytest.mark.parametrize("table", _TENANT_SCOPED)
async def test_every_tenant_scoped_table_is_enabled_and_forced(
    migrator_engine: AsyncEngine, table: str
) -> None:
    """🔒 FORCE is the half `ENABLE` does not do, and migrations run as the
    owner — without it the seeding connection sees everything, and so would the
    application if it ever became the owner."""
    async with migrator_engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class " "WHERE relname = :t"
                ),
                {"t": table},
            )
        ).one()
    assert row.relrowsecurity and row.relforcerowsecurity


async def test_a_tenant_cannot_read_another_tenants_scheduled_messages(
    session_for: SessionFactory, tenant_a: TenantFixture, tenant_b: TenantFixture
) -> None:
    """AC-M0-003, applied to the queue of intent."""
    await _queue(session_for, tenant_a)

    async with session_for(tenant_b.tenant_id) as session:
        visible = (
            await session.execute(text("SELECT count(*) FROM scheduled_messages"))
        ).scalar_one()
    assert visible == 0


async def test_a_tenant_cannot_write_a_row_for_another_tenant(
    session_for: SessionFactory, tenant_a: TenantFixture, tenant_b: TenantFixture
) -> None:
    """🔒 The `WITH CHECK` half. A read-only policy would let one tenant plant a
    message in another's queue and have it dispatched to their client."""
    with pytest.raises(DBAPIError):
        async with session_for(tenant_a.tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO notification_preferences (tenant_id, is_enabled) "
                    "VALUES (:t, false)"
                ),
                {"t": tenant_b.tenant_id},
            )


async def test_the_template_catalogue_is_readable_by_every_tenant(
    session_for: SessionFactory, tenant_a: TenantFixture, tenant_b: TenantFixture
) -> None:
    """DB §11.1 — platform-owned reference data with no `tenant_id`. Both tenants
    see the same eight message types, which is what "platform-owned" means."""
    counts = []
    for tenant in (tenant_a, tenant_b):
        async with session_for(tenant.tenant_id) as session:
            counts.append(
                (await session.execute(text("SELECT count(*) FROM message_templates"))).scalar_one()
            )
    assert counts[0] == counts[1] == 8


async def test_the_application_cannot_write_a_template(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 Two independent mechanisms, and this proves at least one bites: no
    INSERT/UPDATE grant, and no write policy. Practitioner-editable wording is
    Phase 2 (FR-M8-029); a tenant must never be able to change the words another
    tenant's clients receive."""
    with pytest.raises(DBAPIError):
        async with session_for(tenant_a.tenant_id) as session:
            await session.execute(text("UPDATE message_templates SET body_template = 'hijacked'"))


# ─── Append-only delivery log (DB §11.3) ─────────────────────────────────


async def test_the_application_cannot_delete_a_dispatch(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 FR-M8-003 — the log is the answer to "did my client get it?", and an
    answer that can be deleted is not one."""
    with pytest.raises(DBAPIError):
        async with session_for(tenant_a.tenant_id) as session:
            await session.execute(text("DELETE FROM message_dispatches"))


async def test_the_application_cannot_rewrite_which_message_was_sent(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 The column-level grant, tested at the one column that matters most: a
    webhook may advance a status, but nothing may change where a message went."""
    with pytest.raises(DBAPIError):
        async with session_for(tenant_a.tenant_id) as session:
            await session.execute(
                text("UPDATE message_dispatches SET recipient_address = '+910000000000'")
            )


async def test_the_application_may_advance_a_status(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """The other half of the same grant — a delivery receipt must be applicable."""
    async with session_for(tenant_a.tenant_id) as session:
        # ⚠️ No row is needed: `UPDATE … WHERE false` still requires the
        # privilege, so this asserts the grant rather than the data.
        await session.execute(
            text("UPDATE message_dispatches SET status = 'delivered' WHERE false")
        )


async def test_the_application_cannot_delete_a_suppressed_message(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 AC-M8-004 — a suppressed message is retained *with its reason*."""
    await _queue(session_for, tenant_a)
    with pytest.raises(DBAPIError):
        async with session_for(tenant_a.tenant_id) as session:
            await session.execute(text("DELETE FROM scheduled_messages"))


# ─── Constraints ─────────────────────────────────────────────────────────


async def test_the_idempotency_constraint_refuses_the_second_row(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 EC-M8-06, at the database. The service returns ``None`` on a duplicate
    because of ``ON CONFLICT DO NOTHING``; this is what that relies on."""
    await _queue(session_for, tenant_a, occasion="same")

    async with session_for(tenant_a.tenant_id) as session:
        key = (
            await session.execute(text("SELECT idempotency_key FROM scheduled_messages"))
        ).scalar_one()

    with pytest.raises(IntegrityError):
        async with session_for(tenant_a.tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO scheduled_messages "
                    "(tenant_id, client_id, template_id, scheduled_for, idempotency_key, "
                    " source_module) "
                    "SELECT :t, :c, id, now(), :k, 'tests' FROM message_templates "
                    "WHERE code = 'plan_delivered'"
                ),
                {"t": tenant_a.tenant_id, "c": tenant_a.client_id, "k": key},
            )


async def test_the_same_key_may_belong_to_two_tenants(
    session_for: SessionFactory, tenant_a: TenantFixture, tenant_b: TenantFixture
) -> None:
    """The constraint is `(tenant_id, idempotency_key)`. Keyed on the key alone,
    one tenant's message would silently suppress another's."""
    await _queue(session_for, tenant_a, occasion="shared-occasion")
    await _queue(session_for, tenant_b, occasion="shared-occasion")

    for tenant in (tenant_a, tenant_b):
        async with session_for(tenant.tenant_id) as session:
            count = (
                await session.execute(text("SELECT count(*) FROM scheduled_messages"))
            ).scalar_one()
        assert count == 1


async def test_a_suppressed_row_must_carry_a_reason(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 `ck_scheduled_messages__suppression_reason` — the one state this table
    must never hold."""
    message_id = await _queue(session_for, tenant_a)
    with pytest.raises(IntegrityError):
        async with session_for(tenant_a.tenant_id) as session:
            await session.execute(
                text("UPDATE scheduled_messages SET state = 'suppressed' WHERE id = :i"),
                {"i": message_id},
            )


async def test_a_message_needs_a_recipient(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """A row with neither a client nor a user would sit pending forever, looking
    like a stuck queue."""
    with pytest.raises(IntegrityError):
        async with session_for(tenant_a.tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO scheduled_messages "
                    "(tenant_id, template_id, scheduled_for, idempotency_key, source_module) "
                    "SELECT :t, id, now(), 'no-recipient', 'tests' FROM message_templates "
                    "WHERE code = 'plan_delivered'"
                ),
                {"t": tenant_a.tenant_id},
            )


async def test_a_failed_dispatch_must_carry_a_code(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 FR-M8-003/AC-M8-007 — a failure with no code is a log entry nobody can
    act on."""
    with pytest.raises(IntegrityError):
        async with session_for(tenant_a.tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO message_dispatches "
                    "(tenant_id, client_id, template_id, template_version, transport, "
                    " recipient_address, status) "
                    "SELECT :t, :c, id, 1, 'logged', '+919000000000', 'failed' "
                    "FROM message_templates WHERE code = 'plan_delivered'"
                ),
                {"t": tenant_a.tenant_id, "c": tenant_a.client_id},
            )


async def test_one_provider_message_id_belongs_to_one_dispatch(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 `uq_message_dispatches__provider_id` — webhook idempotency rests on the
    lookup being exact."""
    for attempt in (1, 2):
        statement = text(
            "INSERT INTO message_dispatches "
            "(tenant_id, client_id, template_id, template_version, transport, "
            " recipient_address, status, provider_message_id, attempt_number) "
            "SELECT :t, :c, id, 1, 'whatsapp', '+919000000000', 'sent', 'wamid.dup', :a "
            "FROM message_templates WHERE code = 'plan_delivered'"
        )
        params = {"t": tenant_a.tenant_id, "c": tenant_a.client_id, "a": attempt}
        if attempt == 1:
            async with session_for(tenant_a.tenant_id) as session:
                await session.execute(statement, params)
        else:
            with pytest.raises(IntegrityError):
                async with session_for(tenant_a.tenant_id) as session:
                    await session.execute(statement, params)


async def test_a_tenant_holds_one_preference_row_per_scope(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """🔒 `NULLS NOT DISTINCT` — without it a tenant accumulates contradictory
    "default" rows and which one wins depends on physical order."""
    async with session_for(tenant_a.tenant_id) as session:
        await session.execute(
            text("INSERT INTO notification_preferences (tenant_id) VALUES (:t)"),
            {"t": tenant_a.tenant_id},
        )

    with pytest.raises(IntegrityError):
        async with session_for(tenant_a.tenant_id) as session:
            await session.execute(
                text("INSERT INTO notification_preferences (tenant_id) VALUES (:t)"),
                {"t": tenant_a.tenant_id},
            )


async def test_a_client_holds_one_checkin_schedule(
    session_for: SessionFactory, tenant_a: TenantFixture
) -> None:
    """Two schedules would generate two nudges a week, and neither would be
    wrong on its own."""
    for attempt in range(2):
        statement = text("INSERT INTO checkin_schedules (tenant_id, client_id) VALUES (:t, :c)")
        params = {"t": tenant_a.tenant_id, "c": tenant_a.client_id}
        if attempt == 0:
            async with session_for(tenant_a.tenant_id) as session:
                await session.execute(statement, params)
        else:
            with pytest.raises(IntegrityError):
                async with session_for(tenant_a.tenant_id) as session:
                    await session.execute(statement, params)


# ─── Enums (DB §11) ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("enum_name", "expected"),
    [
        ("message_category", {"transactional", "reminder", "nudge", "notification"}),
        ("provider_template_status", {"pending", "approved", "rejected", "paused"}),
        (
            "scheduled_state",
            {"pending", "dispatched", "suppressed", "expired", "cancelled"},
        ),
        (
            "suppression_reason",
            {
                "client_stage_inactive",
                "consent_withdrawn",
                "tenant_suspended",
                "quota_exceeded",
                "frequency_capped",
                "template_paused",
                "client_unsubscribed",
            },
        ),
        ("dispatch_status", {"queued", "sent", "delivered", "read", "failed", "rejected"}),
        ("checkin_frequency", {"weekly", "fortnightly", "monthly"}),
        # 🔒 `logged` is what makes S5 shippable before Meta verification.
        ("transport_type", {"whatsapp", "sms", "email", "logged"}),
    ],
)
async def test_the_database_enum_matches_the_kernel(
    migrator_engine: AsyncEngine, enum_name: str, expected: set[str]
) -> None:
    """A Python enum and a PostgreSQL type that disagree fail at the first write
    of the missing value — in production, for one message type."""
    async with migrator_engine.connect() as connection:
        values = {
            row[0]
            for row in (
                await connection.execute(
                    text(
                        "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                        "WHERE t.typname = :n"
                    ),
                    {"n": enum_name},
                )
            ).all()
        }
    assert values == expected


# ─── Indexes ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("index", "table"),
    [
        ("ix_scheduled_messages__due", "scheduled_messages"),
        ("ix_scheduled_messages__source", "scheduled_messages"),
        ("ix_scheduled_messages__client", "scheduled_messages"),
        ("ix_message_dispatches__tenant_created", "message_dispatches"),
        ("ix_message_dispatches__client_created", "message_dispatches"),
        ("uq_message_dispatches__provider_id", "message_dispatches"),
        ("ix_checkin_schedules__due", "checkin_schedules"),
        ("uq_notification_preferences__scope", "notification_preferences"),
    ],
)
async def test_the_named_index_exists(migrator_engine: AsyncEngine, index: str, table: str) -> None:
    """NFR-009 budgets 60 seconds from trigger to dispatch; the worker's scan is
    an index scan or it is not."""
    async with migrator_engine.connect() as connection:
        found = (
            await connection.execute(
                text("SELECT count(*) FROM pg_indexes WHERE tablename = :t AND indexname = :i"),
                {"t": table, "i": index},
            )
        ).scalar_one()
    assert found == 1


async def test_the_due_index_is_partial(migrator_engine: AsyncEngine) -> None:
    """🔒 The hot path stays small as dispatched rows accumulate."""
    async with migrator_engine.connect() as connection:
        definition = (
            await connection.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :i"),
                {"i": "ix_scheduled_messages__due"},
            )
        ).scalar_one()
    assert "WHERE" in definition and "pending" in definition
