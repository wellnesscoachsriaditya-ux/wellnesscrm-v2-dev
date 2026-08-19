"""The consent schema must hold its structural claims — D3.

🔒 These tests need no database. They exist because the guarantees below are
single lines in a migration that a refactor could delete with every other test
staying green — and because the integration suite that proves them *executing*
skips without a live PostgreSQL, so locally it proves nothing at all.

* 🔒 ``consent_records`` is **append-only by grant** (DDR-15, NFR-047) — UPDATE
  and DELETE are revoked from ``app_user``, and the revoke is present rather
  than assumed. ``ops/db/001_roles.sql`` grants all four verbs on new tables by
  default, so an absent revoke is an editable ledger.
* 🔒 ``consent_notices`` keeps UPDATE but loses DELETE, and a **trigger** blocks
  material edits — the column-level rule a grant cannot express (FR-M0-029).
* 🔒 The **purpose catalogue is the platform's**, not the tenant's.
* 🔒 ``consent_records`` has **no RLS**, deliberately: it is written on the
  anonymous enquiry path where no tenant context exists (FR-M2-004), so a
  Pattern A policy would reject exactly the pre-client consent that must be
  captured. ``data_requests`` *does* have one.
* the ledger carries no plain mobile number (NFR-033, DB §16.4).

⚠️ What these cannot prove: that PostgreSQL enforces any of it. That is
``tests/integration/test_consent_ledger.py``, and the last test here asserts
that file exists so the live gate cannot be quietly dropped.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.kernel import Base

_BACKEND = Path(__file__).resolve().parents[1]
_MIGRATION = _BACKEND / "migrations" / "versions" / "20260807_0006_consent_ledger.py"

#: 🔒 The ledger, and the catalogue tables a tenant must not write.
_APPEND_ONLY_TABLE = "consent_records"

#: Pattern D here, unlike `data_requests` — see the module docstring.
_CONSENT_PLATFORM_TABLES = ("consent_purposes", "consent_notices", "consent_records")


@pytest.fixture(scope="module")
def migration_source() -> str:
    if not _MIGRATION.is_file():
        pytest.fail(f"consent migration is missing: {_MIGRATION}")
    return _MIGRATION.read_text(encoding="utf-8")


# ─── Append-only by grant (DDR-15, NFR-047) ──────────────────────────────


def test_ledger_revokes_update_and_delete(migration_source: str) -> None:
    """🔒 The privilege to revise a consent decision must not exist.

    ⚠️ The default privileges in ``ops/db/001_roles.sql`` grant all four verbs on
    every new table. This revoke is therefore mandatory, not decorative: without
    it the ledger is editable by the application it is evidence against.
    """
    assert re.search(
        rf"REVOKE\s+UPDATE,\s*DELETE\s+ON\s+TABLE\s+{_APPEND_ONLY_TABLE}\s+FROM\s+app_user",
        migration_source,
        re.IGNORECASE,
    ), (
        "consent_records does not revoke UPDATE and DELETE from app_user. "
        "Default privileges grant all four verbs, so the ledger is editable."
    )


def test_ledger_grants_only_insert_and_select(migration_source: str) -> None:
    """The two verbs it does hold — and no third one slipped back in."""
    assert re.search(
        rf"GRANT\s+INSERT,\s*SELECT\s+ON\s+TABLE\s+{_APPEND_ONLY_TABLE}\s+TO\s+app_user",
        migration_source,
        re.IGNORECASE,
    )


def test_ledger_sequence_is_usable(migration_source: str) -> None:
    """A revoke that also blocked inserts would be caught here, not in prod."""
    assert re.search(
        rf"GRANT\s+USAGE,\s*SELECT\s+ON\s+SEQUENCE\s+{_APPEND_ONLY_TABLE}_id_seq\s+TO\s+app_user",
        migration_source,
        re.IGNORECASE,
    )


def test_notices_lose_delete_but_keep_update(migration_source: str) -> None:
    """🔒 Supersession needs UPDATE; nothing needs DELETE.

    Removing a notice would orphan the basis of every consent recorded against
    it — the ledger would point at text that no longer exists.
    """
    assert re.search(
        r"REVOKE\s+DELETE\s+ON\s+TABLE\s+consent_notices\s+FROM\s+app_user",
        migration_source,
        re.IGNORECASE,
    )
    assert not re.search(
        r"REVOKE\s+[^;]*UPDATE[^;]*ON\s+TABLE\s+consent_notices",
        migration_source,
        re.IGNORECASE,
    ), (
        "consent_notices has lost UPDATE. Superseding a notice stamps "
        "`superseded_at`, so this revoke would make supersession impossible."
    )


def test_notice_text_is_immutable_by_trigger(migration_source: str) -> None:
    """🔒 The rule a grant cannot express (FR-M0-029, NFR-051).

    ``consent_notices`` keeps UPDATE for supersession, so column-level
    immutability has to be a trigger. Without it, ``UPDATE consent_notices SET
    body = ...`` silently rewrites what every consenting person agreed to, and
    nothing appears broken until a regulator asks.
    """
    assert "CREATE TRIGGER trg_consent_notices__immutable" in migration_source
    # The columns that constitute the presented notice. `superseded_at` is
    # deliberately absent — stamping it is the one permitted UPDATE.
    for column in ("body", "title", "version", "locale", "purpose_ids"):
        assert re.search(
            rf"NEW\.{column}\s+IS DISTINCT FROM\s+OLD\.{column}", migration_source
        ), f"the immutability trigger does not guard `{column}`"
    assert not re.search(
        r"NEW\.superseded_at\s+IS DISTINCT FROM\s+OLD\.superseded_at", migration_source
    ), "the trigger guards `superseded_at`, which would make supersession impossible"


def test_purpose_catalogue_is_not_tenant_writable(migration_source: str) -> None:
    """🔒 A tenant inventing processing purposes is a compliance problem."""
    assert re.search(
        r"REVOKE\s+INSERT,\s*UPDATE,\s*DELETE\s+ON\s+TABLE\s+consent_purposes\s+FROM\s+app_user",
        migration_source,
        re.IGNORECASE,
    )
    assert re.search(
        r"GRANT\s+SELECT\s+ON\s+TABLE\s+consent_purposes\s+TO\s+app_user",
        migration_source,
        re.IGNORECASE,
    ), "tenants must still be able to read the catalogue to render a notice"


def test_grants_are_guarded_on_role_existence(migration_source: str) -> None:
    """⚠️ A REVOKE naming a missing role aborts the transaction.

    A local developer may run a single-role database, which is the setup most
    likely to run this migration first.
    """
    assert "FROM pg_roles WHERE rolname = 'app_user'" in migration_source


# ─── RLS disposition ─────────────────────────────────────────────────────


@pytest.mark.parametrize("table", _CONSENT_PLATFORM_TABLES)
def test_consent_platform_tables_have_no_policy(migration_source: str, table: str) -> None:
    """🔒 Deliberate for three different reasons — see the module docstring.

    ⚠️ ``consent_records`` is the one that matters. It carries ``tenant_id``, so
    a policy looks obviously correct; but entries are written on the anonymous
    enquiry path before any tenant context is set (FR-M2-004), and a Pattern A
    policy would reject exactly the pre-client consent that must be captured.
    Reads are tenant-filtered in ``platform.consent`` instead.
    """
    assert not re.search(
        rf"CREATE POLICY\s+\w*\s*ON\s+{table}\b", migration_source, re.IGNORECASE
    ), (
        f"`{table}` has an RLS policy. If this is consent_records, it would "
        "reject the anonymous enquiry-path insert that FR-M2-004 requires."
    )
    assert not re.search(
        rf"ALTER TABLE\s+{table}\s+ENABLE ROW LEVEL SECURITY", migration_source, re.IGNORECASE
    ), f"`{table}` has RLS enabled; see above."


def test_data_requests_does_have_forced_rls(migration_source: str) -> None:
    """The one table here that behaves ordinarily (Pattern A).

    Mutable, tenant-scoped, read by the operator console within a tenant — so
    the absence of a policy here would be a leak rather than a design choice.
    """
    assert re.search(
        r"ALTER TABLE\s+data_requests\s+ENABLE ROW LEVEL SECURITY",
        migration_source,
        re.IGNORECASE,
    )
    assert re.search(
        r"ALTER TABLE\s+data_requests\s+FORCE ROW LEVEL SECURITY",
        migration_source,
        re.IGNORECASE,
    ), "without FORCE, the table owner bypasses its own policy"
    assert re.search(
        r"CREATE POLICY\s+data_requests__tenant_isolation\s+ON\s+data_requests",
        migration_source,
        re.IGNORECASE,
    )


# ─── The ledger holds no second copy of contact data (NFR-033) ───────────


def test_ledger_stores_a_mobile_hash_not_a_mobile() -> None:
    """🔒 DB §16.4 — otherwise the ledger is an unprotected contact list.

    Retention here is years longer than for client records, so a plain number in
    this table outlives every deletion path that would have removed it.
    """
    columns = {c.name for c in Base.metadata.tables["consent_records"].columns}
    assert "subject_mobile_hash" in columns
    assert "subject_mobile" not in columns, (
        "consent_records has a plain mobile column. The ledger is retained for "
        "years and read on an anonymous path; hash it (kernel.consent.hash_mobile)."
    )


def test_a_record_must_identify_its_subject(migration_source: str) -> None:
    """An entry nobody can be matched to is not evidence of anything."""
    assert "ck_consent_records__subject_identified" in migration_source


# ─── The live gate cannot be dropped ─────────────────────────────────────


def test_ledger_grants_are_covered_by_an_executable_test() -> None:
    """⚠️ Everything above reads migration text; none of it runs SQL.

    A revoke that is present but misspelled would satisfy every assertion in
    this file. Only PostgreSQL can refuse the UPDATE, so the integration file
    must keep existing.
    """
    live = _BACKEND / "tests" / "integration" / "test_consent_ledger.py"
    assert live.is_file(), (
        "the live consent-ledger gate is missing. The tests in this file read "
        "migration source only — they cannot prove PostgreSQL refuses an UPDATE."
    )
    source = live.read_text(encoding="utf-8")
    assert "permission denied" in source, (
        "the live gate no longer asserts a privilege error; append-only would "
        "then be unproven against a real cluster."
    )
