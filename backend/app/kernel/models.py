"""Platform & identity models — DB §4.

🔒 The dependency spine: every tenant-scoped table carries `tenant_id uuid FK →
tenants.id`. These exist before any domain module's tables can.
"""

from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.kernel import Base
from app.kernel.db import pg_enum


class TenantStatus(str, enum.Enum):
    """DB §4.1."""

    TRIAL = "trial"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSED = "closed"


class UserRole(str, enum.Enum):
    """DB §4.2. Practitioner realm only — clients are not users."""

    OWNER = "owner"
    PRACTITIONER = "practitioner"


class UserStatus(str, enum.Enum):
    """DB §4.2, §4.3."""

    INVITED = "invited"
    ACTIVE = "active"
    DISABLED = "disabled"


class AccessStatus(str, enum.Enum):
    """DB §4.4."""

    ACTIVE = "active"
    REVOKED = "revoked"


class LinkPurpose(str, enum.Enum):
    """DB §4.5. Deep-link destinations (FR-M7-013)."""

    PORTAL = "portal"
    ASSESSMENT = "assessment"
    PLAN_VIEW = "plan_view"


class TransportType(str, enum.Enum):
    """Outbound message channels."""

    WHATSAPP = "whatsapp"
    SMS = "sms"
    EMAIL = "email"


class AuthRealm(str, enum.Enum):
    """DB §4.6. 🔒 Three separate realms — FR-M0-004."""

    PRACTITIONER = "practitioner"
    CLIENT = "client"
    OPERATOR = "operator"


class Tenant(Base):
    """DB §4.1 — the isolation and billing boundary.

    🔒 Every tenant-scoped row in the database descends from here. Nothing can
    be created before this table exists (D0).
    """

    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        unique=True,
        comment="🔒 Public enquiry form URL (FR-M2-001)",
    )
    region_code: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="IN", comment="FR-M0-013"
    )
    timezone: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="Asia/Kolkata", comment="NFR-099"
    )
    currency_code: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default="INR", comment="NFR-098"
    )
    status: Mapped[TenantStatus] = mapped_column(
        pg_enum(TenantStatus, "tenant_status"), nullable=False, server_default="trial"
    )
    suspended_at: Mapped[datetime | None]
    data_retention_days: Mapped[int] = mapped_column(
        nullable=False, server_default="2555", comment="NFR-049 — ~7 years"
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        Index("ix_tenants__status", "status"),
        # 🔒 No RLS — platform table (DB §4.1). Access only via kernel.tenancy.
    )


class User(Base):
    """DB §4.2 — practitioners and clinic owners.

    🔒 The `practitioner` realm only. Clients are **not** users (§4.4). No
    password column exists — credentials live in GoTrue (NFR-029, Arch §2.3).
    """

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        index=True,
        comment="🔒 RLS discriminator",
    )
    auth_subject_id: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        unique=True,
        comment="🔒 GoTrue identifier — no password column",
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    mobile: Mapped[str | None] = mapped_column(Text)
    role: Mapped[UserRole] = mapped_column(pg_enum(UserRole, "user_role"), nullable=False)
    status: Mapped[UserStatus] = mapped_column(
        pg_enum(UserStatus, "user_status"), nullable=False, server_default="invited"
    )
    is_two_factor_enabled: Mapped[bool] = mapped_column(
        nullable=False, server_default="false", comment="Phase 2 (FR-M0-008)"
    )
    last_active_at: Mapped[datetime | None]
    archived_at: Mapped[datetime | None] = mapped_column(comment="Soft delete (EC-M1-04)")
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        Index("ix_users__tenant_status", "tenant_id", "status"),
        CheckConstraint(
            "mobile IS NULL OR mobile ~ '^\\+[1-9][0-9]{1,14}$'",
            name="ck_users__mobile_e164",
        ),
        # 🔒 Partial unique: email unique per tenant, excluding archived rows.
        # Two archived users with the same email are permitted; two active are not.
        Index(
            "uq_users__tenant_email",
            "tenant_id",
            "email",
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
    )


