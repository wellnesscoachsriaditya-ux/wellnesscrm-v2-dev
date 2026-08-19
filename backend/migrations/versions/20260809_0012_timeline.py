"""Timeline — the materialised projection FR-M1-018 reads.

Revision ID: 0012_timeline
Revises: 0011_collaboration
Created: 2026-08-09

🔒 DB §5.6, DDR-06, FR-M1-018/019, NFR-006. One table, owned by `clients`,
written only by event subscribers.

📌 **DDR-06 — why a table at all.** FR-M1-018 wants one reverse-chronological
view aggregating events from six modules. A read-time UNION across their tables
would violate R6 (cross-module reads) and add a branch per new event type, so the
800 ms NFR-006 budget would erode with every slice. A materialised row written by
a subscriber makes a timeline load one indexed query, permanently, and adding an
event type touches no existing module.

⚠️ **A derived projection, never a source of truth** (DB §5.6). Every row here
restates something its owning module still holds. Nothing reads from this table
to make a decision — it feeds one screen. That is what makes it rebuildable if it
drifts, and what makes a summary wording change a backfill rather than a
migration.

🔒 **Append-only, enforced by grant.** `app_user` gets SELECT and INSERT and
nothing else, which puts `timeline_events` in the same category as `audit_log`
(DDR-15) for the verbs that matter. A timeline the application can rewrite is not
a history. Consequences that are deliberate rather than overlooked:

* An edited note appends nothing — FR-M3-020 puts edits in the audit log, and a
  row per typo fix would bury the events that matter.
* An archived note leaves its "Note added" row standing. The note leaving the
  thread does not un-happen the writing of it.
* A detached tag leaves its "Tags updated" row standing, for the same reason.

⚠️ **`timeline_events` is NOT registered in `ops/db/002_verify_grants.sql`.**
That script asserts the DDR-15 append-only *set*, whose membership is a
documented list; adding a table to it is a decision about audit scope, not a
consequence of the grants happening to match. The grants below stand on their
own and the round-trip test covers them. Worth revisiting if the audit scope is
ever formally widened.

🔒 **The enum is declared complete, not incrementally.** `timeline_event_type`
carries values S3–S6 will produce and S2 cannot. Adding an enum value later is a
migration whose transactional behaviour varies by PostgreSQL version, and nine
such migrations is nine chances to get it wrong for no benefit — an unused enum
value costs nothing. See `kernel.timeline.TimelineEventType`, and
`is_producible()` for how the API avoids offering a filter that finds nothing.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_timeline"
down_revision: str | None = "0011_collaboration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: 🟡 PROPOSED (see `kernel.timeline.TimelineEventType`). Derived from
#: FR-M1-018's own list of what the timeline aggregates. Order here is the
#: enum's sort order in PostgreSQL; it carries no meaning, since the timeline is
#: ordered by `occurred_at`.
_EVENT_TYPE = (
    "stage_changed",
    "note_added",
    "tag_applied",
    "ownership_changed",
    "access_changed",
    "client_archived",
    "client_restored",
    "enquiry_received",
    "measurement_recorded",
    "assessment_completed",
    "plan_issued",
    "appointment_scheduled",
    "message_sent",
    "document_uploaded",
    "client_activity",
)

#: DB §5.6 — "practitioner | client | system".
_ACTOR_TYPE = ("practitioner", "client", "system")


def _enum(name: str) -> postgresql.ENUM:
    """Reference an already-created enum without re-emitting its DDL. See 0002."""
    return postgresql.ENUM(name=name, create_type=False)


# ⚠️ Guarded on role existence, matching every migration since 0001.
_APPLY_GRANTS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        -- 🔒 Append-only. No UPDATE, no DELETE — a timeline the application can
        -- rewrite is not a history. See the module docstring for the three
        -- consequences this accepts on purpose.
        REVOKE UPDATE, DELETE ON TABLE timeline_events FROM app_user;
        GRANT SELECT, INSERT ON TABLE timeline_events TO app_user;
    END IF;
END
$$;
"""

_RESTORE_DEFAULT_PRIVILEGES = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE timeline_events TO app_user;
    END IF;
