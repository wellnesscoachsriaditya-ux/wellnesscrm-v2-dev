"""Audit infrastructure — the append-only record of who did what.

Revision ID: 0003_audit_infrastructure
Revises: 0002_platform_kernel
Created: 2026-08-05

🔒 DB §15.3, FR-M0-031..036. One table, and one property that makes it worth
having: **the application cannot alter what it has written.**

That property is created here, by a grant, not by application code. Code is what
the log exists to audit, so code cannot be what protects it. ``ops/db/001_roles.sql``
sets ``DEFAULT PRIVILEGES`` granting ``SELECT, INSERT, UPDATE, DELETE`` on new
tables to ``app_user`` — convenient for every other table and wrong for this one,
so the revoke below is mandatory rather than decorative (DDR-15).

⚠️ **No RLS on this table** (Pattern D), unlike every other table carrying a
``tenant_id``. Three reasons, in order of weight:

1. It is never read on a tenant-facing path. Practitioners have no audit-viewing
   feature; adding a policy would imply one exists.
2. Rows are written for actors with *no* tenant — operators and system jobs. A
   Pattern A policy keyed on ``tenant_id = current_tenant_id()`` would reject
   those inserts, and the entries it would reject are exactly the cross-tenant
   ones that most need recording (FR-M0-032).
3. 🔒 A write-time policy on an append-only table converts a *failure to audit*
   into a *failure of the request that should have been audited* — the loudest
   possible failure for the least dangerous condition.

The control here is therefore the grant plus ``kernel.audit``, not RLS.

🔒 **No foreign keys**, also deliberate. ``tenant_id`` and ``resource_id`` are
references. An audited row may be deleted under retention (NFR-049) or a DPDP
erasure request; a FK would either block that deletion or cascade away the proof
it happened. The trail must outlive what it describes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_audit_infrastructure"
down_revision: str | None = "0002_platform_kernel"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ─── Enum types (DDR-02) ─────────────────────────────────────────────────
#
# `auth_realm` already exists from 0002 and is reused rather than redefined —
# a second realm type would be a second source of truth for the realm boundary.

_ENUMS: dict[str, tuple[str, ...]] = {
    "actor_type": ("practitioner", "client", "operator", "system", "anonymous"),
    # 🔒 `denied` is a first-class outcome, not an absence of `allowed`. A
    # refused attempt is the entry an investigation starts from (FR-M0-033).
    "audit_outcome": ("allowed", "denied", "failed"),
}


# ⚠️ Guarded on role existence, matching 0001. A local developer may run a
# single-role database, and a REVOKE naming a missing role aborts the
# transaction — which would make this migration unrunnable on the setup most
# likely to be running it first.
_ENFORCE_APPEND_ONLY = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        -- 🔒 DDR-15. The default privileges in ops/db/001_roles.sql grant all
        -- four verbs on every new table; this table gets two. There is no
        -- supported path by which the application edits or removes an entry,
        -- because the privilege to do so does not exist.
        REVOKE UPDATE, DELETE ON TABLE audit_log FROM app_user;
        GRANT INSERT, SELECT ON TABLE audit_log TO app_user;
        GRANT USAGE, SELECT ON SEQUENCE audit_log_id_seq TO app_user;
    END IF;
END
$$;
"""

_RESTORE_DEFAULT_PRIVILEGES = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE audit_log TO app_user;
    END IF;
END
$$;
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

    # ─── audit_log (DB §15.3) ────────────────────────────────────────────
    op.create_table(
        "audit_log",
        # bigserial rather than uuid: the highest-volume table in the schema,
        # written on every mutation and read only as an ordered scan. A
        # monotonic key keeps inserts appending to the right edge of the index
        # instead of scattering across it.
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=True),
        sa.Column("actor_type", _enum("actor_type"), nullable=False),
        sa.Column("actor_realm", _enum("auth_realm"), nullable=True),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=False),
        sa.Column("resource_id", sa.UUID(), nullable=True),
        sa.Column("outcome", _enum("audit_outcome"), nullable=False),
        # 🔒 FR-M0-035 — field NAMES, never values. `text[]` rather than jsonb
        # because an array of strings cannot accidentally hold a nested object
        # of before/after pairs; the column's own type refuses the shape that
        # would constitute a leak. `kernel.audit.validate_changed_fields`
        # rejects anything that is not a bare identifier before it gets here.
        sa.Column("changed_fields", postgresql.ARRAY(sa.Text()), nullable=True),
        # 🔒 Allowlisted keys only (DB §15.3), filtered in `kernel.audit`.
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        # 🔒 Hashed with a deployment secret — an IP address is personal data
        # under the DPDP Act, and this table is retained for years (NFR-033).
        sa.Column("ip_hash", sa.Text(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
    )

    # "What happened in this tenant recently" — the support query.
    op.create_index("ix_audit_log__tenant_occurred", "audit_log", ["tenant_id", "occurred_at"])
    # "What happened to this record" — the investigation query.
    op.create_index("ix_audit_log__resource", "audit_log", ["resource_type", "resource_id"])
    # 🔒 "What did this operator touch" — the query that answers a DPDP enquiry
    # about platform staff access (FR-M0-032).
    op.create_index("ix_audit_log__actor_occurred", "audit_log", ["actor_id", "occurred_at"])

    op.execute(_ENFORCE_APPEND_ONLY)


def downgrade() -> None:
    """Drop the audit table and its types.

    ⚠️ Destroys the audit trail. Reversibility is for development; deleting an
    audit log in production is the action an audit log exists to make visible.
    """
    op.execute(_RESTORE_DEFAULT_PRIVILEGES)
    op.drop_table("audit_log")

    for name in _ENUMS:
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=False)
