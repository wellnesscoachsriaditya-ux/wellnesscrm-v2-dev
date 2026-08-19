"""Plan schema reconciliation — three columns DB §8.10 and §8.13 require.

Revision ID: 0019_plan_schema_reconciliation
Revises: 0018_nutrition_plans
Created: 2026-08-13

Revision 0018 created the twelve plan tables. Three columns the database design
specifies were not carried across, and each of them is load-bearing for work that
starts in this slice rather than cosmetic:

* 🔒 **`plan_snapshots.content_hash`** — DB §8.13 lists it `text NOT NULL`, and
  DDR-12 makes it the mechanism, not an optimisation: one immutable snapshot
  serves the PDF, the client portal and offline sync, and `content_hash` is how
  a cached copy is invalidated. EC-M7-03 (the plan is revised while the client is
  looking at it) is *detectable* by hash comparison and undetectable without one.
  The alternative — silently swapping the content under the reader — is the
  failure mode the hash exists to prevent.

* **`plan_snapshots.created_at`** — DB §8.13. Every other record in the schema
  carries one; a snapshot that cannot say when it was taken is the one row in an
  audit trail nobody can place.

* 🔒 **`plan_supplements.is_locked`** — DB §8.10 puts locking on the entities a
  practitioner deliberately fixes, and the implementation plan's S4 section lists
  `plan_supplements` alongside `template_items`, `plan_items` and `plan_slots`.
  Without it a supplement cannot be locked, so recalculation and AI drafting have
  no way to be told to leave one alone — and "the model is never trusted to
  respect a constraint" (DB §8.10) requires the constraint to be expressible.

⚠️ **`content_hash` is added `NOT NULL` with no default, deliberately.** Every
environment has zero `plan_snapshots` rows — the only writer is
``issue_plan_version``, which has existed for one commit and is exercised solely
by tests that clean up after themselves. If that assumption is ever false, this
statement fails at deploy time with a plain "column contains null values", which
is the correct outcome: a backfilled placeholder hash would be *worse* than no
hash, because a portal comparing it would conclude the content had not changed.
A default of `''` would do exactly that, silently.

⚠️ **Not a data migration**, and reversible without one. `downgrade()` drops the
three columns; nothing else in 0018 depends on them, and no row is destroyed that
was not created by the column itself.

⚠️ **No grant work.** 0018 already issues table-level
`GRANT SELECT, INSERT, UPDATE` to `app_user` on both tables, and a table-level
grant covers columns added afterwards. `REVOKE DELETE` on both tables likewise
still stands, which is what DDR-11 and EC-M4-03 want for a snapshot.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_plan_schema_reconciliation"
down_revision: str | None = "0018_nutrition_plans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 🔒 DDR-12 — the client portal's cache-validation key.
    op.add_column(
        "plan_snapshots",
        sa.Column("content_hash", sa.Text(), nullable=False),
    )
    op.add_column(
        "plan_snapshots",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # 🔒 DB §8.10 — a supplement the practitioner has fixed.
    op.add_column(
        "plan_supplements",
        sa.Column("is_locked", sa.Boolean(), server_default="false", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("plan_supplements", "is_locked")
    op.drop_column("plan_snapshots", "created_at")
    op.drop_column("plan_snapshots", "content_hash")
