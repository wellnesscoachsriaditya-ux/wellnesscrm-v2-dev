"""Plan authoring — make a draft editable, and keep an issued one immutable.

Revision ID: 0020_plan_authoring
Revises: 0019_plan_schema_reconciliation
Created: 2026-08-14

Three things, all in service of M4 Slice 1.4a: a practitioner can add days and
slots to a draft plan, put food in them, take food back out, and read the whole
thing back quickly.

═══════════════════════════════════════════════════════════════════════════
1. DELETE on `plan_items` and `plan_slots`
═══════════════════════════════════════════════════════════════════════════

🔒 **The reason, recorded here because DB §17.1 requires one and revision 0018
gave none.** 0018 issued `REVOKE DELETE` on all twelve plan tables. That is
Pattern E treatment (DB §17.1 — "SELECT policy plus no UPDATE/DELETE grant")
applied to tables the same section assigns to Pattern A ("tenant-isolated…
applies to: clients, **plans**, appointments…"). Pattern E is scoped to
`audit_log` and `consent_records`; `ops/db/002_verify_grants.sql` names the
append-only set as exactly `audit_log`, `consent_records`, `operator_actions`,
and no plan table appears in it.

⚠️ Note also that `ops/db/001_roles.sql` grants all four verbs on future tables
by default. So this revision is 0018 ceasing to opt two tables out of the
platform default, not the introduction of a new privilege.

**Why these two and no others:**

* `plan_items` — API §8.1 specifies `PATCH/DELETE /app/plan-items/{id}`.
* `plan_slots` — FR-M4-025 (MVP) requires slots to be "add, rename, **remove**
  and reorder"-able. The API document defines no endpoint for it; this slice
  adds `DELETE /app/plan-slots/{id}` and the omission is being raised against
  API §8.1 separately.
* `plan_days` — 🔒 deliberately **not** granted. No requirement asks for day
  removal; FR-M4-026 requires only that 1-day and 7-day structures be
  *supported*. The consequence is real and is being stated rather than papered
  over: a 7-day plan cannot be shrunk to 1 day in this slice.
* `plan_item_alternatives`, `plan_supplements` — granted by the slice that
  builds them, so a privilege never lands ahead of the code that needs it.

**What is *not* touched:** `REVOKE DELETE` stands on `diet_plans`,
`diet_plan_versions`, `plan_snapshots` and all four `template_*` tables. That is
where DDR-11 (version history is never destroyed) and EC-M4-03 (an issued plan
retains the values in force at issue) actually live.

═══════════════════════════════════════════════════════════════════════════
2. 🔒 Draft-only deletion, enforced by the database
═══════════════════════════════════════════════════════════════════════════

A table-level grant cannot tell a draft row from an issued one. Left there, the
only thing standing between a caller and deleting an item out of an *issued*
plan would be `kernel.nutrition.assert_draft` — application code, which is
exactly the arrangement migration 0010 argued against ("at the table rather than
in the service because Arch R6 already assumes the database is the last line")
and DB §22.2 restates ("a filter can be forgotten, an index predicate is
structural").

So each grant is paired with a policy that walks up to the owning version and
requires `state = 'draft'`.

⚠️ **`AS RESTRICTIVE` is load-bearing.** PostgreSQL OR-s permissive policies
together, so adding a second *permissive* policy would **widen** access, not
narrow it — the new policy would simply offer a second way to qualify. Only a
restrictive policy is AND-ed with the existing `__tenant_isolation` policy.
Getting this backwards would produce a migration that reads like a restriction
and functions as a no-op, which is why `tests/integration/nutrition/
test_plan_deletion_policy.py` exercises it with the service guard bypassed.

⚠️ DELETE policies take a `USING` clause only — there is no `WITH CHECK` on a
statement that writes no row.

⚠️ These are the first `RESTRICTIVE` policies in the schema. Per-verb policies
have precedent (0015's `enquiry_forms__public_read`, 0017's `nutrients__read_all`).

`assert_draft` stays in the service. It is now defence in depth and, just as
importantly, the thing that produces a 409 with a usable message instead of a
row count of zero.

═══════════════════════════════════════════════════════════════════════════
3. Indexes and deterministic ordering
═══════════════════════════════════════════════════════════════════════════

`GET /app/plan-versions/{id}` walks version → days → slots → items on every
refresh of the plan builder, and DB §18 already flags `plan_items` aggregation
during authoring as a concern (~2M rows at scale, the largest domain table).
Four indexes, each matching the exact traversal.

The two unique constraints keep `sort_order` unambiguous within its parent. Two
rows sharing a position would render the plan differently on consecutive reads.

⚠️ **`INITIALLY DEFERRED`, unlike `uq_template_items_order` in 0018**, which is
`DEFERRABLE INITIALLY IMMEDIATE`. Reordering is a first-class operation here
(FR-M4-025) and a swap expressed as two `UPDATE`s transiently collides; deferring
to commit is what lets the service write the obvious statements. The cost is
that a genuine violation surfaces at `COMMIT` rather than at the offending
statement.

⚠️ **Not a data migration.** All five tables are empty in every environment.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020_plan_authoring"
down_revision: str | None = "0019_plan_schema_reconciliation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The tables this revision makes deletable, and the join that proves a row
#: belongs to a draft. Ordered child-first, which is also the order the policies
#: must be dropped in.
_DELETABLE: tuple[str, ...] = ("plan_items", "plan_slots")

#: 🔒 `plan_items` sits two joins below the version; `plan_slots` sits one.
#: Written out per table rather than generated, because a subtly wrong join in a
#: security policy is not something a loop should be trusted to produce.
_DRAFT_PREDICATES: dict[str, str] = {
    "plan_items": """
        EXISTS (
            SELECT 1
            FROM plan_slots s
            JOIN plan_days d ON d.id = s.plan_day_id
            JOIN diet_plan_versions v ON v.id = d.plan_version_id
            WHERE s.id = plan_items.plan_slot_id
              AND v.state = 'draft'
        )
    """,
    "plan_slots": """
        EXISTS (
            SELECT 1
            FROM plan_days d
            JOIN diet_plan_versions v ON v.id = d.plan_version_id
            WHERE d.id = plan_slots.plan_day_id
              AND v.state = 'draft'
        )
    """,
}

_GRANT_DELETE = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        GRANT DELETE ON TABLE plan_items TO app_user;
        GRANT DELETE ON TABLE plan_slots TO app_user;
    END IF;
END
$$;
"""

