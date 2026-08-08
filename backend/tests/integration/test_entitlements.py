"""Entitlements against a live PostgreSQL — S1 Slice E.

🔒 **Everything asserted here is invisible to a unit test**, which is why the file
exists. The append-only property of ``subscription_events`` is a *grant*, not a
rule; the column-level immutability of ``usage_events`` is a *trigger*. No amount
of care in ``platform.entitlements`` would stop a future caller issuing an UPDATE
— what must be proven is that the database refuses it.

What this proves:

* 🔒 ``app_user`` cannot UPDATE or DELETE a subscription event (DDR-15) — the
  history is evidence EC-M10-01 and EC-M10-05 are answered from;
* 🔒 ``usage_events`` rejects an edit to ``amount`` while permitting one to
  ``is_reconciled`` — the narrow UPDATE the reconciliation pass needs, and the
  reason a grant alone could not express this rule (EC-M10-04);
* 🔒 ``app_user`` cannot write ``plan_definitions`` or ``subscriptions``: a tenant
  that could would upgrade itself for free (FR-M10-001, FR-M10-008);
* 🔒 **RLS actually isolates** all four tenant-scoped tables — AC-M0-003 applied
  to this slice. A tenant scoped to A sees none of B's usage, and cannot write a
  row carrying B's id;
* the counter upsert is atomic under concurrency, so two simultaneous metered
  actions cannot lose an increment (DDR-14);
* the seeded plans from migration 0007 are readable and complete.

⚠️ Needs the same setup as the tenant-isolation gate — see
``tests/integration/test_tenant_isolation.py`` for the provisioning sequence.
These fail rather than skip in CI, where ``REQUIRE_LIVE_DATABASE`` is set.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.kernel.entitlements import ResourceCode, check
from app.kernel.errors import EntitlementError
from app.platform.entitlements import (
    load_allowance,
    load_subscription,
    mark_warned,
    month_window,
    record_usage,
    release_usage,
)
from tests.integration.conftest import scope_to

pytestmark = pytest.mark.asyncio

#: The tier seeded by migration 0007 that these tests subscribe tenants to.
_PLAN_CODE = "starter"

#: A metered resource with a counter — deliberately not ``active_clients``,
#: which DB §14.4 counts live and which therefore has no counter row.
_RESOURCE = ResourceCode.AI_GENERATIONS


@pytest_asyncio.fixture
async def subscribed_tenants(
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
) -> AsyncIterator[tuple[uuid.UUID, ...]]:
    """Give both seeded tenants an active subscription on the Starter plan.

    Seeded as ``app_migrator`` because ``app_user`` cannot: migration 0007
    revokes INSERT on ``subscriptions`` precisely so a tenant cannot provision
    its own plan (FR-M10-008), and that restriction is itself asserted below.

    🔒 Each insert runs under its own tenant's scope. ``subscriptions`` is
    Pattern A with FORCE, so even the owner is subject to the policy — an
    unscoped insert is rejected by ``WITH CHECK`` rather than succeeding.
    """
    tenant_a, tenant_b = seeded_tenants

    async with migrator_engine.begin() as connection:
        plan_id = (
            await connection.execute(
                text("SELECT id FROM plan_definitions WHERE code = :code"), {"code": _PLAN_CODE}
            )
        ).scalar_one()

        for tenant_id in (tenant_a, tenant_b):
            await scope_to(connection, tenant_id)
            await connection.execute(
                text(
                    "INSERT INTO subscriptions (tenant_id, plan_definition_id, status) "
                    "VALUES (:tenant, :plan, 'active')"
                ),
                {"tenant": tenant_id, "plan": plan_id},
            )

    try:
        yield (tenant_a, tenant_b)
    finally:
        # 🔒 In FK order and under each tenant's own scope — an unscoped DELETE
        # here removes zero rows and returns quietly, leaving rows that collide
        # with the next run's unique constraint on `tenant_id`.
        async with migrator_engine.begin() as connection:
            for tenant_id in (tenant_a, tenant_b):
                await scope_to(connection, tenant_id)
                for table in ("usage_events", "usage_counters", "subscription_events"):
                    await connection.execute(
                        text(f"DELETE FROM {table} WHERE tenant_id = :id"), {"id": tenant_id}
                    )
                await connection.execute(
                    text("DELETE FROM subscriptions WHERE tenant_id = :id"), {"id": tenant_id}
                )


# ─── Seed data ───────────────────────────────────────────────────────────


async def test_all_four_plans_are_seeded_and_readable(app_engine: AsyncEngine) -> None:
    """🔒 Plan reads are on the enforcement path, so ``app_user`` must hold SELECT.

    ``plan_definitions`` is Pattern D — no policy — so this read needs no scope.
    """
    async with app_engine.connect() as connection:
        codes = (
            (
                await connection.execute(
                    text("SELECT code FROM plan_definitions ORDER BY sort_order")
                )
            )
            .scalars()
            .all()
        )

    assert list(codes) == ["free", "starter", "growth", "clinic"]


async def test_every_seeded_plan_carries_every_limit(app_engine: AsyncEngine) -> None:
    """🔒 A missing limit key is indeterminate at runtime, so that tier's tenants
    would be denied every metered action — an outage for one pricing plan."""
    async with app_engine.connect() as connection:
        rows = (await connection.execute(text("SELECT code, limits FROM plan_definitions"))).all()

    for code, limits in rows:
        for resource in ResourceCode:
            assert resource.limit_key in limits, f"plan {code} is missing {resource.limit_key}"


# ─── Append-only by grant (DDR-15) ───────────────────────────────────────


async def test_app_user_cannot_update_a_subscription_event(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 The history EC-M10-01 and EC-M10-05 are answered from must be immutable."""
    tenant_a, _ = subscribed_tenants

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        subscription_id = (
            await connection.execute(
                text("SELECT id FROM subscriptions WHERE tenant_id = :t"), {"t": tenant_a}
            )
        ).scalar_one()
        await connection.execute(
            text(
                "INSERT INTO subscription_events "
                "  (subscription_id, tenant_id, event_type, actor_type) "
                "VALUES (:s, :t, 'created', 'system')"
            ),
            {"s": subscription_id, "t": tenant_a},
        )

    with pytest.raises(DBAPIError) as excinfo:
        async with app_engine.begin() as connection:
            await scope_to(connection, tenant_a)
            await connection.execute(
                text("UPDATE subscription_events SET reason = 'edited' WHERE tenant_id = :t"),
                {"t": tenant_a},
            )
    assert "permission denied" in str(excinfo.value).lower()


