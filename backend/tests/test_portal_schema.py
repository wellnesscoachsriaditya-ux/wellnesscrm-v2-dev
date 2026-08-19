"""Claims migration ``0025_portal_client_realm_rls`` makes — DB §17.1 Pattern C.

🔒 These need no database, and that is deliberate. What they protect is a set of
*claims in SQL*: that every table the portal reads carries a client-realm policy,
that the policies are RESTRICTIVE rather than PERMISSIVE, and that the realm gate
is keyed on ``app.actor_role``. Each is one line someone could delete during a
refactor with no visible effect on any practitioner-facing test.

⚠️ What they cannot prove: that the SQL executes, or that PostgreSQL actually
filters. That is ``tests/integration/portal/test_client_realm_rls.py``, which
runs the policies against a live server. Two independent checks on one guarantee
is proportionate — this is the boundary between two clients of one practice.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"
_REVISION = _MIGRATIONS / "20260815_0025_portal_client_realm_rls.py"
_ADHERENCE_SHAPE = _MIGRATIONS / "20260815_0026_adherence_logs_to_spec.py"
_ADHERENCE_POLICY = _MIGRATIONS / "20260815_0027_adherence_logs_policy_roles.py"

#: 🔒 Every table reachable from ``/portal/*``. A table added to the portal's
#: read path without a policy here is a table where two clients of one practice
#: are separated by application code alone.
GUARDED_TABLES: frozenset[str] = frozenset(
    {
        "clients",
        "diet_plans",
        "diet_plan_versions",
        "plan_snapshots",
        "plan_days",
        "plan_slots",
        "plan_items",
        "plan_item_alternatives",
        "measurements",
        "assessment_responses",
        "adherence_logs",
    }
)


@pytest.fixture(scope="module")
def source() -> str:
    if not _REVISION.is_file():
        pytest.fail(f"revision is missing: {_REVISION}")
    return _REVISION.read_text(encoding="utf-8")


def test_every_portal_table_is_named(source: str) -> None:
    """The list in the migration and the list here must agree."""
    for table in GUARDED_TABLES:
        assert f'"{table}"' in source, (
            f"{table} is reachable from the portal but has no Pattern C policy. "
            "Two clients of one practice would be separated by a WHERE clause."
        )


def test_the_policy_is_restrictive(source: str) -> None:
    """🔒 PERMISSIVE policies are OR-ed and would *widen* access.

    A Pattern C policy written PERMISSIVE alongside the existing Pattern A one
    would mean "this tenant's rows **or** this client's rows" — which is the
    tenant-wide read it was added to prevent, wearing the right name.
    """
    assert "AS RESTRICTIVE FOR ALL" in source
    assert "AS PERMISSIVE" not in source.split("_POLICY", 1)[1].split("def upgrade", 1)[0]


def test_the_write_half_is_present(source: str) -> None:
    """``USING`` filters reads; ``WITH CHECK`` constrains writes.

    Without it a client could insert a row attributed to another client —
    invisible to themselves afterwards, and present in someone else's record.
    """
    assert "WITH CHECK (portal_client_id() IS NULL OR ({predicate}))" in source


def test_the_realm_gate_reads_the_role(source: str) -> None:
    """🔒 ``app.actor_id`` holds a *user* id on ``/app`` and a *client* id on
    ``/portal``. Comparing ``client_id`` to it unconditionally would hide every
    client from every practitioner, so the role is what distinguishes them."""
    assert "current_setting('app.actor_role', true), '') = 'client'" in source
    assert "current_setting('app.actor_id', true)" in source


def test_the_helper_is_not_marked_immutable(source: str) -> None:
    """It reads a session variable. IMMUTABLE would let the planner fold the
    value into a cached plan, and the policy would then filter on whoever ran
    the query that populated the cache."""
    helper = source.split("CREATE OR REPLACE FUNCTION portal_client_id()", 1)[1]
    assert "STABLE" in helper.split("$$", 1)[0]
    assert "IMMUTABLE" not in helper.split("$$", 1)[0]


def test_the_helper_is_not_executable_by_the_world(source: str) -> None:
    """Consistent with ``identity_lookup_client_by_contact``: revoked from
    ``public``, granted to ``app_user`` only."""
    assert "REVOKE ALL ON FUNCTION portal_client_id() FROM public" in source
    assert "GRANT EXECUTE ON FUNCTION portal_client_id() TO app_user" in source


def test_policies_apply_to_the_application_role_only(source: str) -> None:
    """🔒 ``TO app_user``, matching every other policy in the schema.

    ⚠️ Not cosmetic. ``app_migrator`` owns these tables and seeds fixtures across
    clients; a restrictive policy with no role list would apply to it too, and
    the failure would look like a broken fixture rather than a policy decision.
    """
    assert "TO app_user" in source


def test_the_broken_adherence_policy_is_replaced(source: str) -> None:
    """🔒 Revision ``2eb56b8913d5`` created ``adherence_logs`` with a RESTRICTIVE
    policy reading ``app.current_tenant_id`` — a variable this application never
    sets (``platform.db.TENANT_SETTING`` is ``app.tenant_id``).

    Two defects in one statement: the predicate compared against NULL, and with
    no PERMISSIVE policy on the table RLS denied everything. The table was
    unusable, and would have stayed unusable silently until the first write.
    """
    upgrade = source.split("def upgrade", 1)[1].split("def downgrade", 1)[0]
    assert "DROP POLICY IF EXISTS tenant_isolation ON adherence_logs" in upgrade
    assert "adherence_logs__tenant_isolation" in upgrade
    assert "current_tenant_id()" in upgrade
    assert "ALTER TABLE adherence_logs FORCE ROW LEVEL SECURITY" in upgrade


def test_the_downgrade_restores_what_it_found(source: str) -> None:
    """A downgrade that invented a working policy would not be a downgrade."""
    downgrade = source.split("def downgrade", 1)[1]
    for table in GUARDED_TABLES:
        assert re.search(rf"\b{table}\b", downgrade) or "_ALL_TABLES" in downgrade
    assert "DROP FUNCTION IF EXISTS portal_client_id()" in downgrade


# ─── 0026 — the shape /portal/sync writes ────────────────────────────────


@pytest.fixture(scope="module")
def adherence_shape() -> str:
    if not _ADHERENCE_SHAPE.is_file():
        pytest.fail(f"revision is missing: {_ADHERENCE_SHAPE}")
    return _ADHERENCE_SHAPE.read_text(encoding="utf-8")


def test_the_adherence_columns_api_12_3_sends_all_exist(adherence_shape: str) -> None:
    """🔒 API §12.3's payload, column by column.

    A missing one is not a cosmetic gap: ``/portal/sync`` would have nowhere to
    put the value and would either drop it silently or fail the operation.
    """
    for column in ("logged_for_date", "slot_type", "adherence", "plan_slot_id", "plan_version_id"):
        assert f'"{column}"' in adherence_shape, f"{column} is missing from adherence_logs"


def test_the_guessed_score_column_is_gone(adherence_shape: str) -> None:
    """🔒 A 0–100 score cannot express ``skipped``.

    A skipped meal and a meal not followed are different clinical facts, and any
    scale collapses them. Revision ``2eb56b8913d5`` invented the column; DB §12.1
    never had it.
    """
    upgrade = adherence_shape.split("def upgrade", 1)[1].split("def downgrade", 1)[0]
    assert 'op.drop_column("adherence_logs", "score")' in upgrade
    assert "ck_adherence_logs__score_range" in upgrade


def test_the_idempotency_boundary_is_not_touched(adherence_shape: str) -> None:
    """🔒 UNIQUE(client_id, idempotency_key) is the replay guarantee.

    ⚠️ Asserted as an *absence*. Widening it to the tenant would let one client's
    sync suppress another's log; narrowing it would let a replayed queue
    duplicate. The safest change to a working uniqueness boundary is none.
    """
    assert "drop_constraint" not in adherence_shape.replace(
        'op.drop_constraint("ck_adherence_logs__score_range", "adherence_logs", type_="check")', ""
    )
    assert "uq_adherence_logs__idempotency" not in adherence_shape.split("def upgrade", 1)[1]


def test_slot_type_stays_text(adherence_shape: str) -> None:
    """🔒 ``kernel.nutrition.MealSlotType`` binds this: the vocabulary is 🟡
    PROPOSED (FR-M4-025) and the database type is created by the slice that also
    needs it for ``foods.meal_suitability``. Creating it here would commit a
    vocabulary Validation Gate G1 has not settled."""
    upgrade = adherence_shape.split("def upgrade", 1)[1].split("def downgrade", 1)[0]
    assert "meal_slot_type" not in upgrade
    assert '"slot_type"' in upgrade and "sa.Text()" in upgrade


# ─── 0027 — the policy role scope ────────────────────────────────────────


@pytest.fixture(scope="module")
def adherence_policy() -> str:
    if not _ADHERENCE_POLICY.is_file():
        pytest.fail(f"revision is missing: {_ADHERENCE_POLICY}")
    return _ADHERENCE_POLICY.read_text(encoding="utf-8")


def test_the_tenant_policy_applies_to_every_role(adherence_policy: str) -> None:
    """🔒 The defect ``0025`` introduced and this revision fixes.

    ``FORCE ROW LEVEL SECURITY`` binds the table's owner, and a role with no
    permissive policy sees nothing. Scoping the Pattern A policy ``TO app_user``
    therefore locked ``app_migrator`` out of a table it maintains — silently,
    because "no rows" is indistinguishable from "empty table".

    Every other Pattern A policy in the schema omits the ``TO`` clause. This one
    must too.
    """
    assert '_POLICY = "adherence_logs__tenant_isolation"' in adherence_policy

    upgrade = adherence_policy.split("def upgrade", 1)[1].split("def downgrade", 1)[0]
    assert "CREATE POLICY {_POLICY} ON adherence_logs" in upgrade
    assert "current_tenant_id()" in upgrade
    # 🔒 The whole point of the revision: no role list on the permissive policy.
    assert "TO app_user" not in upgrade


def test_the_client_policy_keeps_its_role_scope(adherence_policy: str) -> None:
    """🔒 The asymmetry is deliberate and stated.

    A *restrictive* client policy applying to the owner would confine
    ``app_migrator`` to one client, and its work — data migration, DPDP erasure,
    fixture teardown — spans every client in a tenant. Only ``0027``'s permissive
    policy is widened; the Pattern C policy from ``0025`` is not mentioned.
    """
    assert "adherence_logs__client_realm" not in adherence_policy.split("def upgrade", 1)[1]
