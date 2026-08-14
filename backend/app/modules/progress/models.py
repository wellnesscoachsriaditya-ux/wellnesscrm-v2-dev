"""ORM models for the progress module.

Handles client adherence logs, measurements, and other progress tracking.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.kernel import Base


class AdherenceLog(Base):
    """A client's daily adherence record.

    🔒 Unique per client + idempotency_key to support offline sync without data loss.
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
        comment="Arch §3.4 — no FK to clients module",
    )
    # The client-provided date they are logging for
    for_date: Mapped[date] = mapped_column(Date, nullable=False)
    
    # 🔒 Offline sync idempotency — the frontend provides this.
    idempotency_key: Mapped[str] = mapped_column(
        Text, nullable=False, comment="🔒 Client-side UUID for idempotency"
    )
    
    # We may capture a percentage score (0-100) or enum. Let's stick to an integer score for simplicity.
    # The S6 plan mentions "one-tap adherence", usually meaning 100% or "did it". 
    # Let's store adherence_score from 0 to 100.
    score: Mapped[int] = mapped_column(
        nullable=False, comment="Adherence score (0-100)"
    )
    
    # 🔒 Client and server timestamps to handle offline sync correctly
    client_timestamp: Mapped[datetime] = mapped_column(
        nullable=False, comment="🔒 When the client actually logged this offline"
    )
    server_timestamp: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("now()"), comment="🔒 When the server received it"
    )

    __table_args__ = (
        Index("ix_adherence_logs__tenant_id", "tenant_id"),
        Index("ix_adherence_logs__client_date", "client_id", "for_date"),
        UniqueConstraint("client_id", "idempotency_key", name="uq_adherence_logs__idempotency"),
        CheckConstraint("score >= 0 AND score <= 100", name="ck_adherence_logs__score_range"),
    )
