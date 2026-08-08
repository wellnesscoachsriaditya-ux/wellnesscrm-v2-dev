"""The files schema must hold its structural claims — S1 Slice F.

🔒 These tests need no database. What they protect is a set of *claims* made in
migration 0008 and in the ORM model, each of which is a line someone could delete
during a refactor without any other test going red:

* 🔒 ``files`` has RLS **enabled and forced**, with a policy constraining writes
  as well as reads — a file index that leaked across tenants would hand out
  storage keys, and the object store has no policy of its own;
* 🔒 DELETE is revoked (Arch §13.2) — the row is the evidence that an object
  existed, and DPDP erasure has to be able to show the bytes were destroyed;
* 🔒 ``storage_key`` is globally unique, so two rows cannot point at one object;
* the partial index the live quota sum depends on exists (FR-M0-040);
* the ORM model and the hand-written migration describe the same table.

⚠️ What they cannot prove: that the SQL executes, that RLS actually filters, or
that the DELETE revoke bites on a live cluster. That is the isolation gate —
``tests/integration/test_storage.py`` — which needs a real PostgreSQL.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.kernel import Base
from app.kernel.models import FileClass

_BACKEND = Path(__file__).resolve().parents[1]
_MIGRATION = _BACKEND / "migrations" / "versions" / "20260808_0008_files.py"


@pytest.fixture(scope="module")
def migration_source() -> str:
    if not _MIGRATION.is_file():
        pytest.fail(f"files migration is missing: {_MIGRATION}")
    return _MIGRATION.read_text(encoding="utf-8")


# ─── Row Level Security ──────────────────────────────────────────────────


def _rls_loop_tables(source: str) -> set[str]:
    """The table names the migration's RLS loop actually iterates.

    🔒 Read from ``_TENANT_SCOPED`` in the migration's own AST rather than from a
    shared constant, matching ``test_kernel_schema.py`` and
    ``test_kernel_entitlements_schema.py``. The statements are f-strings, so the
    literal table name never appears in the file and a substring search would
    find nothing.
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
    pytest.fail("the migration does not declare _TENANT_SCOPED")


def _rls_loop_body(source: str) -> str:
    """The SQL emitted inside the ``for table in _TENANT_SCOPED`` loop."""
    match = re.search(
        r"for table in _TENANT_SCOPED:(.*?)(?=\n\n    #|\n\n    op\.execute\()",
        source,
        re.DOTALL,
    )
    assert match is not None, "could not locate the RLS loop in the migration"
    return match.group(1)


def test_files_is_covered_by_the_rls_loop(migration_source: str) -> None:
    """🔒 ``files`` carries ``tenant_id``, so it must be in the loop that policies it.

    A table dropped from that tuple ships with no isolation at all and the
    migration still runs cleanly, so nothing else would notice.
    """
    assert _rls_loop_tables(migration_source) == {"files"}


def test_the_rls_loop_enables_and_forces(migration_source: str) -> None:
    """🔒 ENABLE alone is not enough.

    ``app_migrator`` owns the table and an owner bypasses policies without FORCE,
    so the policy would look present in ``pg_policies`` while filtering nothing.
    That is the V1 failure ADR-02 exists to prevent.
    """
    body = _rls_loop_body(migration_source)
    assert "ENABLE ROW LEVEL SECURITY" in body
    assert "FORCE ROW LEVEL SECURITY" in body


def test_the_policy_constrains_writes_as_well_as_reads(migration_source: str) -> None:
    """🔒 ``USING`` filters reads; ``WITH CHECK`` is what stops a tenant *writing*
    a row carrying another tenant's id — the direction a read-only policy misses.
    """
    body = _rls_loop_body(migration_source)
    assert "__tenant_isolation ON" in body
    assert "USING (tenant_id = current_tenant_id())" in body
    assert "WITH CHECK (tenant_id = current_tenant_id())" in body


# ─── The row survives the object (Arch §13.2) ────────────────────────────


def test_delete_is_revoked(migration_source: str) -> None:
    """🔒 The row is the evidence that an object existed.

    FR-M0-027 erasure has to be able to show the bytes were destroyed, and a
    deleted row proves nothing. Deletion is therefore ``deleted_at``, and the
    privilege to do otherwise must not exist.
    """
    assert re.search(
        r"REVOKE\s+DELETE\s+ON\s+TABLE\s+files\s+FROM\s+app_user",
        migration_source,
        re.IGNORECASE,
    )


def test_update_is_retained(migration_source: str) -> None:
    """⚠️ Soft delete *is* an UPDATE, so revoking it would break deletion."""
    assert not re.search(
        r"REVOKE\s+[^;]*UPDATE[^;]*ON\s+TABLE\s+files",
        migration_source,
        re.IGNORECASE,
    ), "files has lost UPDATE, which soft delete and confirmation both need"


# ─── Uniqueness and indexes ──────────────────────────────────────────────


def test_storage_key_is_globally_unique(migration_source: str) -> None:
    """🔒 Two rows pointing at one object means deleting either orphans the other.

    Global rather than per-tenant: the key already embeds the tenant, and a
    per-tenant constraint would permit the same key under two tenants — which is
    the cross-tenant collision the uniqueness is for.
    """
    assert "uq_files__storage_key" in migration_source
    assert re.search(
        r"UniqueConstraint\(\s*[\"']storage_key[\"']", migration_source
    ), "storage_key is not globally unique"


