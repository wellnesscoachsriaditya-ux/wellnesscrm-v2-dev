"""Platform, identity and queue models — DB §4, §13, §15.3.

🔒 The dependency spine: every tenant-scoped table carries `tenant_id uuid FK →
tenants.id`. These exist before any domain module's tables can.

⚠️ Three RLS dispositions live in this file, and the differences are deliberate:

* **Pattern A** (`users`, `magic_links`, `idempotency_records`) — tenant-isolated
  by policy. The default for anything a request reads or writes under a tenant.
* **Pattern D, unreachable** (`tenants`, `operators`, `sessions`, `audit_log`) —
  platform tables never read on a tenant-facing path.
* **Pattern D, and RLS would break it** (`jobs`, `job_runs`) — the worker claims
  across tenants with no tenant in scope, so a policy keyed on
  `current_tenant_id()` would match nothing and the queue would silently stop.

Getting these confused is not a lint failure; it is either a leak or a queue
that never runs, so each model states which one it is and why.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.kernel import Base
from app.kernel.audit import AuditOutcome
from app.kernel.context import ActorType
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


class ConsentSubjectType(str, enum.Enum):
    """DB §16.4. Who the consent is about.

    `prospect` exists because the enquiry form captures consent before a client
    record does (FR-M2-004) — the ledger entry is identified by
    `subject_mobile_hash` until a `client_id` exists to point at.
    """

    CLIENT = "client"
    PROSPECT = "prospect"


class ConsentAction(str, enum.Enum):
    """DB §16.4. 🔒 The three things a ledger entry can assert.

    `reconfirmed` is distinct from `granted` because FR-M0-029 asks a different
    question — not "does consent exist" but "was it re-obtained against the
    notice version now in force". Collapsing it into `granted` would make a
    material-change re-consent indistinguishable from the original grant.
    """

    GRANTED = "granted"
    WITHDRAWN = "withdrawn"
    RECONFIRMED = "reconfirmed"


class ConsentChannel(str, enum.Enum):
    """DB §16.4. Where the consent was captured."""

    ENQUIRY_FORM = "enquiry_form"
    PORTAL = "portal"
    PRACTITIONER = "practitioner"
    WHATSAPP = "whatsapp"


class DataRequestType(str, enum.Enum):
    """DB §16.5. DPDP data-principal rights (FR-M0-026/027)."""

    ACCESS = "access"
    CORRECTION = "correction"
    ERASURE = "erasure"


class DataRequestStatus(str, enum.Enum):
    """DB §16.5."""

    RECEIVED = "received"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    REJECTED = "rejected"


class AuthTokenPurpose(str, enum.Enum):
    """What a practitioner-realm one-time token authorises.

    🔒 Purpose is stored, not inferred, and checked on redemption. Without it a
    verification token would be redeemable at the password-reset endpoint —
    turning "prove you own this mailbox" into "set a new password", which is
    account takeover via a link the product itself sent.
    """

    EMAIL_VERIFICATION = "email_verification"
    PASSWORD_RESET = "password_reset"


class AuthRealm(str, enum.Enum):
    """DB §4.6. 🔒 Three separate realms — FR-M0-004."""

    PRACTITIONER = "practitioner"
    CLIENT = "client"
    OPERATOR = "operator"


class JobClass(str, enum.Enum):
    """DB §13.1, Arch §11.2 — job category governs timeout and retry."""

    DISPATCH = "dispatch"
    GENERATION = "generation"
    RENDERING = "rendering"
    RECURRING = "recurring"
    MAINTENANCE = "maintenance"


class JobStatus(str, enum.Enum):
    """DB §13.1 — lifecycle of a queued job."""

    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"


class JobOutcome(str, enum.Enum):
    """DB §13.4 — per-attempt result in job_runs."""

    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"


class IdempotencyState(str, enum.Enum):
    """API §13.2 — whether the first call has returned yet.

    🔒 The two-state model is what makes "concurrent duplicate → 409" possible.
    Without it, a replay arriving while the first request is still executing
    would find a row with no response and have nothing to return.
    """

    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"


class SubscriptionStatus(str, enum.Enum):
    """DB §14.2 — the commercial position of one tenant.

    🔒 Five states, not three. ``past_due`` is distinct from ``suspended``
    because a failed payment must not itself stop a practitioner serving their
    clients — suspension is a deliberate act (FR-M10-008). ``cancelled`` is
    distinct from ``suspended`` because EC-M10-05 (payment arriving after
    suspension) has to be recoverable, and a collapsed state could not say what
    to restore.

    ⚠️ Which of these permit a *new* metered action is decided in
    ``kernel.entitlements``, not here. A status is a fact; what it permits is a
    policy, and putting the policy on the enum would put it out of reach of the
    tests that cover the fail-safe.
    """

    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"


class SubscriptionEventType(str, enum.Enum):
    """DB §14.3 — what happened to a subscription, append-only."""

    CREATED = "created"
    ACTIVATED = "activated"
    PLAN_CHANGED = "plan_changed"
    SUSPENDED = "suspended"
    REACTIVATED = "reactivated"
    CANCELLED = "cancelled"


class BillingPeriod(str, enum.Enum):
    """DB §14.1. Annual is billed at ten months' price (PRD M10.4)."""

    MONTHLY = "monthly"
    ANNUAL = "annual"


