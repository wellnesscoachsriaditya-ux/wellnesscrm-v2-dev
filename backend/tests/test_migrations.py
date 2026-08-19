"""The migration scaffolding and role provisioning must hold their claims — S0-7.

🔒 These tests do not need a database, and that is the point. What they protect
is a set of *claims* made in configuration and SQL — that `app_user` is created
without `BYPASSRLS`, that migrations read their URL from settings rather than
from a checked-in string, that the append-only tables of DDR-15 are on the
verification list. Every one of those is load-bearing for tenant isolation or
audit immutability, and every one is a line someone could delete during a
refactor without any test going red.

⚠️ What they cannot prove: that the SQL executes correctly, or that a real
`app_user` actually lacks `BYPASSRLS` on a real cluster. Those need a live
PostgreSQL and belong to S1's tenant-isolation suite (AC-M0-003), alongside
`verify_no_rls_bypass`, which re-checks the same property at every non-local
startup. Three independent checks on one guarantee is proportionate: it is the
guarantee the whole tenancy model rests on.
"""

from __future__ import annotations

import configparser
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "backend"
_MIGRATIONS = _BACKEND / "migrations"
_OPS_DB = _REPO / "ops" / "db"
_ROLES_SQL = _OPS_DB / "001_roles.sql"
_VERIFY_SQL = _OPS_DB / "002_verify_grants.sql"


@pytest.fixture(scope="module")
def alembic_ini() -> configparser.ConfigParser:
    path = _BACKEND / "alembic.ini"
    if not path.is_file():
        pytest.fail(f"alembic.ini is missing: {path}")
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    return parser


def test_alembic_ini_is_pure_ascii() -> None:
    """🔒 Alembic reads `alembic.ini` with ConfigParser at the *locale* encoding.

    ⚠️ This is a real defect that file-content assertions cannot see, and it was
    found by running the CLI. On Windows the locale encoding is cp1252, so a
    single em dash or section sign anywhere in this file — including in a comment
    — raises `UnicodeDecodeError` naming only a byte offset. It passes in CI,
    which runs UTF-8 Linux, and fails on the developer's machine. Every other
    file in `migrations/` is Python or Mako and is UTF-8 by specification; this
    one is the exception.
    """
    raw = (_BACKEND / "alembic.ini").read_bytes()
    try:
        raw.decode("ascii")
    except UnicodeDecodeError as exc:
        offending = raw[exc.start : exc.end]
        line = raw[: exc.start].count(b"\n") + 1
        pytest.fail(
            f"alembic.ini contains a non-ASCII byte {offending!r} on line {line}. "
            "ConfigParser reads this file at the locale encoding (cp1252 on "
            "Windows), so Alembic would fail to start there while passing in CI."
        )


@pytest.fixture(scope="module")
def roles_sql() -> str:
    if not _ROLES_SQL.is_file():
        pytest.fail(f"role provisioning script is missing: {_ROLES_SQL}")
    return _ROLES_SQL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def verify_sql() -> str:
    if not _VERIFY_SQL.is_file():
        pytest.fail(f"grant verification script is missing: {_VERIFY_SQL}")
    return _VERIFY_SQL.read_text(encoding="utf-8")


def _normalise(sql: str) -> str:
    """Collapse whitespace so assertions survive reformatting."""
    return re.sub(r"\s+", " ", sql)


def _executable(sql: str) -> str:
    """Strip ``--`` comments, leaving only SQL the server will run.

    ⚠️ Necessary because these files are heavily commented by design — the
    header of ``001_roles.sql`` documents the ``ALTER ROLE … PASSWORD`` step an
    operator must perform, and the rationale comments discuss ``BYPASSRLS`` at
    length. Asserting over the raw text would flag that prose, and the obvious
    "fix" would be to delete the explanation, which is the opposite of what
    these assertions are protecting.
    """
    return _normalise(re.sub(r"--[^\n]*", "", sql))


