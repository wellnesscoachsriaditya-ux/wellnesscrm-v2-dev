"""Collaboration — notes, tags and shared access to a client.

Revision ID: 0011_collaboration
Revises: 0010_client_lifecycle
Created: 2026-08-08

🔒 DB §5.4–5.5, FR-M1-007/008, FR-M3-020/021, EC-M0-04. Four tables, owned by
`clients`:

* `client_notes` — free-text notes with author and timestamp (FR-M1-007)
* `tags` — the tenant's own vocabulary (FR-M1-008)
* `client_tags` — the junction
* `client_assignments` — 🔒 *additional* grants only (EC-M0-04)

🔒 **`client_notes` gets no client-realm policy, and that is the enforcement.**
FR-M3-021 says notes must never be visible to the client. DB §17.1 makes the
point that the *absence* of a policy is stronger than a condition in one: a
condition can be written wrongly, widened by a later migration, or accidentally
satisfied. There is nothing to get wrong when the client realm simply has no way
in. The same absence covers `client_tags` and `client_assignments`, which are
practice-internal for the same reason.

🔒 **One owner, N grants** (DB §5.5). `clients.owner_user_id` is the owning
practitioner and stays the single answer to "whose client is this"; this table
holds only *additional* access. Modelling shared care as a second owner column,
or as a list, is what makes "who is accountable for this client" unanswerable.

⚠️ **Assignments are revoked, not deleted.** `revoked_at` rather than a DELETE,
because EC-M1-04 requires assignment history to survive a practitioner leaving —
"who could see this client last March" is a question a DPDP access request can
ask, and a deleted row cannot answer it. The primary key is therefore
`(client_id, user_id, granted_at)` rather than `(client_id, user_id)` as DB §5.5
sketches: with revocation, one pair legitimately recurs over time, and the
narrower key would make re-granting a colleague impossible after a revoke.

🔒 **Tag uniqueness is case-insensitive** — a unique index on
`(tenant_id, lower(name))` rather than the plain `uq_tags__tenant_name` DB §5.4
names. A practitioner with a "PCOS" tag who types "pcos" means the tag they
already have, and a case-sensitive constraint would let them split their own
caseload across two labels that look identical in a filter list.
`kernel.collaboration.tag_match_key` computes the same value in the application
so the two cannot disagree.

⚠️ **None of these four is registered in `ops/db/002_verify_grants.sql`**, and
that is correct rather than an omission. That script checks the append-only set
(DDR-15), whose shape is "no UPDATE, no DELETE". These tables are soft-deleted or
revocable, so they *need* UPDATE — `client_notes` and `tags` to archive,
`client_assignments` to revoke — and `client_tags` needs DELETE because untagging
is a real removal. Listing any of them would fail on a grant they are designed to
hold.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_collaboration"
down_revision: str | None = "0010_client_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: 🟡 PROPOSED (see `kernel.collaboration.TagColour`). A closed palette rather
#: than free hex: every value maps to a design token whose contrast is asserted
#: (NFR-060), and ADR-03 keeps raw colour values out of everything but the design
#: system.
_TAG_COLOUR = (
    "slate",
    "red",
    "amber",
    "green",
    "teal",
    "blue",
    "violet",
    "pink",
)

#: 🔒 Pattern A — all four carry `tenant_id` and are read on a tenant-facing
#: path. One greppable declaration, matching 0002, 0007, 0008 and 0009.
_TENANT_SCOPED: tuple[str, ...] = (
    "client_notes",
    "tags",
    "client_tags",
    "client_assignments",
)


def _enum(name: str) -> postgresql.ENUM:
    """Reference an already-created enum without re-emitting its DDL. See 0002."""
    return postgresql.ENUM(name=name, create_type=False)


# ⚠️ Guarded on role existence, matching every migration since 0001.
_APPLY_GRANTS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        -- Notes and tags are soft-deleted (DB §22.2), so the application never
        -- needs DELETE. Withholding the verb is what makes that structural
        -- rather than a convention the next writer may not know about.
        REVOKE DELETE ON TABLE client_notes FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE client_notes TO app_user;

        REVOKE DELETE ON TABLE tags FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE tags TO app_user;

        -- ⚠️ `client_tags` keeps DELETE, deliberately. Untagging is a real
        -- removal: the row records no event, so there is nothing to preserve
        -- and a tombstone would complicate the primary key that makes
        -- "is this client tagged X" a single lookup.
        GRANT SELECT, INSERT, DELETE ON TABLE client_tags TO app_user;
        REVOKE UPDATE ON TABLE client_tags FROM app_user;

        -- 🔒 Assignments are revoked in place (EC-M1-04 keeps the history), so
        -- UPDATE is needed and DELETE is not.
        REVOKE DELETE ON TABLE client_assignments FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE client_assignments TO app_user;
    END IF;
END
$$;
"""

