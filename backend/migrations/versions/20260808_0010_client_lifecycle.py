"""Client lifecycle — make the archive encoding unambiguous at the table.

Revision ID: 0010_client_lifecycle
Revises: 0009_clients
Created: 2026-08-08

🔒 DB §5.2, FR-M1-010, AC-M1-005, AC-M1-007. Slice A gave `clients` both a
`stage` column and a nullable `archived_at`. Slice B makes stage transitions a
first-class action, and doing so exposes a contradiction the schema currently
permits: **archived is expressible twice**, as `stage='archived'` and as
`archived_at IS NOT NULL`, and nothing stops the two disagreeing.

That matters because the entitlement predicate reads both. DB §5.2 counts
`stage='active' AND archived_at IS NULL`, and the second clause only earns its
place if an archived row can still carry its pre-archive stage — which is the
behaviour FR-M1-010 wants, since restoring a client must return them to what
they were rather than to a guess. So `archived_at` is the archive flag, and
`stage` records lifecycle position independently of it.

Which leaves `stage='archived'` as a value with no meaning that `archived_at`
does not already carry, and two ways to encode one fact is one way too many:
a row at `stage='archived'` with `archived_at IS NULL` would be invisible to
`ClientDirectory.count_active` yet visible to every default view, and no
reviewer reading either half alone would see it.

This revision adds one CHECK, `ck_clients__stage_not_archived`, refusing that
stage at the table. 🔒 At the table rather than in the service because Arch R6
already assumes the database is the last line: the transitions service refuses
it too, and a constraint is what makes the refusal true for a row arriving by
backfill, by psql, or by a future module that forgets.

⚠️ **The enum value is deliberately left in place.** OD-01 has not resolved the
stage vocabulary, dropping a value from a PostgreSQL enum requires rewriting the
type and every column using it, and the value may yet be wanted — a practitioner
vocabulary where "archived" is a visible pipeline stage is a legitimate outcome
of OD-01. Keeping an unusable value costs nothing; a type rewrite to reinstate
it later costs a migration with a table lock.

⚠️ **Not a data migration.** `clients` was created one revision ago and the
stage was never reachable through any write path, so there is nothing to fix
up. The constraint is added `NOT VALID`-free — a plain ADD CONSTRAINT — because
validating an empty-to-small table is instant. 🔒 If this were being added to a
populated production table it would need `NOT VALID` then `VALIDATE`, to avoid
holding ACCESS EXCLUSIVE while every row is scanned.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_client_lifecycle"
down_revision: str | None = "0009_clients"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CONSTRAINT = "ck_clients__stage_not_archived"


def upgrade() -> None:
    """Refuse `stage='archived'`; `archived_at` is the only archive flag."""
    op.execute(
        f"""
        ALTER TABLE clients
        ADD CONSTRAINT {_CONSTRAINT}
        CHECK (stage <> 'archived'::client_stage)
        """
    )


def downgrade() -> None:
    """Drop the constraint.

    ⚠️ Reversible and genuinely safe to reverse: dropping a CHECK cannot fail on
    existing data and loses no information. What it restores is the ambiguity
    described above, so a downgrade should be followed by re-reading which
    encoding the application is relying on.
    """
    op.execute(f"ALTER TABLE clients DROP CONSTRAINT {_CONSTRAINT}")