class FileClass(str, enum.Enum):
    """DB §19.1, Arch §13.2 — what a file is, which decides how it is treated.

    ⚠️ The class is not cosmetic: it determines retention and whether erasure
    must traverse the object (Arch §13.2). Adding a member without deciding both
    is how a clinical document ends up outliving the client it belongs to.
    """

    CLIENT_DOCUMENT = "client_document"
    PLAN_PDF = "plan_pdf"
    INVOICE_PDF = "invoice_pdf"
    BRANDING = "branding"
    EXPORT = "export"


class FileStatus(str, enum.Enum):
    """DB §19.1 — the upload lifecycle (ADR-12).

    🔒 `pending` is the window between "the client says it uploaded" and "we
    checked". `confirmed` means the stored object was verified to exist and to
    match the size and content type that were authorized.

    ⚠️ `quarantined` is not a synonym for `deleted`. A file whose bytes do not
    match what was authorized is a security event, and destroying the evidence
    is the wrong first response.
    """

    PENDING = "pending"
    CONFIRMED = "confirmed"
    QUARANTINED = "quarantined"
    DELETED = "deleted"


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


class AuthToken(Base):
    """Practitioner-realm one-time tokens — email verification and password reset.

    🔒 The same shape as ``magic_links`` (DDR-04): a hash, never the token; short
    expiry; single-use enforced by a conditional update rather than an
    application check. A separate table rather than a `purpose` added to
    ``magic_links`` because that table is client-realm — it carries `client_id`
    and a tenant, neither of which applies to a practitioner resetting a
    password, and one of which (`tenant_id`) does not exist yet at verification
    time.

    ⚠️ Keyed on ``auth_subject_id``, the identity provider's handle, not on
    ``users.id``. Verification happens before the account is usable and password
    reset must work for an account whose user row is archived; both are
    credential operations, and credentials belong to the provider (NFR-029).

    🔒 No RLS. Like ``sessions``, this is a platform table: a reset token is
    presented by someone not yet authenticated, so there is no tenant in scope to
    isolate on. Isolation here is the token's own entropy.
    """

    __tablename__ = "auth_tokens"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    auth_subject_id: Mapped[str] = mapped_column(
        Text, nullable=False, comment="🔒 Identity provider handle — not users.id"
    )
    token_hash: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True, comment="🔒 Hash only — never the token (DDR-04)"
    )
    purpose: Mapped[AuthTokenPurpose] = mapped_column(
        pg_enum(AuthTokenPurpose, "auth_token_purpose"),
        nullable=False,
        comment="🔒 Checked on redemption — a verification token must not reset a password",
    )
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(comment="Single-use enforcement")
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        Index("ix_auth_tokens__subject_purpose", "auth_subject_id", "purpose"),
        Index("ix_auth_tokens__expires", "expires_at"),
    )


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


class AuditLog(Base):
    """DB §15.3 — the immutable record of who did what (FR-M0-031..036).

    🔒 **Append-only at the grant level** (DDR-15). ``app_user`` holds
    ``INSERT`` and ``SELECT`` and nothing else; there is no ``UPDATE`` or
    ``DELETE`` to revoke because none is ever granted. A log the application can
    rewrite proves nothing, and the application is what is being audited.

    🔒 **No foreign keys, deliberately.** ``tenant_id`` and ``resource_id`` are
    references, not relationships. An audited row may later be deleted under a
    retention policy or a DPDP erasure request; a foreign key would either block
    that deletion or cascade away the evidence that it happened. The audit trail
    must outlive what it describes.

    🔒 **No RLS** (Pattern D). This table is not tenant-readable at all — it is
    reached by platform tooling and by ``kernel.audit``, never by a tenant
    query. Adding a tenant-isolation policy would imply practitioners can read
    their audit log, which is a product decision nobody has taken.
    """

    __tablename__ = "audit_log"

    # bigserial, not uuid: this is the highest-volume table in the schema and
    # the only access pattern is an ordered scan within a tenant. A monotonic
    # key keeps that scan on a dense B-tree rather than scattering inserts
    # across it, which is the difference between an append and a rewrite.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    tenant_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), comment="🔒 Reference only — NULL for platform-level actions"
    )
    actor_type: Mapped[ActorType] = mapped_column(
        pg_enum(ActorType, "actor_type"), nullable=False, comment="DB §15.1"
    )
    actor_realm: Mapped[AuthRealm | None] = mapped_column(pg_enum(AuthRealm, "auth_realm"))
    actor_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), comment="user / client / operator id — NULL for system"
    )

    action: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Registered action, e.g. client.update"
    )
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    outcome: Mapped[AuditOutcome] = mapped_column(
        pg_enum(AuditOutcome, "audit_outcome"),
        nullable=False,
        comment="🔒 Denials are recorded, not only successes (FR-M0-033)",
    )

    changed_fields: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text),
        comment="🔒 Field NAMES only — never values (FR-M0-035)",
    )
    # `metadata` is taken by SQLAlchemy's declarative machinery, so the
    # attribute is renamed while the column keeps the name the schema specifies.
    entry_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata",
        JSONB,
        comment="🔒 Allowlisted keys only (DB §15.3)",
    )

    request_id: Mapped[str | None] = mapped_column(Text, comment="Correlates with logs")
    ip_hash: Mapped[str | None] = mapped_column(
        Text, comment="🔒 Hashed — an IP is personal data (NFR-033)"
    )
    occurred_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        # "What happened in this tenant recently" — the support query.
        Index("ix_audit_log__tenant_occurred", "tenant_id", "occurred_at"),
        # "What happened to this record" — the investigation query.
        Index("ix_audit_log__resource", "resource_type", "resource_id"),
        # 🔒 "What did this operator touch" — the one that answers a DPDP
        # enquiry about platform staff access (FR-M0-032).
        Index("ix_audit_log__actor_occurred", "actor_id", "occurred_at"),
    )


