"""The platform kernel schema must hold its structural claims — S1 D0.

🔒 These tests need no database. What they protect is the set of guarantees the
whole tenancy model rests on, each of which is a line someone could delete
during a refactor without any other test going red:

* every tenant-scoped table has RLS **enabled and forced**, with a policy that
  constrains writes as well as reads;
* no password column exists anywhere (NFR-029);
* timestamps are timezone-aware (NFR-099);
* enum type names follow DB §1, and store values rather than member names;
* the ORM models and the hand-written migration describe the same schema.

⚠️ What they cannot prove: that the SQL executes, or that RLS actually filters
on a live cluster. That is AC-M0-003 — the S1 launch gate — and it needs a real
PostgreSQL. The last test in this file asserts that gate is not yet claimed as
met, so it cannot be quietly forgotten.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.kernel import Base
from app.kernel.models import (
    AccessStatus,
    AuthRealm,
    LinkPurpose,
    Operator,
    Session,
    Tenant,
    TransportType,
    User,
    UserRole,
    UserStatus,
)

_BACKEND = Path(__file__).resolve().parents[1]
_MIGRATION = _BACKEND / "migrations" / "versions" / "20260805_0002_platform_kernel.py"

#: Tables carrying `tenant_id` and therefore requiring Pattern A RLS.
_TENANT_SCOPED = ("users", "client_access_grants", "magic_links")

#: Platform tables (DB §2.2 Pattern D) — deliberately without RLS.
_PLATFORM_TABLES = ("tenants", "operators", "sessions")


@pytest.fixture(scope="module")
def migration_source() -> str:
    if not _MIGRATION.is_file():
        pytest.fail(f"platform kernel migration is missing: {_MIGRATION}")
    return _MIGRATION.read_text(encoding="utf-8")


def _ddl(table_name: str) -> str:
    """Compile one table to PostgreSQL DDL exactly as the server would see it."""
    return str(CreateTable(Base.metadata.tables[table_name]).compile(dialect=postgresql.dialect()))


def _rls_loop_tables(source: str) -> set[str]:
    """The table names the migration's RLS loop actually iterates.

    🔒 Read from `_TENANT_SCOPED` in the migration's own AST rather than from
    the importable module, so this asserts on what the file says rather than on
    what a shared constant happens to hold. If someone drops a table from that
    tuple, the table ships without isolation and this is what notices.
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
    """The SQL emitted inside the migration's `for table in _TENANT_SCOPED` loop.

    The statements are f-strings, so the literal table names never appear in the
    file. Coverage is decided by the loop's list (checked above); correctness of
    the statements is decided by this body.
    """
    match = re.search(
        r"for table in _TENANT_SCOPED:(.*?)(?=\n\ndef |\n    # 🔒 WITH CHECK)",
        source,
        re.DOTALL,
    )
    assert match is not None, "could not locate the RLS loop in the migration"
    return match.group(1)


# ─── The D0 dependency root ──────────────────────────────────────────────


def test_tenants_table_exists() -> None:
    """🔒 DB §21.1 — `tenants` is the FK target of every tenant-scoped table.

    Nothing else in the schema can be created before it.
    """
    assert "tenants" in Base.metadata.tables
    assert Tenant.__tablename__ == "tenants"


@pytest.mark.parametrize("table", _TENANT_SCOPED)
def test_tenant_scoped_tables_carry_tenant_id(table: str) -> None:
    """🔒 DB §1.1 — every tenant-scoped table carries the RLS discriminator."""
    columns = Base.metadata.tables[table].columns
    assert "tenant_id" in columns, f"{table} has no tenant_id; RLS cannot isolate it"
    assert not columns["tenant_id"].nullable, (
        f"{table}.tenant_id is nullable. A NULL discriminator matches no policy "
        "and the row becomes invisible to everyone — including its owner."
    )


