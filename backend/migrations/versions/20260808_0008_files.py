"""File metadata — bytes live in object storage, facts about them live here.

Revision ID: 0008_files
Revises: 0007_entitlements
Created: 2026-08-08

🔒 DB §19.1, Arch §13, ADR-12, FR-M0-037..040, NFR-035/036. One table. The bytes
are never in PostgreSQL and never pass through FastAPI (ADR-12) — this row is the
authorization record and the erasure index for an object that lives elsewhere.

📌 **ADR-12 — the row is created on confirmation, not on request.** The client
asks permission, the server authorizes and issues a scoped credential, the client
uploads directly, and only then is a row written. An abandoned upload therefore
leaves an orphan object (reaped after 24h, approved proposal #9) rather than a
phantom database row that nothing will ever complete.

⚠️ 🔒 **A documented inconsistency, resolved here.** DB §19.1 says both
"``status='pending'`` until confirmed" *and* "creates the row on confirmation".
Read literally those cannot both hold — a row created at confirmation would never
be observed pending. The reading implemented is: the row is created **at confirm
time** as ``pending``, and becomes ``confirmed`` only once the backend has
verified the stored object actually exists and matches the size and content type
that were authorized.

That verification is not ceremony. Without it the allowlist is advisory: a client
could authorize a 40 KB JPEG and upload a 2 GB executable, and nothing would ever
compare the two. ``pending`` is the window between "the client says it uploaded"
and "we checked", and ``quarantined`` is where a mismatch lands — kept for
investigation rather than deleted, because a deliberate mismatch is a security
event and destroying the evidence is the wrong first move.

🔒 **Pattern A RLS.** ``files`` carries ``tenant_id`` and is read on a
tenant-facing path (every download authorizes first, NFR-035), so the policy is
the isolation boundary and AC-M0-003 covers it.

🔒 **DELETE is revoked** (DDR-15 reasoning, applied to a mutable table). Files are
soft-deleted: ``status='deleted'`` plus ``deleted_at``, with a purge job removing
the bytes afterwards. The application cannot make a file record disappear, because
the record is what proves a file existed — and DPDP erasure (FR-M0-027) has to be
able to show that the object it was pointing at was actually destroyed. UPDATE is
retained: the status transitions above are the table's whole lifecycle.

⚠️ **`contains_clinical_data` is the erasure index** (Arch §13.2). Without it,
DPDP erasure would have to infer each file's purpose at request time — exactly
the lookup that gets missed, which is why it is a stored column rather than a
join through whatever record happens to reference the file.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_files"
down_revision: str | None = "0007_entitlements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ─── Enum types (DDR-02) ─────────────────────────────────────────────────
#
# ⚠️ Enums here, unlike `resource_code` in 0007. The difference is deliberate:
# FR-M10-001 requires new metered *resources* without a migration, but a new file
# class changes retention and erasure behaviour (Arch §13.2) and must not appear
# without someone deciding which of those it gets. A migration is the right
# amount of friction for that.

_ENUMS: dict[str, tuple[str, ...]] = {
    "file_class": ("client_document", "plan_pdf", "invoice_pdf", "branding", "export"),
    # 🔒 `quarantined` is not a synonym for `deleted`. A file whose bytes do not
    # match what was authorized is evidence, and the response to it is
    # investigation rather than destruction.
    "file_status": ("pending", "confirmed", "quarantined", "deleted"),
}


#: 🔒 Pattern A — the tables carrying `tenant_id`, and therefore the tables that
#: get RLS enabled, forced and policied. One entry today; declared as a tuple so
#: the loop and its test match the shape used by 0002 and 0007.
_TENANT_SCOPED: tuple[str, ...] = ("files",)


# ⚠️ Guarded on role existence, matching every migration since 0001. A local
# developer may run a single-role database, and a REVOKE naming a missing role
# aborts the transaction.
_ENFORCE_SOFT_DELETE_ONLY = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        -- 🔒 The application may create a file record, move it through its
        -- lifecycle, and mark it deleted. It may not make one vanish.
        --
        -- A hard DELETE would destroy the only record that an object ever
        -- existed, and DPDP erasure (FR-M0-027) has to be able to show that the
        -- object a row pointed at was actually destroyed. Purging bytes is a
        -- maintenance job's work, not the request path's.
        REVOKE DELETE ON TABLE files FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE files TO app_user;
    END IF;
END
$$;
"""

