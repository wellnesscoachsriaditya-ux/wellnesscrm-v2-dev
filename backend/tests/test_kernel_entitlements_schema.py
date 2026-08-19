"""The entitlements schema must hold its structural claims — S1 Slice E.

🔒 These tests need no database. What they protect is a set of *claims* made in
migration 0007 and in the ORM models, each of which is a line someone could
delete during a refactor without any other test going red:

* 🔒 ``subscription_events`` is **append-only by grant** (DDR-15) — UPDATE and
  DELETE revoked, and the table registered in ``ops/db/002_verify_grants.sql``;
* 🔒 ``usage_events`` keeps UPDATE for ``is_reconciled`` alone, with every other
  column frozen by a trigger (EC-M10-04) — the one place a grant cannot express
  the rule, so the protection lives somewhere a reader might not look;
* 🔒 the four tenant-scoped tables have RLS **enabled and forced**, with a policy
  constraining writes as well as reads;
* 🔒 ``subscriptions`` and ``plan_definitions`` are read-only to the application
  (FR-M10-008, FR-M10-001) — a tenant that can write either can upgrade itself;
* the seeded plans match the tiers in PRD M10.4;
* the ORM models and the hand-written migration describe the same tables.

⚠️ What they cannot prove: that the SQL executes, that RLS actually filters, or
that the revokes bite on a live cluster. That is the isolation gate —
``tests/integration/test_entitlements.py`` — which needs a real PostgreSQL.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from app.kernel import Base
from app.kernel.entitlements import ResourceCode

_BACKEND = Path(__file__).resolve().parents[1]
_MIGRATION = _BACKEND / "migrations" / "versions" / "20260808_0007_entitlements.py"

#: 🔒 Strictly append-only: no UPDATE, no DELETE.
_APPEND_ONLY_TABLE = "subscription_events"

#: 🔒 Append-only *except* `is_reconciled` — protected by trigger, not by grant.
_TRIGGER_PROTECTED_TABLE = "usage_events"

#: Pattern A — every one of these is read on a tenant-facing path.
_TENANT_SCOPED = ("subscriptions", "subscription_events", "usage_counters", "usage_events")

#: Pattern D — platform-wide catalogue, no `tenant_id` to key a policy on.
_PLATFORM_TABLES = ("plan_definitions",)

#: The tables this migration creates.
_ALL_TABLES = (*_PLATFORM_TABLES, *_TENANT_SCOPED)


@pytest.fixture(scope="module")
def migration_source() -> str:
    if not _MIGRATION.is_file():
        pytest.fail(f"entitlements migration is missing: {_MIGRATION}")
    return _MIGRATION.read_text(encoding="utf-8")


# ─── Append-only by grant (DDR-15) ───────────────────────────────────────


def test_subscription_history_revokes_update_and_delete(migration_source: str) -> None:
    """🔒 The privilege to revise a subscription's history must not exist.

    ⚠️ Default privileges in ``ops/db/001_roles.sql`` grant all four verbs on
    every new table, so this revoke is mandatory rather than decorative.
    EC-M10-01 and EC-M10-05 both turn on *when* state changed; a history the
    application can rewrite cannot answer either.
    """
    assert re.search(
        rf"REVOKE\s+UPDATE,\s*DELETE\s+ON\s+TABLE\s+{_APPEND_ONLY_TABLE}\s+FROM\s+app_user",
        migration_source,
        re.IGNORECASE,
    ), f"{_APPEND_ONLY_TABLE} does not revoke UPDATE and DELETE from app_user."


def test_subscription_history_grants_only_insert_and_select(migration_source: str) -> None:
    assert re.search(
        rf"GRANT\s+INSERT,\s*SELECT\s+ON\s+TABLE\s+{_APPEND_ONLY_TABLE}\s+TO\s+app_user",
        migration_source,
        re.IGNORECASE,
    )


def test_append_only_sequences_are_usable(migration_source: str) -> None:
    """A revoke that also blocked inserts would be caught here, not in production."""
    for table in (_APPEND_ONLY_TABLE, _TRIGGER_PROTECTED_TABLE):
        assert re.search(
            rf"GRANT\s+USAGE,\s*SELECT\s+ON\s+SEQUENCE\s+{table}_id_seq\s+TO\s+app_user",
            migration_source,
            re.IGNORECASE,
        ), f"{table} inserts would fail: its sequence is not granted"


def test_usage_events_lose_delete_but_keep_update(migration_source: str) -> None:
    """🔒 EC-M10-04 — the reconciliation pass needs UPDATE; nothing needs DELETE.

    Deleting a usage event would destroy the record the counter is rebuilt from.
    """
    assert re.search(
        rf"REVOKE\s+DELETE\s+ON\s+TABLE\s+{_TRIGGER_PROTECTED_TABLE}\s+FROM\s+app_user",
        migration_source,
        re.IGNORECASE,
    )
    assert not re.search(
        rf"REVOKE\s+[^;]*UPDATE[^;]*ON\s+TABLE\s+{_TRIGGER_PROTECTED_TABLE}",
        migration_source,
        re.IGNORECASE,
    ), "usage_events has lost UPDATE, which the reconciliation pass needs to " "set is_reconciled."


def test_usage_event_columns_are_immutable_by_trigger(migration_source: str) -> None:
    """🔒 The rule a grant cannot express — DB §14.5, EC-M10-04.

    ``usage_events`` keeps UPDATE for ``is_reconciled``, so column-level
    immutability has to be a trigger. Without it, ``UPDATE usage_events SET
    amount = ...`` silently rewrites the log a drifted counter is recovered from,
    and nothing appears broken until the two disagree.
    """
    assert "CREATE TRIGGER trg_usage_events__immutable" in migration_source
    # Every column that constitutes the recorded consumption. `is_reconciled` is
    # deliberately absent — setting it is the one permitted UPDATE.
    for column in ("tenant_id", "resource_code", "amount", "source_module", "occurred_at"):
        assert re.search(
            rf"NEW\.{column}\s+IS DISTINCT FROM\s+OLD\.{column}", migration_source
        ), f"the immutability trigger does not guard `{column}`"
    assert not re.search(
        r"NEW\.is_reconciled\s+IS DISTINCT FROM\s+OLD\.is_reconciled", migration_source
    ), "the trigger guards is_reconciled, which is the one column that must stay writable"


# ─── Read-only commercial state ──────────────────────────────────────────


def test_plans_and_subscriptions_are_read_only_to_the_application(
    migration_source: str,
) -> None:
    """🔒 FR-M10-001 / FR-M10-008 — a tenant that can write either upgrades itself.

    Activation is a manual operator action at MVP, and plans are configuration we
    own. Both are therefore SELECT-only for ``app_user``.
    """
    for table in ("plan_definitions", "subscriptions"):
        assert re.search(
            rf"REVOKE\s+INSERT,\s*UPDATE,\s*DELETE\s+ON\s+TABLE\s+{table}\s+FROM\s+app_user",
            migration_source,
            re.IGNORECASE,
        ), f"{table} is writable by app_user"
        assert re.search(
            rf"GRANT\s+SELECT\s+ON\s+TABLE\s+{table}\s+TO\s+app_user",
            migration_source,
            re.IGNORECASE,
        ), f"{table} is not readable by app_user, which breaks enforcement"


# ─── Row Level Security ──────────────────────────────────────────────────


def _rls_loop_tables(source: str) -> set[str]:
    """The table names the migration's RLS loop actually iterates.

    🔒 Read from ``_TENANT_SCOPED`` in the migration's own AST rather than from
    the importable module, so this asserts on what the file says rather than on
    what a shared constant happens to hold. If someone drops a table from that
    tuple, the table ships without isolation and this is what notices.

    ⚠️ The same approach as ``test_kernel_schema.py``, and for the same reason:
    the statements are f-strings, so the literal table names never appear in the
    migration source and a plain substring search would find nothing.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign | ast.Assign):
            continue
        targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        names = {t.id for t in targets if isinstance(t, ast.Name)}
        if "_TENANT_SCOPED" not in names or node.value is None:
            continue
        return {
            element.value
            for element in getattr(node.value, "elts", [])
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        }
    pytest.fail("migration does not declare _TENANT_SCOPED")