@pytest.mark.parametrize("table", _TENANT_SCOPED)
def test_tenant_id_references_tenants(table: str) -> None:
    """A `tenant_id` without a foreign key permits orphaned rows."""
    fks = Base.metadata.tables[table].columns["tenant_id"].foreign_keys
    targets = {fk.target_fullname for fk in fks}
    assert "tenants.id" in targets, f"{table}.tenant_id does not reference tenants.id"


# ─── Row Level Security (the S1 gate's structural half) ──────────────────


@pytest.mark.isolation
@pytest.mark.parametrize("table", _TENANT_SCOPED)
def test_rls_is_enabled_and_forced(migration_source: str, table: str) -> None:
    """🔒 ADR-06 — RLS must be both ENABLEd and FORCEd.

    ⚠️ FORCE is the half that is easy to omit and impossible to notice. Without
    it the table *owner* bypasses every policy, and migrations run as
    `app_migrator`, which owns these tables. An unforced table has policies that
    are inert for exactly the role a fix-up script is most likely to use.

    ⚠️ The migration applies RLS in a loop over `_TENANT_SCOPED`, so the literal
    statements never appear in the source text. Asserting on the text would
    check the loop body once and silently ignore which tables it covers — so
    this asserts on the *list the loop iterates*, which is the thing that
    actually determines coverage.
    """
    covered = _rls_loop_tables(migration_source)
    assert table in covered, (
        f"{table} is not in the migration's RLS list; it would ship without " "tenant isolation."
    )
    body = _rls_loop_body(migration_source)
    assert "ENABLE ROW LEVEL SECURITY" in body
    assert "FORCE ROW LEVEL SECURITY" in body, (
        "RLS is enabled without being forced. The owning role would bypass "
        "every policy while `pg_policies` still showed them present."
    )


@pytest.mark.isolation
@pytest.mark.parametrize("table", _TENANT_SCOPED)
def test_rls_policy_constrains_writes_not_only_reads(migration_source: str, table: str) -> None:
    """🔒 USING filters reads; WITH CHECK constrains writes.

    Without WITH CHECK a tenant can INSERT a row carrying another tenant's
    `tenant_id` — invisible to the writer afterwards, but present in the
    victim's data. A read-only policy is half a policy.
    """
    assert table in _rls_loop_tables(migration_source)
    body = _rls_loop_body(migration_source)
    assert "CREATE POLICY" in body, "no tenant-isolation policy is created"
    assert "USING" in body, "policy has no USING clause"
    assert "WITH CHECK" in body, (
        "policy has no WITH CHECK clause — it filters reads but permits a write "
        "carrying another tenant's id."
    )


@pytest.mark.isolation
def test_policies_read_the_tenant_setting_through_one_helper(migration_source: str) -> None:
    """🔒 One definition of "the current tenant", not one per policy.

    A repeated `current_setting(...)::uuid` cast is a repeated opportunity to
    drift. NFR-072: every responsibility has exactly one home.
    """
    assert "CREATE FUNCTION current_tenant_id()" in migration_source
    body = _rls_loop_body(migration_source)
    assert "current_tenant_id()" in body, (
        "the policy reads the session variable directly instead of through " "current_tenant_id()"
    )


@pytest.mark.isolation
def test_missing_tenant_setting_yields_null_not_an_error(migration_source: str) -> None:
    """🔒 `current_setting('app.tenant_id', true)` — the `true` is load-bearing.

    Without it, an unscoped connection raises instead of matching zero rows. A
    policy that raises is a policy someone disables to get their job done.
    """
    helper = re.search(
        r"CREATE FUNCTION current_tenant_id\(\).*?\$\$;", migration_source, re.DOTALL
    )
    assert helper is not None
    assert "'app.tenant_id', true" in helper.group(0), (
        "current_tenant_id() reads the setting without missing_ok=true; an "
        "unscoped connection would error rather than simply see nothing."
    )