def _role_statements(sql: str) -> list[str]:
    """Every ``CREATE ROLE`` / ``ALTER ROLE`` statement, comments removed.

    Role attributes are asserted against these rather than the whole file, so
    the word ``BYPASSRLS`` inside a ``RAISE EXCEPTION`` message — which is there
    to explain the failure — is not mistaken for a grant of it.
    """
    return re.findall(r"(?:CREATE|ALTER)\s+ROLE\s+\w+[^;]*", _executable(sql))


# ─── Alembic configuration ───────────────────────────────────────────────


def test_alembic_ini_declares_no_connection_string(alembic_ini: configparser.ConfigParser) -> None:
    """🔒 NFR-034 — the URL is a secret and must not be in source control.

    Alembic's own template ships a `sqlalchemy.url` line. Leaving it, even
    pointing at localhost, is how a real credential eventually gets pasted over
    it and committed.
    """
    url = alembic_ini.get("alembic", "sqlalchemy.url", fallback="")
    assert url == "", (
        "alembic.ini declares sqlalchemy.url. The connection string comes from "
        "DATABASE_MIGRATION_URL via migrations/env.py (NFR-034/NFR-075)."
    )


def test_alembic_can_import_the_application(alembic_ini: configparser.ConfigParser) -> None:
    """`env.py` imports `app.platform.config`, which needs `backend/` on the path."""
    assert alembic_ini.get("alembic", "prepend_sys_path", fallback="") == "."


def test_migration_filenames_sort_chronologically(
    alembic_ini: configparser.ConfigParser,
) -> None:
    """A directory of random revision ids is unreadable as a history."""
    template = alembic_ini.get("alembic", "file_template", fallback="")
    assert "year" in template and "month" in template and "day" in template


def test_sqlalchemy_engine_logging_is_not_verbose(
    alembic_ini: configparser.ConfigParser,
) -> None:
    """🔒 NFR-033 — at INFO, SQLAlchemy echoes statements, and a data migration's
    statements carry parameter values. That is clinical data in a deploy log."""
    level = alembic_ini.get("logger_sqlalchemy", "level", fallback="WARN")
    assert level.upper() in {
        "WARN",
        "WARNING",
        "ERROR",
    }, f"sqlalchemy.engine logs at {level}; statement parameters would be logged"


# ─── env.py ──────────────────────────────────────────────────────────────


def test_env_reads_the_migration_url_from_settings() -> None:
    """🔒 DB §2.4 — migrations connect as `app_migrator`, never as `app_user`.

    `Settings.migration_url` returns DATABASE_MIGRATION_URL and falls back to
    DATABASE_URL only locally. Reading DATABASE_URL directly here would run
    every migration as the application role, which has no DDL rights.
    """
    env = (_MIGRATIONS / "env.py").read_text(encoding="utf-8")
    assert "migration_url" in env
    assert "get_settings" in env


def test_env_escapes_percent_signs() -> None:
    """A URL-encoded password (`%40` for `@`) is interpolation syntax to
    ConfigParser, and the resulting error names neither the password nor why."""
    env = (_MIGRATIONS / "env.py").read_text(encoding="utf-8")
    assert 'replace("%", "%%")' in env


def test_env_detects_type_changes() -> None:
    """Without `compare_type`, autogenerate silently ignores a column type change."""
    env = (_MIGRATIONS / "env.py").read_text(encoding="utf-8")
    assert "compare_type=True" in env


# ─── The baseline revision ───────────────────────────────────────────────


def _revisions() -> list[Path]:
    return sorted((_MIGRATIONS / "versions").glob("*.py"))


def _baseline() -> Path:
    """The root of the chain — the revision with no predecessor.

    ⚠️ Located by its `down_revision = None` rather than by being the only file
    or the first alphabetically. Filename ordering happens to match revision
    order today because both are date-prefixed, but that is a convention, not a
    guarantee, and every assertion below is about the *baseline* specifically.
    """
    for path in _revisions():
        if re.search(r"down_revision:\s*str\s*\|\s*None\s*=\s*None", path.read_text("utf-8")):
            return path
    pytest.fail("no baseline revision found (none has down_revision = None)")


