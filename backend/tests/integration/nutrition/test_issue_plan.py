"""Tests for the nutrition plan issuance workflow (M4 Slice 1.3)."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.kernel.clients import ClientStage
from app.kernel.context import (
    Actor,
    ActorType,
    AuthRealm,
    RequestContext,
    UserRole,
    context_scope,
    new_request_id,
)
from app.kernel.errors import ConflictError, NotFoundError
from app.kernel.events import configure_deferred_enqueuer, reset_subscriptions
from app.kernel.jobs import reset_handlers
from app.kernel.models import Job, JobClass, JobStatus
from app.kernel.nutrition import PlanOrigin, PlanState
from app.modules.clients.models import Client
from app.modules.nutrition.jobs import register_jobs
from app.modules.nutrition.models import DietPlan, DietPlanVersion, PlanSnapshot
from app.modules.nutrition.plans import issue_plan_version
from app.platform.jobs import enqueue_for_event
from tests.integration.conftest import scope_to

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _ensure_handler():
    register_jobs()
    configure_deferred_enqueuer(enqueue_for_event)
    yield
    reset_handlers()
    reset_subscriptions()


@pytest_asyncio.fixture(autouse=True)
async def _clean_pdf_jobs(migrator_engine: AsyncEngine) -> AsyncIterator[None]:
    from sqlalchemy import text

    async def purge():
        async with migrator_engine.begin() as conn:
            await conn.execute(text("DELETE FROM jobs WHERE job_type = 'generate_nutrition_pdf'"))

    await purge()
    yield
    await purge()


@pytest_asyncio.fixture
async def sample_plan(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """A sample diet plan and its draft version."""
    tenant_id = seeded_tenants[0]
    client_id = uuid.uuid4()
    async with async_sessionmaker(app_engine)() as session:
        await scope_to(await session.connection(), tenant_id)

        plan_id = uuid.uuid4()
        version_id = uuid.uuid4()

        from sqlalchemy import text

        result = await session.execute(
            text("SELECT id FROM users WHERE tenant_id = :tid"), {"tid": tenant_id}
        )
        user_id = result.scalar_one()

        client = Client(
            id=client_id,
            tenant_id=tenant_id,
            full_name="Test Client",
            email="test@example.com",
            stage=ClientStage.ACTIVE,
            activated_at=datetime.now(UTC),
            owner_user_id=user_id,
        )
        plan = DietPlan(
            id=plan_id,
            tenant_id=tenant_id,
            client_id=client_id,
            title="Test Plan",
            created_by_user_id=user_id,
        )
        version = DietPlanVersion(
            id=version_id,
            tenant_id=tenant_id,
            plan_id=plan_id,
            version_number=1,
            state=PlanState.draft,
            origin=PlanOrigin.manual,
        )

        session.add(client)
        session.add(plan)
        session.add(version)
        await session.commit()

    try:
        yield plan_id, version_id
    finally:
        async with migrator_engine.begin() as conn:
            await scope_to(conn, tenant_id)
            await conn.execute(
                text(
                    "DELETE FROM plan_snapshots WHERE tenant_id = :tid AND "
                    "plan_version_id IN (SELECT id FROM diet_plan_versions WHERE plan_id = :pid)"
                ),
                {"tid": tenant_id, "pid": plan_id},
            )
            await conn.execute(text("DELETE FROM jobs WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(
                text(
                    "UPDATE diet_plans SET current_version_id = NULL "
                    "WHERE tenant_id = :tid AND id = :pid"
                ),
                {"tid": tenant_id, "pid": plan_id},
            )
            await conn.execute(
                text("DELETE FROM diet_plan_versions WHERE tenant_id = :tid AND plan_id = :pid"),
                {"tid": tenant_id, "pid": plan_id},
            )
            await conn.execute(
                text("DELETE FROM diet_plans WHERE tenant_id = :tid AND id = :pid"),
                {"tid": tenant_id, "pid": plan_id},
            )
            await conn.execute(
                text("DELETE FROM clients WHERE tenant_id = :tid AND id = :cid"),
                {"tid": tenant_id, "cid": client_id},
            )


def _practitioner_ctx(tenant_id: uuid.UUID, user_id: uuid.UUID) -> context_scope:
    """Build a request context that mirrors a real practitioner HTTP request."""
    actor = Actor(
        actor_type=ActorType.PRACTITIONER,
        realm=AuthRealm.PRACTITIONER,
        subject_id=user_id,
        tenant_id=tenant_id,
        role=UserRole.OWNER,
    )
    return context_scope(
        RequestContext(request_id=new_request_id(), actor=actor),
    )


async def test_issue_draft_successfully(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    sample_plan: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """AC-M4-xxx: Draft is issued successfully, snapshot created, PDF job enqueued."""
    tenant_id = seeded_tenants[0]
    plan_id, version_id = sample_plan
    user_id = uuid.uuid4()

    with _practitioner_ctx(tenant_id, user_id):
        async with async_sessionmaker(app_engine)() as session:
            await scope_to(await session.connection(), tenant_id)
            await issue_plan_version(session, tenant_id, plan_id, version_id, user_id)
            await session.commit()

    # Verify in a fresh session — no context_scope needed for reads
    async with async_sessionmaker(app_engine)() as session:
        await scope_to(await session.connection(), tenant_id)

        # 1. Verify state transition and immutability attributes
        result = await session.execute(
            select(DietPlanVersion).where(DietPlanVersion.id == version_id)
        )
        issued_version = result.scalar_one()
        assert issued_version.state == PlanState.issued
        assert issued_version.issued_at is not None
        assert issued_version.issued_by_user_id == user_id
        assert issued_version.computed_totals is not None

        # 2. Verify snapshot is created
        snap_result = await session.execute(
            select(PlanSnapshot).where(PlanSnapshot.plan_version_id == version_id)
        )
        snapshot = snap_result.scalar_one()
        assert snapshot.document["version_number"] == 1

        # 3. Verify PDF job is enqueued
        job_result = await session.execute(select(Job).where(Job.tenant_id == tenant_id))
        jobs = list(job_result.scalars())
        assert len(jobs) == 1
        job = jobs[0]
        assert job.job_type == "generate_nutrition_pdf"
        assert job.job_class == JobClass.RENDERING.value
        assert job.status == JobStatus.PENDING.value
        assert job.payload["plan_version_id"] == str(version_id)


async def test_invalid_issue_transition(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    sample_plan: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Cannot issue a plan that is already issued or discarded."""
    tenant_id = seeded_tenants[0]
    plan_id, version_id = sample_plan
    user_id = uuid.uuid4()

    async with async_sessionmaker(app_engine)() as session:
        await scope_to(await session.connection(), tenant_id)
        # Manually change to issued
        version = (
            await session.execute(select(DietPlanVersion).where(DietPlanVersion.id == version_id))
        ).scalar_one()
        version.state = PlanState.issued
        version.issued_at = datetime.now(UTC)
        version.issued_by_user_id = user_id
        version.computed_totals = {}
        await session.commit()

    async with async_sessionmaker(app_engine)() as session:
        await scope_to(await session.connection(), tenant_id)
        with pytest.raises(ConflictError):
            await issue_plan_version(session, tenant_id, plan_id, version_id, user_id)


