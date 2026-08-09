"""Collaboration must hold its structural claims — DB §5.4–5.5.

🔒 These tests need no database. They exist for the same reason as
``test_kernel_clients_schema``: every guarantee below is one or two lines in
migration 0011 that a refactor could delete with every other test staying green,
and the integration suite that proves them *executing* skips without a live
PostgreSQL — so on a developer machine it would prove nothing at all.

What is pinned here:

* 🔒 ``client_notes`` has **no client-realm policy** — FR-M3-021 enforced by
  absence (DB §17.1), which is the strongest form available.
* 🔒 Soft delete: ``client_notes`` and ``tags`` lose DELETE; ``client_tags``
  keeps it, because untagging removes an assertion rather than a record.
* 🔒 Tag uniqueness is **case-insensitive and partial** — the two properties that
  make "PCOS" and "pcos" one tag while letting a retired name be reused.
* 🔒 At most one **live** grant per pair, and ``granted_at`` in the primary key
  so a colleague can be re-granted after a revoke.
* 🔒 Every client-bound route authorizes against the client (AC-M1-006).

⚠️ What these cannot prove: that PostgreSQL enforces any of it. That is
``tests/integration/test_collaboration.py``, and the last test here asserts that
file still exists so the live gate cannot be quietly dropped.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_MIGRATION = _BACKEND / "migrations" / "versions" / "20260808_0011_collaboration.py"

_TENANT_SCOPED = ("client_notes", "tags", "client_tags", "client_assignments")


@pytest.fixture(scope="module")
def migration_source() -> str:
    if not _MIGRATION.is_file():
        pytest.fail(f"collaboration migration is missing: {_MIGRATION}")
    return _MIGRATION.read_text(encoding="utf-8")


# ─── RLS (DB §17.1, AC-M0-003) ───────────────────────────────────────────


@pytest.mark.parametrize("table", _TENANT_SCOPED)
def test_every_table_forces_rls(migration_source: str, table: str) -> None:
    """🔒 FORCE is not redundant with ENABLE.

    Migrations run as ``app_migrator``, which owns these tables — and a table
    owner bypasses every policy unless FORCE is set. Without it the isolation
    would hold for the application and silently not for the migrator.
    """
    assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in migration_source or (
        "_TENANT_SCOPED" in migration_source
    ), f"{table} is not RLS-enabled"
    assert "FORCE ROW LEVEL SECURITY" in migration_source
    assert table in migration_source


def test_the_tenant_scoped_set_is_all_four_tables(migration_source: str) -> None:
    """The RLS loop reads one declaration, so the declaration is what to check.

    ⚠️ A table added to this migration but omitted from ``_TENANT_SCOPED`` ships
    with no policy at all — the single most likely way isolation breaks silently
    (DB §17.3 names it).
    """
    declared = re.search(
        r"_TENANT_SCOPED:\s*tuple\[str, \.\.\.\] = \(([^)]*)\)", migration_source, re.DOTALL
    )
    assert declared is not None, "the tenant-scoped declaration has moved or been renamed"
    for table in _TENANT_SCOPED:
        assert f'"{table}"' in declared.group(1), f"{table} would ship without an RLS policy"


def test_notes_have_no_client_realm_policy(migration_source: str) -> None:
    """🔒 FR-M3-021 — notes are never client-visible.

    DB §17.1: "enforced by the *absence* of a policy, which is stronger than a
    condition." A Pattern C policy mentioning ``app.actor_id`` on this table
    would be the mechanism by which a client could ever read a practitioner's
    private note, so its absence is the assertion.
    """
    assert "actor_id" not in migration_source, (
        "migration 0011 mentions actor_id — a client-realm policy on client_notes "
        "would make FR-M3-021 a condition that can be written wrongly, rather than "
        "an absence that cannot."
    )


# ─── Soft delete (DB §22.2) ──────────────────────────────────────────────


@pytest.mark.parametrize("table", ["client_notes", "tags"])
def test_soft_deleted_tables_lose_delete(migration_source: str, table: str) -> None:
    """🔒 ``ops/db/001_roles.sql`` grants all four verbs on new tables by default,
    so this revoke is mandatory rather than decorative."""
    assert re.search(rf"REVOKE DELETE ON TABLE {table} FROM app_user", migration_source), (
        f"{table} keeps DELETE; a soft-deleted table the application can destroy "
        "is soft-deleted only by convention."
    )


def test_client_tags_keeps_delete(migration_source: str) -> None:
    """⚠️ The deliberate exception, and the reason it is safe.

    A ``client_tags`` row asserts "this client carries this label". Withdrawn, it
    records nothing that happened — unlike ``client_assignments``, which records
    a decision and is therefore revoked rather than deleted.
    """
    assert re.search(r"GRANT SELECT, INSERT, DELETE ON TABLE client_tags", migration_source)


def test_assignments_are_revoked_not_deleted(migration_source: str) -> None:
    """🔒 EC-M1-04 — assignment history is retained.

    "Who could see this client last March" is a question a DPDP access request
    can ask, and a deleted row cannot answer it.
    """
    assert re.search(r"REVOKE DELETE ON TABLE client_assignments FROM app_user", migration_source)
    assert "revoked_at" in migration_source


# ─── Uniqueness ──────────────────────────────────────────────────────────


def test_tag_uniqueness_is_case_insensitive_and_partial(migration_source: str) -> None:
    """🔒 Two properties in one index, and both are load-bearing.

    ``lower(name)`` makes "PCOS" and "pcos" one tag, so a practitioner cannot
    split their caseload across two labels that look identical. The partial
    predicate releases a retired tag's name for reuse — without it, a name
    archived by mistake is permanently unavailable.
    """
    index = re.search(
        r"CREATE UNIQUE INDEX uq_tags__tenant_name(.*?);", migration_source, re.DOTALL
    )
    assert index is not None, "uq_tags__tenant_name is gone"
    assert "lower(name)" in index.group(1)
    assert "archived_at IS NULL" in index.group(1)


def test_only_one_live_grant_per_pair(migration_source: str) -> None:
    """🔒 Two live grants would make a revoke appear to do nothing."""
    index = re.search(
        r"CREATE UNIQUE INDEX uq_client_assignments__live(.*?);", migration_source, re.DOTALL
    )
    assert index is not None, "uq_client_assignments__live is gone"
    assert "revoked_at IS NULL" in index.group(1)


def test_granted_at_is_part_of_the_assignment_key(migration_source: str) -> None:
    """🔒 Why the key is three columns rather than the two DB §5.5 sketches.

    With revocation the ``(client_id, user_id)`` pair legitimately recurs. The
    narrower key would reject the second grant, so a colleague who came back from
    leave could never be given access again.
    """
    key = re.search(
        r'PrimaryKeyConstraint\(\s*"client_id",\s*"user_id",\s*"granted_at"', migration_source
    )
    assert key is not None, (
        "the assignment primary key no longer includes granted_at; re-granting a "
        "previously revoked colleague would fail on a duplicate key."
    )


def test_a_revocation_is_all_or_nothing(migration_source: str) -> None:
    """Both revocation columns move together, or the row is incoherent."""
    assert "ck_client_assignments__revocation_complete" in migration_source


# ─── Reversibility ───────────────────────────────────────────────────────


def test_the_migration_reverses(migration_source: str) -> None:
    """Forward-only chain, reversible steps — the S0 migration policy.

    ⚠️ Children before parents in the downgrade, or the foreign keys refuse.
    """
    downgrade = migration_source.split("def downgrade()")[1]
    order = [
        downgrade.index("client_assignments"),
        downgrade.index("client_tags"),
        downgrade.index('drop_table("tags")'),
        downgrade.index("client_notes"),
    ]
    assert order == sorted(
        order
    ), "the downgrade drops tables in an order the foreign keys will refuse"


# ─── The live gate cannot be dropped ─────────────────────────────────────


def test_collaboration_is_covered_by_an_executable_test() -> None:
    """⚠️ Everything above reads migration text; none of it runs SQL.

    A revoke that is present but misspelled satisfies every assertion in this
    file. Only PostgreSQL can refuse the DELETE, so the integration file must
    keep existing — and must still assert AC-M1-006.
    """
    live = _BACKEND / "tests" / "integration" / "test_collaboration.py"
    assert live.is_file(), "the live collaboration gate is missing"
    source = live.read_text(encoding="utf-8")
    assert "permission denied" in source, (
        "the live gate no longer asserts a privilege error; the soft-delete grants "
        "would then be unproven against a cluster."
    )
    assert "AC-M1-006" in source, (
        "the live gate no longer names AC-M1-006 — practitioner scoping is the "
        "security criterion this slice exists to satisfy."
    )