def test_exactly_one_baseline_revision_exists() -> None:
    """🔒 One root, and only one.

    Two revisions with `down_revision = None` are two independent chains.
    Alembic reports "multiple heads" only once they diverge downstream, by which
    point the fix is a manual merge revision.
    """
    roots = [
        path
        for path in _revisions()
        if re.search(r"down_revision:\s*str\s*\|\s*None\s*=\s*None", path.read_text("utf-8"))
    ]
    assert len(roots) == 1, f"expected exactly one root revision, found {len(roots)}: {roots}"


def test_revision_chain_is_linear() -> None:
    """🔒 Every non-root revision names a predecessor, and none is named twice.

    A duplicated `down_revision` is a fork: two revisions claiming the same
    parent. Alembic then has two heads and `upgrade head` refuses to run.
    """
    parents: dict[str, Path] = {}
    for path in _revisions():
        match = re.search(
            r'down_revision:\s*str\s*\|\s*None\s*=\s*"([^"]+)"', path.read_text("utf-8")
        )
        if match is None:
            continue  # the root, asserted separately
        parent = match.group(1)
        assert parent not in parents, (
            f"{path.name} and {parents[parent].name} both descend from `{parent}` — "
            "the chain has forked and Alembic would report multiple heads."
        )
        parents[parent] = path


def test_baseline_has_no_predecessor() -> None:
    """The chain must start somewhere, and `down_revision = None` is where."""
    source = _baseline().read_text(encoding="utf-8")
    assert re.search(r"down_revision:\s*str\s*\|\s*None\s*=\s*None", source)


def test_baseline_revokes_the_version_table_from_app_user() -> None:
    """🔒 `001_roles.sql` grants CRUD on future tables by default privilege, and
    `alembic_version` is created like any other table — so the application would
    inherit write access to its own schema history.

    An application that can UPDATE `alembic_version` can convince the next deploy
    that a migration already ran.
    """
    source = _normalise(_baseline().read_text(encoding="utf-8"))
    assert "REVOKE ALL ON TABLE alembic_version FROM app_user" in source


def test_baseline_creates_no_application_tables() -> None:
    """🔒 PDR-01 — build vertically. Tables land with the code that owns them (S1),
    not in a scaffolding sprint that has no consumer for them."""
    source = _baseline().read_text(encoding="utf-8")
    assert "create_table" not in source


def test_baseline_downgrade_is_not_a_silent_no_op() -> None:
    """A `downgrade` that does nothing makes the chain dishonestly reversible."""
    source = _baseline().read_text(encoding="utf-8")
    downgrade = source.split("def downgrade()")[-1]
    assert "op.execute" in downgrade


# ─── The revision template ───────────────────────────────────────────────
#
# ⚠️ Every one of these guards a defect found by generating a real revision and
# running the linters over it, not by reading the template. A template that
# produces code failing the project's own gates makes every future migration
# start with unrelated errors to clear.


def test_template_produces_lint_clean_imports() -> None:
    """`op` and `sa` are unused in a revision that only runs raw SQL.

    Both are kept — most revisions want them and Alembic's own autogenerate
    emits calls against them — so both carry `noqa: F401`. Without it, `ruff
    check` fails on every generated file before a line is written.
    """
    template = (_MIGRATIONS / "script.py.mako").read_text(encoding="utf-8")
    for line in template.splitlines():
        if line.startswith(("import sqlalchemy", "from alembic import op")):
            assert "noqa: F401" in line, f"unused-import guard missing: {line!r}"


def test_template_import_order_satisfies_isort() -> None:
    """Ruff's `I` rules put plain `import x` before `from x import y`."""
    template = (_MIGRATIONS / "script.py.mako").read_text(encoding="utf-8")
    assert template.index("import sqlalchemy as sa") < template.index("from alembic import op")


