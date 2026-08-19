"""Clients — the spine. One entity for leads and clients, separated by stage.

Revision ID: 0009_clients
Revises: 0008_files
Created: 2026-08-08

🔒 DB §5.1–5.3, FR-M1-004..016, M1.3. **Leads and clients are one entity**
distinguished by lifecycle stage — DB §5.1 calls this "the single most important
modelling decision in the schema".

Everything downstream depends on that choice. Converting a lead retains the
identifier and the whole history (AC-M1-003) because nothing moves; a returning
client is reactivated rather than recreated (EC-M1-02) because there is only one
row to return to; and the entitlement count is a single predicate over `stage`
(M1.5) rather than a join across two tables that could disagree.

Two tables:

* ``clients`` — the spine
* ``client_stage_history`` — 🔒 append-only, FR-M1-015

🔒 **`client_stage_history` is separate from `audit_log`, deliberately** (DB §5.3).
This is queryable domain history feeding the timeline and conversion metrics
(FR-M9-006); the audit log is compliance evidence with different retention and
immutability rules. Conflating them would force compliance-grade retention on
operational data — and would make "how long did this lead take to convert" a
query against a table nobody may prune.

⚠️ **No unique constraint on `mobile`** (DB §5.1). EC-M1-01 explicitly permits
family members sharing a number, which is a real and common Indian usage
pattern. Duplicate detection is a *warning* (FR-M1-024), never a constraint —
enforcing uniqueness here would reject a mother and daughter on one handset.

🔒 **Archived rows are excluded structurally, not by application filter**
(DB §22.2): the partial indexes below carry `archived_at IS NULL`, on the same
reasoning as RLS. A filter can be forgotten; an index predicate cannot.

🔒 **Search is a generated `tsvector` with a GIN index** (NFR-005 ≤300ms,
FR-M1-021). Generated rather than trigger-maintained: a trigger is a second
place the projection can drift from its source, and `GENERATED ALWAYS` makes
that impossible by construction.

⚠️ 🔒 **`is_minor` is NOT a column, and DB §5.1 is wrong to specify one.**
The table lists it as "GENERATED from `date_of_birth`". PostgreSQL requires a
generation expression to be IMMUTABLE, and age is not: it depends on the current
date. `CURRENT_DATE` in a generated column is rejected outright — which is how
this was found.

The deeper problem is that the column would be *wrong* even if the database
allowed it. A row computed while the client was 17 keeps saying `true` the day
they turn 18, because nothing rewrites a stored value when the calendar moves.
FR-M0-028 gates guardian consent on exactly this, so a stale flag means asking a
legal adult for parental consent — or worse, not asking a minor's guardian.

Minor status is therefore **derived at read time** from `date_of_birth`, in
`kernel.clients.is_minor()`. The cost is that "list every minor" cannot use an
index; that query is rare, and correctness is not negotiable here.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_clients"
down_revision: str | None = "0008_files"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ─── Enum types (DDR-02) ─────────────────────────────────────────────────
#
# Enums rather than text here, unlike `resource_code` in 0007. The difference:
# a new metered resource must not need a migration (FR-M10-001), but a new
# client stage changes what is metered (M1.5), what receives messages
# (FR-M1-014) and what the pipeline view shows. A migration is the right amount
# of friction for a value with that many consequences.

_ENUMS: dict[str, tuple[str, ...]] = {
    # 🟡 PROPOSED pending OD-01 (DB §5.2).
    "client_stage": (
        "lead",
        "contacted",
        "consultation_scheduled",
        "active",
        "paused",
        "churned",
        "archived",
    ),
    # ⚠️ 🟡 PROPOSED — DB §5.1 names the type and not its values. See the note on
    # `kernel.clients.SexType`: the column exists for BMR equations, which take a
    # male/female term, so `other` and NULL both mean "no equation applies"
    # rather than a value to substitute.
    "sex_type": ("male", "female", "other"),
    # DB §8.5. `jain` is a separate axis from the others — it excludes root
    # vegetables, which no vegetarian/vegan distinction captures.
    "dietary_class": ("vegetarian", "eggetarian", "non_vegetarian", "vegan", "jain"),
}


#: 🔒 Pattern A — both tables carry `tenant_id` and both are read on a
#: tenant-facing path. Named as a tuple so the protected set is one greppable
#: declaration, matching 0002, 0007 and 0008.
_TENANT_SCOPED: tuple[str, ...] = ("clients", "client_stage_history")


# ⚠️ Guarded on role existence, matching every migration since 0001.
_ENFORCE_HISTORY_APPEND_ONLY = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        -- 🔒 FR-M1-015 — every transition recorded, and none revised. The
        -- history feeds conversion metrics (FR-M9-006) and the timeline; a
        -- rewritable history makes both unfalsifiable.
        --
        -- ⚠️ Registered in ops/db/002_verify_grants.sql in this same commit, as
        -- that file requires of every append-only table.
        REVOKE UPDATE, DELETE ON TABLE client_stage_history FROM app_user;
        GRANT INSERT, SELECT ON TABLE client_stage_history TO app_user;

        -- 🔒 Clients are soft-deleted (FR-M1-010): `archived_at`, never a DELETE.
        -- FR-M1-011 puts hard deletion behind the DPDP erasure pathway, which
        -- runs as the migrator role, so the application never needs the verb.
        REVOKE DELETE ON TABLE clients FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE clients TO app_user;
    END IF;
END
$$;
"""