async def test_app_user_cannot_delete_a_subscription_event(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    tenant_a, _ = subscribed_tenants

    with pytest.raises(DBAPIError) as excinfo:
        async with app_engine.begin() as connection:
            await scope_to(connection, tenant_a)
            await connection.execute(
                text("DELETE FROM subscription_events WHERE tenant_id = :t"), {"t": tenant_a}
            )
    assert "permission denied" in str(excinfo.value).lower()


# ─── Immutability by trigger (EC-M10-04) ─────────────────────────────────


async def test_usage_event_amount_cannot_be_edited(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 The rule a grant cannot express.

    ``usage_events`` holds UPDATE so a reconciliation pass can set
    ``is_reconciled``. Without the trigger, that same grant would permit
    rewriting ``amount`` — silently changing the log a drifted counter is
    rebuilt from, with nothing appearing broken until the two disagree.
    """
    tenant_a, _ = subscribed_tenants
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await record_usage(
            session,
            tenant_id=tenant_a,
            resource=_RESOURCE,
            amount=1,
            source_module="tests",
        )

    with pytest.raises(DBAPIError) as excinfo:
        async with app_engine.begin() as connection:
            await scope_to(connection, tenant_a)
            await connection.execute(
                text("UPDATE usage_events SET amount = 999 WHERE tenant_id = :t"), {"t": tenant_a}
            )
    assert "append-only" in str(excinfo.value).lower()


async def test_usage_event_reconciliation_flag_can_be_set(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    """The one permitted UPDATE — the counterpart to the test above.

    ⚠️ Both directions matter. A trigger that rejected every UPDATE would pass the
    immutability test while making reconciliation impossible, and nothing else
    would notice until a counter drifted and could not be corrected.
    """
    tenant_a, _ = subscribed_tenants
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await record_usage(
            session, tenant_id=tenant_a, resource=_RESOURCE, amount=1, source_module="tests"
        )

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        await connection.execute(
            text("UPDATE usage_events SET is_reconciled = true WHERE tenant_id = :t"),
            {"t": tenant_a},
        )
        reconciled = (
            await connection.execute(
                text("SELECT bool_and(is_reconciled) FROM usage_events WHERE tenant_id = :t"),
                {"t": tenant_a},
            )
        ).scalar_one()

    assert reconciled is True


async def test_app_user_cannot_delete_a_usage_event(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    """DELETE is revoked even though UPDATE is not — the log must not shrink."""
    tenant_a, _ = subscribed_tenants

    with pytest.raises(DBAPIError) as excinfo:
        async with app_engine.begin() as connection:
            await scope_to(connection, tenant_a)
            await connection.execute(
                text("DELETE FROM usage_events WHERE tenant_id = :t"), {"t": tenant_a}
            )
    assert "permission denied" in str(excinfo.value).lower()


# ─── Read-only commercial state ──────────────────────────────────────────


async def test_app_user_cannot_write_the_plan_catalogue(app_engine: AsyncEngine) -> None:
    """🔒 A tenant editing its own limits is the entitlement system defeating itself."""
    with pytest.raises(DBAPIError) as excinfo:
        async with app_engine.begin() as connection:
            await connection.execute(
                text("UPDATE plan_definitions SET limits = '{}'::jsonb WHERE code = 'free'")
            )
    assert "permission denied" in str(excinfo.value).lower()


async def test_app_user_cannot_change_its_own_subscription(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 FR-M10-008 — activation is a manual operator action at MVP.

    A tenant that could write this row could move itself to the Clinic plan.
    """
    tenant_a, _ = subscribed_tenants

    with pytest.raises(DBAPIError) as excinfo:
        async with app_engine.begin() as connection:
            await scope_to(connection, tenant_a)
            await connection.execute(
                text("UPDATE subscriptions SET status = 'active' WHERE tenant_id = :t"),
                {"t": tenant_a},
            )
    assert "permission denied" in str(excinfo.value).lower()


# ─── Tenant isolation (AC-M0-003) ────────────────────────────────────────


async def test_usage_is_invisible_across_the_tenant_boundary(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 AC-M0-003 for this slice — the policy, not a WHERE clause, isolates.

    ⚠️ The read below carries **no** ``tenant_id`` predicate. That is deliberate:
    it is the application filter removed, which is exactly what the sprint gate
    requires be proven safe.
    """
    tenant_a, tenant_b = subscribed_tenants
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await record_usage(
            session, tenant_id=tenant_a, resource=_RESOURCE, amount=7, source_module="tests"
        )

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_b)
        rows = (await connection.execute(text("SELECT count(*) FROM usage_events"))).scalar_one()

    assert rows == 0, "tenant B can see tenant A's usage events with the filter removed"


async def test_counters_are_invisible_across_the_tenant_boundary(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    tenant_a, tenant_b = subscribed_tenants
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await record_usage(
            session, tenant_id=tenant_a, resource=_RESOURCE, amount=3, source_module="tests"
        )

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_b)
        rows = (await connection.execute(text("SELECT count(*) FROM usage_counters"))).scalar_one()

    assert rows == 0


async def test_a_usage_event_carrying_another_tenants_id_is_rejected(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 The ``WITH CHECK`` half. ``USING`` alone would filter reads and accept
    this write, leaving one tenant able to charge another's quota."""
    tenant_a, tenant_b = subscribed_tenants

    with pytest.raises(DBAPIError) as excinfo:
        async with app_engine.begin() as connection:
            await scope_to(connection, tenant_a)
            await connection.execute(
                text(
                    "INSERT INTO usage_events "
                    "  (tenant_id, resource_code, amount, source_module) "
                    "VALUES (:other, 'ai_generations', 1, 'tests')"
                ),
                {"other": tenant_b},
            )
    assert "row-level security" in str(excinfo.value).lower()


async def test_an_unscoped_connection_sees_no_usage_at_all(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    """⚠️ Unscoped is scoped to *nothing*, not to everything.

    ``current_tenant_id()`` is NULL and ``tenant_id = NULL`` is NULL rather than
    true, so the policy admits no rows. A connection that forgot to set a scope
    fails closed.
    """
    tenant_a, _ = subscribed_tenants
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await record_usage(
            session, tenant_id=tenant_a, resource=_RESOURCE, amount=1, source_module="tests"
        )

    async with app_engine.connect() as connection:
        rows = (await connection.execute(text("SELECT count(*) FROM usage_events"))).scalar_one()

    assert rows == 0


# ─── Metering behaviour (DDR-14) ─────────────────────────────────────────


async def test_recording_usage_writes_both_the_event_and_the_counter(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 DDR-14 — a counter without its event is a number nothing explains."""
    tenant_a, _ = subscribed_tenants
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await record_usage(
            session, tenant_id=tenant_a, resource=_RESOURCE, amount=5, source_module="tests"
        )

    async with app_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        events = (
            await connection.execute(
                text("SELECT sum(amount) FROM usage_events WHERE tenant_id = :t"), {"t": tenant_a}
            )
        ).scalar_one()
        counter = (
            await connection.execute(
                text("SELECT used_amount FROM usage_counters WHERE tenant_id = :t"),
                {"t": tenant_a},
            )
        ).scalar_one()

    assert events == Decimal(5)
    assert counter == Decimal(5)


async def test_a_rolled_back_transaction_records_no_usage(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 Consumption joins the caller's transaction (ADR-04).

    An action that fails must not leave quota consumed — that is the property
    that makes the compensating-event path (EC-M10-04) a rare correction rather
    than the normal case.
    """
    tenant_a, _ = subscribed_tenants
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as session:
        await session.begin()
        await scope_to(await session.connection(), tenant_a)
        await record_usage(
            session, tenant_id=tenant_a, resource=_RESOURCE, amount=4, source_module="tests"
        )
        await session.rollback()

    async with app_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        events = (
            await connection.execute(
                text("SELECT count(*) FROM usage_events WHERE tenant_id = :t"), {"t": tenant_a}
            )
        ).scalar_one()

    assert events == 0


async def test_release_appends_a_compensating_negative_event(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 EC-M10-04 — quota is returned by appending, never by editing.

    Two events remain in the log afterwards: the consumption and its reversal.
    A decrement that left one event would make the log stop explaining the
    counter.
    """
    tenant_a, _ = subscribed_tenants
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await record_usage(
            session, tenant_id=tenant_a, resource=_RESOURCE, amount=3, source_module="tests"
        )
        await release_usage(
            session, tenant_id=tenant_a, resource=_RESOURCE, amount=3, source_module="tests"
        )

    async with app_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        count = (
            await connection.execute(
                text("SELECT count(*) FROM usage_events WHERE tenant_id = :t"), {"t": tenant_a}
            )
        ).scalar_one()
        counter = (
            await connection.execute(
                text("SELECT used_amount FROM usage_counters WHERE tenant_id = :t"),
                {"t": tenant_a},
            )
        ).scalar_one()

    assert count == 2, "the reversal must be a second event, not an edit to the first"
    assert counter == Decimal(0)


async def test_releasing_more_than_was_consumed_clamps_at_zero(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 The correction path must not abort — the regression this file caught.

    ⚠️ PostgreSQL validates a CHECK constraint against the *proposed* row before
    ``ON CONFLICT`` resolution, so an unclamped negative amount aborts the
    statement even when the UPDATE branch is the one that would run. That made
    ``release_usage`` fail exactly when it was needed: rolling back consumption
    after a failed action.

    The counter clamps; ``usage_events`` keeps the true signed history, so an
    over-release is still visible to reconciliation rather than being erased.
    """
    tenant_a, _ = subscribed_tenants
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await record_usage(
            session, tenant_id=tenant_a, resource=_RESOURCE, amount=2, source_module="tests"
        )
        await release_usage(
            session, tenant_id=tenant_a, resource=_RESOURCE, amount=5, source_module="tests"
        )

    async with app_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        counter = (
            await connection.execute(
                text("SELECT used_amount FROM usage_counters WHERE tenant_id = :t"),
                {"t": tenant_a},
            )
        ).scalar_one()
        logged = (
            await connection.execute(
                text("SELECT sum(amount) FROM usage_events WHERE tenant_id = :t"),
                {"t": tenant_a},
            )
        ).scalar_one()

    assert counter == Decimal(0), "the counter went negative or the statement aborted"
    assert logged == Decimal(-3), "the log must retain the true signed history"


async def test_a_release_with_no_prior_counter_does_not_abort(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    """The INSERT half of the same bug — no counter row exists to conflict with.

    Reached when a compensating event lands in a period whose counter was never
    opened, which is what happens when usage is released across a month boundary.
    """
    tenant_a, _ = subscribed_tenants
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await release_usage(
            session, tenant_id=tenant_a, resource=_RESOURCE, amount=4, source_module="tests"
        )

    async with app_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        counter = (
            await connection.execute(
                text("SELECT used_amount FROM usage_counters WHERE tenant_id = :t"),
                {"t": tenant_a},
            )
        ).scalar_one()

    assert counter == Decimal(0)


async def test_concurrent_increments_do_not_lose_a_count(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 The upsert, under real concurrency (DDR-14).

    ⚠️ This is the test that cannot be reasoned about from the code alone. Two
    transactions increment the same counter simultaneously; a read-modify-write
    would lose one and hand the tenant free quota, while the unique constraint
    plus ``ON CONFLICT DO UPDATE`` makes the increment atomic.
    """
    tenant_a, _ = subscribed_tenants
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async def consume_one() -> None:
        async with sessions() as session, session.begin():
            await scope_to(await session.connection(), tenant_a)
            await record_usage(
                session, tenant_id=tenant_a, resource=_RESOURCE, amount=1, source_module="tests"
            )

    await asyncio.gather(*(consume_one() for _ in range(5)))

    async with app_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        counter = (
            await connection.execute(
                text("SELECT used_amount FROM usage_counters WHERE tenant_id = :t"),
                {"t": tenant_a},
            )
        ).scalar_one()

    assert counter == Decimal(5), "a concurrent increment was lost"


async def test_counter_period_is_the_calendar_month(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    """The counter opened by a metered action covers the month containing it."""
    tenant_a, _ = subscribed_tenants
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await record_usage(
            session, tenant_id=tenant_a, resource=_RESOURCE, amount=1, source_module="tests"
        )

    async with app_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        row = (
            await connection.execute(
                text("SELECT period_start, period_end FROM usage_counters WHERE tenant_id = :t"),
                {"t": tenant_a},
            )
        ).one()

    expected_start, expected_end = month_window(row.period_start)
    assert row.period_start == expected_start
    assert row.period_end == expected_end


# ─── The enforcement read, end to end ────────────────────────────────────


async def test_allowance_reflects_the_plan_and_the_counter(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    """The whole read path: subscription → plan limits → counter → allowance."""
    tenant_a, _ = subscribed_tenants
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await record_usage(
            session, tenant_id=tenant_a, resource=_RESOURCE, amount=6, source_module="tests"
        )
        allowance = await load_allowance(session, tenant_id=tenant_a, resource=_RESOURCE)

    # Starter permits 40 AI drafts a month (PRD M10.4).
    assert allowance.limit == Decimal(40)
    assert allowance.used == Decimal(6)
    assert allowance.plan_code == _PLAN_CODE
    assert allowance.is_determinate
    check(allowance, amount=1)


async def test_enforcement_denies_once_the_plan_limit_is_reached(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 FR-M0-045 end to end, against the real seeded plan."""
    tenant_a, _ = subscribed_tenants
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await record_usage(
            session, tenant_id=tenant_a, resource=_RESOURCE, amount=40, source_module="tests"
        )
        allowance = await load_allowance(session, tenant_id=tenant_a, resource=_RESOURCE)

    with pytest.raises(EntitlementError) as raised:
        check(allowance, amount=1)
    assert raised.value.details["upgrade_to"] == "growth"


async def test_a_tenant_without_a_subscription_is_indeterminate(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 FR-M0-046 — no subscription is unknown, not free.

    ⚠️ Uses ``seeded_tenants`` rather than ``subscribed_tenants``: these tenants
    deliberately have no subscription row.
    """
    tenant_a, _ = seeded_tenants
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        assert await load_subscription(session, tenant_id=tenant_a) is None
        allowance = await load_allowance(session, tenant_id=tenant_a, resource=_RESOURCE)

    assert not allowance.is_determinate
    assert not allowance.is_unlimited
    with pytest.raises(EntitlementError):
        check(allowance, amount=1)


async def test_the_warning_stamp_is_set_once(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 FR-M10-005 — the second call must not move the timestamp.

    The guard is in the UPDATE's own WHERE clause, so two concurrent metered
    actions that both observe the crossing still send one warning.
    """
    tenant_a, _ = subscribed_tenants
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await record_usage(
            session, tenant_id=tenant_a, resource=_RESOURCE, amount=32, source_module="tests"
        )
        await mark_warned(session, tenant_id=tenant_a, resource=_RESOURCE)

    async with app_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        first = (
            await connection.execute(
                text("SELECT warned_at_80pct FROM usage_counters WHERE tenant_id = :t"),
                {"t": tenant_a},
            )
        ).scalar_one()

    assert first is not None

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        await mark_warned(session, tenant_id=tenant_a, resource=_RESOURCE)

    async with app_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        second = (
            await connection.execute(
                text("SELECT warned_at_80pct FROM usage_counters WHERE tenant_id = :t"),
                {"t": tenant_a},
            )
        ).scalar_one()

    assert second == first, "the warning stamp moved; the tenant would be warned twice"


async def test_active_clients_refuses_to_be_countered(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    """🔒 DB §14.4 / M1.5 — counting it would create the drift it avoids."""
    tenant_a, _ = subscribed_tenants
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        with pytest.raises(ValueError, match="counted live"):
            await record_usage(
                session,
                tenant_id=tenant_a,
                resource=ResourceCode.ACTIVE_CLIENTS,
                amount=1,
                source_module="tests",
            )


async def test_active_clients_uses_the_supplied_live_count(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    """The caller supplies the count; the plan supplies the limit."""
    tenant_a, _ = subscribed_tenants
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        allowance = await load_allowance(
            session,
            tenant_id=tenant_a,
            resource=ResourceCode.ACTIVE_CLIENTS,
            live_used=29,
        )

    # Starter permits 30 active clients (PRD M10.4).
    assert allowance.limit == Decimal(30)
    assert allowance.used == Decimal(29)
    check(allowance, amount=1)


async def test_active_clients_without_a_live_count_is_indeterminate(
    app_engine: AsyncEngine, subscribed_tenants: tuple[uuid.UUID, ...]
) -> None:
    """⚠️ A caller who forgets the count gets a fail-safe denial, not a silent zero.

    A zero would read as "no clients yet" and permit the action — which is the
    most dangerous possible default for the product's most visible limit.
    """
    tenant_a, _ = subscribed_tenants
    sessions = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_a)
        allowance = await load_allowance(
            session, tenant_id=tenant_a, resource=ResourceCode.ACTIVE_CLIENTS
        )

    assert not allowance.is_determinate
    with pytest.raises(EntitlementError):
        check(allowance, amount=1)