def test_template_emits_double_quoted_strings() -> None:
    """`repr()` produces single quotes, which `ruff format` rewrites.

    A generated file that is not already formatted fails `ruff format --check`
    on the first commit that adds a migration.
    """
    template = (_MIGRATIONS / "script.py.mako").read_text(encoding="utf-8")
    assert 'revision: str = "${up_revision}"' in template
    assert "repr(" not in template, "repr() emits single quotes; ruff format wants double"


def test_template_carries_the_ddr15_checklist() -> None:
    """🔒 The one thing a migration author must not forget. `001_roles.sql` grants
    all four verbs by default privilege, so an append-only table is mutable
    unless its own migration revokes."""
    template = (_MIGRATIONS / "script.py.mako").read_text(encoding="utf-8")
    assert "DDR-15" in template
    assert "REVOKE UPDATE, DELETE" in template
    assert "002_verify_grants" in template


def test_template_docstring_uses_no_html_comment_syntax() -> None:
    """`<!-- ... -->` renders literally in a Python docstring — it is not a
    comment there, it is text in the revision's own documentation."""
    template = (_MIGRATIONS / "script.py.mako").read_text(encoding="utf-8")
    assert "<!--" not in template


# ─── Role provisioning — DB §2.4 ─────────────────────────────────────────


@pytest.mark.isolation
def test_app_user_is_created_without_bypassrls(roles_sql: str) -> None:
    """🔒 **The single most important assertion in this file.**

    With BYPASSRLS, every tenant-isolation policy in DB §8 is inert while still
    appearing present in `pg_policies` — the exact V1 failure ADR-02 exists to
    prevent. The attribute is stated explicitly in the script rather than left to
    the server default, so it is a claim a reviewer can check.

    Asserted over the role statements themselves: every one must say NOBYPASSRLS,
    and none may grant it.
    """
    statements = _role_statements(roles_sql)
    assert statements, "no CREATE/ALTER ROLE statements found"

    for statement in statements:
        assert "NOBYPASSRLS" in statement, f"role statement omits NOBYPASSRLS: {statement!r}"
        # `NOBYPASSRLS` contains `BYPASSRLS`, so it is removed before checking
        # that the privilege is never granted outright.
        assert "BYPASSRLS" not in statement.replace(
            "NOBYPASSRLS", ""
        ), f"role statement grants BYPASSRLS: {statement!r}"


@pytest.mark.isolation
def test_neither_role_is_a_superuser(roles_sql: str) -> None:
    """A superuser bypasses RLS regardless of the BYPASSRLS attribute."""
    statements = _role_statements(roles_sql)
    assert statements, "no CREATE/ALTER ROLE statements found"

    for statement in statements:
        assert "NOSUPERUSER" in statement, f"role statement omits NOSUPERUSER: {statement!r}"

    created = {
        match.group(1) for match in re.finditer(r"CREATE\s+ROLE\s+(\w+)", _executable(roles_sql))
    }
    assert created == {"app_user", "app_migrator"}, f"unexpected roles created: {created}"


def test_only_the_migrator_holds_ddl(roles_sql: str) -> None:
    """🔒 DB §2.4 — `app_user` has no DDL. Enforced by withholding CREATE on the
    schema, which makes it a database-level fact rather than a property of code
    that happens never to issue DDL."""
    executable = _executable(roles_sql)
    assert "GRANT USAGE, CREATE ON SCHEMA public TO app_migrator" in executable
    assert "GRANT USAGE ON SCHEMA public TO app_user" in executable
    assert "CREATE ON SCHEMA public TO app_user" not in executable


def test_roles_script_verifies_its_own_postcondition(roles_sql: str) -> None:
    """A provisioning script that reports success without checking is how a
    cluster ends up one attribute away from having no tenant isolation."""
    executable = _executable(roles_sql)
    assert "rolbypassrls" in executable
    assert "RAISE EXCEPTION" in executable
    assert "has_schema_privilege('app_user', 'public', 'CREATE')" in executable