_RESTORE_DEFAULT_PRIVILEGES = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE client_notes TO app_user;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE tags TO app_user;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE client_tags TO app_user;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE client_assignments TO app_user;
    END IF;
END
$$;
"""


def upgrade() -> None:
    sa.Enum(*_TAG_COLOUR, name="tag_colour").create(op.get_bind(), checkfirst=False)

    # ─── client_notes (DB §5.4, FR-M1-007) ────────────────────────────────
    op.create_table(
        "client_notes",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        # 🔒 FR-M1-007 — "with timestamp and author". NOT NULL: an unattributed
        # note is one nobody can be asked about, and FR-M3-020 keys editing off
        # exactly this column.
        sa.Column("author_user_id", sa.UUID(), nullable=False),
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
        # Soft delete, like everything user-facing (DB §22.2, FR-M1-010).
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_client_notes"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_client_notes__tenant"),
        # 🔒 Cascades with the client. A DPDP erasure (FR-M0-027) that removes a
        # client must take the notes written about them, or the erasure is
        # partial in the most sensitive place.
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name="fk_client_notes__client", ondelete="CASCADE"
        ),
        # ⚠️ No cascade on the author: EC-M1-04 reassigns a departing
        # practitioner's work rather than deleting it, and a cascade here would
        # erase the practice's record of what was discussed.
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"], name="fk_client_notes__author"),
        sa.CheckConstraint("length(btrim(body)) > 0", name="ck_client_notes__body_not_blank"),
    )

    # FR-M1-007 — "appended chronologically", which is how the thread is read.
    # ⚠️ DESC to match the query; a plain ascending index would still be usable
    # but forces a backward scan on the hot path.
    op.execute(
        """
        CREATE INDEX ix_client_notes__client_created
            ON client_notes (client_id, created_at DESC)
            WHERE archived_at IS NULL;
        """
    )
    op.create_index("ix_client_notes__tenant_id", "client_notes", ["tenant_id"])

    # ─── tags (DB §5.4, FR-M1-008) ────────────────────────────────────────
    op.create_table(
        "tags",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("colour", _enum("tag_colour"), server_default="slate", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_tags"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_tags__tenant"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_tags__name_not_blank"),
        sa.CheckConstraint("length(name) <= 40", name="ck_tags__name_length"),
    )

    # 🔒 Case-insensitive uniqueness — see the module docstring. A partial index
    # so an archived tag's name is released for reuse: a practitioner who
    # archived "Weight loss" and later recreates it should not be told the name
    # is taken by something they can no longer see.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_tags__tenant_name
            ON tags (tenant_id, lower(name))
            WHERE archived_at IS NULL;
        """
    )

    # ─── client_tags (DB §5.4) ────────────────────────────────────────────
    op.create_table(
        "client_tags",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("tag_id", sa.UUID(), nullable=False),
        sa.Column(
            "tagged_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tagged_by_user_id", sa.UUID(), nullable=True),
        # DB §5.4 — PK is the pair. Untagging is a real DELETE rather than a soft
        # delete: a tag is a label, not a record of anything that happened, so
        # there is no history to preserve and a tombstone would only complicate
        # the uniqueness that makes "is this client tagged X" a primary-key hit.
        sa.PrimaryKeyConstraint("client_id", "tag_id", name="pk_client_tags"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_client_tags__tenant"),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name="fk_client_tags__client", ondelete="CASCADE"
        ),
        # 🔒 Cascades: deleting a tag removes its applications. Unlike a note,
        # the junction row carries no information of its own — an orphaned one
        # would be a tag id pointing at nothing.
        sa.ForeignKeyConstraint(
            ["tag_id"], ["tags.id"], name="fk_client_tags__tag", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["tagged_by_user_id"], ["users.id"], name="fk_client_tags__tagged_by"
        ),
    )
    # Serves the list embed (API §7.1) and "which clients carry this tag"
    # (FR-M1-022), which Slice E's filter needs.
    op.create_index("ix_client_tags__tag", "client_tags", ["tenant_id", "tag_id"])
    op.create_index("ix_client_tags__tenant_id", "client_tags", ["tenant_id"])

    # ─── client_assignments (DB §5.5, EC-M0-04) ───────────────────────────
    op.create_table(
        "client_assignments",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("granted_by_user_id", sa.UUID(), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", sa.UUID(), nullable=True),
        # ⚠️ `granted_at` is part of the key — see the module docstring. Revoking
        # and re-granting the same colleague is legitimate, and the two-column
        # key DB §5.5 sketches would reject the second grant.
        sa.PrimaryKeyConstraint("client_id", "user_id", "granted_at", name="pk_client_assignments"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_client_assignments__tenant"
        ),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name="fk_client_assignments__client", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_client_assignments__user"),
        sa.ForeignKeyConstraint(
            ["granted_by_user_id"], ["users.id"], name="fk_client_assignments__granted_by"
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by_user_id"], ["users.id"], name="fk_client_assignments__revoked_by"
        ),
        # A revocation has a revoker, and a live grant has neither. Without this
        # the two columns can disagree about whether the grant is still active.
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by_user_id IS NULL)",
            name="ck_client_assignments__revocation_complete",
        ),
    )

    # 🔒 **At most one live grant per (client, user).** A partial unique index
    # rather than a constraint, because the uniqueness only applies to rows that
    # are still in force. Two live grants for one colleague would make a revoke
    # appear to do nothing — the second row would keep the access alive.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_client_assignments__live
            ON client_assignments (client_id, user_id)
            WHERE revoked_at IS NULL;
        """
    )
    # 🔒 The authorization read: "which clients may this practitioner see"
    # (FR-M0-017). On the hot path for every client-bound request they make.
    op.execute(
        """
        CREATE INDEX ix_client_assignments__user_live
            ON client_assignments (tenant_id, user_id)
            WHERE revoked_at IS NULL;
        """
    )
    op.create_index("ix_client_assignments__tenant_id", "client_assignments", ["tenant_id"])

    # ─── RLS: Pattern A on all four (DB §17.1) ────────────────────────────
    #
    # 🔒 FORCE is not redundant with ENABLE: without it the table owner bypasses
    # every policy, and migrations run as `app_migrator`, which owns these.
    #
    # 🔒 **No client-realm policy on any of them.** FR-M3-021 — notes are never
    # client-visible, and tags and assignments are practice-internal. The absence
    # of a policy is the enforcement (DB §17.1).
    #
    # ⚠️ Practitioner-scoping (FR-M0-017 / AC-M1-006) is **not** here either. It
    # is an authorization decision about a *user*, and `current_tenant_id()` is
    # all a policy has. DB §17.2: "RLS is the tenant seatbelt; authz is the
    # steering." It lives in `kernel.authz.owner_or_assigned`.
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

    op.execute(_APPLY_GRANTS)


def downgrade() -> None:
    """Drop everything this revision created.

    ⚠️ 🔒 **Destroys every note, tag and access grant.** Reversibility exists so
    the chain is honestly testable in development; running this against a
    database with real practice notes is not a supported operation.
    """
    op.execute(_RESTORE_DEFAULT_PRIVILEGES)
    op.drop_table("client_assignments")
    op.drop_table("client_tags")
    op.drop_table("tags")
    op.drop_table("client_notes")
    sa.Enum(name="tag_colour").drop(op.get_bind(), checkfirst=False)