class Job(Base):
    """DB §13.1 — the Postgres-backed work queue (ADR-11).

    🔒 **Transactional enqueue is the property that justifies the choice.** A job
    row is written in the same transaction as the change that caused it, so an
    approved plan cannot fail to schedule its delivery and a rolled-back approval
    cannot leave a ghost job. An external broker would need an outbox pattern to
    reproduce this; here it is free.

    🔒 **No RLS** (Pattern D, DB §17.1 — `jobs` is listed as platform data in
    DB §2.2). Not an oversight, and not the same reasoning as `audit_log`:
    the worker claims work *across* tenants, with no tenant in scope. A Pattern A
    policy keyed on `current_tenant_id()` would make the claim query return zero
    rows on every poll, and the queue would silently never run. Tenant scope is
    re-established from `tenant_id` when the handler executes — that is where
    isolation applies, because that is where tenant data is touched.

    ⚠️ **`payload` holds identifiers only** (DB §13.1, NFR-033). A job row is not
    an audit record and must not become a clinical data store. Enforced in
    `kernel.events.to_payload` and `kernel.jobs.validate_payload` rather than by
    convention, because this table is retained, backed up and read by operators.
    """

    __tablename__ = "jobs"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # 🔒 Nullable: platform jobs (quota reset, retention purge) belong to no
    # tenant. No FK — a job may outlive the tenant it referenced under a DPDP
    # erasure, and a cascade would delete the record that the work was done.
    tenant_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), comment="🔒 Reference only — NULL for platform jobs"
    )
    job_type: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Handler key, e.g. send_message"
    )
    job_class: Mapped[JobClass] = mapped_column(
        pg_enum(JobClass, "job_class"),
        nullable=False,
        comment="Governs timeout, retry and priority (Arch §11.2)",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="🔒 IDs only — never clinical data (DB §13.1)"
    )
    status: Mapped[JobStatus] = mapped_column(
        pg_enum(JobStatus, "job_status"), nullable=False, server_default="pending"
    )
    priority: Mapped[int] = mapped_column(
        nullable=False, server_default="100", comment="Lower runs sooner"
    )
    run_after: Mapped[datetime] = mapped_column(
        nullable=False,
        server_default=text("now()"),
        comment="Backoff scheduling — a retry sets this forward",
    )
    attempt_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        nullable=False, comment="🔒 Always 1 for `generation` — see the check constraint"
    )
    claimed_at: Mapped[datetime | None]
    claimed_by: Mapped[str | None] = mapped_column(Text, comment="Worker identity, for diagnosis")
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        comment="🔒 Past this, a recovery sweep returns the job to pending (DB §13.3)"
    )
    last_error: Mapped[str | None] = mapped_column(
        Text, comment="🔒 Sanitised — no clinical data (NFR-033)"
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        Text, comment="Duplicate suppression across enqueues"
    )
    dedupe_key: Mapped[str | None] = mapped_column(
        Text, comment="🟡 Collapses redundant recurring work"
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        # 🔒 The queue's hot path (DB §13.1). Partial on `pending` so the index
        # holds only claimable rows: succeeded jobs accumulate until pruned, and
        # an unfiltered index would grow without bound while the useful portion
        # stays small. Ordered (priority, run_after) to match the claim's ORDER BY.
        Index(
            "ix_jobs__claimable",
            "priority",
            "run_after",
            postgresql_where=text("status = 'pending'"),
        ),
        # 🔒 Drives the lease-expiry sweep (DB §13.3). Partial on the two states
        # that can hold a lease — the sweep is the only reader, and it only ever
        # asks about in-flight work.
        Index(
            "ix_jobs__lease",
            "lease_expires_at",
            postgresql_where=text("status IN ('claimed', 'running')"),
        ),
        # 🔒 Duplicate suppression. Partial because most jobs have no key, so
        # the index stays small.
        #
        # ⚠️ `NULLS NOT DISTINCT` is load-bearing, not a detail. `tenant_id` is
        # NULL for platform jobs (quota reset, retention purge), and under
        # PostgreSQL's default NULL semantics two rows with a NULL tenant never
        # conflict — so the one class of job most likely to be enqueued twice by
        # a scheduler restart would be the one class this index failed to
        # deduplicate. Requires PostgreSQL 15+; we pin 16.4 (ops/db).
        Index(
            "uq_jobs__idempotency",
            "tenant_id",
            "job_type",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
            postgresql_nulls_not_distinct=True,
        ),
        # 🔒 Arch §11.2 — AI generation is never auto-retried, because each
        # attempt costs money and a failure is usually deterministic (malformed
        # output, provider rejection). A constraint rather than a policy lookup:
        # the database refuses a retryable generation job, so no future caller
        # can reintroduce the cost by passing max_attempts=3.
        CheckConstraint(
            "job_class <> 'generation' OR max_attempts = 1",
            name="ck_jobs__generation_not_retried",
        ),
        CheckConstraint("max_attempts >= 1", name="ck_jobs__max_attempts_positive"),
        CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= max_attempts",
            name="ck_jobs__attempt_count_bounded",
        ),
    )