@pytest.mark.isolation
@pytest.mark.parametrize("table", _PLATFORM_TABLES)
def test_platform_tables_have_no_tenant_policy(migration_source: str, table: str) -> None:
    """🔒 DB §2.2 Pattern D — platform tables are not RLS-isolated.

    `tenants` cannot filter on `app.tenant_id` because it *defines* it.
    `operators` has no tenant by design (FR-M0-032). `sessions` spans realms and
    operator rows have no `tenant_id`, so a Pattern A policy would silently hide
    every operator session. All three are confined to `kernel`, off any
    client-facing path.
    """
    assert f"CREATE POLICY {table}__tenant_isolation" not in migration_source


# ─── Credential storage (NFR-029) ────────────────────────────────────────


@pytest.mark.parametrize("model", [User, Operator])
def test_no_password_column_exists(model: type) -> None:
    """🔒 Arch §2.3 — credentials live in GoTrue.

    This is a structural guarantee, not a policy: we cannot leak what we do not
    store. A `password_hash` column added "temporarily" is how that guarantee
    ends.
    """
    forbidden = {"password", "password_hash", "hashed_password", "salt", "secret"}
    present = {column.name for column in model.__table__.columns}
    assert not (forbidden & present), (
        f"{model.__tablename__} declares a credential column: {forbidden & present}. "
        "Credentials belong in GoTrue (NFR-029)."
    )


def test_tokens_are_stored_only_as_hashes() -> None:
    """🔒 DDR-04 / DDR-05 — a database read must not yield working credentials."""
    magic = {c.name for c in Base.metadata.tables["magic_links"].columns}
    assert "token_hash" in magic
    assert "token" not in magic, "magic_links stores a raw token; DDR-04 requires the hash only"

    sessions = {c.name for c in Session.__table__.columns}
    assert "refresh_token_hash" in sessions
    assert "refresh_token" not in sessions, "sessions stores a raw refresh token (DDR-05)"


def test_user_agent_is_hashed_not_raw() -> None:
    """🔒 NFR-033 — a raw user agent is a fingerprint, and it reaches the logs."""
    columns = {c.name for c in Session.__table__.columns}
    assert "user_agent_hash" in columns
    assert "user_agent" not in columns


# ─── Type correctness ────────────────────────────────────────────────────


@pytest.mark.parametrize("table", sorted(Base.metadata.tables))
def test_every_timestamp_is_timezone_aware(table: str) -> None:
    """🔒 NFR-099 — never a naive timestamp.

    SQLAlchemy's default for `Mapped[datetime]` is `TIMESTAMP WITHOUT TIME
    ZONE`. For a product whose tenants each declare a timezone, a naive column
    records "some wall clock somewhere" and cross-tenant comparison becomes
    meaningless. Fixed once in `Base.type_annotation_map`; asserted here so a
    later model cannot opt out.
    """
    for column in Base.metadata.tables[table].columns:
        if isinstance(column.type, sa.DateTime):
            assert column.type.timezone, (
                f"{table}.{column.name} is TIMESTAMP WITHOUT TIME ZONE. "
                "Every timestamp must be timestamptz (NFR-099)."
            )


@pytest.mark.parametrize(
    ("python_enum", "type_name"),
    [
        (UserRole, "user_role"),
        (UserStatus, "user_status"),
        (AccessStatus, "access_status"),
        (LinkPurpose, "link_purpose"),
        (TransportType, "transport_type"),
        (AuthRealm, "auth_realm"),
    ],
)
def test_enum_types_are_named_per_convention(python_enum: type, type_name: str) -> None:
    """🔒 DB §1 — enum types are singular snake_case.

    SQLAlchemy would otherwise name the type `userrole`. A PostgreSQL type name
    is not something a later migration renames cheaply, so it has to be right
    the first time.
    """
    found = {
        column.type.name
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, sa.Enum)
    }
    assert type_name in found, f"no column uses enum type `{type_name}`"


