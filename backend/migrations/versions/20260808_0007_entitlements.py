"""Entitlements — plans, subscriptions and metered usage.

Revision ID: 0007_entitlements
Revises: 0006_consent_ledger
Created: 2026-08-08

🔒 DB §14.1..14.5, FR-M0-044/045/046, FR-M10-001..005, DDR-14. Enforcement is
structural in S1; **collection is deferred to M10.3**. The distinction matters
for reading this migration: nothing here charges anyone. It decides whether a
metered action is permitted, and records what was consumed.

Five tables, in dependency order:

* ``plan_definitions`` — plans as *configuration* (FR-M10-001), versioned
* ``subscriptions`` — one per tenant, the current commercial position
* ``subscription_events`` — 🔒 append-only history of that position
* ``usage_counters`` — O(1) enforcement state on the hot path (DDR-14)
* ``usage_events`` — the log that makes any counter reconcilable

📌 **DDR-14 — a counter row plus an append-only event log.** Counting live from
source tables is cheap for active clients and expensive for AI generations and
messages, and cross-module counting violates Arch R6. The counter answers the
enforcement question in one indexed read; the event log is what makes a drifted
counter recoverable rather than merely wrong.

⚠️ 🔒 **Active clients are the exception — counted live** from ``clients WHERE
stage='active'`` (DB §14.4, M1.5). It is a cheap indexed count, and it is the
product's most visible limit: a counter that drifts there produces a wrong bill
on the number the practitioner can verify by eye. ``clients`` does not exist
until S2, so no counter row is seeded for that resource.

⚠️ **RLS dispositions differ across these five tables, deliberately:**

* ``plan_definitions`` — Pattern D. Platform-wide catalogue, no ``tenant_id`` to
  key a policy on. Tenants read it; only the operator role writes it.
* ``subscriptions`` / ``usage_counters`` / ``usage_events`` — Pattern A.
  Tenant-scoped and read on the enforcement path, which always runs with a
  tenant in scope.
* ``subscription_events`` — Pattern A via ``tenant_id``. 🔒 It carries a
  redundant ``tenant_id`` *purely* so a policy can be keyed on it: the natural
  key is ``subscription_id``, and a policy joining to ``subscriptions`` to reach
  the tenant would make every insert depend on a subquery. Denormalising one
  column is cheaper than an unenforceable policy.

🔒 **Two append-only tables, protected differently, and the difference is not an
inconsistency.** ``subscription_events`` is strictly append-only — UPDATE and
DELETE revoked, and it is registered in ``ops/db/002_verify_grants.sql`` in this
same commit, as that file requires. ``usage_events`` keeps UPDATE, because
``is_reconciled`` is bookkeeping a reconciliation pass must be able to set; a
trigger rejects edits to every *other* column. This is the ``consent_notices``
pattern from 0006, chosen for the same reason: a grant cannot express "every
column but one", and a trigger can.

⚠️ 🔒 **The seeded plan limits are PROPOSED** (PRD M10.4, ASM-08, OD-04). Pricing
requires market validation and quota values require cost verification. They are
seeded because enforcement without a plan to enforce is untestable, not because
the numbers are settled.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_entitlements"
down_revision: str | None = "0006_consent_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ─── Enum types (DDR-02) ─────────────────────────────────────────────────
#
# ⚠️ 🔒 `plan_definitions.code` and the two `resource_code` columns are
# deliberately **text, not enums**. FR-M10-001 requires that plans are
# configuration and that "adding a new metered resource must not require a
# migration" — DB §14.1 permits `jsonb` limits for exactly that reason. An enum
# would put the release train back in front of every new tier and every new
# metered resource, which is the constraint that requirement exists to remove.
# The valid set lives in `kernel.entitlements.ResourceCode`, which is where a
# typo is caught; the database's job here is to store what it is given.

_ENUMS: dict[str, tuple[str, ...]] = {
    # 🔒 `trialing` and `past_due` both permit metered actions; `suspended` and
    # `cancelled` do not. Collapsing them would remove the state EC-M10-05
    # (payment arriving after suspension) needs in order to be recoverable.
    "subscription_status": ("trialing", "active", "past_due", "suspended", "cancelled"),
    "subscription_event": (
        "created",
        "activated",
        "plan_changed",
        "suspended",
        "reactivated",
        "cancelled",
    ),
    "billing_period": ("monthly", "annual"),
}


# ⚠️ Guarded on role existence, matching 0001, 0003 and 0006. A local developer
# may run a single-role database, and a REVOKE naming a missing role aborts the
# transaction — which would make this migration unrunnable on the setup most
# likely to run it first.
_ENFORCE_APPEND_ONLY = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        -- 🔒 DDR-15. Strictly append-only: two verbs, not four. A subscription
        -- history the application can revise cannot answer EC-M10-01 (downgrade
        -- while over limit) or EC-M10-05 (payment after suspension), both of
        -- which turn on *when* state changed rather than what it is now.
        -- Registered in ops/db/002_verify_grants.sql in this same commit.
        REVOKE UPDATE, DELETE ON TABLE subscription_events FROM app_user;
        GRANT INSERT, SELECT ON TABLE subscription_events TO app_user;
        GRANT USAGE, SELECT ON SEQUENCE subscription_events_id_seq TO app_user;

        -- 🔒 `usage_events` keeps UPDATE and loses DELETE — the consent_notices
        -- pattern from 0006. `is_reconciled` is bookkeeping a reconciliation
        -- pass must set; every other column is immutable, enforced by the
        -- trigger below because a grant cannot express "all columns but one".
        -- ⚠️ Deliberately absent from 002_verify_grants.sql: that check asserts
        -- neither UPDATE nor DELETE, which this table would fail by design.
        REVOKE DELETE ON TABLE usage_events FROM app_user;
        GRANT INSERT, SELECT, UPDATE ON TABLE usage_events TO app_user;
        GRANT USAGE, SELECT ON SEQUENCE usage_events_id_seq TO app_user;

        -- 🔒 Plans are ours, not the tenant's. FR-M10-001 makes them
        -- configuration; a tenant editing its own limits is the entitlement
        -- system defeating itself.
        REVOKE INSERT, UPDATE, DELETE ON TABLE plan_definitions FROM app_user;
        GRANT SELECT ON TABLE plan_definitions TO app_user;

        -- 🔒 FR-M10-008 — activation is manual by an operator at MVP. The
        -- application may read its subscription and never change which plan it
        -- is on, or a tenant could upgrade itself for free.
        REVOKE INSERT, UPDATE, DELETE ON TABLE subscriptions FROM app_user;
        GRANT SELECT ON TABLE subscriptions TO app_user;
    END IF;
END
$$;
"""