class JobRun(Base):
    """DB §13.4 — one row per attempt.

    🔒 Separate from `jobs` so retry history survives. A job that succeeded on
    attempt 3 must still show that attempts 1 and 2 failed, which a single
    mutable row cannot express — and "why did this take three tries" is the
    question an operator actually asks (FR-M11-004).

    🟡 Pruned after 30 days (DB §26).
    """

    __tablename__ = "job_runs"

    # bigserial for the same reason as `audit_log`: append-only, high churn,
    # read as an ordered scan. A uuid PK would scatter inserts across the index.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        comment="Cascades: pruning a job takes its attempt history with it",
    )
    attempt_number: Mapped[int] = mapped_column(nullable=False)
    started_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    finished_at: Mapped[datetime | None]
    outcome: Mapped[JobOutcome | None] = mapped_column(
        pg_enum(JobOutcome, "job_outcome"),
        comment="NULL while the attempt is in flight",
    )
    error_class: Mapped[str | None] = mapped_column(
        Text, comment="Exception type — safe to group on"
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, comment="🔒 Sanitised — an exception string can carry a row's values"
    )
    duration_ms: Mapped[int | None]
    worker_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # "Show me this job's history" — the operator console query, and the
        # only way to read this table.
        Index("ix_job_runs__job_attempt", "job_id", "attempt_number"),
        # 🔒 One row per attempt, enforced by the database. A retry loop that
        # double-recorded an attempt would make `attempt_count` and the run
        # history disagree, and the history is what an operator trusts.
        Index("uq_job_runs__job_attempt", "job_id", "attempt_number", unique=True),
    )


class IdempotencyRecord(Base):
    """API §13.2 / ADR-A10 — server-stored request idempotency.

    Keyed on `(tenant, endpoint, key)`. A replay with the same payload returns
    the **stored response** rather than re-executing; a replay with a *different*
    payload is a 409, because a reused key with new content is a client bug and
    silently treating it as a replay would return the wrong answer.

    ⚠️ **This table stores response bodies, and a response body contains the
    data the endpoint returned.** That is a deliberate exception to the "IDs
    only" rule that governs `jobs` and `audit_log`, and it is unavoidable: ADR-A10
    requires the *stored* response be returned, since a re-execution that
    produced a different result would defeat the purpose. Three things contain
    the exposure, and all three are load-bearing rather than incidental:

    1. 🔒 **Pattern A RLS**, unlike `jobs`. These rows are written and read
       inside a tenant-scoped request transaction and never crossed by the
       worker, so the policy that would break the queue is exactly right here.
    2. 🔒 **`expires_at` is NOT NULL** — 24 hours (API §13.2). Long enough for
       any realistic retry, short enough that this is a cache rather than a
       second copy of the record.
    3. **`state`** distinguishes in-flight from completed, which is what makes
       the concurrent-duplicate case a 409 instead of a race on a half-written
       response.
    """

    __tablename__ = "idempotency_records"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # NOT NULL, unlike `jobs.tenant_id`: every endpoint requiring an
    # Idempotency-Key is tenant-scoped (API §13.1). A nullable column here would
    # also break the unique constraint, since NULL never equals NULL in
    # PostgreSQL and two platform-level replays would both insert.
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        index=True,
        comment="🔒 RLS discriminator",
    )
    endpoint: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Route template — keys are scoped per endpoint"
    )
    idempotency_key: Mapped[str] = mapped_column(
        Text, nullable=False, comment="🔒 Client-generated UUID (API §13.2)"
    )
    request_fingerprint: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="🔒 Hash of the request body — same key + different body is a 409",
    )
    state: Mapped[IdempotencyState] = mapped_column(
        pg_enum(IdempotencyState, "idempotency_state"),
        nullable=False,
        server_default="in_flight",
    )
    response_status: Mapped[int | None] = mapped_column(comment="NULL until the first call returns")
    response_body: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="⚠️ The stored response — see the class docstring"
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    completed_at: Mapped[datetime | None]
    expires_at: Mapped[datetime] = mapped_column(
        nullable=False, comment="🔒 24h (API §13.2) — bounds how long a response is retained"
    )

    __table_args__ = (
        # 🔒 ADR-A10's key. Unique because the whole contract depends on a second
        # request with this triple finding the first one rather than creating a
        # sibling — which is a race no application-level check can close.
        Index(
            "uq_idempotency_records__key",
            "tenant_id",
            "endpoint",
            "idempotency_key",
            unique=True,
        ),
        # Drives the expiry sweep.
        Index("ix_idempotency_records__expires", "expires_at"),
    )


class ConsentPurpose(Base):
    """What we might process personal data for, and why — DB §16.2.

    🔒 FR-M0-022 — consent is captured **per purpose**, itemised, never blanket.
    A single "I agree to the terms" checkbox is the thing this table exists to
    make impossible to express.

    **Pattern D, platform-wide catalogue.** No `tenant_id`: purposes are defined
    by us as the data fiduciary, not per tenant. A tenant inventing its own
    processing purposes is a compliance problem, not a feature.

    ⚠️ `is_essential` is the legally consequential column (ASM-10, OD-05).
    Essential purposes cannot be withdrawn while the relationship is active;
    marking too much essential defeats FR-M0-024's requirement that withdrawal
    be as easy as granting. The seed values are 🟡 PROPOSED pending the privacy
    lawyer review.
    """

    __tablename__ = "consent_purposes"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    code: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True, comment="Stable identifier, e.g. service_delivery"
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(
        Text, nullable=False, comment="🔒 Plain-language — shown to the data principal"
    )
    is_essential: Mapped[bool] = mapped_column(
        nullable=False,
        server_default=text("false"),
        comment="🔒 Required for service delivery; withdrawal refused while active",
    )
    data_categories: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, comment="What classes of data this purpose covers"
    )
    retention_days: Mapped[int | None] = mapped_column(
        comment="NULL = retained for the life of the relationship (NFR-049)"
    )
    legal_basis: Mapped[str] = mapped_column(
        Text, nullable=False, comment="DPDP basis — consent, contract, legal obligation"
    )
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        # 🔒 Marketing can never be essential — the one rule in this table that
        # is a legal invariant rather than a policy choice, so it is a constraint
        # rather than a seed-data convention someone can later edit.
        CheckConstraint(
            "NOT (code = 'marketing' AND is_essential)",
            name="ck_consent_purposes__marketing_not_essential",
        ),
    )