def test_enums_store_values_not_member_names() -> None:
    """🔒 The stored representation must match every `server_default`.

    SQLAlchemy persists the member *name* (`OWNER`) by default, while DB §4
    writes defaults as values (`owner`). Left unfixed, a column accepts `OWNER`
    from the ORM while rejecting its own default — a contradiction that only
    appears on the first insert that relies on the default.
    """
    role_column = User.__table__.columns["role"]
    assert isinstance(role_column.type, sa.Enum)
    assert set(role_column.type.enums) == {"owner", "practitioner"}, (
        f"user_role stores {role_column.type.enums}; expected lowercase values, "
        "not member names."
    )


def test_operator_two_factor_defaults_to_enabled() -> None:
    """🔒 FR-M0-009 / AC-M11-004 — operator 2FA is mandatory.

    A default of false would mean the *first* operator account, created before
    anyone thinks about it, is the one without 2FA.
    """
    default = Operator.__table__.columns["is_two_factor_enabled"].server_default
    assert default is not None
    assert "true" in str(default.arg).lower()


# ─── Model / migration agreement ─────────────────────────────────────────


@pytest.mark.parametrize("table", sorted(Base.metadata.tables))
def test_migration_creates_every_model_table(migration_source: str, table: str) -> None:
    """The hand-written migration and the ORM models must not drift.

    ⚠️ This is the cost of hand-writing the migration instead of autogenerating
    it: nothing but a test keeps the two in step. Autogenerate was rejected here
    because it cannot see RLS, FORCE, or grant revocations — all load-bearing.
    """
    assert (
        f'op.create_table(\n        "{table}"' in migration_source
    ), f"model declares table `{table}` but the migration does not create it"


def test_migration_columns_match_models(migration_source: str) -> None:
    """Every model column appears in the migration, and vice versa.

    Compares names only — types are checked by the compiled-DDL tests above.
    """
    tree = ast.parse(migration_source)
    in_migration: dict[str, set[str]] = {}

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "create_table"):
            continue
        if not (node.args and isinstance(node.args[0], ast.Constant)):
            continue
        table_name = node.args[0].value
        columns: set[str] = set()
        for arg in node.args[1:]:
            if (
                isinstance(arg, ast.Call)
                and getattr(arg.func, "attr", "") == "Column"
                and arg.args
                and isinstance(arg.args[0], ast.Constant)
            ):
                columns.add(arg.args[0].value)
        in_migration[table_name] = columns

    for table_name, table in Base.metadata.tables.items():
        model_columns = {c.name for c in table.columns}
        migration_columns = in_migration.get(table_name, set())
        assert model_columns == migration_columns, (
            f"`{table_name}` has drifted.\n"
            f"  only in model:     {sorted(model_columns - migration_columns)}\n"
            f"  only in migration: {sorted(migration_columns - model_columns)}"
        )


def test_downgrade_drops_enum_types(migration_source: str) -> None:
    """⚠️ A type created implicitly by a column is not dropped with it.

    Rollback-then-reapply would fail with "type already exists" — the failure
    lands on whoever is already having a bad day.
    """
    assert "def downgrade()" in migration_source
    assert re.search(
        r"sa\.Enum\(name=name\)\.drop|DROP TYPE", migration_source
    ), "downgrade() does not drop the enum types it created"


def test_enum_types_are_created_exactly_once_with_their_values(migration_source: str) -> None:
    """⚠️ Regression: `sa.Enum(name=..., create_type=False)` still emits DDL.

    Found by rendering the migration with `alembic upgrade head --sql`, not by
    reading it. Inside `create_table`, that form emits a second
    `CREATE TYPE <name> AS ENUM ()` — an *empty* type — because `create_type`
    controls checkfirst behaviour rather than suppressing inline emission. On a
    real server the migration then fails with "type already exists", and the
    failure is at deploy time on a database that already has half the schema.

    `postgresql.ENUM(name=..., create_type=False)` is the form that emits
    nothing. Every column must use the `_enum()` helper; a bare `sa.Enum` in a
    column definition reintroduces the defect silently.
    """
    columns = re.findall(r"sa\.Column\([^)]*sa\.Enum\(", migration_source)
    assert not columns, (
        f"{len(columns)} column(s) use `sa.Enum(...)` directly. Use `_enum(name)` "
        "— sa.Enum emits a duplicate empty CREATE TYPE inside create_table."
    )
    # The values are declared once, in the explicit creation loop.
    assert "sa.Enum(*values, name=name).create(" in migration_source


