"""ORM models for the progress module — DB §12.

🔒 **Owner of ``adherence_logs``** (DB §12: "Writers: `progress`"). The portal
writes through this module's service; nothing else writes the table.

⚠️ This module must not import ``app.platform`` (Arch R5) and must not import
another module (R3). It therefore knows nothing about plans or clients beyond the
identifiers handed to it.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.kernel import Base
from app.kernel.db import pg_enum


class AdherenceValue(str, enum.Enum):
    """What the client reported for one slot — API §12.3, DB §12.1.

    🟡 The value set is marked PROPOSED in DB §12.1; API §12.3 lists the same
    four without a marker, and the sync contract branches on them.

    ⚠️ **Not a 0–100 score.** ``skipped`` and ``not_followed`` are different
    facts — a client who deliberately skipped a mid-morning snack has not
    *failed* to follow it — and a scale cannot hold the difference. Migration
    ``0026`` replaced the score column this distinction could not survive.

    🔒 Lives here rather than in ``kernel``: ``kernel.clinical`` holds the
    clinical vocabulary because other modules read clinical data through a port,
    and nothing reads adherence through one. A kernel enum with a single
    consumer would be surface without a reason.
    """

    FOLLOWED = "followed"
    PARTIAL = "partial"
    NOT_FOLLOWED = "not_followed"
    SKIPPED = "skipped"


class AdherenceLog(Base):
    """One client's report for one slot on one day — DB §12.1, FR-M7-004.

    🔒 **Both timestamps are stored** (API §12.3). A log queued Tuesday and
    synced Thursday is *dated* Tuesday and *recorded* Thursday; collapsing them
    would corrupt adherence history, which is the primary engagement signal.

    🔒 ``slot_type`` is denormalised beside ``plan_slot_id`` so the row stays
    interpretable after the plan is revised (DDR-11, DB §12.1). Justified
    denormalisation per Principle 4.

    **Pattern A + Pattern C RLS**, both forced (migrations ``0025``). The client
    realm reaches this table, so the tenant predicate alone is not the boundary.
    """

    __tablename__ = "adherence_logs"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        comment="🔒 RLS discriminator",
    )
    client_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        comment="🔒 Pattern C discriminator — compared to app.actor_id",
    )

    #: The date the client is reporting *for*, which is not necessarily today.
    logged_for_date: Mapped[date] = mapped_column(Date, nullable=False)

    #: 🔒 Which plan they were following. Resolved server-side; never accepted
    #: from a request, because a client-supplied version id would let a client
    #: attribute their log to a plan they were never issued.
    plan_version_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    plan_slot_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    slot_type: Mapped[str] = mapped_column(Text, nullable=False)

    adherence: Mapped[AdherenceValue] = mapped_column(
        pg_enum(AdherenceValue, "adherence_value"), nullable=False
    )

    #: 🔒 Offline replay safety — the client generates this on its own device.
    idempotency_key: Mapped[str] = mapped_column(
        Text, nullable=False, comment="🔒 Client-side UUID for idempotency"
    )

    #: 🔒 When the client actually acted, from their device clock.
    client_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="🔒 When the client actually logged this offline",
    )
    #: When the server received it. Server-set, so a wrong device clock cannot
    #: move a row in the record of what we were told and when.
    server_recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        comment="🔒 When the server received it",
    )

    #: ⏳ DB §12.1 marks a free-text note Phase 2. Declared so the column exists
    #: where the specification puts it; nothing writes it yet.
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_adherence_logs__tenant_id", "tenant_id"),
        Index("ix_adherence_logs__client_date", "client_id", "logged_for_date"),
        # 🔒 FR-M9-003 — the practitioner's check-in review reads a tenant's logs
        # by date, which the client-scoped index cannot serve.
        Index("ix_adherence_logs__tenant_date", "tenant_id", "logged_for_date"),
        # 🔒 The replay guarantee. Scoped to the client because two clients
        # generate keys on their own devices with no knowledge of each other.
        UniqueConstraint("client_id", "idempotency_key", name="uq_adherence_logs__idempotency"),
    )