class ConsentNotice(Base):
    """The versioned notice text actually presented to a person — DB §16.3.

    🔒 **Immutable once effective.** A `consent_records` row references the exact
    notice version presented, and that reference is what makes NFR-051 —
    "produce the consent basis for any client" — answerable at all. Editing the
    body of a live notice would silently rewrite the basis of every consent
    already given against it, which is why the enforcement is a grant in the
    migration and not a convention here.

    `requires_reconsent` marks a material change (FR-M0-029): superseding a
    notice with this set means existing grants no longer cover the processing
    and must be re-obtained rather than carried forward.
    """

    __tablename__ = "consent_notices"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # uuid[] rather than a join table: a notice's purpose set is fixed at
    # creation and only ever read as a whole. A join table would imply the set
    # is editable, which for an immutable notice it is not.
    purpose_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)),
        nullable=False,
        comment="🔒 The purposes this notice covers — fixed at creation",
    )
    version: Mapped[str] = mapped_column(Text, nullable=False)
    locale: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="en-IN", comment="India-first (NFR-058)"
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, comment="🔒 Immutable once effective")
    requires_reconsent: Mapped[bool] = mapped_column(
        nullable=False,
        server_default=text("false"),
        comment="🔒 FR-M0-029 — a material change invalidates prior grants",
    )
    effective_from: Mapped[datetime] = mapped_column(nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(
        comment="NULL = currently in force for this locale"
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        Index("uq_consent_notices__version_locale", "version", "locale", unique=True),
        # "Which notice is in force for this locale" — the capture-path query.
        Index("ix_consent_notices__locale_effective", "locale", "effective_from"),
    )


class ConsentRecord(Base):
    """🔒 **The ledger.** Append-only grants and withdrawals — DB §16.4.

    🔒 NFR-047, DDR-15. Same immutability mechanism as `audit_log`: `app_user`
    holds INSERT and SELECT and nothing else. This is the legal record of a
    consent decision; the application must not be able to revise it.

    🔒 **Current state is derived, never stored** (DB §16.3) — the latest record
    per `(subject, purpose)`. There is deliberately no `is_consented` column,
    because a mutable flag and an append-only ledger would eventually disagree,
    and at that point the flag is what code reads while the ledger is what a
    regulator reads.

    **Pattern D, unreachable.** Carries `tenant_id` but no RLS policy, for the
    same reason as `audit_log`: it is never read on a tenant-facing path — the
    portal shows a client their own consents through `kernel.consent`, not by
    querying this table — and entries are written on the anonymous enquiry path
    where no tenant context is yet established.

    ⚠️ 🔒 OD-05 (verifiable parental consent for under-18s) is an unresolved
    launch blocker. The guardian columns exist; the verification *mechanism*
    needs legal advice. Nutritionists routinely see teenage clients, so this is
    not an edge case that can be deferred indefinitely.
    """

    __tablename__ = "consent_records"

    # bigserial, matching audit_log: append-only, written on every consent
    # interaction, read as an ordered scan per subject.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Reference only, no FK — same reasoning as audit_log. An erasure request
    # deletes the client; a FK would either block that or cascade away the proof
    # that consent was ever given, and the basis must outlive the record.
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, comment="🔒 Reference only — see the class docstring"
    )
    subject_type: Mapped[ConsentSubjectType] = mapped_column(
        pg_enum(ConsentSubjectType, "consent_subject"), nullable=False
    )
    subject_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), comment="Client id once one exists"
    )
    subject_mobile_hash: Mapped[str | None] = mapped_column(
        Text,
        comment="🔒 Pre-client consent (FR-M2-004) — hashed, not plain",
    )

    purpose_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("consent_purposes.id"), nullable=False
    )
    notice_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("consent_notices.id"),
        nullable=False,
        comment="🔒 The exact version presented (NFR-051)",
    )
    action: Mapped[ConsentAction] = mapped_column(
        pg_enum(ConsentAction, "consent_action"), nullable=False
    )
    captured_via: Mapped[ConsentChannel] = mapped_column(
        pg_enum(ConsentChannel, "consent_channel"), nullable=False
    )
    captured_by_actor_type: Mapped[ActorType] = mapped_column(
        pg_enum(ActorType, "actor_type"), nullable=False
    )
    captured_by_actor_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), comment="NULL for anonymous and system capture"
    )

    guardian_name: Mapped[str | None] = mapped_column(Text, comment="🔒 FR-M0-028 — minors")
    guardian_relationship: Mapped[str | None] = mapped_column(Text)
    guardian_verification_method: Mapped[str | None] = mapped_column(
        Text, comment="⚠️ OD-05 unresolved — mechanism pending legal advice"
    )

    evidence: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        comment="🔒 Non-clinical only: notice hash, UI version, timestamp",
    )
    occurred_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        # 🔒 The derived-state query: latest row per (subject, purpose). Ordered
        # descending on id so the read is a backwards index scan stopping at the
        # first row, not a sort over a subject's whole consent history.
        Index(
            "ix_consent_records__subject_purpose",
            "tenant_id",
            "subject_id",
            "purpose_id",
            text("id DESC"),
        ),
        # 🔒 The enquiry-form path, where there is no subject_id yet.
        Index(
            "ix_consent_records__mobile_purpose",
            "tenant_id",
            "subject_mobile_hash",
            "purpose_id",
            text("id DESC"),
        ),
        # 🔒 A ledger entry nobody can be matched to is not evidence of
        # anything, so one identifier is mandatory. Enforced here because the
        # anonymous capture path is exactly where subject_id is legitimately
        # absent and a NOT NULL on either column alone would be wrong.
        CheckConstraint(
            "subject_id IS NOT NULL OR subject_mobile_hash IS NOT NULL",
            name="ck_consent_records__subject_identified",
        ),
    )