def test_roles_script_sets_no_passwords(roles_sql: str) -> None:
    """🔒 NFR-034 — no secrets in source control. A LOGIN role with no password
    cannot authenticate under scram/md5, so the default state is closed.

    Checked against executable SQL only: the header deliberately documents the
    `ALTER ROLE … PASSWORD '<from secret store>'` step the operator must run, and
    that instruction must not be mistaken for a committed credential.
    """
    assert not re.search(
        r"(?i)\bPASSWORD\s+'", _executable(roles_sql)
    ), "a literal password appears in the executable SQL of the provisioning script"


def test_roles_script_is_idempotent(roles_sql: str) -> None:
    """It is re-run to repair grants after a migration; it must not fail on the
    second run because the roles already exist."""
    executable = _executable(roles_sql)
    assert "IF NOT EXISTS (SELECT 1 FROM pg_roles" in executable
    assert executable.count("ALTER ROLE") >= 2, "existing roles must have attributes reasserted"


def test_roles_script_runs_in_one_transaction(roles_sql: str) -> None:
    """Its own verification block raises on failure — which only rolls back the
    partial provisioning if the whole script is one transaction."""
    executable = _executable(roles_sql)
    assert "BEGIN;" in executable
    assert "COMMIT;" in executable


def test_readonly_role_is_not_created(roles_sql: str) -> None:
    """🟡 DB §2.4 lists `app_readonly` as future. An unused role that can log in
    is attack surface for a capability nobody has asked for."""
    assert "CREATE ROLE app_readonly" not in _executable(roles_sql)


# ─── Append-only verification — DDR-15 ───────────────────────────────────


@pytest.mark.isolation
def test_every_append_only_table_is_verified(verify_sql: str) -> None:
    """🔒 DDR-15 — audit and consent immutability is enforced by the *absence* of
    an UPDATE/DELETE grant, and nothing fails loudly when an absence is
    accidentally filled in. This script is what notices.

    ⚠️ `usage_events` is append-only in design but deliberately absent, and must
    stay absent: it retains UPDATE so a reconciliation pass can set
    `is_reconciled`, and every other column is frozen by a trigger instead
    (migration 0007). Listing it here would fail on the grant it is designed to
    hold. `test_usage_events_is_protected_by_trigger_not_by_grant` covers it.
    """
    executable = _executable(verify_sql)
    for table in ("audit_log", "consent_records", "operator_actions", "subscription_events"):
        assert f"'{table}'" in executable, f"{table} is append-only but is not verified"


@pytest.mark.isolation
def test_usage_events_is_protected_by_trigger_not_by_grant() -> None:
    """🔒 The one append-only table a grant cannot fully protect — DB §14.5.

    A grant cannot express "every column but one", so `usage_events` keeps UPDATE
    for `is_reconciled` and a trigger rejects edits to everything else. This test
    exists because the protection lives somewhere unusual: a reader checking
    `002_verify_grants.sql` would find the table missing and reasonably conclude
    it was forgotten.
    """
    migration = (_MIGRATIONS / "versions" / "20260808_0007_entitlements.py").read_text(
        encoding="utf-8"
    )
    assert "usage_events__reject_material_edit" in migration
    assert "trg_usage_events__immutable" in migration
    # DELETE is still revoked — only UPDATE is retained.
    assert re.search(r"REVOKE\s+DELETE\s+ON\s+TABLE\s+usage_events\s+FROM\s+app_user", migration)
    # And the compensating-event rule is what makes the retained UPDATE safe.
    assert "EC-M10-04" in migration


def test_verification_checks_both_forbidden_privileges(verify_sql: str) -> None:
    executable = _executable(verify_sql)
    assert "ARRAY['UPDATE', 'DELETE']" in executable
    assert "has_table_privilege" in executable
    assert "RAISE EXCEPTION" in executable