_RESTORE_DEFAULT_PRIVILEGES = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE files TO app_user;
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

    op.create_table(
        "files",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        # 🔒 Opaque, tenant-scoped, non-enumerable (Arch §13.1). Globally unique
        # rather than unique-per-tenant: the key already contains the tenant, and
        # a global constraint makes key reuse across tenants impossible rather
        # than merely unlikely.
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("bucket", sa.Text(), nullable=False),
        # ⚠️ The name the user chose, kept for display only. Never used to build
        # a storage path — that is what `storage_key` is for, and deriving a path
        # from a filename is how a traversal or an enumeration gets in.
        sa.Column("original_filename", sa.Text(), nullable=False),
        # 🔒 NFR-036 — validated against an allowlist in `kernel.storage` before
        # the upload is authorized, and re-checked against the stored object at
        # confirmation. The allowlist lives in code because it is policy that
        # changes without a schema change.
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        # Integrity, and the second half of the confirmation check when the
        # backend can supply one.
        sa.Column("checksum", sa.Text(), nullable=True),
        sa.Column("file_class", _enum("file_class"), nullable=False),
        # 🔒 Arch §13.2 — the erasure index. Stored rather than inferred.
        sa.Column("contains_clinical_data", sa.Boolean(), nullable=False),
        sa.Column("uploaded_by_actor_type", _enum("actor_type"), nullable=False),
        sa.Column("uploaded_by_actor_id", sa.UUID(), nullable=True),
        sa.Column("status", _enum("file_status"), server_default="pending", nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_files"),
        sa.UniqueConstraint("storage_key", name="uq_files__storage_key"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_files__tenant"),
        # 🔒 A zero-byte file is a failed upload that reported success. Rejecting
        # it here means the quota arithmetic in `kernel.entitlements` never has
        # to reason about a row that consumes nothing.
        sa.CheckConstraint("size_bytes > 0", name="ck_files__size_positive"),
        # 🔒 The state machine, as far as a constraint can express it: a
        # confirmed file has a confirmation time, a deleted file has a deletion
        # time. Without this, a soft delete that forgot its timestamp would leave
        # the purge job unable to tell what is due for reaping.
        sa.CheckConstraint(
            "(status <> 'confirmed' OR confirmed_at IS NOT NULL)"
            " AND (status <> 'deleted' OR deleted_at IS NOT NULL)",
            name="ck_files__lifecycle_timestamped",
        ),
    )

    op.create_index("ix_files__tenant_id", "files", ["tenant_id"])
    # 🔒 The quota read (FR-M0-040). Storage is counted live from this table
    # rather than from a `usage_counters` row — see `kernel.entitlements` — so
    # this index is what keeps that count O(rows for one tenant) instead of a
    # scan. Partial, because deleted files consume no quota and are the majority
    # of rows in a mature tenant.
    op.execute(
        """
        CREATE INDEX ix_files__tenant_live
            ON files (tenant_id, size_bytes)
            WHERE status = 'confirmed' AND deleted_at IS NULL;
        """
    )
    # 🔒 The erasure traversal (FR-M0-027, Arch §13.2): every clinical object
    # belonging to one tenant, including already-deleted rows whose bytes may not
    # yet have been purged.
    op.create_index(
        "ix_files__tenant_clinical",
        "files",
        ["tenant_id", "contains_clinical_data"],
    )
    # The reaper's query: rows stuck pending past the 24h window.
    op.create_index("ix_files__status_created", "files", ["status", "created_at"])

    # ─── Row Level Security ───────────────────────────────────────────────
    #
    # 🔒 Pattern A. Unlike `consent_records`, `files` is read on a tenant-facing
    # path — every download authorizes first (NFR-035) — so the policy is the
    # isolation boundary and AC-M0-003 covers it.
    #
    # 🔒 `FORCE ROW LEVEL SECURITY` is not redundant with `ENABLE`. Without
    # FORCE, the table owner bypasses every policy, and migrations run as
    # `app_migrator`, which owns this table.
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

    # ⚠️ Last: once the revoke lands, this migration's own connection keeps
    # working only because it runs as the migrator role.
    op.execute(_ENFORCE_SOFT_DELETE_ONLY)


def downgrade() -> None:
    """Drop everything this revision created.

    ⚠️ 🔒 **Destroys the file index without touching the objects it describes.**
    Reversibility exists so the chain is honestly testable in development.
    Running this against a database with real files would orphan every object in
    the bucket — bytes with nothing left to say who they belong to or whether
    they are clinical — and is not a supported operation.
    """
    op.execute(_RESTORE_DEFAULT_PRIVILEGES)
    op.drop_table("files")

    for name in _ENUMS:
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=False)
