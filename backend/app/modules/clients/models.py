"""ORM models for the client spine — DB §5.1, §5.3.

🔒 These live in the module, not the kernel. ``clients`` owns these tables
(DB §5: "Writers: `clients` only"), and a model in ``kernel.models`` would be a
table the kernel owns — which is what R6 exists to prevent one module doing to
another.

⚠️ The FKs to ``tenants`` and ``users`` are **not** an R6 violation. Those are
kernel tables, and the kernel is the one thing every module may depend on
(Arch §3.1) — the spine every tenant-scoped row descends from. R6 forbids
referencing another *module's* tables.
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
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.kernel import Base
from app.kernel.clients import ClientStage, DietaryClass, SexType
from app.kernel.clients import is_minor as derive_is_minor
from app.kernel.db import pg_enum


class Client(Base):
    """A person the practice is engaged with — lead or client, one row.

    🔒 **The spine** (DB §5.1). M1.3 — leads and clients are one entity
    distinguished by ``stage``, which is "the single most important modelling
    decision in the schema". Converting a lead changes a column; it does not move
    a record, which is why AC-M1-003 (identity and history retained) is true by
    construction.

    **Pattern A RLS**, forced. Read on every tenant-facing path.

    🔒 **Soft delete only** (FR-M1-010): ``archived_at``, never a DELETE.
    Migration 0009 revokes the privilege. FR-M1-011 puts hard deletion behind the
    DPDP erasure pathway, which runs as the migrator role.

    ⚠️ **No unique constraint on ``mobile``** (DB §5.1, EC-M1-01). Family members
    sharing a handset is a real and common pattern in the launch market;
    duplicate detection is a warning (FR-M1-024), never a constraint.
    """

    __tablename__ = "clients"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        comment="🔒 RLS discriminator",
    )
    stage: Mapped[ClientStage] = mapped_column(
        pg_enum(ClientStage, "client_stage"),
        nullable=False,
        server_default="lead",
        comment="🔒 M1.3 — lead and client are one entity, separated by this",
    )
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    mobile: Mapped[str | None] = mapped_column(
        Text, comment="🔒 E.164 (NFR-100). Not unique — EC-M1-01"
    )
    email: Mapped[str | None] = mapped_column(Text)
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    sex: Mapped[SexType | None] = mapped_column(pg_enum(SexType, "sex_type"))
    city: Mapped[str | None] = mapped_column(Text)
    preferred_language: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="en", comment="NFR-096"
    )
    source: Mapped[str | None] = mapped_column(Text, comment="FR-M2-009")
    source_detail: Mapped[str | None] = mapped_column(Text, comment="Link parameter")
    owner_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="FR-M1-009"
    )
    dietary_class: Mapped[DietaryClass | None] = mapped_column(
        pg_enum(DietaryClass, "dietary_class"),
        comment="🔒 Drives plan filtering (FR-M4-035)",
    )
    # 🔒 FR-M0-028 — minor status is NOT a column. See `is_minor` below.
    activated_at: Mapped[datetime | None] = mapped_column(
        comment="First entry to active — the check-in anchor (FR-M8-023)"
    )
    archived_at: Mapped[datetime | None] = mapped_column(comment="Soft delete (FR-M1-010)")
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    @property
    def is_minor(self) -> bool | None:
        """🔒 FR-M0-028 — derived, never stored.

        DB §5.1 specifies a generated column here. It is not implementable —
        PostgreSQL requires generation expressions to be IMMUTABLE and age is
        not — and a stored flag would be silently wrong the day a client turns
        18, which is the day it matters most. See
        :func:`app.kernel.clients.is_minor`.
        """
        return derive_is_minor(self.date_of_birth)

    __table_args__ = (
        # 🔒 FR-M1-004 / EC-M1-08 — at least one way to reach them.
        CheckConstraint(
            "mobile IS NOT NULL OR email IS NOT NULL", name="ck_clients__contact_present"
        ),
        CheckConstraint(
            "mobile IS NULL OR mobile ~ '^\\+[1-9][0-9]{7,14}$'",
            name="ck_clients__mobile_e164",
        ),
        CheckConstraint(
            "stage <> 'active' OR owner_user_id IS NOT NULL", name="ck_clients__stage_owner"
        ),
        CheckConstraint(
            "activated_at IS NOT NULL OR stage <> 'active'",
            name="ck_clients__active_has_activated_at",
        ),
        # ⚠️ The partial indexes and the generated `search_vector` are created in
        # migration 0009 with raw SQL — SQLAlchemy cannot express a `GENERATED
        # ALWAYS AS ... STORED` tsvector or a `gin_trgm_ops` operator class in a
        # column definition. The migration is authoritative for those.
        Index("ix_clients__tenant_id", "tenant_id"),
    )


class ClientStageHistory(Base):
    """🔒 Every lifecycle transition, append-only — DB §5.3, FR-M1-015.

    🔒 **Separate from ``audit_log``, deliberately.** This is queryable domain
    history: it feeds the timeline (FR-M1-018) and conversion metrics
    (FR-M9-006). The audit log is compliance evidence with different retention
    and immutability rules, and conflating the two would force compliance-grade
    retention onto operational data.

    ``app_user`` holds INSERT and SELECT only (migration 0009), and the table is
    registered in ``ops/db/002_verify_grants.sql``.
    """

    __tablename__ = "client_stage_history"

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
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_stage: Mapped[ClientStage | None] = mapped_column(
        pg_enum(ClientStage, "client_stage"), comment="NULL on creation"
    )
    to_stage: Mapped[ClientStage] = mapped_column(
        pg_enum(ClientStage, "client_stage"), nullable=False
    )
    changed_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), comment="NULL when system-driven"
    )
    reason: Mapped[str | None] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        Index("ix_client_stage_history__tenant_id", "tenant_id"),
        CheckConstraint(
            "from_stage IS NULL OR from_stage <> to_stage",
            name="ck_client_stage_history__actual_transition",
        ),
    )
