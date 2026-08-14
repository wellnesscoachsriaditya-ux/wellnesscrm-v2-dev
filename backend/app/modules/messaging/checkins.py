"""Recurring client check-ins — FR-M8-022…025, DB §11.7.

🔒 **A schedule generates `scheduled_messages`; it does not send.** One engine,
one dispatch path (M8.3). A schedule that sent directly would be the second
scheduler the design forbids, and every message it produced would bypass
suppression, quiet hours and the delivery log at once.

🔒 **Check-ins stop when the client leaves `active`** (FR-M8-025), driven by the
``ClientStageChanged`` event rather than a nightly reconciliation. The difference
is a day: a client paused on Wednesday must not be nudged on Friday, and a sweep
that noticed on Saturday would have already sent it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.clients import get_client_directory
from app.kernel.errors import NotFoundError, ValidationError
from app.kernel.messaging import CheckinFrequency, utc_now
from app.modules.messaging.links import portal_url
from app.modules.messaging.models import CheckinSchedule
from app.modules.messaging.scheduling import MessageRequest, cancel_for_client, schedule

#: The message type a check-in generates. 🔒 A constant rather than a parameter:
#: a schedule that could generate *any* template would be a general-purpose
#: scheduler living outside the one the design permits.
CHECKIN_TEMPLATE_CODE = "checkin_nudge"

#: 🔒 DB §11.7's provenance, and the handle EC-M8-08 cancels by. `source_module`
#: is this module rather than `clients` because the *schedule* is what produced
#: the message, and cancelling "everything this schedule queued" is the operation
#: a pause needs.
SOURCE_MODULE = "messaging.checkins"

#: How far apart occurrences are. 🔒 Monthly is 28 days, matching
#: `kernel.messaging.next_checkin_due`: a check-in is a cadence, not a billing
#: date, and "the 31st" has no meaning in February.
_STEP_DAYS: dict[CheckinFrequency, int] = {
    CheckinFrequency.WEEKLY: 7,
    CheckinFrequency.FORTNIGHTLY: 14,
    CheckinFrequency.MONTHLY: 28,
}


@dataclass(frozen=True, slots=True)
class CheckinSettings:
    """What a practitioner may configure — FR-M8-022."""

    frequency: CheckinFrequency = CheckinFrequency.WEEKLY
    #: ISO weekday, Monday = 1 … Sunday = 7. 🟡 FR-M8-023 defaults it to the day
    #: the client became active, which the caller supplies.
    day_of_week: int | None = None
    time_of_day: time = time(9, 0)


def next_occurrence(*, after: date, frequency: CheckinFrequency, day_of_week: int | None) -> date:
    """The next date this cadence falls on, strictly after ``after``.

    ⚠️ ``day_of_week`` anchors a *weekly* cadence only. For fortnightly and
    monthly it is deliberately ignored: honouring it would mean "every 14 days,
    but on a Tuesday", which is either 14 days or 21 depending on where the
    arithmetic lands — a cadence that silently skips a fortnight twice a year.
    """
    step = _STEP_DAYS[frequency]
    candidate = after + timedelta(days=step)

    if frequency is CheckinFrequency.WEEKLY and day_of_week is not None:
        shift = (day_of_week - candidate.isoweekday()) % 7
        candidate += timedelta(days=shift)
    return candidate


async def configure(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    settings: CheckinSettings,
    starting_from: date | None = None,
) -> CheckinSchedule:
    """Create or update a client's check-in cadence — FR-M8-022.

    ⚠️ Changing the cadence recomputes ``next_due_on`` from *today*, not from the
    last occurrence. A practitioner switching a client from weekly to monthly
    expects the next nudge a month away; anchoring to the previous due date could
    fire one the same afternoon.
    """
    today = starting_from or utc_now().date()
    row = await session.scalar(
        select(CheckinSchedule).where(
            CheckinSchedule.tenant_id == tenant_id, CheckinSchedule.client_id == client_id
        )
    )
    if row is None:
        row = CheckinSchedule(tenant_id=tenant_id, client_id=client_id)
        session.add(row)

    if settings.day_of_week is not None and not 1 <= settings.day_of_week <= 7:
        raise ValidationError(
            "A day of the week is Monday (1) through Sunday (7).",
            action="Choose a day in that range.",
        )

    row.frequency = settings.frequency
    row.day_of_week = settings.day_of_week
    row.time_of_day = settings.time_of_day
    row.next_due_on = next_occurrence(
        after=today, frequency=settings.frequency, day_of_week=settings.day_of_week
    )
    await session.flush()
    return row


async def ensure_for_activation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    activated_at: datetime,
) -> CheckinSchedule:
    """Start a default cadence when a client becomes `active` — FR-M8-023.

    🟡 The default is weekly, on the weekday the client became active. The PRD
    marks that PROPOSED; it is implemented as stated because the alternative — no
    schedule until someone configures one — makes US-M8-02 ("check-ins sent
    without me remembering") false for every client by default, which is the
    failure the requirement exists to prevent.

    ⚠️ Existing schedules are left alone. A client who returns to `active` after
    a pause keeps the cadence their practitioner chose rather than silently
    reverting to weekly.
    """
    existing = await session.scalar(
        select(CheckinSchedule).where(
            CheckinSchedule.tenant_id == tenant_id, CheckinSchedule.client_id == client_id
        )
    )
    if existing is not None:
        if existing.is_paused:
            await resume(session, tenant_id=tenant_id, client_id=client_id)
        return existing

    return await configure(
        session,
        tenant_id=tenant_id,
        client_id=client_id,
        settings=CheckinSettings(day_of_week=activated_at.isoweekday()),
        starting_from=activated_at.date(),
    )


async def pause(
    session: AsyncSession, *, tenant_id: uuid.UUID, client_id: uuid.UUID
) -> CheckinSchedule | None:
    """Stop generating check-ins without changing the lifecycle stage — FR-M8-024.

    🔒 Also cancels anything already queued. A pause that left Friday's nudge in
    the queue would send one more message after the practitioner asked us to
    stop, which is the only outcome that makes the control feel broken.

    Returns ``None`` when the client has no schedule — pausing what does not
    exist is not an error, it is the state the caller wanted.
    """
    row = await session.scalar(
        select(CheckinSchedule).where(
            CheckinSchedule.tenant_id == tenant_id, CheckinSchedule.client_id == client_id
        )
    )
    if row is None:
        return None

    if not row.is_paused:
        row.is_paused = True
        row.paused_at = utc_now()
        await session.flush()

    await cancel_for_client(
        session,
        tenant_id=tenant_id,
        client_id=client_id,
        template_codes=[CHECKIN_TEMPLATE_CODE],
    )
    return row


async def resume(
    session: AsyncSession, *, tenant_id: uuid.UUID, client_id: uuid.UUID
) -> CheckinSchedule:
    """Restart a paused cadence — FR-M8-024.

    ⚠️ ``next_due_on`` is recomputed from today rather than resumed from where it
    stopped. A schedule paused for two months would otherwise be immediately
    overdue and fire on the next sweep — a nudge triggered by the act of
    resuming, which is not what the practitioner asked for.
    """
    row = await session.scalar(
        select(CheckinSchedule).where(
            CheckinSchedule.tenant_id == tenant_id, CheckinSchedule.client_id == client_id
        )
    )
    if row is None:
        raise NotFoundError(
            "This client has no check-in schedule.",
            action="Set one up first.",
        )

    row.is_paused = False
    row.paused_at = None
    row.next_due_on = next_occurrence(
        after=utc_now().date(), frequency=row.frequency, day_of_week=row.day_of_week
    )
    await session.flush()
    return row


async def load(
    session: AsyncSession, *, tenant_id: uuid.UUID, client_id: uuid.UUID
) -> CheckinSchedule | None:
    """One client's schedule, or ``None``."""
    row: CheckinSchedule | None = await session.scalar(
        select(CheckinSchedule).where(
            CheckinSchedule.tenant_id == tenant_id, CheckinSchedule.client_id == client_id
        )
    )
    return row


async def generate_due(
    session: AsyncSession, *, tenant_id: uuid.UUID, today: date | None = None
) -> int:
    """Turn every due schedule into a `scheduled_messages` row — FR-M8-022.

    🔒 **Generates intent, never a send.** The rows this creates go through the
    same dispatch engine as everything else, so a client paused between
    generation and dispatch is still suppressed with a reason (DB §11.5).

    🔒 Idempotent by construction: the occasion is the schedule and the due date,
    so re-running for the same day produces the same idempotency key and
    `uq_scheduled_messages__idempotency` refuses the duplicate. That is what
    makes a worker restart, a double sweep or a replayed job harmless.

    ⚠️ A schedule whose client has left `active` is skipped *and paused*, not
    merely skipped. FR-M8-025 requires check-ins to stop; the `StageChanged`
    subscriber is the fast path, and this is the reconciliation that catches a
    stage change which happened while the process was down.

    Returns:
        How many messages were created.
    """
    day = today or utc_now().date()
    directory = get_client_directory()

    rows = list(
        await session.scalars(
            select(CheckinSchedule).where(
                CheckinSchedule.tenant_id == tenant_id,
                CheckinSchedule.is_paused.is_(False),
                CheckinSchedule.next_due_on.is_not(None),
                CheckinSchedule.next_due_on <= day,
            )
        )
    )

    created = 0
    for row in rows:
        identity = await directory.find(session, tenant_id=tenant_id, client_id=row.client_id)
        if identity is None or not identity.receives_engagement:
            await pause(session, tenant_id=tenant_id, client_id=row.client_id)
            continue

        due_on = row.next_due_on or day
        message = await schedule(
            session,
            tenant_id=tenant_id,
            request=MessageRequest(
                template_code=CHECKIN_TEMPLATE_CODE,
                # 🔒 The logical occasion: this schedule, this date. Not "now" —
                # see `MessageRequest.occasion`.
                occasion=f"checkin:{row.id}:{due_on.isoformat()}",
                scheduled_for=datetime.combine(due_on, row.time_of_day, tzinfo=UTC),
                source_module=SOURCE_MODULE,
                source_record_id=row.id,
                client_id=row.client_id,
                variables={
                    "client_name": identity.full_name,
                    "portal_url": portal_url(),
                },
            ),
        )
        if message is not None:
            created += 1

        row.last_generated_for = due_on
        row.next_due_on = next_occurrence(
            after=due_on, frequency=row.frequency, day_of_week=row.day_of_week
        )

    await session.flush()
    return created
