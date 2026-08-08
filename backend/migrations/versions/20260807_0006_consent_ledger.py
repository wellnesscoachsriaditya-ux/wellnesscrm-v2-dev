"""Consent ledger — the legal record of what we may process, and why.

Revision ID: 0006_consent_ledger
Revises: 0005_jobs_queue
Created: 2026-08-07

🔒 DB §16, FR-M0-021..030, NFR-044..053. **The DPDP Act 2023 is the governing
regime at launch, not HIPAA** — the difference that matters here is that DPDP
makes consent itself the artefact to be evidenced, per purpose, against the
exact notice text presented.

Four tables, in dependency order:

* ``consent_purposes`` — what we might process, and why (FR-M0-022)
* ``consent_notices`` — the versioned text actually shown to a person
* ``consent_records`` — 🔒 the append-only ledger (NFR-047)
* ``data_requests`` — access, correction and erasure (FR-M0-026/027)

🔒 **``consent_records`` gets the same treatment as ``audit_log``** (DDR-15):
``app_user`` holds INSERT and SELECT, nothing more. ``ops/db/001_roles.sql`` sets
DEFAULT PRIVILEGES granting all four verbs on new tables, so the revoke below is
mandatory rather than decorative. A ledger the application can revise is not a
legal record of anything.

🔒 **``consent_notices`` is immutable once effective**, enforced the same way.
The application may insert a new version and stamp ``superseded_at`` on the old
one — that single UPDATE is the reason the revoke here is narrower than the
ledger's — but it may never edit a notice body. A consent record points at the
exact version presented, and NFR-051 ("produce the consent basis for any
client") is answerable only if that text cannot have changed since.

⚠️ **RLS dispositions differ across these four tables, deliberately:**

* ``consent_purposes`` / ``consent_notices`` — Pattern D. Platform-wide
  catalogue, no ``tenant_id`` to key a policy on. Tenants read them; only the
  operator role writes them.
* ``consent_records`` — Pattern D, unreachable. Carries ``tenant_id`` but no
  policy, matching ``audit_log``: never read on a tenant-facing path, and
  written on the anonymous enquiry path where no tenant context exists yet. A
  Pattern A policy would reject exactly the pre-client consent that FR-M2-004
  requires be captured.
* ``data_requests`` — Pattern A. Mutable, tenant-scoped, read by the operator
  console within a tenant. The one table here that behaves ordinarily.

⚠️ 🔒 The seeded purposes are **PROPOSED** and the essential/non-essential split
is legally consequential (ASM-10, OD-05). Marking too much as essential defeats
FR-M0-024's requirement that withdrawal be as easy as granting. This needs the
privacy lawyer's review before launch, not after.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_consent_ledger"
down_revision: str | None = "0005_jobs_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ─── Enum types (DDR-02) ─────────────────────────────────────────────────
#
# `actor_type` already exists from 0003 and is reused rather than redefined —
# a second actor type would be a second source of truth for who acts.

_ENUMS: dict[str, tuple[str, ...]] = {
    "consent_subject": ("client", "prospect"),
    # 🔒 `reconfirmed` is not a synonym for `granted`. FR-M0-029 asks whether
    # consent was re-obtained against the notice version now in force, which a
    # collapsed enum could not answer.
    "consent_action": ("granted", "withdrawn", "reconfirmed"),
    "consent_channel": ("enquiry_form", "portal", "practitioner", "whatsapp"),
    "data_request_type": ("access", "correction", "erasure"),
    "data_request_status": ("received", "in_progress", "completed", "rejected"),
}


# ⚠️ Guarded on role existence, matching 0001 and 0003. A local developer may
# run a single-role database, and a REVOKE naming a missing role aborts the
# transaction — which would make this migration unrunnable on the setup most
# likely to run it first.
_ENFORCE_LEDGER_APPEND_ONLY = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        -- 🔒 DDR-15, NFR-047. Two verbs, not four. There is no supported path
        -- by which the application edits or removes a consent decision,
        -- because the privilege to do so does not exist.
        REVOKE UPDATE, DELETE ON TABLE consent_records FROM app_user;
        GRANT INSERT, SELECT ON TABLE consent_records TO app_user;
        GRANT USAGE, SELECT ON SEQUENCE consent_records_id_seq TO app_user;

        -- 🔒 Notices keep UPDATE — superseding one stamps `superseded_at` —
        -- but never DELETE. Removing a notice would orphan the basis of every
        -- consent given against it. Body immutability is enforced by trigger
        -- below, since it is a column-level rule a grant cannot express.
        REVOKE DELETE ON TABLE consent_notices FROM app_user;

        -- 🔒 The catalogue is ours, not the tenant's. A tenant inventing its
        -- own processing purposes is a compliance problem, not a feature.
        REVOKE INSERT, UPDATE, DELETE ON TABLE consent_purposes FROM app_user;
        GRANT SELECT ON TABLE consent_purposes TO app_user;
    END IF;
END
$$;
"""

