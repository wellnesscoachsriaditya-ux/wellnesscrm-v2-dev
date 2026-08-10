"""Measurements — FR-M3-011…015, EC-M3-02, EC-M3-05.

🔒 **BMI and waist-hip ratio are never stored** (FR-M3-012). They are derived on
read by ``kernel.clinical``, so a corrected weight corrects every derived value
at once. Storing them would create a second source of truth that drifts silently.

🔒 **Append-only.** Migration 0016 revokes UPDATE and DELETE from ``app_user``: a
correction is a new dated row, not an edit. EC-M3-05 already expects several
values for one date, and a trend whose history can be rewritten records nothing.

⚠️ Functions take an ``AsyncSession`` rather than opening one (ADR-04).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.clinical import (
    MeasurementRecorded,
    MeasurementSource,
    assert_storable,
    body_mass_index,
    display_precedence,
    is_implausible,
    waist_hip_ratio,
)
from app.kernel.errors import ValidationError
from app.kernel.events import publish
from app.modules.clinical.models import ClientNutritionProfile, Measurement

#: The most rows a trend read returns — NFR-006's budget applies to this page too.
MAX_TREND_ROWS = 500


def now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class DerivedMetrics:
    """What is computed from a measurement rather than stored on it.

    🔒 FR-M3-012. ``bmi`` needs a height, which a measurement may not carry — the
    client's height is on their nutrition profile, entered once. That is why
    :func:`with_derived` takes a fallback height rather than reading only the row.

    ⚠️ 🔒 **No band, no label, no colour.** OD-08 is unresolved (Indian cut-offs
    differ from WHO) and the implementation plan's DoD forbids displaying a
    clinical threshold without a citation. A number is a fact; "overweight" is a
    judgement we are not yet entitled to publish.
    """

    bmi: Decimal | None
    waist_hip_ratio: Decimal | None


def with_derived(measurement: Measurement, *, fallback_height_cm: Decimal | None) -> DerivedMetrics:
    """Derive BMI and WHR for one row — FR-M3-012, AC-M3-004.

    ⚠️ The row's own height wins over the profile's. A measurement taken with a
    height recorded on the day is more likely to be right than one entered at
    assessment time months earlier.
    """
    height = measurement.height_cm or fallback_height_cm
    bmi = (
        body_mass_index(weight_kg=measurement.weight_kg, height_cm=height)
        if measurement.weight_kg is not None and height is not None
        else None
    )
    whr = (
        waist_hip_ratio(waist_cm=measurement.waist_cm, hip_cm=measurement.hip_cm)
        if measurement.waist_cm is not None and measurement.hip_cm is not None
        else None
    )
    return DerivedMetrics(bmi=bmi, waist_hip_ratio=whr)


async def record(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    measured_on: date,
    source: MeasurementSource,
    recorded_by_user_id: uuid.UUID | None,
    weight_kg: Decimal | None = None,
    height_cm: Decimal | None = None,
    waist_cm: Decimal | None = None,
    hip_cm: Decimal | None = None,
    body_fat_pct: Decimal | None = None,
    notes: str | None = None,
    confirmed_implausible: bool = False,
) -> Measurement:
    """Record a dated measurement — FR-M3-011, FR-M3-013, EC-M3-02.

    🔒 **EC-M3-02's two-step, and the order matters.** A value outside the
    plausible range is *refused once* with a warning, then accepted if the caller
    resends it with ``confirmed_implausible``. A real 180 kg client exists; a
    system that will not record them is useless exactly when the record matters.
    The row is flagged so the practitioner sees what was confirmed.

    ⚠️ Both a practitioner's and a client's value for one date are kept
    (EC-M3-05). There is deliberately no upsert here: overwriting would discard
    the disagreement, which is itself clinically interesting.

    Raises:
        ValidationError: The value is outside what a measurement can be, or is
            implausible and unconfirmed.
    """
    if measured_on > now().date():
        raise ValidationError(
            "A measurement cannot be dated in the future.",
            action="Check the date and try again.",
        )

    # 🔒 The outer bound first — a value the database would refuse is not a
    # measurement at all, and confirming it must not make it storable.
    assert_storable(weight_kg=weight_kg, height_cm=height_cm)

    flagged = is_implausible(weight_kg=weight_kg, height_cm=height_cm)
    if flagged and not confirmed_implausible:
        raise ValidationError(
            "That reading looks unusual — please check it.",
            action="Confirm the value to record it as entered.",
            details={"requires_confirmation": True},
        )

    if all(v is None for v in (weight_kg, height_cm, waist_cm, hip_cm, body_fat_pct)):
        raise ValidationError(
            "Enter at least one measurement.",
            action="Add a weight, height, waist, hip or body fat value.",
        )

    measurement = Measurement(
        tenant_id=tenant_id,
        client_id=client_id,
        measured_on=measured_on,
        weight_kg=weight_kg,
        height_cm=height_cm,
        waist_cm=waist_cm,
        hip_cm=hip_cm,
        body_fat_pct=body_fat_pct,
        source=source,
        recorded_by_user_id=recorded_by_user_id,
        is_flagged_implausible=flagged,
        notes=notes,
    )
    session.add(measurement)
    await session.flush()

    await publish(
        MeasurementRecorded(
            client_id=client_id,
            tenant_id=tenant_id,
            measurement_id=measurement.id,
            source=source,
            occurred_at=now(),
            actor_user_id=recorded_by_user_id,
        ),
        session,
    )
    return measurement


async def history(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    limit: int = MAX_TREND_ROWS,
) -> list[Measurement]:
    """The measurement trend, newest first — FR-M3-014, AC-M3-004.

    🔒 **Every row, including same-date duplicates** (EC-M3-05). The display
    rule — practitioner value takes precedence — is applied by
    :func:`preferred_per_date` at render time, not by dropping rows here: both
    were recorded, and the one not shown is still part of the record.
    """
    statement = (
        select(Measurement)
        .where(Measurement.tenant_id == tenant_id, Measurement.client_id == client_id)
        .order_by(Measurement.measured_on.desc(), Measurement.created_at.desc())
        .limit(min(limit, MAX_TREND_ROWS))
    )
    return list((await session.execute(statement)).scalars().all())


def preferred_per_date(measurements: list[Measurement]) -> list[Measurement]:
    """One row per date, by source precedence — EC-M3-05.

    🔒 "Both retained with source attribution; practitioner value takes display
    precedence." This is the display half; nothing is deleted.

    ⚠️ Ties within a source are broken by recency, so a practitioner correcting
    their own earlier entry on the same day sees the correction.
    """
    best: dict[date, Measurement] = {}
    for row in measurements:
        current = best.get(row.measured_on)
        if current is None:
            best[row.measured_on] = row
            continue
        if (display_precedence(row.source), -row.created_at.timestamp()) < (
            display_precedence(current.source),
            -current.created_at.timestamp(),
        ):
            best[row.measured_on] = row
    return sorted(best.values(), key=lambda m: m.measured_on, reverse=True)


async def height_for(
    session: AsyncSession, *, tenant_id: uuid.UUID, client_id: uuid.UUID
) -> Decimal | None:
    """The client's height, for deriving BMI — FR-M3-012.

    ⚠️ Read from the nutrition profile rather than from the newest measurement:
    height is captured once at assessment and rarely re-measured, so most
    measurement rows carry none. Falls back to the most recent row that does.
    """
    profile_height = (
        await session.execute(
            select(ClientNutritionProfile.height_cm).where(
                ClientNutritionProfile.tenant_id == tenant_id,
                ClientNutritionProfile.client_id == client_id,
            )
        )
    ).scalar_one_or_none()
    if profile_height is not None:
        return profile_height

    return (
        await session.execute(
            select(Measurement.height_cm)
            .where(
                Measurement.tenant_id == tenant_id,
                Measurement.client_id == client_id,
                Measurement.height_cm.is_not(None),
            )
            .order_by(Measurement.measured_on.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