END
$$;
"""


def upgrade() -> None:
    sa.Enum(*_EVENT_TYPE, name="timeline_event_type").create(op.get_bind(), checkfirst=False)
    sa.Enum(*_ACTOR_TYPE, name="timeline_actor_type").create(op.get_bind(), checkfirst=False)

    op.create_table(
        "timeline_events",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("event_type", _enum("timeline_event_type"), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("source_module", sa.Text(), nullable=False),
        # ⚠️ Nullable and **no foreign key**, deliberately. It points into six
        # different modules' tables, so there is no single referent — and a
        # cascade from any of them would delete history, which is the opposite of
        # what this table is for. An orphaned id renders as a non-link.
        sa.Column("source_record_id", sa.UUID(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("actor_type", _enum("timeline_actor_type"), nullable=False),
        # ⚠️ No FK. An actor may be a client or the system, neither of which has
        # a `users` row.
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_timeline_events"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_timeline_events__tenant"),
        # 🔒 Cascades with the client. A DPDP erasure (FR-M0-027) that removes a
        # client must take their timeline: it names what happened to them.
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name="fk_timeline_events__client", ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "length(btrim(summary)) > 0", name="ck_timeline_events__summary_not_blank"
        ),
        # 🔒 DB §5.6 — a summary is a label, not a description. The bound is a
        # backstop; `kernel.timeline.summarise` returns fixed strings far under
        # it, and its signature is what actually keeps prose out.
        sa.CheckConstraint("length(summary) <= 200", name="ck_timeline_events__summary_length"),
        # 🔒 The system acts as nobody — attributing an automated action to a
        # person is the exact misreading `actor_type` exists to prevent.
        sa.CheckConstraint(
            "actor_type <> 'system' OR actor_id IS NULL",
            name="ck_timeline_events__system_has_no_actor",
        ),
    )

    # 🔒 **NFR-006 rests entirely on this index.** (tenant, client, time DESC)
    # matches the only query this table has, so 20 rows come back from an index
    # scan with no sort.
    #
    # ⚠️ `id DESC` is the tiebreaker and it is load-bearing, not decoration.
    # Cursor pagination (ADR-A05) keys on `occurred_at`, and several events
    # committed in one transaction share a timestamp to the microsecond — a
    # stage change and its entitlement side effects, for instance. Without a
    # unique tiebreaker in both the index and the cursor, those rows have no
    # stable order and a page boundary landing mid-group would silently skip or
    # repeat them.
    op.execute(
        """
        CREATE INDEX ix_timeline_events__client_occurred
            ON timeline_events (tenant_id, client_id, occurred_at DESC, id DESC);
        """
    )

    # FR-M1-019 — filtering by type. Partial on nothing, because every row is
    # live; the type is low-cardinality, so this earns its keep only on clients
    # with long histories, which is precisely where the filter gets used.
    op.create_index(
        "ix_timeline_events__client_type",
        "timeline_events",
        ["tenant_id", "client_id", "event_type"],
    )

    # ─── RLS: Pattern A (DB §17.1) ────────────────────────────────────────
    #
    # 🔒 FORCE is not redundant with ENABLE: without it the table owner bypasses
    # every policy, and migrations run as `app_migrator`, which owns this table.
    #
    # ⚠️ **No client-realm policy**, and this one deserves stating. FR-M3-021
    # keeps notes from clients, and a "Note added" timeline row would leak the
    # *existence* of a note even without its body — which is enough to tell a
    # client something was written about them after an appointment. When S6
    # gives the portal a timeline it must be a filtered projection with its own
    # policy naming permitted event types, never this table unfiltered.
    #
    # ⚠️ Practitioner-scoping (AC-M1-006) is not here either — it is an
    # authorization question about a user, and a policy only has
    # `current_tenant_id()`. It lives in `kernel.authz.owner_or_assigned`, and
    # the route reaches it through `access.load_for_access`.
    op.execute("ALTER TABLE timeline_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE timeline_events FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY timeline_events__tenant_isolation ON timeline_events
        USING (tenant_id = current_tenant_id())
        WITH CHECK (tenant_id = current_tenant_id());
        """
    )

    op.execute(_APPLY_GRANTS)


def downgrade() -> None:
    """Drop the projection.

    ⚠️ Destroys every timeline row. Less alarming than 0011's downgrade: this
    table is derived, so a rebuild job can regenerate it from the module tables
    that are still intact. That is the practical payoff of DB §5.6's insistence
    that it is a projection and not a source of truth.
    """
    op.execute(_RESTORE_DEFAULT_PRIVILEGES)
    op.drop_table("timeline_events")
    sa.Enum(name="timeline_actor_type").drop(op.get_bind(), checkfirst=False)
    sa.Enum(name="timeline_event_type").drop(op.get_bind(), checkfirst=False)