class DataRequest(Base):
    """Access, correction and erasure requests — DB §16.5.

    🔒 FR-M0-026/027, NFR-048. The DPDP data-principal rights workflow. Unlike
    the ledger this table is mutable: a request moves through states, so it is
    Pattern A with RLS.

    ⚠️ 🔒 **Erasure must traverse object storage** (Arch §13.2) — deleting a
    `client_documents` row while the file itself persists in the bucket is a
    compliance failure and a routine oversight. `erasure_scope` records what was
    actually traversed so the completion is auditable rather than asserted.
    **Launch gate.**
    """

    __tablename__ = "data_requests"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        index=True,
        comment="🔒 RLS discriminator (Pattern A)",
    )
    client_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        comment="Reference only — survives the erasure it may describe",
    )
    request_type: Mapped[DataRequestType] = mapped_column(
        pg_enum(DataRequestType, "data_request_type"), nullable=False
    )
    status: Mapped[DataRequestStatus] = mapped_column(
        pg_enum(DataRequestStatus, "data_request_status"),
        nullable=False,
        server_default="received",
    )
    requested_via: Mapped[ConsentChannel] = mapped_column(
        pg_enum(ConsentChannel, "consent_channel"), nullable=False
    )
    requested_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    # 🟡 The statutory deadline is pending ASM-10. Stored per row rather than
    # computed on read so that a later change to the legal period does not
    # retroactively alter the due date of a request already in flight.
    due_by: Mapped[datetime] = mapped_column(nullable=False, comment="🟡 Period pending ASM-10")
    completed_at: Mapped[datetime | None]
    handled_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), comment="user or operator id"
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    export_file_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), comment="Set for a fulfilled access request"
    )
    erasure_scope: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="⚠️ What was traversed, storage included (Arch §13.2)"
    )

    __table_args__ = (
        # The operator queue: what is outstanding, oldest deadline first.
        Index("ix_data_requests__status_due", "tenant_id", "status", "due_by"),
        # 🔒 A rejection without a reason is not a defensible response to a
        # statutory request, and a completion without a timestamp cannot be
        # shown to have met the deadline.
        CheckConstraint(
            "(status <> 'rejected' OR rejection_reason IS NOT NULL)"
            " AND (status <> 'completed' OR completed_at IS NOT NULL)",
            name="ck_data_requests__terminal_state_evidenced",
        ),
    )


# ─── Entitlements (DB §14) ───────────────────────────────────────────────
#
# 🔒 Enforcement is structural in S1; collection is deferred to M10.3. None of
# these models charges anyone — they decide whether a metered action is
# permitted and record what was consumed.
#
# ⚠️ RLS dispositions differ, deliberately. `plan_definitions` is Pattern D
# (platform-wide catalogue, no `tenant_id` to key a policy on). The other four
# are Pattern A: unlike `consent_records`, every one of them *is* read on a
# tenant-facing path with a tenant in scope, because the enforcement check runs
# inside a request. So AC-M0-003 covers these four and does not cover the
# catalogue.


class PlanDefinition(Base):
    """A plan, as configuration — DB §14.1, FR-M10-001.

    🔒 **Versioned, never mutated.** `effective_from`/`effective_to` mean a
    tenant on last year's pricing keeps it. Editing a plan row in place would
    silently reprice every existing customer on it, which is why the unique
    constraint is on `(code, effective_from)` rather than on `code`.

    🔒 **`limits` is `jsonb`** — the one place DB §14.1 permits it for
    configuration, because FR-M10-001 requires that adding a metered resource
    not require a migration. The keys are read through
    `kernel.entitlements.ResourceCode.limit_key`, which is where a typo is
    caught; a `CHECK` that the value is an object is the most the database can
    usefully assert.

    **Pattern D, platform-wide.** No `tenant_id`: plans are ours. A tenant
    editing its own limits is the entitlement system defeating itself, so
    migration 0007 revokes tenant writes outright.
    """

    __tablename__ = "plan_definitions"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # 🔒 Text, not an enum — FR-M10-001. A new tier must not need a migration.
    code: Mapped[str] = mapped_column(
        Text, nullable=False, comment="free | starter | growth | clinic (FR-M10-001)"
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # 🔒 numeric(10,2), never float — this feeds GST invoices (FR-M10-011).
    price_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default="INR", comment="NFR-098"
    )
    billing_period: Mapped[BillingPeriod] = mapped_column(
        pg_enum(BillingPeriod, "billing_period"), nullable=False
    )
    limits: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="🔒 Keyed by ResourceCode.limit_key (DB §14.1)"
    )
    features: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    is_public: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    effective_from: Mapped[datetime] = mapped_column(nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(
        comment="NULL = currently in force (DB §14.1)"
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("code", "effective_from", name="uq_plan_definitions__code_effective"),
        Index("ix_plan_definitions__code_effective", "code", "effective_from"),
        CheckConstraint("price_amount >= 0", name="ck_plan_definitions__price_non_negative"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_plan_definitions__effective_window_ordered",
        ),
        # 🔒 A plan whose limits are not an object cannot be read by the
        # enforcement path, and the failure would surface as a fail-safe denial
        # for every tenant on that plan rather than as a bad row.
        CheckConstraint(
            "jsonb_typeof(limits) = 'object'", name="ck_plan_definitions__limits_is_object"
        ),
    )


class Subscription(Base):
    """One tenant's current plan — DB §14.2.

    🔒 **Exactly one row per tenant** (unique on `tenant_id`). History lives in
    `SubscriptionEvent`; a second row here would make "which plan is this tenant
    on" a question with two answers, on the hot path.

    🔒 **Read-only to the application** (migration 0007). FR-M10-008 makes
    activation a manual operator action at MVP, so `app_user` holds SELECT and
    nothing else — a tenant that could write this row could upgrade itself.
    """

    __tablename__ = "subscriptions"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        comment="🔒 RLS discriminator",
    )
    plan_definition_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("plan_definitions.id"), nullable=False
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        pg_enum(SubscriptionStatus, "subscription_status"),
        nullable=False,
        server_default="trialing",
    )
    trial_ends_on: Mapped[date | None] = mapped_column(Date)
    current_period_start: Mapped[datetime | None]
    current_period_end: Mapped[datetime | None]
    cancel_at_period_end: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    activated_by_operator_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("operators.id"),
        comment="🔒 FR-M10-008 — manual activation at MVP",
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_subscriptions__tenant"),
        CheckConstraint(
            "current_period_end IS NULL"
            " OR current_period_start IS NULL"
            " OR current_period_end > current_period_start",
            name="ck_subscriptions__period_ordered",
        ),
    )