_RESTORE_DEFAULT_PRIVILEGES = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE consent_records TO app_user;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE consent_notices TO app_user;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE consent_purposes TO app_user;
    END IF;
END
$$;
"""

# 🔒 FR-M0-029, NFR-051. The one rule a grant cannot express: a notice may be
# superseded but its presented text may never change. Without this, `UPDATE
# consent_notices SET body = ...` would silently rewrite the basis of every
# consent already recorded against that version — the failure mode being that
# nothing appears to break until a regulator asks what the person agreed to.
_ENFORCE_NOTICE_IMMUTABILITY = """
CREATE OR REPLACE FUNCTION consent_notices__reject_material_edit()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.body IS DISTINCT FROM OLD.body
       OR NEW.title IS DISTINCT FROM OLD.title
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.locale IS DISTINCT FROM OLD.locale
       OR NEW.purpose_ids IS DISTINCT FROM OLD.purpose_ids
       OR NEW.effective_from IS DISTINCT FROM OLD.effective_from
       OR NEW.requires_reconsent IS DISTINCT FROM OLD.requires_reconsent
    THEN
        RAISE EXCEPTION
            'consent_notices is immutable once effective; supersede it instead (FR-M0-029)';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_consent_notices__immutable
    BEFORE UPDATE ON consent_notices
    FOR EACH ROW
    EXECUTE FUNCTION consent_notices__reject_material_edit();