def _rls_loop_body(source: str) -> str:
    """The SQL emitted inside the ``for table in _TENANT_SCOPED`` loop.

    Coverage is decided by the loop's tuple; correctness of the statements it
    emits is decided by this body.
    """
    match = re.search(
        r"for table in _TENANT_SCOPED:(.*?)(?=\n\n    op\.execute\()",
        source,
        re.DOTALL,
    )
    assert match is not None, "could not locate the RLS loop in the migration"
    return match.group(1)


def test_every_tenant_scoped_table_is_covered_by_the_rls_loop(
    migration_source: str,
) -> None:
    """🔒 Every table carrying ``tenant_id`` must be in the loop that policies it.

    A table dropped from that tuple ships with no isolation whatsoever, and the
    migration still runs cleanly — so nothing else in the suite would notice.
    """
    assert _rls_loop_tables(migration_source) == set(_TENANT_SCOPED)


def test_the_rls_loop_enables_and_forces(migration_source: str) -> None:
    """🔒 ENABLE alone is not enough.

    ``app_migrator`` owns these tables and an owner bypasses policies without
    FORCE — so migrations would see everything while the policy looked present in
    ``pg_policies``. That is the V1 failure ADR-02 exists to prevent.
    """
    body = _rls_loop_body(migration_source)
    assert "ENABLE ROW LEVEL SECURITY" in body
    assert "FORCE ROW LEVEL SECURITY" in body, (
        "RLS is enabled but not forced; app_migrator owns these tables and "
        "would bypass every policy"
    )


