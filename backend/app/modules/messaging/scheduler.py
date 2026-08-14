"""The scheduler — the one thing that decides what is due (M8.3).

🔒 **One scheduler.** Not one per module, and not one per message type. Its whole
job is: generate the recurring intent that is due, then hand every due
`scheduled_messages` row to the job queue for dispatch. It makes no send of its
own and knows nothing about transports.

🔒 **It does not bypass the dispatch engine.** The sweep enqueues a
`dispatch_scheduled_message` job per due row; the job runs
``dispatch.dispatch_scheduled``, which is the only code in the product that
talks to a transport. A sweep that sent inline would be a second dispatch path
with no retry policy, no delivery log discipline and no suppression.

⚠️ **Per tenant, one transaction each, and that is a deliberate cost.** RLS
scopes a transaction to a single tenant (DB §17.1), so there is no such thing as
a cross-tenant "find everything due" query for the application role — and there
must not be. At the 50–200 tenants this product is sized for (Arch §14, the
budget constraint), a poll per tenant per minute is a few hundred cheap indexed
queries a minute. 🔒 **The documented revisit trigger:** if the sweep's own
queries become a measurable share of database load, batch the tenant list by
"has anything due" using a platform-scoped query written for that purpose —
before reaching for a scheduler service.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.jobs import get_job_enqueuer
from app.kernel.messaging import ScheduledState, utc_now
from app.kernel.models import Tenant, TenantStatus
from app.modules.messaging.checkins import generate_due
from app.modules.messaging.models import ScheduledMessage
from app.modules.messaging.scheduling import DISPATCH_JOB_TYPE

#: 🔒 How many messages one sweep will queue for one tenant. A ceiling rather
#: than "everything due": after an outage a tenant can have thousands of overdue
#: rows, and queueing them all in one transaction would hold a long write while
#: every other tenant waited. The next sweep takes the next batch, and the
#: partial index makes each pass cheap.
BATCH_SIZE = 200


@dataclass(frozen=True, slots=True)
class SweepResult:
    """What one pass over one tenant produced."""

    tenant_id: uuid.UUID
    checkins_generated: int
    dispatches_queued: int

    @property
    def did_work(self) -> bool:
        return bool(self.checkins_generated or self.dispatches_queued)


async def sweep_tenant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    now: datetime | None = None,
    today: date | None = None,
) -> SweepResult:
    """Generate what is due for one tenant and queue it for dispatch.

    🔒 Runs inside the caller's transaction, under that tenant's scope. Both
    halves must commit together: a check-in row generated without its dispatch
    job would sit pending until the next sweep noticed it, and a job enqueued
    for a row that rolled back would fail on every attempt.
    """
    moment = now or utc_now()

    generated = await generate_due(session, tenant_id=tenant_id, today=today or moment.date())

    # 🔒 The hot path, matching `ix_scheduled_messages__due` — partial on
    # `state = 'pending'`, ordered by the column the index is built on.
    due = list(
        await session.scalars(
            select(ScheduledMessage.id)
            .where(
                ScheduledMessage.tenant_id == tenant_id,
                ScheduledMessage.state == ScheduledState.PENDING,
                ScheduledMessage.scheduled_for <= moment,
            )
            .order_by(ScheduledMessage.scheduled_for)
            .limit(BATCH_SIZE)
        )
    )

    enqueue = get_job_enqueuer()
    queued = 0
    for message_id in due:
        job_id = await enqueue(
            session,
            job_type=DISPATCH_JOB_TYPE,
            payload={"scheduled_message_id": str(message_id)},
            tenant_id=tenant_id,
            # 🔒 One job per message, ever. `uq_jobs__idempotency` refuses the
            # second, which is what stops two sweeps — or a sweep racing a
            # just-completed one — queueing the same message twice.
            #
            # ⚠️ The consequence, stated because it is not obvious: a message
            # whose job has already run and *succeeded* keeps its key, so it can
            # never be re-queued. That is correct — it has been dispatched — and
            # the dispatch engine's own re-entry guard covers the case where the
            # job runs twice before finishing.
            idempotency_key=f"dispatch:{message_id}",
        )
        if job_id is not None:
            queued += 1

    return SweepResult(
        tenant_id=tenant_id,
        checkins_generated=generated,
        dispatches_queued=queued,
    )


async def active_tenant_ids(session: AsyncSession) -> list[uuid.UUID]:
    """Tenants the sweep should visit.

    🔒 ``tenants`` has no RLS (DB §4.1), so this is readable from a
    platform-scoped transaction — which is the only reason a per-tenant sweep is
    possible at all.

    ⚠️ Suspended and closed tenants are skipped, and this is an optimisation
    rather than the enforcement. FR-M8-007 is enforced at dispatch, by
    `evaluate_suppression`, which records `tenant_suspended` as the reason. If
    this filter were the only check, a tenant suspended *after* their messages
    were queued would simply have them sit pending with no explanation.
    """
    rows = await session.scalars(
        select(Tenant.id)
        .where(Tenant.status.in_([TenantStatus.TRIAL, TenantStatus.ACTIVE]))
        .order_by(Tenant.created_at)
    )
    return list(rows)