def test_every_declared_enum_is_used_by_a_column(migration_source: str) -> None:
    """An enum type created but never used is dead schema.

    Also catches the reverse of the previous test: a type dropped from a column
    during a refactor while its `CREATE TYPE` stayed behind.
    """
    declared = set(re.findall(r'^\s{4}"(\w+)": \(', migration_source, re.MULTILINE))
    referenced = set(re.findall(r'_enum\("(\w+)"\)', migration_source))
    assert declared == referenced, (
        f"enum types declared but unused: {sorted(declared - referenced)}; "
        f"referenced but undeclared: {sorted(referenced - declared)}"
    )


def test_migration_revision_chain(migration_source: str) -> None:
    """The chain must be linear and rooted in the baseline."""
    assert 'revision: str = "0002_platform_kernel"' in migration_source
    assert 'down_revision: str | None = "0001_baseline"' in migration_source


def test_env_imports_every_models_module() -> None:
    """🔒 Autogenerate sees only tables registered on `Base.metadata`.

    ⚠️ Registration is a side effect of *importing* the models module —
    subclassing `Base` is what adds the table. `env.py` importing `Base` alone
    leaves the metadata empty, and `alembic revision --autogenerate` then reads
    that as "the database has six tables the models do not" and proposes
    dropping all of them. The generated migration looks plausible.

    A models module is invisible until it is named here, so each new one must be
    added to `env.py`. This test fails when a module exists that `env.py` does
    not import.
    """
    env = (_BACKEND / "migrations" / "env.py").read_text(encoding="utf-8")
    models_modules = {
        f"app.{path.relative_to(_BACKEND / 'app').with_suffix('').as_posix().replace('/', '.')}"
        for path in (_BACKEND / "app").rglob("models.py")
    }
    assert models_modules, "no models.py found; this test is not doing its job"

    missing = {module for module in models_modules if f"import {module}" not in env}
    assert not missing, (
        f"migrations/env.py does not import {sorted(missing)}. Their tables are "
        "absent from Base.metadata, so autogenerate would propose dropping them."
    )


# ─── The gate that is not yet met ────────────────────────────────────────


@pytest.mark.isolation
def test_ac_m0_003_is_covered_by_an_executable_test() -> None:
    """🔒 AC-M0-003 is the S1 launch gate, and nothing in *this* file meets it.

    Everything above verifies *structure*: that policies are declared, forced,
    and constrain writes. None of it proves PostgreSQL enforces them, because
    none of it runs SQL.

    The executable gate lives in ``tests/integration/test_tenant_isolation.py``
    and needs a live cluster. This test asserts that file exists and still omits
    the tenant filter in its central query — because the failure mode of that
    suite is not a red test, it is someone "tidying up" the deliberately
    unfiltered `SELECT ... FROM users` into a filtered one. That change keeps
    every test green while testing nothing at all.
    """
    gate = _BACKEND / "tests" / "integration" / "test_tenant_isolation.py"
    assert gate.is_file(), (
        "The AC-M0-003 executable gate is missing. Structural tests alone cannot "
        "satisfy it — see this file's module docstring."
    )

    source = gate.read_text(encoding="utf-8")
    assert 'text("SELECT tenant_id FROM users")' in source, (
        "The AC-M0-003 read is no longer unfiltered. A query carrying "
        "`WHERE tenant_id = ...` passes even with RLS disabled, which is the "
        "false pass the whole suite exists to prevent."
    )