#: 🔒 Pattern A — the tables carrying `tenant_id`, and therefore the tables that
#: get RLS enabled, forced and policied below. Named rather than inlined in the
#: loop so the protected set is one greppable declaration: dropping a table from
#: this tuple ships it without isolation, and
#: `tests/test_kernel_entitlements_schema.py` reads this tuple from the AST for
#: exactly that reason.
#:
#: ⚠️ `plan_definitions` is deliberately absent — Pattern D, no `tenant_id` to
#: key a policy on. A policy there would match nothing, every tenant would read
#: no plan, and the enforcement path would then deny every metered action.
_TENANT_SCOPED: tuple[str, ...] = (
    "subscriptions",
    "subscription_events",
    "usage_counters",
    "usage_events",
)


_RESTORE_DEFAULT_PRIVILEGES = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE subscription_events TO app_user;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE usage_events TO app_user;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE plan_definitions TO app_user;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE subscriptions TO app_user;
    END IF;
END
$$;
"""

# 🔒 EC-M10-04, DB §14.5. A consumed quota that must be given back is a
# compensating **negative event**, never an edit to the event that consumed it
# and never a decrement of the counter alone. Without this trigger, correcting a
# failed AI generation would rewrite history, and the event log's only purpose is
# to be the thing history can be recovered from.
_ENFORCE_USAGE_EVENT_IMMUTABILITY = """
CREATE OR REPLACE FUNCTION usage_events__reject_material_edit()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.tenant_id       IS DISTINCT FROM OLD.tenant_id
       OR NEW.resource_code   IS DISTINCT FROM OLD.resource_code
       OR NEW.amount          IS DISTINCT FROM OLD.amount
       OR NEW.source_module   IS DISTINCT FROM OLD.source_module
       OR NEW.source_record_id IS DISTINCT FROM OLD.source_record_id
       OR NEW.occurred_at     IS DISTINCT FROM OLD.occurred_at
    THEN
        RAISE EXCEPTION
            'usage_events is append-only; record a compensating negative event '
            'instead of editing one (EC-M10-04). Only is_reconciled may change.';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_usage_events__immutable
    BEFORE UPDATE ON usage_events
    FOR EACH ROW
    EXECUTE FUNCTION usage_events__reject_material_edit();
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

    # ─── plan_definitions (DB §14.1) ──────────────────────────────────────
    op.create_table(
        "plan_definitions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        # Text, not an enum — see the note above _ENUMS. FR-M10-001.
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        # 🔒 numeric(10,2), never float. Money in binary floating point produces
        # totals that do not reconcile, and this column feeds GST invoices.
        sa.Column("price_amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency_code", sa.Text(), server_default="INR", nullable=False),
        sa.Column("billing_period", _enum("billing_period"), nullable=False),
        # 🔒 DB §14.1 — the one place jsonb is permitted for configuration,
        # because adding a metered resource must not require a migration.
        # Keys at MVP: active_clients, ai_generations_per_month,
        # whatsapp_messages_per_month, storage_mb, practitioner_seats.
        sa.Column("limits", postgresql.JSONB(), nullable=False),
        sa.Column(
            "features", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("is_public", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        # 🔒 Versioned rather than mutated. A tenant on last year's pricing keeps
        # it; editing a plan row in place would silently reprice every existing
        # customer on it, which is the failure DB §14.1 calls out.
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_plan_definitions"),
        # 🔒 Not unique on `code` alone — that is the whole point of versioning.
        # Unique on the pair, so one tier can have a priced history.
        sa.UniqueConstraint("code", "effective_from", name="uq_plan_definitions__code_effective"),
        sa.CheckConstraint("price_amount >= 0", name="ck_plan_definitions__price_non_negative"),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_plan_definitions__effective_window_ordered",
        ),
        # 🔒 A plan whose limits are not an object cannot be read by the
        # enforcement path, and the failure would surface as a fail-safe denial
        # for every tenant on that plan rather than as a bad row.
        sa.CheckConstraint(
            "jsonb_typeof(limits) = 'object'", name="ck_plan_definitions__limits_is_object"
        ),
    )
    # "Which version of this plan is in force now" — read on every activation.
    op.create_index(
        "ix_plan_definitions__code_effective",
        "plan_definitions",
        ["code", "effective_from"],
    )

    # ─── subscriptions (DB §14.2) ─────────────────────────────────────────
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("plan_definition_id", sa.UUID(), nullable=False),
        sa.Column(
            "status", _enum("subscription_status"), server_default="trialing", nullable=False
        ),
        sa.Column("trial_ends_on", sa.Date(), nullable=True),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancel_at_period_end", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        # 🔒 FR-M10-008 — manual activation at MVP. Recorded so "who turned this
        # on" is answerable without reading the event log.
        sa.Column("activated_by_operator_id", sa.UUID(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_subscriptions"),
        # 🔒 DB §14.2 — exactly one subscription per tenant. History lives in
        # subscription_events, so a second row here would make "which plan is
        # this tenant on" a question with two answers.
        sa.UniqueConstraint("tenant_id", name="uq_subscriptions__tenant"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_subscriptions__tenant"),
        # 🔒 No cascade: a plan version referenced by a live subscription must not
        # be removable, or a tenant would be left with no limits to enforce.
        sa.ForeignKeyConstraint(
            ["plan_definition_id"],
            ["plan_definitions.id"],
            name="fk_subscriptions__plan_definition",
        ),
        sa.ForeignKeyConstraint(
            ["activated_by_operator_id"],
            ["operators.id"],
            name="fk_subscriptions__activated_by_operator",
        ),
        sa.CheckConstraint(
            "current_period_end IS NULL"
            " OR current_period_start IS NULL"
            " OR current_period_end > current_period_start",
            name="ck_subscriptions__period_ordered",
        ),
    )

    # ─── subscription_events — 🔒 append-only (DB §14.3) ───────────────────
    op.create_table(
        "subscription_events",
        # bigserial, matching audit_log and consent_records: append-only, read as
        # an ordered scan per subscription.
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("subscription_id", sa.UUID(), nullable=False),
        # 🔒 Redundant against subscriptions.tenant_id, and carried anyway so a
        # Pattern A policy can be keyed on it directly. A policy that joined to
        # subscriptions to reach the tenant would put a subquery in front of
        # every insert on the activation path.
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("event_type", _enum("subscription_event"), nullable=False),
        sa.Column("from_plan_id", sa.UUID(), nullable=True),
        sa.Column("to_plan_id", sa.UUID(), nullable=True),
        sa.Column("actor_type", _enum("actor_type"), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_subscription_events"),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["subscriptions.id"], name="fk_subscription_events__subscription"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_subscription_events__tenant"
        ),
        # 🔒 A plan change with no destination is not a plan change. The check is
        # narrow on purpose: only `plan_changed` and `activated` name a plan,
        # because a suspension does not move anyone.
        sa.CheckConstraint(
            "event_type NOT IN ('plan_changed', 'activated') OR to_plan_id IS NOT NULL",
            name="ck_subscription_events__plan_move_has_destination",
        ),
    )
    op.create_index(
        "ix_subscription_events__subscription",
        "subscription_events",
        ["subscription_id", "occurred_at"],
    )
    op.create_index("ix_subscription_events__tenant_id", "subscription_events", ["tenant_id"])

    # ─── usage_counters (DB §14.4) ────────────────────────────────────────
    op.create_table(
        "usage_counters",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        # Text, not an enum — FR-M10-001. Valid values live in
        # kernel.entitlements.ResourceCode.
        sa.Column("resource_code", sa.Text(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        # 🔒 numeric, not integer: storage is metered in fractional MB and a
        # future resource may be metered in minutes or tokens.
        sa.Column("used_amount", sa.Numeric(), server_default=sa.text("0"), nullable=False),
        # 🔒 DB §14.4 — snapshotted from the plan, not read through the FK. A
        # mid-period plan change would otherwise retroactively change what the
        # tenant was allowed to do earlier in the same period, and the 80%
        # warning already sent would refer to a limit that no longer exists.
        sa.Column("limit_amount", sa.Numeric(), nullable=True),
        # 🔒 FR-M10-005 — stamped when the warning is sent, so it is sent once.
        # A boolean would not survive a period roll; a timestamp is also the
        # evidence that the warning happened.
        sa.Column("warned_at_80pct", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_usage_counters"),
        # 🔒 DB §14.4. One counter per resource per period, which is what makes
        # the increment an upsert rather than a read-modify-write race.
        sa.UniqueConstraint(
            "tenant_id",
            "resource_code",
            "period_start",
            name="uq_usage_counters__tenant_resource_period",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_usage_counters__tenant"),
        sa.CheckConstraint("period_end > period_start", name="ck_usage_counters__period_ordered"),
        # 🔒 The counter never goes negative. A compensating negative event
        # (EC-M10-04) may bring it back to zero and no further.
        #
        # ⚠️ This constraint is why `platform.entitlements.record_usage` clamps
        # both halves of its upsert. PostgreSQL validates CHECK against the
        # *proposed* row before `ON CONFLICT` resolution, so an unclamped
        # negative aborts the statement even when the UPDATE branch is what would
        # have run — the correction path failing exactly when it is needed.
        #
        # Clamping loses nothing: `usage_events` keeps the true signed history,
        # so a double refund is still detectable by replaying the log, which is
        # the reconciliation DDR-14 designs the log for.
        sa.CheckConstraint("used_amount >= 0", name="ck_usage_counters__used_non_negative"),
        sa.CheckConstraint(
            "limit_amount IS NULL OR limit_amount >= 0",
            name="ck_usage_counters__limit_non_negative",
        ),
    )
    # 🔒 The enforcement read: one indexed lookup per metered action (DDR-14).
    op.create_index(
        "ix_usage_counters__tenant_resource",
        "usage_counters",
        ["tenant_id", "resource_code", "period_start"],
    )

    # ─── usage_events (DB §14.5) ──────────────────────────────────────────
    op.create_table(
        "usage_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("resource_code", sa.Text(), nullable=False),
        # 🔒 EC-M10-04 — signed. A negative amount is a compensating event for an
        # action that consumed quota and then failed. Never a counter decrement
        # without a matching event here, or the log stops explaining the counter.
        sa.Column("amount", sa.Numeric(), nullable=False),
        # 🔒 Arch R6 — the emitting module by name, not a FK to its tables. The
        # entitlements module must not reference another module's schema.
        sa.Column("source_module", sa.Text(), nullable=False),
        sa.Column("source_record_id", sa.UUID(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # 🔒 The one mutable column. See _ENFORCE_USAGE_EVENT_IMMUTABILITY.
        sa.Column("is_reconciled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_usage_events"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_usage_events__tenant"),
        # A zero-amount event records nothing and would only dilute the log.
        sa.CheckConstraint("amount <> 0", name="ck_usage_events__amount_non_zero"),
    )
    # The reconciliation read: replay one tenant's resource history for a period.
    op.create_index(
        "ix_usage_events__tenant_resource_time",
        "usage_events",
        ["tenant_id", "resource_code", "occurred_at"],
    )
    # 🔒 Finds unreconciled events without scanning the whole log. Partial, since
    # the reconciled majority is never the target of this query.
    op.execute(
        """
        CREATE INDEX ix_usage_events__unreconciled
            ON usage_events (tenant_id, resource_code)
            WHERE NOT is_reconciled;
        """
    )

    # ─── Row Level Security ───────────────────────────────────────────────
    #
    # 🔒 Pattern A on the four tenant-scoped tables. `plan_definitions` is
    # omitted deliberately: Pattern D, platform-wide, no `tenant_id` to key a
    # policy on, and tenant writes are already revoked below.
    #
    # ⚠️ Unlike `consent_records`, every table here *is* read on a tenant-facing
    # path with a tenant in scope — the enforcement check runs inside a request.
    # So Pattern A is correct here where it was wrong there, and AC-M0-003
    # covers these four.
    #
    # 🔒 `FORCE ROW LEVEL SECURITY` is not redundant with `ENABLE`. Without
    # FORCE, the table *owner* bypasses every policy. Migrations run as
    # `app_migrator`, which owns these tables, so an unforced table would leave
    # policies inert for exactly the role most likely to be used in a fix-up
    # script.
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

    op.execute(_ENFORCE_USAGE_EVENT_IMMUTABILITY)
    # ⚠️ Last, and after the trigger: once the revokes land, this migration's own
    # connection keeps working only because it runs as the migrator role.
    op.execute(_ENFORCE_APPEND_ONLY)

    # ─── Seed plans (🟡 PROPOSED — PRD M10.4) ─────────────────────────────
    #
    # ⚠️ Seeded in the migration rather than a fixture: DB §23 lists
    # `plan_definitions` as reference data the code depends on, and a tenant
    # cannot have a subscription without a plan to point at. DDR-17 keeps
    # *curated* catalogues (foods) out of migrations; this is neither large nor
    # curated by non-developers.
    #
    # ⚠️ 🔒 Prices need market validation (OD-04) and quotas need cost
    # verification (ASM-08). `ON CONFLICT DO NOTHING` makes the seed idempotent
    # per DB §23, so re-running cannot duplicate a tier.
    #
    # `storage_mb` rather than GB, matching the resource code — one unit, chosen
    # once, so no call site has to convert.
    op.execute(
        """
        INSERT INTO plan_definitions
            (code, name, price_amount, currency_code, billing_period,
             limits, features, is_public, sort_order, effective_from)
        VALUES
            ('free', 'Free', 0, 'INR', 'monthly',
             '{"active_clients": 3, "ai_generations_per_month": 5,
               "whatsapp_messages_per_month": 50, "storage_mb": 100,
               "practitioner_seats": 1}'::jsonb,
             '{}'::jsonb, true, 0, now()),
            ('starter', 'Starter', 799, 'INR', 'monthly',
             '{"active_clients": 30, "ai_generations_per_month": 40,
               "whatsapp_messages_per_month": 600, "storage_mb": 2048,
               "practitioner_seats": 1}'::jsonb,
             '{}'::jsonb, true, 1, now()),
            ('growth', 'Growth', 1799, 'INR', 'monthly',
             '{"active_clients": 100, "ai_generations_per_month": 150,
               "whatsapp_messages_per_month": 2000, "storage_mb": 10240,
               "practitioner_seats": 1}'::jsonb,
             '{}'::jsonb, true, 2, now()),
            ('clinic', 'Clinic', 3499, 'INR', 'monthly',
             '{"active_clients": 300, "ai_generations_per_month": 400,
               "whatsapp_messages_per_month": 6000, "storage_mb": 30720,
               "practitioner_seats": 3}'::jsonb,
             '{}'::jsonb, true, 3, now())
        ON CONFLICT (code, effective_from) DO NOTHING;
        """
    )


def downgrade() -> None:
    """Drop everything this revision created, in dependency order.

    ⚠️ 🔒 **Destroys the usage record.** Reversibility exists so the migration
    chain is honestly testable in development. Running this against a database
    with live subscriptions would delete what each tenant is entitled to and what
    they have consumed, and is not a supported operation.
    """
    op.execute(_RESTORE_DEFAULT_PRIVILEGES)
    op.execute("DROP TRIGGER IF EXISTS trg_usage_events__immutable ON usage_events")
    op.execute("DROP FUNCTION IF EXISTS usage_events__reject_material_edit()")

    op.drop_table("usage_events")
    op.drop_table("usage_counters")
    op.drop_table("subscription_events")
    op.drop_table("subscriptions")
    op.drop_table("plan_definitions")

    for name in _ENUMS:
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=False)