def test_verification_tolerates_tables_that_do_not_exist_yet(verify_sql: str) -> None:
    """None of these tables exist at S0. The script must run cleanly now and
    become a real gate on the S1 migration that creates `audit_log`."""
    executable = _executable(verify_sql)
    assert "pg_tables" in executable
    assert "CONTINUE" in executable


def test_roles_script_documents_the_hazard_it_creates(roles_sql: str) -> None:
    """The blanket default privilege is the reason `002` must exist. That
    connection belongs in the file that creates the hazard, not only in the one
    that cleans up after it."""
    assert "DDR-15" in roles_sql
    assert "002_verify_grants" in roles_sql


# ─── Operator documentation ──────────────────────────────────────────────


def test_ops_readme_states_the_running_order() -> None:
    """🔒 Roles must exist before the first migration: Alembic authenticates as
    `app_migrator` and cannot create the role it is connecting as."""
    readme = _OPS_DB / "README.md"
    assert readme.is_file(), "ops/db/README.md is missing"

    text = readme.read_text(encoding="utf-8")
    assert "001_roles.sql" in text
    assert "alembic upgrade head" in text
    assert text.index("001_roles.sql") < text.index("alembic upgrade head")


# ─── The gate must not be able to skip itself ────────────────────────────
#
# 🔒 AC-M0-003 spent all of S0 and most of S1 reporting "skipped", which is
# green. These two tests protect the mechanism that ends that:
# `REQUIRE_LIVE_DATABASE` turns a missing database into a failure.
#
# ⚠️ Both directions are asserted, and both matter. A gate that cannot fail in
# CI protects nothing; a gate that always fails on a developer machine with no
# PostgreSQL gets switched off, and a switched-off gate protects nothing either.
# The flag must be the *only* difference between the two behaviours.
#
# These run the suite in a subprocess rather than inspecting the source, because
# what needs protecting is the observable outcome — the exit code — not the
# continued presence of a particular function name in a file.


def _run_integration_suite(*, require: bool) -> subprocess.CompletedProcess[str]:
    """Run `tests/integration` with no database configured.

    🔒 The two connection variables are cleared explicitly. A developer who
    happens to have a live database configured would otherwise run the real
    suite here, which proves nothing about the fail-closed path.
    """
    env = dict(os.environ)
    env.pop("TEST_DATABASE_URL", None)
    env.pop("TEST_DATABASE_MIGRATION_URL", None)
    if require:
        env["REQUIRE_LIVE_DATABASE"] = "1"
    else:
        env.pop("REQUIRE_LIVE_DATABASE", None)

    return subprocess.run(
        # ⚠️ No `-q` here: `addopts` in pyproject.toml already sets it, and a
        # second one means `-qq`, which suppresses the very summary line these
        # assertions read.
        [sys.executable, "-m", "pytest", "tests/integration", "--no-header"],
        cwd=_BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_require_flag_turns_a_missing_database_into_a_failure() -> None:
    """🔒 With the flag set and no database, the suite must fail, not skip."""
    result = _run_integration_suite(require=True)

    assert result.returncode != 0, (
        "REQUIRE_LIVE_DATABASE=1 with no database configured exited zero. "
        "The isolation gate can silently skip itself in CI, which is the one "
        "failure mode the flag exists to prevent.\n"
        f"{result.stdout[-2000:]}"
    )
    assert "REQUIRE_LIVE_DATABASE" in result.stdout, (
        "the suite failed, but not with the message naming the fix; a reader of "
        f"the CI log would not know what to do:\n{result.stdout[-2000:]}"
    )


def test_suite_still_skips_without_the_flag() -> None:
    """The other half — a machine with no PostgreSQL gets a green, skipped run."""
    result = _run_integration_suite(require=False)

    assert result.returncode == 0, (
        "the integration suite fails when no database is configured. It must "
        "skip, or developers without PostgreSQL cannot run the test suite at "
        f"all:\n{result.stdout[-2000:]}"
    )
    assert (
        "skipped" in result.stdout
    ), f"expected skips without the require flag:\n{result.stdout[-2000:]}"