"""


def _enum(name: str) -> postgresql.ENUM:
    """Reference an already-created enum type without re-emitting its DDL.

    See 0002 for the full explanation: ``sa.Enum(name=..., create_type=False)``
    still emits an empty ``CREATE TYPE`` when compiled inside ``create_table``.
    ``postgresql.ENUM`` is the form that genuinely emits nothing.
    """
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    for name, values in _ENUMS.items():
        sa.Enum(*values, name=name).create(op.get_bind(), checkfirst=False)

    # ─── consent_purposes (DB §16.2) ─────────────────────────────────────
    op.create_table(
        "consent_purposes",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        # 🔒 Plain language, because it is shown to the data principal. A purpose
        # described only in legal register does not produce informed consent.
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("is_essential", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("data_categories", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=True),
        sa.Column("legal_basis", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_consent_purposes"),
        sa.UniqueConstraint("code", name="uq_consent_purposes__code"),
        # 🔒 A legal invariant, not a policy choice: marketing can never be
        # essential. A constraint rather than a seed convention, because seed
        # data is editable and this must not be.
        sa.CheckConstraint(
            "NOT (code = 'marketing' AND is_essential)",
            name="ck_consent_purposes__marketing_not_essential",
        ),
    )

    # ─── consent_notices (DB §16.3) ──────────────────────────────────────
    op.create_table(
        "consent_notices",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        # uuid[] rather than a join table: the purpose set is fixed at creation
        # and only ever read whole. A join table would imply it is editable.
        sa.Column("purpose_ids", postgresql.ARRAY(sa.UUID()), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("locale", sa.Text(), server_default="en-IN", nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "requires_reconsent",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_consent_notices"),
    )
    op.create_index(
        "uq_consent_notices__version_locale",
        "consent_notices",
        ["version", "locale"],
        unique=True,
    )
    # "Which notice is in force for this locale" — the capture-path query.
    op.create_index(
        "ix_consent_notices__locale_effective",
        "consent_notices",
        ["locale", "effective_from"],
    )

    # ─── consent_records — 🔒 the ledger (DB §16.4) ───────────────────────
    op.create_table(
        "consent_records",
        # bigserial, matching audit_log: append-only, written on every consent
        # interaction, read as an ordered scan per subject.
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        # 🔒 No FK on tenant_id or subject_id — same reasoning as audit_log. An
        # erasure request deletes the client; a FK would either block that or
        # cascade away the proof consent was given. The basis must outlive the
        # record it describes.
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("subject_type", _enum("consent_subject"), nullable=False),
        sa.Column("subject_id", sa.UUID(), nullable=True),
        # 🔒 Hashed, not plain: the ledger must not become a second, unprotected
        # copy of contact data (DB §16.4).
        sa.Column("subject_mobile_hash", sa.Text(), nullable=True),
        sa.Column("purpose_id", sa.UUID(), nullable=False),
        # 🔒 The exact version presented — what makes NFR-051 answerable.
        sa.Column("notice_id", sa.UUID(), nullable=False),
        sa.Column("action", _enum("consent_action"), nullable=False),
        sa.Column("captured_via", _enum("consent_channel"), nullable=False),
        sa.Column("captured_by_actor_type", _enum("actor_type"), nullable=False),
        sa.Column("captured_by_actor_id", sa.UUID(), nullable=True),
        # 🔒 FR-M0-028 — minors. ⚠️ OD-05: the columns exist, the verification
        # mechanism awaits legal advice and is a launch blocker.
        sa.Column("guardian_name", sa.Text(), nullable=True),
        sa.Column("guardian_relationship", sa.Text(), nullable=True),
        sa.Column("guardian_verification_method", sa.Text(), nullable=True),
        # 🔒 Non-clinical only: notice hash, UI version, timestamp. Clinical
        # facts belong in clinical tables with their own retention rules.
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_consent_records"),
        # FKs to the catalogue only. These rows are never erased, so there is
        # nothing for a cascade to destroy.
        sa.ForeignKeyConstraint(
            ["purpose_id"], ["consent_purposes.id"], name="fk_consent_records__purpose"
        ),
        sa.ForeignKeyConstraint(
            ["notice_id"], ["consent_notices.id"], name="fk_consent_records__notice"
        ),
        # 🔒 A ledger entry nobody can be matched to is not evidence. One
        # identifier is mandatory; which one depends on whether a client record
        # existed at capture time.
        sa.CheckConstraint(
            "subject_id IS NOT NULL OR subject_mobile_hash IS NOT NULL",
            name="ck_consent_records__subject_identified",
        ),
    )
    # 🔒 The derived-state query: latest row per (subject, purpose). `id DESC` so
    # the read is a backwards index scan stopping at the first row, not a sort
    # over the subject's whole consent history.
    op.execute(
        """
        CREATE INDEX ix_consent_records__subject_purpose
            ON consent_records (tenant_id, subject_id, purpose_id, id DESC);
        """
    )
    # 🔒 The enquiry-form path, where subject_id does not exist yet.
    op.execute(
        """
        CREATE INDEX ix_consent_records__mobile_purpose
            ON consent_records (tenant_id, subject_mobile_hash, purpose_id, id DESC);
        """
    )

    # ─── data_requests (DB §16.5) ────────────────────────────────────────
    op.create_table(
        "data_requests",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        # Reference only: an erasure request must survive the client it erased.
        sa.Column("client_id", sa.UUID(), nullable=True),
        sa.Column("request_type", _enum("data_request_type"), nullable=False),
        sa.Column(
            "status", _enum("data_request_status"), server_default="received", nullable=False
        ),
        sa.Column("requested_via", _enum("consent_channel"), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # 🟡 Statutory period pending ASM-10. Stored per row rather than computed
        # on read, so a later change to the legal period cannot retroactively
        # move the deadline of a request already in flight.
        sa.Column("due_by", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handled_by", sa.UUID(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("export_file_id", sa.UUID(), nullable=True),
        # ⚠️ What was actually traversed, object storage included (Arch §13.2).
        sa.Column("erasure_scope", postgresql.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_data_requests"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_data_requests__tenant"),
        # 🔒 A rejection without a reason is not a defensible response to a
        # statutory request, and a completion without a timestamp cannot be
        # shown to have met the deadline.
        sa.CheckConstraint(
            "(status <> 'rejected' OR rejection_reason IS NOT NULL)"
            " AND (status <> 'completed' OR completed_at IS NOT NULL)",
            name="ck_data_requests__terminal_state_evidenced",
        ),
    )
    op.create_index("ix_data_requests__tenant_id", "data_requests", ["tenant_id"])
    # The operator queue: what is outstanding, oldest deadline first.
    op.create_index(
        "ix_data_requests__status_due", "data_requests", ["tenant_id", "status", "due_by"]
    )

    # ─── Row Level Security ───────────────────────────────────────────────
    #
    # 🔒 Pattern A on `data_requests` only, and the three omissions are each
    # deliberate:
    #
    # * `consent_purposes` / `consent_notices` — Pattern D. No `tenant_id` to
    #   key a policy on; the catalogue is platform-wide and tenant writes are
    #   already revoked above.
    # * `consent_records` — Pattern D, unreachable, matching `audit_log`. It is
    #   never read on a tenant-facing path, and a Pattern A policy would reject
    #   inserts on the anonymous enquiry path where no tenant is yet in scope —
    #   which is precisely the pre-client consent FR-M2-004 requires be captured.
    #   ⚠️ Reads therefore go through `kernel.consent`, which filters by tenant
    #   explicitly. That filter is application code, so AC-M0-003 does not cover
    #   it; the compensating control is the append-only grant plus the fact that
    #   no endpoint exposes the table directly.

    op.execute("ALTER TABLE data_requests ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE data_requests FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY data_requests__tenant_isolation ON data_requests
        USING (tenant_id = current_tenant_id())
        WITH CHECK (tenant_id = current_tenant_id());
        """
    )

    op.execute(_ENFORCE_NOTICE_IMMUTABILITY)
    # ⚠️ Last, and after the trigger: once the revoke lands, this migration's own
    # connection keeps working only because it runs as the migrator role.
    op.execute(_ENFORCE_LEDGER_APPEND_ONLY)

    # ─── Seed purposes (🟡 PROPOSED — DB §16.2) ───────────────────────────
    #
    # ⚠️ Seeded in the migration rather than a fixture because a consent record
    # cannot exist without a purpose to reference, and the enquiry form ships in
    # S2. An empty catalogue would make the capture path unreachable.
    #
    # ⚠️ 🔒 The essential/non-essential split below is PROPOSED and requires the
    # privacy lawyer review (ASM-10, OD-05). Three are marked essential; each
    # additional one narrows FR-M0-024's withdrawal right, so the bar for adding
    # to that list is legal advice, not convenience.
    op.execute(
        """
        INSERT INTO consent_purposes
            (code, name, description, is_essential, data_categories,
             retention_days, legal_basis)
        VALUES
            ('service_delivery', 'Service delivery',
             'To provide the nutrition and wellness services you have engaged '
             'your practitioner for.',
             true, ARRAY['identity','contact','engagement'], NULL, 'contract'),
            ('clinical_records', 'Clinical records',
             'To maintain the health and dietary records needed to advise you safely.',
             true, ARRAY['health','dietary','measurements'], NULL, 'consent'),
            ('plan_delivery', 'Plan delivery',
             'To prepare and send your nutrition plans and related documents.',
             true, ARRAY['dietary','contact'], NULL, 'contract'),
            ('progress_tracking', 'Progress tracking',
             'To record measurements and adherence over time so your plan can be adjusted.',
             false, ARRAY['health','measurements'], NULL, 'consent'),
            ('whatsapp_communication', 'WhatsApp communication',
             'To contact you on WhatsApp about your plans, sessions and questions.',
             false, ARRAY['contact','message_content'], NULL, 'consent'),
            ('appointment_reminders', 'Appointment reminders',
             'To remind you about upcoming consultations.',
             false, ARRAY['contact','scheduling'], NULL, 'consent'),
            ('marketing', 'Marketing',
             'To tell you about new services, offers and content.',
             false, ARRAY['contact'], 730, 'consent');
        """
    )


def downgrade() -> None:
    """Drop everything this revision created, in dependency order.

    ⚠️ 🔒 **Destroys the consent ledger.** Reversibility exists so the migration
    chain is honestly testable in development. Running this in production would
    delete the legal basis on which client data is held — the records that answer
    NFR-051 — and is not a supported operation.
    """
    op.execute(_RESTORE_DEFAULT_PRIVILEGES)
    op.execute("DROP TRIGGER IF EXISTS trg_consent_notices__immutable ON consent_notices")
    op.execute("DROP FUNCTION IF EXISTS consent_notices__reject_material_edit()")

    op.drop_table("data_requests")
    op.drop_table("consent_records")
    op.drop_table("consent_notices")
    op.drop_table("consent_purposes")

    for name in _ENUMS:
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=False)
