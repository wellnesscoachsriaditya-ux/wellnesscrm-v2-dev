"""The client spine must hold its structural claims — DB §5.1–5.3.

🔒 These tests need no database. They exist for the same reason as
``test_kernel_consent_schema``: every guarantee below is one or two lines in
migration 0009 that a refactor could delete with every other test staying green,
and the integration suite that proves them *executing* skips without a live
PostgreSQL — so on a developer machine it proves nothing at all.

What is pinned here:

* 🔒 ``client_stage_history`` is **append-only by grant** (FR-M1-015) and
  ``clients`` **loses DELETE** (FR-M1-010, soft delete only).
* 🔒 Both tables carry **forced** Pattern A RLS. FORCE is not redundant with
  ENABLE — migrations run as the table owner, who otherwise bypasses every policy.
* 🔒 ``mobile`` is **not unique** (EC-M1-01) — family members share a handset.
* 🔒 Archived rows are excluded **structurally**, by index predicate (DB §22.2).
* 🔒 ``is_minor`` is **not a column** (FR-M0-028) — see the migration's own note.

⚠️ What these cannot prove: that PostgreSQL enforces any of it. A revoke that is
present but misspelled satisfies every assertion in this file. That is
``tests/integration/test_client_spine.py``, and the last test here asserts the
file exists so the live gate cannot be quietly dropped.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_MIGRATION = _BACKEND / "migrations" / "versions" / "20260808_0009_clients.py"
_VERIFY_GRANTS = _BACKEND.parent / "ops" / "db" / "002_verify_grants.sql"

#: 🔒 FR-M1-015 — the transition log, and the reason it is separate from `audit_log`.
_APPEND_ONLY_TABLE = "client_stage_history"

#: Pattern A on both (DB §5): tenant_id, read on a tenant-facing path.
_TENANT_SCOPED = ("clients", "client_stage_history")


@pytest.fixture(scope="module")
def migration_source() -> str:
    if not _MIGRATION.is_file():
        pytest.fail(f"clients migration is missing: {_MIGRATION}")
    return _MIGRATION.read_text(encoding="utf-8")


# ─── Append-only history (FR-M1-015, DB §5.3) ────────────────────────────


def test_stage_history_revokes_update_and_delete(migration_source: str) -> None:
    """🔒 FR-M1-015 — every transition recorded, and none revised.

    ⚠️ ``ops/db/001_roles.sql`` grants all four verbs on every new table by
    default, so this revoke is mandatory rather than decorative. A rewritable
    history makes both the timeline (FR-M1-018) and the conversion metrics
    (FR-M9-006) unfalsifiable.
    """
    assert re.search(
        rf"REVOKE\s+UPDATE,\s*DELETE\s+ON\s+TABLE\s+{_APPEND_ONLY_TABLE}\s+FROM\s+app_user",
        migration_source,
        re.IGNORECASE,
    ), (
        f"{_APPEND_ONLY_TABLE} does not revoke UPDATE and DELETE from app_user. "
        "Default privileges grant all four verbs, so the history is editable."
    )


def test_stage_history_keeps_insert_and_select(migration_source: str) -> None:
    """The two verbs it does hold — a revoke that also blocked inserts would
    stop every client creation, which is worse than the bug it prevented."""
    assert re.search(
        rf"GRANT\s+INSERT,\s*SELECT\s+ON\s+TABLE\s+{_APPEND_ONLY_TABLE}\s+TO\s+app_user",
        migration_source,
        re.IGNORECASE,
    )


def test_clients_lose_delete_but_keep_update(migration_source: str) -> None:
    """🔒 FR-M1-010 — soft delete only: ``archived_at``, never a DELETE.

    UPDATE must survive, because archiving *is* an update. FR-M1-011 puts hard
    deletion behind the DPDP erasure pathway, which runs as the migrator role —
    so the application never needs the verb.
    """
    assert re.search(
        r"REVOKE\s+DELETE\s+ON\s+TABLE\s+clients\s+FROM\s+app_user",
        migration_source,
        re.IGNORECASE,
    )
    assert re.search(
        r"GRANT\s+SELECT,\s*INSERT,\s*UPDATE\s+ON\s+TABLE\s+clients\s+TO\s+app_user",
        migration_source,
        re.IGNORECASE,
    ), "clients lost UPDATE; archiving a client is an UPDATE, so this breaks FR-M1-010."


def test_stage_history_is_registered_in_the_grant_verifier() -> None:
    """🔒 ``ops/db/002_verify_grants.sql`` requires it, in the same commit.

    ⚠️ That file's own warning: a table that is append-only in the design but
    missing from its list is unverified, and nothing else would notice.
    """
    assert _VERIFY_GRANTS.is_file(), f"missing: {_VERIFY_GRANTS}"
    assert f"'{_APPEND_ONLY_TABLE}'" in _VERIFY_GRANTS.read_text(encoding="utf-8"), (
        f"`{_APPEND_ONLY_TABLE}` is append-only but absent from "
        "ops/db/002_verify_grants.sql, so its revoke is never verified."
    )


def test_grants_are_guarded_on_role_existence(migration_source: str) -> None:
    """⚠️ A REVOKE naming a missing role aborts the whole transaction, and a
    single-role local database is the setup most likely to run this first."""
    assert "FROM pg_roles WHERE rolname = 'app_user'" in migration_source


# ─── Row Level Security (Pattern A, AC-M0-003) ───────────────────────────


@pytest.mark.parametrize("table", _TENANT_SCOPED)
def test_both_tables_are_in_the_rls_protected_set(migration_source: str, table: str) -> None:
    """🔒 Pattern A on both — DB §5.

    ⚠️ The migration applies RLS by looping over ``_TENANT_SCOPED``, so the table
    name never appears literally in an ``ALTER TABLE`` statement. The membership
    of that tuple *is* the protection, which is why it is asserted directly: a
    table dropped from the list loses its policy with no other visible change.
    """
    protected = re.search(
        r"_TENANT_SCOPED:\s*tuple\[str, \.\.\.\]\s*=\s*\((.*?)\)",
        migration_source,
        re.DOTALL,
    )
    assert protected is not None, "the migration no longer declares _TENANT_SCOPED"
    assert f'"{table}"' in protected.group(
        1
    ), f"`{table}` is not in _TENANT_SCOPED, so it gets no RLS policy at all."


def test_the_rls_loop_enables_forces_and_creates_a_policy(migration_source: str) -> None:
    """🔒 All three, for every table in the protected set.

    FORCE is not redundant with ENABLE: without it the table *owner* bypasses
    every policy — and migrations run as ``app_migrator``, which owns these
    tables. The gap only appears the day something else connects as the owner,
    which is the day it matters.
    """
    for statement in (
        "ENABLE ROW LEVEL SECURITY",
        "FORCE ROW LEVEL SECURITY",
        "CREATE POLICY",
    ):
        assert statement in migration_source, f"the RLS loop no longer emits {statement}"


def test_policies_check_both_directions(migration_source: str) -> None:
    """🔒 ``USING`` filters reads; ``WITH CHECK`` refuses writes carrying another
    tenant's id. A policy with only ``USING`` lets a caller *insert* across the
    boundary while being unable to read it back — a silent, one-way leak."""
    assert "USING (tenant_id = current_tenant_id())" in migration_source
    assert "WITH CHECK (tenant_id = current_tenant_id())" in migration_source


def test_practitioner_scoping_is_not_smuggled_into_the_tenant_policy(
    migration_source: str,
) -> None:
    """⚠️ AC-M1-006 is an authorization decision about a *user*, not a tenant.

    ``current_tenant_id()`` is all the policy has. Expressing per-practitioner
    visibility here would either be wrong or require a second session variable —
    it belongs in Slice C, with ``client_assignments``.
    """
    assert "current_user_id()" not in migration_source, (
        "the RLS policy references a user, not a tenant. Practitioner scoping "
        "(AC-M1-006) belongs in Slice C, not in the tenant isolation boundary."
    )


# ─── The modelling decisions that must not drift ─────────────────────────


def test_mobile_is_not_unique(migration_source: str) -> None:
    """🔒 EC-M1-01 — family members sharing a handset is a real and common
    pattern in the launch market.

    Duplicate detection is a *warning* (FR-M1-024), never a constraint. A unique
    index here would reject a mother and daughter on one number — and the
    practitioner would have no way to record the second person at all.
    """
    assert not re.search(
        r"CREATE\s+UNIQUE\s+INDEX[^;]*\bON\s+clients\b[^;]*\bmobile\b",
        migration_source,
        re.IGNORECASE | re.DOTALL,
    ), "a unique index on clients.mobile violates EC-M1-01."


def test_there_is_no_leads_table(migration_source: str) -> None:
    """🔒 M1.3 / DB §5.1 — "the single most important modelling decision".

    A lead is a ``clients`` row at stage ``lead``. A separate table would make
    AC-M1-003 (converting retains identifier and history) a copying exercise
    that can lose data, rather than true by construction.
    """
    assert not re.search(
        r"create_table\(\s*[\"']leads[\"']", migration_source, re.IGNORECASE
    ), "a `leads` table contradicts M1.3 — leads and clients are one entity."


def test_is_minor_is_not_a_stored_column(migration_source: str) -> None:
    """🔒 FR-M0-028 — derived at read time, and DB §5.1 is wrong to specify a
    generated column.

    PostgreSQL rejects it (generation expressions must be IMMUTABLE; age is not),
    and it would be *wrong* even if allowed: a row computed while the client was
    17 keeps saying so the day they turn 18 — the day guardian consent stops
    being required.
    """
    assert not re.search(
        r"sa\.Column\(\s*[\"']is_minor[\"']", migration_source, re.IGNORECASE
    ), "is_minor is stored. It goes stale on a birthday; derive it (FR-M0-028)."


def test_archived_rows_are_excluded_by_index_predicate(migration_source: str) -> None:
    """🔒 DB §22.2 — structural, not by application filter.

    Same reasoning as RLS: a filter can be forgotten in one query out of thirty;
    an index predicate cannot. Every list index must carry it.
    """
    for index in (
        "ix_clients__tenant_stage",
        "ix_clients__tenant_owner_stage",
        "ix_clients__tenant_mobile",
    ):
        block = re.search(
            rf"CREATE INDEX {index}(.*?);", migration_source, re.IGNORECASE | re.DOTALL
        )
        assert block is not None, f"missing index {index}"
        assert "archived_at IS NULL" in block.group(1), (
            f"{index} does not carry `archived_at IS NULL`. A query that forgets "
            "the filter would then see archived clients through this index."
        )


def test_search_vector_is_generated_not_trigger_maintained(migration_source: str) -> None:
    """🔒 NFR-005 / FR-M1-021 — ``GENERATED ALWAYS ... STORED``.

    A trigger is a second place the projection can drift from its source. There
    is no way to write a row that bypasses a generated column.
    """
    assert "GENERATED ALWAYS AS (" in migration_source
    assert "STORED" in migration_source
    assert re.search(r"USING GIN \(search_vector\)", migration_source, re.IGNORECASE)


def test_search_vector_coalesces_every_part(migration_source: str) -> None:
    """⚠️ ``to_tsvector`` of NULL is NULL, and a NULL tsvector matches nothing.

    Without ``coalesce`` a client with no email would silently drop out of search
    entirely — the kind of bug that reads as "search is flaky" for months.
    """
    generated = re.search(r"GENERATED ALWAYS AS \((.*?)\) STORED", migration_source, re.DOTALL)
    assert generated is not None
    for column in ("full_name", "mobile", "email"):
        assert f"coalesce({column}, '')" in generated.group(1), (
            f"`{column}` is not coalesced; one NULL nulls the whole vector and "
            "the client vanishes from search."
        )


def test_contact_and_activation_invariants_are_database_constraints(
    migration_source: str,
) -> None:
    """The rules that must hold however the row arrived.

    ``kernel.clients`` checks the same things early enough to name the field;
    these catch anything reaching the table by another path — a future import
    job, a fix-up script, a migration.
    """
    for constraint in (
        "ck_clients__contact_present",  # FR-M1-004 / EC-M1-08
        "ck_clients__mobile_e164",  # NFR-100
        "ck_clients__stage_owner",  # FR-M1-009
        "ck_clients__active_has_activated_at",  # FR-M8-023 check-in anchor
        "ck_client_stage_history__actual_transition",
    ):
        assert constraint in migration_source, f"missing constraint {constraint}"


def test_history_cascades_from_the_client(migration_source: str) -> None:
    """🔒 Unlike the audit log, this is domain history *of* a client.

    A DPDP erasure (FR-M0-027) that removes the client must take it along, or the
    record of their lifecycle outlives the erasure request.
    """
    assert re.search(
        r"ondelete=[\"']CASCADE[\"']", migration_source
    ), "client_stage_history does not cascade; erasure would leave it behind."


def test_owner_fk_does_not_cascade(migration_source: str) -> None:
    """🔒 EC-M1-04 — a practitioner with clients is *reassigned*, not deleted out
    from under them. A cascade here would delete a client base with a user."""
    owner_fk = re.search(
        r"ForeignKeyConstraint\(\s*\[[\"']owner_user_id[\"']\](.*?)\)",
        migration_source,
        re.DOTALL,
    )
    assert owner_fk is not None, "the owner foreign key is missing"
    assert "CASCADE" not in owner_fk.group(1), (
        "fk_clients__owner cascades. Deleting a practitioner would delete their "
        "clients; EC-M1-04 requires reassignment."
    )


# ─── The live gate cannot be dropped ─────────────────────────────────────


def test_client_grants_are_covered_by_an_executable_test() -> None:
    """⚠️ Everything above reads migration text; none of it runs SQL.

    A revoke that is present but misspelled satisfies every assertion in this
    file. Only PostgreSQL can refuse the UPDATE, so the integration file must
    keep existing — and must still assert the privilege error.
    """
    live = _BACKEND / "tests" / "integration" / "test_client_spine.py"
    assert live.is_file(), (
        "the live client-spine gate is missing. The tests in this file read "
        "migration source only — they cannot prove PostgreSQL refuses a write."
    )
    source = live.read_text(encoding="utf-8")
    assert "permission denied" in source, (
        "the live gate no longer asserts a privilege error; append-only history "
        "and soft-delete-only clients would then be unproven against a cluster."
    )