def test_policies_constrain_writes_as_well_as_reads(migration_source: str) -> None:
    """🔒 ``USING`` filters reads; only ``WITH CHECK`` stops a write carrying
    another tenant's id. A policy with just ``USING`` reads as isolated and
    accepts a cross-tenant insert."""
    body = _rls_loop_body(migration_source)
    assert "__tenant_isolation ON" in body
    assert "USING (tenant_id = current_tenant_id())" in body
    assert (
        "WITH CHECK (tenant_id = current_tenant_id())" in body
    ), "the policy has no WITH CHECK; a cross-tenant write would succeed"


def test_the_platform_catalogue_has_no_policy(migration_source: str) -> None:
    """⚠️ ``plan_definitions`` has no ``tenant_id``, so a Pattern A policy would
    match nothing and every tenant would read no plan at all — which the
    enforcement path would then treat as indeterminate and deny."""
    for table in _PLATFORM_TABLES:
        assert f"CREATE POLICY {table}__tenant_isolation" not in migration_source


# ─── Seed data (PRD M10.4) ───────────────────────────────────────────────


def test_all_four_tiers_are_seeded(migration_source: str) -> None:
    """A tenant cannot have a subscription without a plan to point at."""
    for code in ("free", "starter", "growth", "clinic"):
        assert f"'{code}'" in migration_source, f"plan tier {code} is not seeded"


def test_seed_is_idempotent(migration_source: str) -> None:
    """🔒 DB §23 — reference data ships as an *idempotent* migration. Re-running
    must not duplicate a tier, which would make "which plan is in force" ambiguous
    on the enforcement read."""
    assert "ON CONFLICT" in migration_source


def test_every_seeded_plan_carries_every_metered_limit(migration_source: str) -> None:
    """🔒 A limit key missing from a plan is indeterminate at runtime (FR-M0-046),
    so that plan's tenants would be denied every metered action. The failure would
    appear as an outage for one pricing tier."""
    limit_blobs = re.findall(r"\{\"active_clients\".*?\}", migration_source, re.DOTALL)
    assert len(limit_blobs) == 4, f"expected 4 seeded limit objects, found {len(limit_blobs)}"

    for blob in limit_blobs:
        parsed = json.loads(" ".join(blob.split()))
        for resource in ResourceCode:
            assert resource.limit_key in parsed, (
                f"seeded plan is missing the {resource.limit_key!r} limit; "
                "its tenants would be denied that resource entirely"
            )


def test_seeded_limits_increase_up_the_ladder(migration_source: str) -> None:
    """Free ⊂ Starter ⊂ Growth ⊂ Clinic. A tier that gives less than the one below
    it is a pricing error the ladder in ``kernel.entitlements`` would then offer
    as an *upgrade*."""
    limit_blobs = re.findall(r"\{\"active_clients\".*?\}", migration_source, re.DOTALL)
    parsed = [json.loads(" ".join(blob.split())) for blob in limit_blobs]

    for key in ("active_clients", "ai_generations_per_month", "whatsapp_messages_per_month"):
        values = [plan[key] for plan in parsed]
        assert values == sorted(values), f"{key} does not increase monotonically: {values}"


# ─── ORM / migration parity ──────────────────────────────────────────────


def test_every_table_exists_in_both_the_orm_and_the_migration(
    migration_source: str,
) -> None:
    """🔒 The models and the hand-written migration must describe the same schema.

    They are maintained separately — there is no autogenerate here — so a column
    added to one and not the other is a runtime error on the first query.
    """
    for table in _ALL_TABLES:
        assert table in Base.metadata.tables, f"{table} has no ORM model"
        assert (
            f'op.create_table(\n        "{table}"' in migration_source
        ), f"{table} is not created by the migration"


def test_orm_columns_match_the_migration(migration_source: str) -> None:
    """Column-level parity, which the table-level check above cannot see."""
    for table in _ALL_TABLES:
        for column in Base.metadata.tables[table].columns:
            assert (
                f'"{column.name}"' in migration_source
            ), f"{table}.{column.name} exists in the ORM but not in the migration"


def test_usage_counter_uniqueness_is_what_makes_the_upsert_safe() -> None:
    """🔒 The increment is ``ON CONFLICT DO UPDATE`` against this constraint.

    Without it, two concurrent metered actions in one period both insert and one
    increment is silently lost — the tenant gets free quota and the counter
    disagrees with its own event log.
    """
    constraints = {
        constraint.name for constraint in Base.metadata.tables["usage_counters"].constraints
    }
    assert "uq_usage_counters__tenant_resource_period" in constraints


def test_one_subscription_per_tenant() -> None:
    """DB §14.2 — otherwise "which plan is this tenant on" has two answers."""
    constraints = {
        constraint.name for constraint in Base.metadata.tables["subscriptions"].constraints
    }
    assert "uq_subscriptions__tenant" in constraints


def test_no_money_column_is_floating_point() -> None:
    """🔒 Binary floating point produces totals that do not reconcile, and
    ``price_amount`` feeds GST invoices (FR-M10-011)."""
    price = Base.metadata.tables["plan_definitions"].columns["price_amount"]
    assert "NUMERIC" in str(price.type).upper()