_RESTORE_DEFAULT_PRIVILEGES = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE client_stage_history TO app_user;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE clients TO app_user;
    END IF;
END
$$;
"""


def _enum(name: str) -> postgresql.ENUM:
    """Reference an already-created enum type without re-emitting its DDL.

    See 0002 for the full explanation: ``sa.Enum(name=..., create_type=False)``
    still emits an empty ``CREATE TYPE`` when compiled inside ``create_table``.
    """
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    for name, values in _ENUMS.items():
        sa.Enum(*values, name=name).create(op.get_bind(), checkfirst=False)

    # ─── clients (DB §5.1) ────────────────────────────────────────────────
    op.create_table(
        "clients",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        # 🔒 M1.3 — the column that makes a lead and a client the same row.
        sa.Column("stage", _enum("client_stage"), server_default="lead", nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        # 🔒 NFR-100 — E.164. The CHECK is a shape assertion; the Indian-number
        # rule lives in `kernel.clients.normalise_mobile`, because a regex that
        # encodes one country's numbering plan in the schema is a migration every
        # time the market widens.
        sa.Column("mobile", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("date_of_birth", sa.Date(), nullable=True),
        sa.Column("sex", _enum("sex_type"), nullable=True),
        sa.Column("city", sa.Text(), nullable=True),
        # NFR-096 — externalised from the first commit even though only English
        # ships at launch, because retrofitting locale is the expensive kind.
        sa.Column("preferred_language", sa.Text(), server_default="en", nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("source_detail", sa.Text(), nullable=True),
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        # 🔒 Drives plan filtering at authoring time (FR-M4-035), before a plan
        # exists — which is why it is on the person rather than the plan.
        sa.Column("dietary_class", _enum("dietary_class"), nullable=True),
        # 🔒 FR-M0-028 — minor status is **not stored**. See the note below.
        # First entry to `active` — the check-in anchor day (FR-M8-023). Set once
        # and never cleared, so a client who churns and returns keeps their
        # original activation date.
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_clients"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_clients__tenant"),
        # 🔒 FR-M1-009 — every client has an owning practitioner. No cascade: a
        # user with clients must be reassigned (EC-M1-04), not deleted out from
        # under them.
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_clients__owner"),
        # 🔒 FR-M1-004 / EC-M1-08 — at least one way to reach them. EC-M1-08 is
        # why it is "at least one" rather than "mobile": an email-only client is
        # legitimate and simply loses WhatsApp delivery.
        sa.CheckConstraint(
            "mobile IS NOT NULL OR email IS NOT NULL", name="ck_clients__contact_present"
        ),
        # 🔒 NFR-100 — international format. Shape only; see the column comment.
        sa.CheckConstraint(
            "mobile IS NULL OR mobile ~ '^\\+[1-9][0-9]{7,14}$'",
            name="ck_clients__mobile_e164",
        ),
        # 🔒 DB §5.1 — an active client must have an owner. Trivially true while
        # `owner_user_id` is NOT NULL, and stated anyway: if the column is ever
        # relaxed for unassigned leads, this is the half that must not relax.
        sa.CheckConstraint(
            "stage <> 'active' OR owner_user_id IS NOT NULL", name="ck_clients__stage_owner"
        ),
        # An activated client has an activation time. Without this, FR-M8-023's
        # check-in day has no anchor and the scheduler silently skips them.
        sa.CheckConstraint(
            "activated_at IS NOT NULL OR stage <> 'active'",
            name="ck_clients__active_has_activated_at",
        ),
    )

    # 🔒 NFR-005 (≤300ms) / FR-M1-021 — search across name, mobile and email.
    #
    # `GENERATED ALWAYS` rather than a trigger: a trigger is a second place the
    # projection can drift from its source, and there is no way to write a row
    # that bypasses a generated column.
    #
    # ⚠️ `coalesce` on every part. `to_tsvector` of NULL is NULL, and a NULL
    # tsvector matches nothing — so a client with no email would silently drop
    # out of search entirely, which is the kind of bug that looks like "search is
    # flaky" for months.
    op.execute(
        """
        ALTER TABLE clients
            ADD COLUMN search_vector tsvector
            GENERATED ALWAYS AS (
                to_tsvector(
                    'simple',
                    coalesce(full_name, '') || ' ' ||
                    coalesce(mobile, '') || ' ' ||
                    coalesce(email, '')
                )
            ) STORED;
        """
    )
    op.execute("CREATE INDEX ix_clients__search ON clients USING GIN (search_vector)")

    # ⚠️ 🔒 **Mobile-suffix search is deliberately not indexed here** — AC-M1-002
    # asks for "the last 4 digits of a mobile", which a tsvector cannot serve:
    # it tokenises the number as one term, so a suffix match degrades to a scan.
    #
    # The fix is a `pg_trgm` GIN index, and `CREATE EXTENSION` requires
    # superuser — which `app_migrator` deliberately is not (DB §2.4). Adding it
    # would mean a new operator provisioning step on every environment, and this
    # slice does not yet have the search implementation to justify one. Slice E
    # owns that decision, with a measurement behind it.

    # 🔒 DB §22.2 — archived rows are excluded structurally. Every list index
    # below carries the predicate, so a query that forgets the filter still
    # cannot see archived rows through the index.
    op.execute(
        """
        CREATE INDEX ix_clients__tenant_stage
            ON clients (tenant_id, stage)
            WHERE archived_at IS NULL;
        """
    )
    op.execute(
        """
        CREATE INDEX ix_clients__tenant_owner_stage
            ON clients (tenant_id, owner_user_id, stage)
            WHERE archived_at IS NULL;
        """
    )
    # Duplicate detection (EC-M2-02, FR-M1-024). ⚠️ Not unique — EC-M1-01.
    op.execute(
        """
        CREATE INDEX ix_clients__tenant_mobile
            ON clients (tenant_id, mobile)
            WHERE mobile IS NOT NULL AND archived_at IS NULL;
        """
    )

    # ─── client_stage_history — 🔒 append-only (DB §5.3) ──────────────────
    op.create_table(
        "client_stage_history",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        # NULL on creation — there is no stage to come *from* when the record
        # first exists, and a synthetic "none" value would be a stage the enum
        # has to carry forever for one row per client.
        sa.Column("from_stage", _enum("client_stage"), nullable=True),
        sa.Column("to_stage", _enum("client_stage"), nullable=False),
        # NULL when system-driven (an enquiry creating a lead, a scheduled
        # churn). Distinguishable from "we forgot to record it" only because the
        # column is documented, so it is documented.
        sa.Column("changed_by_user_id", sa.UUID(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_client_stage_history"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_client_stage_history__tenant"
        ),
        # 🔒 Cascade. Unlike the audit log, this is domain history *of* a client:
        # a DPDP erasure (FR-M0-027) that removes the client must take it with
        # them, or the record of their lifecycle outlives the erasure request.
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            ondelete="CASCADE",
            name="fk_client_stage_history__client",
        ),
        sa.ForeignKeyConstraint(
            ["changed_by_user_id"], ["users.id"], name="fk_client_stage_history__user"
        ),
        # A transition that goes nowhere is not a transition.
        sa.CheckConstraint(
            "from_stage IS NULL OR from_stage <> to_stage",
            name="ck_client_stage_history__actual_transition",
        ),
    )
    # The client's own history, newest first — the timeline read and the
    # conversion-metric scan.
    op.execute(
        """
        CREATE INDEX ix_client_stage_history__client_changed
            ON client_stage_history (client_id, changed_at DESC);
        """
    )
    op.create_index("ix_client_stage_history__tenant_id", "client_stage_history", ["tenant_id"])

    # ─── Row Level Security ───────────────────────────────────────────────
    #
    # 🔒 Pattern A on both. Unlike `consent_records`, these are read on a
    # tenant-facing path with a tenant in scope on every request, so the policy
    # is the isolation boundary and AC-M0-003 covers them.
    #
    # 🔒 FORCE is not redundant with ENABLE: without it the table owner bypasses
    # every policy, and migrations run as `app_migrator`, which owns these.
    #
    # ⚠️ Practitioner-scoping (AC-M1-006 — a non-owner cannot see another
    # practitioner's clients) is **not** in this policy. It is an authorization
    # decision about a user, not a tenant, and `current_tenant_id()` is the only
    # thing the policy has. It lands in Slice C with `client_assignments`, where
    # the grant model exists to express it.
    for table in _TENANT_SCOPED:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}__tenant_isolation ON {table}
            USING (tenant_id = current_tenant_id())
            WITH CHECK (tenant_id = current_tenant_id());
            """
        )

    # ⚠️ Last: once the revokes land, this migration's own connection keeps
    # working only because it runs as the migrator role.
    op.execute(_ENFORCE_HISTORY_APPEND_ONLY)


def downgrade() -> None:
    """Drop everything this revision created.

    ⚠️ 🔒 **Destroys the client base.** Reversibility exists so the chain is
    honestly testable in development. Running this against a database with real
    clients would delete the practice, and is not a supported operation.
    """
    op.execute(_RESTORE_DEFAULT_PRIVILEGES)
    op.drop_table("client_stage_history")
    op.drop_table("clients")

    for name in _ENUMS:
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=False)