async def test_missing_plan_version_raises_not_found(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
) -> None:
    """Issuing a non-existent version raises NotFoundError."""
    tenant_id = seeded_tenants[0]
    user_id = uuid.uuid4()

    async with async_sessionmaker(app_engine)() as session:
        await scope_to(await session.connection(), tenant_id)
        with pytest.raises(NotFoundError):
            await issue_plan_version(session, tenant_id, uuid.uuid4(), uuid.uuid4(), user_id)


async def test_tenant_isolation_during_issue(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    sample_plan: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Tenant A cannot issue Tenant B's plan."""
    tenant_b = seeded_tenants[1]
    plan_id, version_id = sample_plan
    user_id = uuid.uuid4()

    async with async_sessionmaker(app_engine)() as session:
        # Use Tenant B's context to try to issue Tenant A's plan
        await scope_to(await session.connection(), tenant_b)
        with pytest.raises(NotFoundError):
            await issue_plan_version(session, tenant_b, plan_id, version_id, user_id)


async def test_failed_enqueue_rolls_back_issue(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    sample_plan: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """If the transaction fails, the plan state remains draft and no job is queued."""
    tenant_id = seeded_tenants[0]
    plan_id, version_id = sample_plan
    user_id = uuid.uuid4()

    with _practitioner_ctx(tenant_id, user_id):
        async with async_sessionmaker(app_engine)() as session:
            await scope_to(await session.connection(), tenant_id)

            try:
                await issue_plan_version(session, tenant_id, plan_id, version_id, user_id)
                # Simulate a failure before commit
                raise RuntimeError("Database exploded")
            except RuntimeError:
                await session.rollback()

    async with async_sessionmaker(app_engine)() as session:
        await scope_to(await session.connection(), tenant_id)
        version = (
            await session.execute(select(DietPlanVersion).where(DietPlanVersion.id == version_id))
        ).scalar_one()
        assert version.state == PlanState.draft

        job_count = (await session.execute(select(Job).where(Job.tenant_id == tenant_id))).all()
        assert len(job_count) == 0


async def test_superseding_previous_version(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    sample_plan: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Issuing v2 automatically supersedes v1."""
    tenant_id = seeded_tenants[0]
    plan_id, v1_id = sample_plan
    user_id = uuid.uuid4()
    v2_id = uuid.uuid4()

    async with async_sessionmaker(app_engine)() as session:
        await scope_to(await session.connection(), tenant_id)
        # Setup V1 as already issued
        v1 = (
            await session.execute(select(DietPlanVersion).where(DietPlanVersion.id == v1_id))
        ).scalar_one()
        v1.state = PlanState.issued
        v1.issued_at = datetime.now(UTC)
        v1.issued_by_user_id = user_id
        v1.computed_totals = {}

        # Setup V2 as draft
        v2 = DietPlanVersion(
            id=v2_id,
            tenant_id=tenant_id,
            plan_id=plan_id,
            version_number=2,
            state=PlanState.draft,
            origin=PlanOrigin.revision,
        )
        session.add(v2)
        await session.commit()

    with _practitioner_ctx(tenant_id, user_id):
        async with async_sessionmaker(app_engine)() as session:
            await scope_to(await session.connection(), tenant_id)
            # Issue V2
            await issue_plan_version(session, tenant_id, plan_id, v2_id, user_id)
            await session.commit()

    # Verify in a fresh session
    async with async_sessionmaker(app_engine)() as session:
        await scope_to(await session.connection(), tenant_id)
        # Verify V1 is superseded
        v1 = (
            await session.execute(select(DietPlanVersion).where(DietPlanVersion.id == v1_id))
        ).scalar_one()
        assert v1.state == PlanState.superseded

        # Verify V2 is issued
        v2 = (
            await session.execute(select(DietPlanVersion).where(DietPlanVersion.id == v2_id))
        ).scalar_one()
        assert v2.state == PlanState.issued
