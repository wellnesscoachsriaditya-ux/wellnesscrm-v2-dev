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