def test_the_live_quota_index_exists(migration_source: str) -> None:
    """🔒 FR-M0-040 — the quota sum runs on the upload path.

    A partial index over exactly the rows the sum admits keeps it an index-only
    scan of one tenant's live files. Without it, every upload scans the table.

    ⚠️ The predicate must match ``platform.storage.current_usage_bytes`` exactly.
    A partial index whose WHERE clause is narrower than the query's is simply not
    used, and the regression is a slow upload path rather than a wrong answer —
    which is why it would go unnoticed.
    """
    assert "ix_files__tenant_live" in migration_source
    assert re.search(
        r"WHERE\s+status\s*=\s*'confirmed'\s+AND\s+deleted_at\s+IS\s+NULL",
        migration_source,
        re.IGNORECASE,
    ), "the live-quota index is not partial on (confirmed, not deleted)"


def test_the_orphan_reaper_index_exists(migration_source: str) -> None:
    """The reaper scans for pending rows past the window (ADR-12)."""
    assert "ix_files__status_created" in migration_source


def test_the_erasure_traversal_index_exists(migration_source: str) -> None:
    """🔒 Arch §13.2 — erasure must find every clinical object for a tenant."""
    assert "ix_files__tenant_clinical" in migration_source


# ─── Constraints ─────────────────────────────────────────────────────────


def test_size_must_be_positive(migration_source: str) -> None:
    """🔒 A zero-byte row is a failed upload recorded as a success, and it would
    make the quota sum disagree with what is actually stored."""
    assert re.search(r"size_bytes\s*>\s*0", migration_source)


def test_lifecycle_status_and_timestamps_cannot_disagree(migration_source: str) -> None:
    """🔒 Status and timestamp are two representations of one fact.

    If they can disagree, the quota sum and the retrievability check disagree
    too — one reads ``status``, the other reads ``deleted_at``. One combined
    constraint covers both directions, matching the ``data_requests`` pattern
    from 0006.
    """
    assert "ck_files__lifecycle_timestamped" in migration_source
    # Both halves of the rule, not just the one a happy-path row exercises.
    assert re.search(r"status\s*=\s*'confirmed'", migration_source)
    assert re.search(r"status\s*=\s*'deleted'", migration_source)
    assert re.search(r"confirmed_at\s+IS\s+NOT\s+NULL", migration_source, re.IGNORECASE)
    assert re.search(r"deleted_at\s+IS\s+NOT\s+NULL", migration_source, re.IGNORECASE)


# ─── The enums ───────────────────────────────────────────────────────────


def _enum_values(source: str, name: str) -> list[str]:
    """The declared values of one enum in the migration's ``_ENUMS`` literal.

    🔒 Read from the migration's own AST rather than from the importable module,
    so this asserts on what the file says rather than on what a shared constant
    happens to hold.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign | ast.Assign):
            continue
        targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        if not any(isinstance(t, ast.Name) and t.id == "_ENUMS" for t in targets):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for key, value in zip(node.value.keys, node.value.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == name:
                return [
                    element.value
                    for element in getattr(value, "elts", [])
                    if isinstance(element, ast.Constant)
                ]
    pytest.fail(f"the migration does not declare an enum named {name}")


def test_file_status_has_a_quarantined_state(migration_source: str) -> None:
    """🔒 ADR-12 — a mismatch is quarantined, not deleted.

    A deliberate mismatch is a security event, and destroying the object is the
    wrong first move. Without this state the only options are "confirm it" or
    "lose the evidence".
    """
    assert "quarantined" in _enum_values(migration_source, "file_status")


def test_file_status_covers_the_whole_lifecycle(migration_source: str) -> None:
    assert set(_enum_values(migration_source, "file_status")) == {
        "pending",
        "confirmed",
        "quarantined",
        "deleted",
    }


def test_file_class_matches_the_kernel_enum(migration_source: str) -> None:
    """A class the database rejects is a 500 at upload time."""
    assert set(_enum_values(migration_source, "file_class")) == {c.value for c in FileClass}


# ─── ORM and migration agree ─────────────────────────────────────────────


def test_the_orm_model_matches_the_migration(migration_source: str) -> None:
    """🔒 The migration is authoritative; the model must describe the same table.

    A drift here is invisible until a query references a column the database does
    not have — at runtime, on the upload path.
    """
    table = Base.metadata.tables["files"]
    for column in table.columns:
        assert re.search(
            rf"[\"']{column.name}[\"']", migration_source
        ), f"files.{column.name} is in the ORM model but not in migration 0008"


def test_the_migration_is_reversible(migration_source: str) -> None:
    """Every S1 migration round-trips, so a failed deploy can be rolled back."""
    assert "def downgrade()" in migration_source
    assert "op.drop_table" in migration_source
    # 🔒 The enums are dropped too. A leftover type makes the next `upgrade`
    # fail with "type already exists", which turns a rollback into a dead end.
    assert re.search(
        r"for name in _ENUMS:.*?\.drop\(", migration_source, re.DOTALL
    ), "the downgrade leaves its enum types behind, so re-upgrading would fail"