class Operator(Base):
    """DB §4.3 — platform staff (P5).

    🔒 Separate realm (FR-M0-004, AC-M11-005). Separate table, not a role on
    `users` — makes cross-realm authentication structurally impossible. No
    `tenant_id` — that is precisely what makes them cross-tenant, and why every
    read is audited (FR-M0-032).
    """

    __tablename__ = "operators"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    auth_subject_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    is_two_factor_enabled: Mapped[bool] = mapped_column(
        nullable=False, server_default="true", comment="🔒 AC-M11-004"
    )
    status: Mapped[UserStatus] = mapped_column(pg_enum(UserStatus, "user_status"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))


class ClientAccessGrant(Base):
    """DB §4.4 — client realm identity anchor.

    🔒 Clients access the portal without being `users` (FR-M0-005). This table
    holds that capability, one per client.
    """

    __tablename__ = "client_access_grants"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        index=True,
        comment="🔒 RLS",
    )
    client_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        unique=True,
        comment="🔒 One grant per client",
    )
    status: Mapped[AccessStatus] = mapped_column(
        pg_enum(AccessStatus, "access_status"), nullable=False, server_default="active"
    )
    last_accessed_at: Mapped[datetime | None] = mapped_column(
        comment="Feeds at-risk detection (FR-M9-001)"
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))


class MagicLink(Base):
    """DB §4.5 — passwordless client access.

    🔒 DDR-04: store a hash, never the token. Single-use (EC-M0-01), short-lived
    (15–30 min approved refinement), audited.
    """

    __tablename__ = "magic_links"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        index=True,
        comment="🔒 RLS",
    )
    client_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True, comment="🔒 Hash only — never the token (DDR-04)"
    )
    purpose: Mapped[LinkPurpose] = mapped_column(
        pg_enum(LinkPurpose, "link_purpose"),
        nullable=False,
        comment="Deep-link destination (FR-M7-013)",
    )
    target_ref: Mapped[str | None] = mapped_column(Text, comment="Deep-link target")
    expires_at: Mapped[datetime] = mapped_column(
        nullable=False, comment="🔒 15-30 min (approved refinement)"
    )
    consumed_at: Mapped[datetime | None] = mapped_column(comment="Single-use enforcement")
    issued_via: Mapped[TransportType] = mapped_column(
        pg_enum(TransportType, "transport_type"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (Index("ix_magic_links__client_expires", "client_id", "expires_at"),)


class Session(Base):
    """DB §4.6 — rotating refresh tokens with reuse detection.

    🔒 DDR-05: secure renewal means each refresh issues a new token and
    invalidates the old. **If a previously-rotated token is presented, the
    entire session family is revoked** — signature of a stolen token.
    """

    __tablename__ = "sessions"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    realm: Mapped[AuthRealm] = mapped_column(
        pg_enum(AuthRealm, "auth_realm"), nullable=False, comment="🔒 Three realms"
    )
    tenant_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id"), comment="NULL for operators"
    )
    subject_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, comment="user / client / operator id"
    )
    refresh_token_hash: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True, comment="🔒 Hash only"
    )
    previous_token_hash: Mapped[str | None] = mapped_column(
        Text, comment="🔒 Reuse detection (DDR-05)"
    )
    issued_at: Mapped[datetime] = mapped_column(nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        nullable=False, comment="~30d clients/practitioners; short operators"
    )
    rotated_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None] = mapped_column(comment="Logout, password change (NFR-042)")
    revocation_reason: Mapped[str | None] = mapped_column(Text)
    user_agent_hash: Mapped[str | None] = mapped_column(
        Text, comment="🔒 Hashed — not raw (NFR-033)"
    )

    __table_args__ = (Index("ix_sessions__subject_expires", "subject_id", "expires_at"),)