class SubscriptionEvent(Base):
    """🔒 Append-only history of a subscription — DB §14.3.

    EC-M10-01 (downgrade while over limit) and EC-M10-05 (payment after
    suspension) both turn on *when* state changed, not on what it is now.
    `app_user` holds INSERT and SELECT only (migration 0007), and the table is
    registered in `ops/db/002_verify_grants.sql`.

    ⚠️ `tenant_id` is redundant against `subscriptions.tenant_id` and carried
    anyway, purely so a Pattern A policy can be keyed on it. A policy joining to
    `subscriptions` to reach the tenant would put a subquery in front of every
    insert on the activation path.
    """

    __tablename__ = "subscription_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subscription_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=False
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        comment="🔒 RLS discriminator — denormalised deliberately",
    )
    event_type: Mapped[SubscriptionEventType] = mapped_column(
        pg_enum(SubscriptionEventType, "subscription_event"), nullable=False
    )
    from_plan_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    to_plan_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    actor_type: Mapped[ActorType] = mapped_column(pg_enum(ActorType, "actor_type"), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    reason: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        Index("ix_subscription_events__subscription", "subscription_id", "occurred_at"),
        Index("ix_subscription_events__tenant_id", "tenant_id"),
        # 🔒 A plan change with no destination is not a plan change. Narrow on
        # purpose: a suspension does not move anyone.
        CheckConstraint(
            "event_type NOT IN ('plan_changed', 'activated') OR to_plan_id IS NOT NULL",
            name="ck_subscription_events__plan_move_has_destination",
        ),
    )


class UsageCounter(Base):
    """O(1) enforcement state for one resource in one period — DB §14.4.

    📌 **DDR-14.** Counting live from source tables is cheap for active clients
    and expensive for AI generations and messages, and cross-module counting
    violates Arch R6. This row answers the enforcement question in one indexed
    read; `UsageEvent` is what makes it reconcilable when it drifts.

    🔒 **`limit_amount` is snapshotted from the plan**, not read through the FK.
    A mid-period plan change would otherwise retroactively change what the tenant
    was allowed to do earlier in the same period, and an 80% warning already sent
    would refer to a limit that no longer exists.

    ⚠️ **No counter row exists for `active_clients`** — DB §14.4 counts that one
    live from `clients WHERE stage='active'` (M1.5). It is the product's most
    visible limit, where a drifting counter would produce a bill the practitioner
    can disprove by eye.
    """

    __tablename__ = "usage_counters"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        comment="🔒 RLS discriminator",
    )
    # 🔒 Text, not an enum — FR-M10-001. Valid values are ResourceCode.
    resource_code: Mapped[str] = mapped_column(
        Text, nullable=False, comment="kernel.entitlements.ResourceCode"
    )
    period_start: Mapped[datetime] = mapped_column(nullable=False)
    period_end: Mapped[datetime] = mapped_column(nullable=False)
    # 🔒 numeric, not integer: storage is metered in fractional MB.
    used_amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False, server_default=text("0"))
    limit_amount: Mapped[Decimal | None] = mapped_column(
        Numeric, comment="🔒 Snapshotted from the plan; NULL = unlimited"
    )
    warned_at_80pct: Mapped[datetime | None] = mapped_column(
        comment="🔒 FR-M10-005 — stamped so the warning is sent once"
    )
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        # 🔒 What makes the increment an upsert rather than a read-modify-write
        # race between two concurrent metered actions.
        UniqueConstraint(
            "tenant_id",
            "resource_code",
            "period_start",
            name="uq_usage_counters__tenant_resource_period",
        ),
        Index("ix_usage_counters__tenant_resource", "tenant_id", "resource_code", "period_start"),
        CheckConstraint("period_end > period_start", name="ck_usage_counters__period_ordered"),
        # ⚠️ Not `> 0`. A compensating negative event can legitimately bring a
        # counter back to zero, and clamping would hide a double refund.
        CheckConstraint("used_amount >= 0", name="ck_usage_counters__used_non_negative"),
        CheckConstraint(
            "limit_amount IS NULL OR limit_amount >= 0",
            name="ck_usage_counters__limit_non_negative",
        ),
    )


