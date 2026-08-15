"""Bring ``adherence_logs`` to DB §12.1 — the shape ``/portal/sync`` writes.

Revision ID: 0026_adherence_logs_to_spec
Revises: 0025_portal_client_realm_rls
Created: 2026-08-15

🔒 **S6 Slice 2 Step 3.** ``/portal/sync`` carries ``adherence.log`` operations
whose payload is fixed by API §12.3: ``logged_for_date``, ``slot_id?`` /
``slot_type``, an ``adherence`` enum of four values, and ``client_timestamp``.
The table cannot store any of them.

⚠️ **This is not a redesign; it is the specified schema.** Revision
``2eb56b8913d5`` created ``adherence_logs`` with a single integer ``score``
column, and its own source records the guess — *"The S6 plan mentions 'one-tap
adherence', usually meaning 100% or 'did it'. Let's store adherence_score from 0
to 100."* DB §12.1 specifies something different and more precise, and API §12.3
depends on that difference: ``followed`` and ``partial`` are clinical
observations a practitioner reads, not points on a scale somebody has to
interpret. A 0–100 score cannot express ``skipped`` at all — a skipped meal and
a meal not followed are different facts, and averaging them would be wrong.

🔒 **No data is migrated because none exists.** The table has been unreadable and
unwritable since it was created: revision ``2eb56b8913d5`` gave it a RESTRICTIVE
policy alone, so RLS denied everything, and revision ``0025`` (which repaired
that) is one commit old. ``SELECT count(*)`` is 0. Dropping ``score`` therefore
discards nothing, and the columns below can be ``NOT NULL`` without a backfill.

⚠️ **``slot_type`` is ``text``, not the ``meal_slot_type`` DB §12.1 names.** That
is not an oversight: ``kernel.nutrition.MealSlotType`` states the rule this
follows — *"Deliberately not a PostgreSQL enum yet. ``plan_slots.slot_type`` and
``template_slots.slot_type`` stay ``text`` until the vocabulary is confirmed, and
the database type is created in the slice that also needs it for
``foods.meal_suitability``."* Creating the type here for one column would make
this the slice that commits the vocabulary, which FR-M4-025 marks 🟡 PROPOSED and
Validation Gate G1 has not settled. The value is validated in Python against the
same enum the plan builder uses.

🔒 **``adherence_value`` *is* created**, because both DB §12.1 and API §12.3 name
the same four values, and the wire contract branches on them. Two documents
agreeing is the bar for a database type; one proposal is not.

🔒 **``uq_adherence_logs__idempotency`` is untouched.** UNIQUE(``client_id``,
``idempotency_key``) is what DB §12.1 specifies and what makes a replayed offline
queue a no-op rather than a duplicate. Widening or narrowing it would change the
replay guarantee ``/portal/sync`` rests on, so it is left exactly as it is.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_adherence_logs_to_spec"
down_revision: str | None = "0025_portal_client_realm_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 🟡 DB §12.1 / API §12.3. Marked PROPOSED in the database document; the API
#: document lists the same four without a marker, and the response contract
#: branches on them.
_ADHERENCE_VALUES = ("followed", "partial", "not_followed", "skipped")


def upgrade() -> None:
    sa.Enum(*_ADHERENCE_VALUES, name="adherence_value").create(op.get_bind(), checkfirst=False)

    # ─── 1. Names DB §12.1 uses ──────────────────────────────────────────
    # ⚠️ Renames rather than drop-and-add: the index and the RLS policies below
    # follow a renamed column automatically, where a drop would take
    # `ix_adherence_logs__client_date` with it.
    op.alter_column("adherence_logs", "for_date", new_column_name="logged_for_date")
    op.alter_column("adherence_logs", "server_timestamp", new_column_name="server_recorded_at")

    # ─── 2. The guessed column goes ──────────────────────────────────────
    op.drop_constraint("ck_adherence_logs__score_range", "adherence_logs", type_="check")
    op.drop_column("adherence_logs", "score")

    # ─── 3. What API §12.3 actually sends ────────────────────────────────
    op.add_column(
        "adherence_logs",
        sa.Column(
            "adherence",
            sa.Enum(*_ADHERENCE_VALUES, name="adherence_value", create_type=False),
            nullable=False,
            comment="🔒 API §12.3 — the observation, not a score",
        ),
    )
    op.add_column(
        "adherence_logs",
        sa.Column(
            "slot_type",
            sa.Text(),
            nullable=False,
            comment=(
                "🔒 DB §12.1 — denormalised beside plan_slot_id so a log stays "
                "interpretable after the plan is revised (DDR-11). `text` rather "
                "than an enum: see the module docstring."
            ),
        ),
    )
    op.add_column(
        "adherence_logs",
        sa.Column(
            "plan_slot_id",
            sa.UUID(),
            nullable=True,
            comment="Which slot, when the client's cached plan still names one",
        ),
    )
    op.add_column(
        "adherence_logs",
        sa.Column(
            "plan_version_id",
            sa.UUID(),
            nullable=True,
            comment=(
                "🔒 Which plan they were following. Resolved server-side from the "
                "client's issued plan — never accepted from the request."
            ),
        ),
    )
    op.add_column(
        "adherence_logs",
        sa.Column("note", sa.Text(), nullable=True, comment="⏳ DB §12.1 — Phase 2"),
    )

    # 🔒 DB §12.1's second index — the practitioner's check-in review
    # (FR-M9-003) reads a tenant's logs by date, not one client's.
    op.create_index(
        "ix_adherence_logs__tenant_date",
        "adherence_logs",
        ["tenant_id", "logged_for_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_adherence_logs__tenant_date", table_name="adherence_logs")

    for column in ("note", "plan_version_id", "plan_slot_id", "slot_type", "adherence"):
        op.drop_column("adherence_logs", column)

    sa.Enum(name="adherence_value").drop(op.get_bind(), checkfirst=False)

    # ⚠️ `score` returns NOT NULL with no default, which is only safe because the
    # table is empty — the same fact that makes the upgrade safe. A downgrade run
    # against rows written by this revision would fail here, loudly, rather than
    # inventing a score for observations that never had one.
    op.add_column(
        "adherence_logs",
        sa.Column("score", sa.Integer(), nullable=False, comment="Adherence score (0-100)"),
    )
    op.create_check_constraint(
        "ck_adherence_logs__score_range", "adherence_logs", "score >= 0 AND score <= 100"
    )

    op.alter_column("adherence_logs", "server_recorded_at", new_column_name="server_timestamp")
    op.alter_column("adherence_logs", "logged_for_date", new_column_name="for_date")
