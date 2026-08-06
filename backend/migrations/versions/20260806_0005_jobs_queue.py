"""Job queue — the Postgres-backed work queue with SKIP LOCKED claiming.

Revision ID: 0005_jobs_queue
Revises: 0004_auth_tokens
Created: 2026-08-06

🔒 **ADR-11 — the property that justifies this choice.** A job row is written in
the same transaction as the change that caused it, so an approved plan cannot
fail to schedule its delivery and a rolled-back approval cannot leave a ghost
job. An external broker would need an outbox pattern to reproduce this; here it
is free (Arch §11.1).

Three tables:

* **`jobs`** — the queue. Claimed with `SELECT ... FOR UPDATE SKIP LOCKED`,
  which is the primitive that makes multiple workers safe without a lock
  manager. Correct with one worker today (Arch §11.3) and correct with five
  later — concurrency safety is built in now because retrofitting it is far
  harder than including it.
* **`job_runs`** — one row per attempt. Separate from `jobs` so retry history
  survives: a job that succeeded on attempt 3 must still show that attempts 1
  and 2 failed, and the operator console question is "why did this take three
  tries" (FR-M11-004).
* **`idempotency_records`** — API §13.2 / ADR-A10. Server-stored request
  idempotency for POST endpoints where the client supplies an `Idempotency-Key`.
  A replay with the same payload returns the **stored response** rather than
  re-executing; a replay with a different payload is a 409.

🔒 **No RLS on `jobs` and `job_runs`** (Pattern D, DB §17.1). The worker claims
work across tenants with no tenant in scope, so a Pattern A policy keyed on
`current_tenant_id()` would match nothing and the queue would silently never run.
Tenant scope is re-established from `tenant_id` when the handler executes.

🔒 **Pattern A RLS on `idempotency_records`**. These are written and read inside
a tenant-scoped request transaction and never crossed by the worker, so the
policy that would break the queue is exactly right here.

⚠️ **`jobs.payload` holds identifiers only** (DB §13.1, NFR-033). A job row must
not become a clinical data store. Enforced in `kernel.events.to_payload` and
`kernel.jobs.validate_payload` rather than by convention, because this table is
retained, backed up and read by operators.

⚠️ **`idempotency_records.response_body` is the exception.** ADR-A10 requires the
stored response be returned, since a re-execution that produced a different
result would defeat the purpose. Three things contain the exposure: (1) Pattern A
RLS, (2) `expires_at` NOT NULL — 24 hours (API §13.2), and (3) `state`
distinguishes in-flight from completed, making the concurrent-duplicate case a
409 instead of a race on a half-written response.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_jobs_queue"
down_revision: str | None = "0004_auth_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ENUMS: dict[str, tuple[str, ...]] = {
    "job_class": ("dispatch", "generation", "rendering", "recurring", "maintenance"),
    "job_status": ("pending", "claimed", "running", "succeeded", "failed", "dead"),
    "job_outcome": ("success", "failure", "timeout"),
    "idempotency_state": ("in_flight", "completed"),
}


def _enum(name: str) -> postgresql.ENUM:
    """Reference an existing enum type without re-emitting its DDL (see 0002)."""
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    for name, values in _ENUMS.items():
        sa.Enum(*values, name=name).create(op.get_bind(), checkfirst=False)

    # ─── jobs (DB §13.1) ──────────────────────────────────────────────────
    op.create_table(
        "jobs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        # 🔒 Nullable: platform jobs (quota reset, retention purge) belong to no
        # tenant. No FK — a job may outlive the tenant it referenced under a DPDP
        # erasure, and a cascade would delete the record that the work was done.
        sa.Column("tenant_id", sa.UUID(), nullable=True),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column("job_class", _enum("job_class"), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", _enum("job_status"), nullable=False, server_default="pending"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("dedupe_key", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        # 🔒 Arch §11.2 — AI generation is never auto-retried, because each
        # attempt costs money and a failure is usually deterministic. A
        # constraint rather than a policy lookup: the database refuses it, so no
        # future caller can reintroduce the cost by passing max_attempts=3.
        sa.CheckConstraint(
            "job_class <> 'generation' OR max_attempts = 1",
            name="ck_jobs__generation_not_retried",
        ),
        sa.CheckConstraint("max_attempts >= 1", name="ck_jobs__max_attempts_positive"),
        sa.CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= max_attempts",
            name="ck_jobs__attempt_count_bounded",
        ),
    )

    # 🔒 The queue's hot path (DB §13.1). Partial on `pending` so the index
    # holds only claimable rows: succeeded jobs accumulate until pruned, and an
    # unfiltered index would grow without bound while the useful portion stays
    # small. Ordered (priority, run_after) to match the claim's ORDER BY.
    op.create_index(
        "ix_jobs__claimable",
        "jobs",
        ["priority", "run_after"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    # 🔒 Drives the lease-expiry sweep (DB §13.3). Partial on the two states
    # that can hold a lease — the sweep is the only reader, and it only ever
    # asks about in-flight work.
    op.create_index(
        "ix_jobs__lease",
        "jobs",
        ["lease_expires_at"],
        postgresql_where=sa.text("status IN ('claimed', 'running')"),
    )
    # 🔒 Duplicate suppression. Partial because most jobs have no key, so the
    # index stays small.
    #
    # ⚠️ `NULLS NOT DISTINCT` is load-bearing, not a detail. `tenant_id` is NULL
    # for platform jobs (quota reset, retention purge), and under PostgreSQL's
    # default NULL semantics two rows with a NULL tenant never conflict — so the
    # one class of job most likely to be enqueued twice by a scheduler restart
    # would be the one class this index failed to deduplicate. Requires
    # PostgreSQL 15+; ops/db pins 16.4.
    op.create_index(
        "uq_jobs__idempotency",
        "jobs",
        ["tenant_id", "job_type", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
        postgresql_nulls_not_distinct=True,
    )

    # ─── job_runs (DB §13.4) ──────────────────────────────────────────────
    op.create_table(
        "job_runs",
        # bigserial for the same reason as `audit_log`: append-only, high churn,
        # read as an ordered scan. A uuid PK would scatter inserts.
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", _enum("job_outcome"), nullable=True),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_job_runs"),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name="fk_job_runs__job",
            ondelete="CASCADE",
        ),
    )

    # "Show me this job's history" — the operator console query.
    op.create_index("ix_job_runs__job_attempt", "job_runs", ["job_id", "attempt_number"])
    # 🔒 One row per attempt, enforced by the database.
    op.create_index(
        "uq_job_runs__job_attempt",
        "job_runs",
        ["job_id", "attempt_number"],
        unique=True,
    )

    # ─── idempotency_records (API §13.2, ADR-A10) ─────────────────────────
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_fingerprint", sa.Text(), nullable=False),
        sa.Column(
            "state",
            _enum("idempotency_state"),
            nullable=False,
            server_default="in_flight",
        ),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_idempotency_records"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_idempotency_records__tenant",
        ),
    )

    # 🔒 ADR-A10's key. Unique because the whole contract depends on a second
    # request with this triple finding the first one rather than creating a
    # sibling — which is a race no application-level check can close.
    op.create_index(
        "uq_idempotency_records__key",
        "idempotency_records",
        ["tenant_id", "endpoint", "idempotency_key"],
        unique=True,
    )
    # Tenant for RLS.
    op.create_index(
        "ix_idempotency_records__tenant",
        "idempotency_records",
        ["tenant_id"],
    )
    # Drives the expiry sweep.
    op.create_index(
        "ix_idempotency_records__expires",
        "idempotency_records",
        ["expires_at"],
    )

    # ─── Row Level Security ───────────────────────────────────────────────
    #
    # 🔒 Pattern A RLS ONLY on `idempotency_records`. These rows are written
    # and read inside a tenant-scoped request transaction and never crossed by
    # the worker, so the policy works. `jobs` and `job_runs` are deliberately
    # absent — the worker claims work across tenants with no tenant in scope,
    # so a policy keyed on `current_tenant_id()` would match nothing and the
    # queue would silently never run.

    op.execute("ALTER TABLE idempotency_records ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE idempotency_records FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY idempotency_records__tenant_isolation ON idempotency_records
        USING (tenant_id = current_tenant_id())
        WITH CHECK (tenant_id = current_tenant_id());
        """
    )


def downgrade() -> None:
    """Drop everything this revision created, in dependency order.

    ⚠️ Destroys all queued, in-flight and completed jobs, and all idempotency
    records. This exists so the chain is honestly reversible in development; it
    is not a production operation.
    """
    op.drop_table("idempotency_records")
    op.drop_table("job_runs")
    op.drop_table("jobs")

    for name in _ENUMS:
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=False)
