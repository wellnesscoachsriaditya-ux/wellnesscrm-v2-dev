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
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, Text, text
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