_REVOKE_DELETE = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        REVOKE DELETE ON TABLE plan_items FROM app_user;
        REVOKE DELETE ON TABLE plan_slots FROM app_user;
    END IF;
END
$$;
"""


def upgrade() -> None:
    # ─── 1 + 2: deletion, and the condition on it ────────────────────────
    op.execute(_GRANT_DELETE)

    for table_name in _DELETABLE:
        op.execute(
            f"""
            CREATE POLICY {table_name}__delete_draft_only ON {table_name}
            AS RESTRICTIVE FOR DELETE
            TO app_user
            USING ({_DRAFT_PREDICATES[table_name]})
            """
        )

    # ─── 3: the aggregate read path ──────────────────────────────────────
    op.create_index(
        "ix_plan_days__version",
        "plan_days",
        ["tenant_id", "plan_version_id", "day_number"],
    )
    op.create_index(
        "ix_plan_slots__day",
        "plan_slots",
        ["tenant_id", "plan_day_id", "sort_order"],
    )
    op.create_index(
        "ix_plan_items__slot",
        "plan_items",
        ["tenant_id", "plan_slot_id", "sort_order"],
    )
    op.create_index(
        "ix_diet_plans__client",
        "diet_plans",
        ["tenant_id", "client_id"],
    )

    op.create_unique_constraint(
        "uq_plan_slots_order",
        "plan_slots",
        ["plan_day_id", "sort_order"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_unique_constraint(
        "uq_plan_items_order",
        "plan_items",
        ["plan_slot_id", "sort_order"],
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    op.drop_constraint("uq_plan_items_order", "plan_items", type_="unique")
    op.drop_constraint("uq_plan_slots_order", "plan_slots", type_="unique")

    op.drop_index("ix_diet_plans__client", table_name="diet_plans")
    op.drop_index("ix_plan_items__slot", table_name="plan_items")
    op.drop_index("ix_plan_slots__day", table_name="plan_slots")
    op.drop_index("ix_plan_days__version", table_name="plan_days")

    for table_name in _DELETABLE:
        op.execute(f"DROP POLICY {table_name}__delete_draft_only ON {table_name}")

    # 🔒 Restores 0018's grant state exactly. A downgrade that left DELETE in
    # place would be a rollback that quietly kept the privilege it was rolling
    # back.
    op.execute(_REVOKE_DELETE)