class UsageEvent(Base):
    """One metered consumption — DB §14.5.

    🔒 **EC-M10-04 — `amount` is signed.** An action that consumed quota and then
    failed is corrected by a *compensating negative event*, never by editing the
    event that consumed it and never by decrementing the counter alone. The log
    is the thing a drifted counter is recovered from, so it has to stay true.

    ⚠️ **Append-only with one exception.** `is_reconciled` is bookkeeping a
    reconciliation pass must be able to set, so this table keeps UPDATE and loses
    DELETE, and `trg_usage_events__immutable` (migration 0007) rejects edits to
    every other column. That is the `ConsentNotice` pattern, chosen for the same
    reason: a grant cannot express "every column but one". It is therefore
    deliberately **absent** from `ops/db/002_verify_grants.sql`, which asserts
    neither UPDATE nor DELETE.
    """

    __tablename__ = "usage_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        comment="🔒 RLS discriminator",
    )
    resource_code: Mapped[str] = mapped_column(
        Text, nullable=False, comment="kernel.entitlements.ResourceCode"
    )
    amount: Mapped[Decimal] = mapped_column(
        Numeric, nullable=False, comment="🔒 Signed — negative compensates (EC-M10-04)"
    )
    # 🔒 Arch R6 — the emitting module by name, not a FK to its tables.
    source_module: Mapped[str] = mapped_column(Text, nullable=False)
    source_record_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    occurred_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    is_reconciled: Mapped[bool] = mapped_column(
        nullable=False,
        server_default=text("false"),
        comment="🔒 The only mutable column — see trg_usage_events__immutable",
    )

    __table_args__ = (
        Index("ix_usage_events__tenant_resource_time", "tenant_id", "resource_code", "occurred_at"),
        # A zero-amount event records nothing and would only dilute the log.
        CheckConstraint("amount <> 0", name="ck_usage_events__amount_non_zero"),
    )


# ─── Storage (DB §19) ────────────────────────────────────────────────────


class File(Base):
    """Metadata for one object in storage — DB §19.1, Arch §13.

    🔒 The bytes are never here and never pass through FastAPI (ADR-12). This row
    is two things: the authorization record consulted on every retrieval
    (NFR-035), and the index DPDP erasure traverses to find objects that must be
    destroyed alongside the database rows (FR-M0-027, Arch §13.2).

    📌 **ADR-12 — created on confirmation, not on request.** An abandoned upload
    leaves an orphan object for the reaper, not a phantom row nothing completes.

    **Pattern A RLS.** Read on a tenant-facing path — every download authorizes
    first — so the policy is the isolation boundary and AC-M0-003 covers it.

    🔒 **Soft delete only.** Migration 0008 revokes DELETE: the record is what
    proves an object existed, and erasure has to be able to show that the object
    a row pointed at was actually destroyed.
    """

    __tablename__ = "files"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        comment="🔒 RLS discriminator",
    )
    # 🔒 Opaque, tenant-scoped, non-enumerable (Arch §13.1). Globally unique: the
    # key already contains the tenant, so a global constraint makes reuse across
    # tenants impossible rather than merely unlikely.
    storage_key: Mapped[str] = mapped_column(
        Text, nullable=False, comment="🔒 Opaque and non-enumerable — never derived from the name"
    )
    bucket: Mapped[str] = mapped_column(Text, nullable=False)
    # ⚠️ Display only. Never used to build a storage path — deriving a path from
    # a user-supplied name is how a traversal gets in.
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(
        Text, nullable=False, comment="🔒 Allowlisted in kernel.storage (NFR-036)"
    )
    size_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="Quota input (FR-M0-040)"
    )
    checksum: Mapped[str | None] = mapped_column(Text)
    file_class: Mapped[FileClass] = mapped_column(pg_enum(FileClass, "file_class"), nullable=False)
    contains_clinical_data: Mapped[bool] = mapped_column(
        nullable=False, comment="🔒 The erasure index (Arch §13.2)"
    )
    uploaded_by_actor_type: Mapped[ActorType] = mapped_column(
        pg_enum(ActorType, "actor_type"), nullable=False
    )
    uploaded_by_actor_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    status: Mapped[FileStatus] = mapped_column(
        pg_enum(FileStatus, "file_status"), nullable=False, server_default="pending"
    )
    confirmed_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None] = mapped_column(
        comment="Soft delete; a purge job removes the bytes afterwards"
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_files__storage_key"),
        Index("ix_files__tenant_id", "tenant_id"),
        Index("ix_files__tenant_clinical", "tenant_id", "contains_clinical_data"),
        Index("ix_files__status_created", "status", "created_at"),
        # 🔒 A zero-byte file is a failed upload reporting success.
        CheckConstraint("size_bytes > 0", name="ck_files__size_positive"),
        # 🔒 A confirmed file has a confirmation time; a deleted file has a
        # deletion time. Without this a soft delete that forgot its timestamp
        # would leave the purge job unable to tell what is due.
        CheckConstraint(
            "(status <> 'confirmed' OR confirmed_at IS NOT NULL)"
            " AND (status <> 'deleted' OR deleted_at IS NOT NULL)",
            name="ck_files__lifecycle_timestamped",
        ),
    )
