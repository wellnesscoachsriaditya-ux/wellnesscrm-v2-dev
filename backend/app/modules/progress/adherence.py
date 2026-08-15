"""Adherence logging — FR-M7-004, API §12.3, DB §12.1.

🔒 **The primary engagement signal.** Validation Gate G3 asks "do clients log
adherence more than once?", and every answer comes from this table.

🔒 **Idempotency is the unique index, not a prior read.** A ``SELECT`` then
``INSERT`` can lose a race between two replays of the same offline queue
arriving together — which is not a hypothetical for a PWA that retries on
reconnect. ``uq_adherence_logs__idempotency`` decides, and the caller reports
the resulting :class:`~sqlalchemy.exc.IntegrityError` as a *duplicate*, which
API §12.4 defines as a success state. This is the same argument migration
``0024`` makes for measurements.

⚠️ **Nothing here commits** (ADR-04). The router owns the transaction, which is
what lets a batch of operations share one and what lets a single failed
operation roll back to a savepoint without discarding the ones before it.

⚠️ **Every query carries an explicit ``tenant_id`` predicate** even though RLS
already filters. RLS is the guarantee; the predicate is the part a reviewer can
see.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.errors import ValidationError
from app.modules.progress.models import AdherenceLog, AdherenceValue

#: 🟡 **PROPOSED backdating window: 7 days** (API §12.3, EC-M7-04).
#:
#: 🔒 Bounded in both directions and for different reasons. Backwards, because a
#: queue that has been offline for a month is reporting on a plan that has moved
#: on, and accepting it would silently rewrite an adherence history a
#: practitioner has already reviewed. Forwards, because a client cannot report
#: on a meal they have not had, and a device with a wrong clock is the common
#: cause rather than a dishonest client.
BACKDATING_WINDOW_DAYS = 7


def now() -> datetime:
    """Timezone-aware current time. Centralised so every row is UTC (NFR-099)."""
    return datetime.now(UTC)


def assert_within_window(logged_for_date: date, *, today: date) -> None:
    """🔒 EC-M7-04 — refuse a date outside the bounded window.

    Raises:
        ValidationError: Reported per operation by ``/portal/sync``; it never
            fails the batch (API §12.4 guarantee 1).
    """
    if logged_for_date > today:
        raise ValidationError(
            "That date is in the future.",
            action="Check your device's date and try again.",
        )
    if (today - logged_for_date).days > BACKDATING_WINDOW_DAYS:
        raise ValidationError(
            f"Logs can only be added for the last {BACKDATING_WINDOW_DAYS} days.",
            action="Your practitioner can record older entries for you.",
        )


async def log(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    logged_for_date: date,
    slot_type: str,
    adherence: AdherenceValue,
    idempotency_key: str,
    client_timestamp: datetime,
    plan_slot_id: uuid.UUID | None = None,
    plan_version_id: uuid.UUID | None = None,
) -> AdherenceLog:
    """Record one slot's adherence — FR-M7-004.

    Args:
        session: The request's transaction. Never committed here (ADR-04).
        tenant_id: From the verified token, never from the request.
        client_id: 🔒 From the verified token. The caller must not pass a value
            that came from a request body — ``/portal/sync`` takes it from the
            actor's ``subject_id`` and the Pattern C policy rejects the write if
            the two ever disagree.
        plan_version_id: 🔒 Resolved server-side from the client's issued plan.
            Accepting one from the client would let them attribute a log to a
            plan they were never given.

    Returns:
        The row, flushed so its id is available to the caller's response.

    Raises:
        IntegrityError: On a replayed ``idempotency_key`` for this client. 🔒
            Deliberately not caught here — the caller decides whether a replay is
            an error (it is not) and how to report it (API §12.4: ``duplicate``).
    """
    entry = AdherenceLog(
        tenant_id=tenant_id,
        client_id=client_id,
        logged_for_date=logged_for_date,
        plan_version_id=plan_version_id,
        plan_slot_id=plan_slot_id,
        slot_type=slot_type,
        adherence=adherence,
        idempotency_key=idempotency_key,
        client_timestamp=client_timestamp,
    )
    session.add(entry)
    # 🔒 Flushed rather than left to the caller's commit: the unique index must
    # be consulted *now*, inside the caller's savepoint, so a replay is reported
    # as a duplicate for this operation rather than failing the whole batch at
    # commit time — which is exactly what API §12.4 guarantee 1 forbids.
    await session.flush()
    return entry
