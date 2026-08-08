"""The queue schema must hold its structural claims — C2.

🔒 These tests need no database. What they protect is the set of properties that
make a Postgres-backed queue safe, each of which is a line someone could delete
during a refactor without any other test going red:

* the hot-path index is **partial**, so it holds claimable rows rather than every
  job ever run;
* the lease index exists, so the recovery sweep is an index scan rather than a
  sequential one over the whole table;
* 🔒 **AI generation cannot be made retryable** — enforced by a check constraint,
  not by a policy lookup a caller could bypass;
* 🔒 `jobs` and `job_runs` have **no RLS**, and that is deliberate rather than
  forgotten — a Pattern A policy here would make the claim query match nothing
  and the queue would silently never run;
* 🔒 `idempotency_records` **does** have RLS, because it is the one table here
  that is only ever touched inside a tenant-scoped request.

⚠️ What they cannot prove: that `SKIP LOCKED` actually prevents a double claim,
that a lease actually expires, or that the partial index is actually used. Those
need a live PostgreSQL and belong to the C6 integration suite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.kernel import Base

_BACKEND = Path(__file__).resolve().parents[1]
_MIGRATION = _BACKEND / "migrations" / "versions" / "20260806_0005_jobs_queue.py"

#: 🔒 Platform tables (Pattern D). `jobs` and `job_runs` are here for a reason
#: unlike any other table in the schema: RLS would not merely be unnecessary, it
#: would *break* them. See `test_queue_tables_have_no_rls`.
_QUEUE_PLATFORM_TABLES = ("jobs", "job_runs")


@pytest.fixture(scope="module")
def migration_source() -> str:
    if not _MIGRATION.is_file():
        pytest.fail(f"queue migration is missing: {_MIGRATION}")
    return _MIGRATION.read_text(encoding="utf-8")


def _index_ddl(index_name: str) -> str:
    """Compile one index to PostgreSQL DDL exactly as the server would see it."""
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            if index.name == index_name:
                return str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    pytest.fail(f"no index named `{index_name}` is declared on any model")


def _table_ddl(table_name: str) -> str:
    return str(CreateTable(Base.metadata.tables[table_name]).compile(dialect=postgresql.dialect()))


# ─── Tables exist ────────────────────────────────────────────────────────


@pytest.mark.parametrize("table", ("jobs", "job_runs", "idempotency_records"))
def test_queue_tables_are_declared(table: str) -> None:
    """S1's table list (implementation plan §S1) names all three."""
    assert table in Base.metadata.tables


# ─── The claim hot path ──────────────────────────────────────────────────


def test_claimable_index_is_partial_on_pending() -> None:
    """🔒 DB §13.1 — the queue's hot path.

    Partial on `status = 'pending'` for a reason that only shows up at scale:
    succeeded and dead rows accumulate until the 30-day prune, so an unfiltered
    index would grow without bound while the portion the claim query actually
    reads stays small. The claim runs on every worker tick; this is the one
    index whose size directly costs latency.
    """
    ddl = _index_ddl("ix_jobs__claimable")
    assert "WHERE status = 'pending'" in ddl, (
        "ix_jobs__claimable is not partial. It would index every job ever "
        "enqueued, including the succeeded ones nothing will ever claim."
    )
    # Ordered to match the claim's ORDER BY, or PostgreSQL sorts after scanning.
    assert re.search(r"\(priority,\s*run_after\)", ddl), (
        "ix_jobs__claimable must be ordered (priority, run_after) to match the "
        "claim query's ORDER BY."
    )


def test_lease_index_covers_in_flight_states() -> None:
    """🔒 DB §13.3 — the recovery sweep's index.

    Without it the sweep scans the whole table on every tick. Partial on the two
    states that can hold a lease, because those are the only rows the sweep asks
    about.
    """
    ddl = _index_ddl("ix_jobs__lease")
    assert "lease_expires_at" in ddl
    assert "claimed" in ddl and "running" in ddl, (
        "ix_jobs__lease must be partial on the states that can hold a lease "
        "('claimed', 'running'); those are the only rows the sweep reads."
    )


def test_idempotency_index_treats_null_tenant_as_a_value() -> None:
    """🔒 `NULLS NOT DISTINCT` — the platform-job deduplication gap.

    ⚠️ The subtle one. `tenant_id` is NULL for platform jobs (quota reset,
    retention purge), and under PostgreSQL's default NULL semantics two rows
    with a NULL tenant never conflict. Without this clause the index would
    silently fail to deduplicate exactly the class of job most likely to be
    enqueued twice — a scheduler restart re-enqueueing its maintenance work.
    """
    ddl = _index_ddl("uq_jobs__idempotency")
    assert "UNIQUE" in ddl
    assert "NULLS NOT DISTINCT" in ddl, (
        "uq_jobs__idempotency must be NULLS NOT DISTINCT. Platform jobs carry a "
        "NULL tenant_id, and NULL != NULL would let duplicates through."
    )
    assert "WHERE idempotency_key IS NOT NULL" in ddl


def test_one_run_row_per_attempt_is_enforced_by_the_database() -> None:
    """🔒 A retry loop that double-recorded an attempt would make
    `attempt_count` and the run history disagree — and the history is what an
    operator trusts when asking why a job took three tries."""
    ddl = _index_ddl("uq_job_runs__job_attempt")
    assert "UNIQUE" in ddl
    assert "job_id" in ddl and "attempt_number" in ddl


# ─── Retry policy is structural ──────────────────────────────────────────


def test_generation_jobs_cannot_be_made_retryable() -> None:
    """🔒 Arch §11.2 — AI generation is never auto-retried.

    Each attempt costs money and a failure is usually deterministic (malformed
    output, provider rejection). A check constraint rather than a policy lookup:
    the database refuses the row, so no future caller can reintroduce the cost
    by passing `max_attempts=3`. FR-M5-010 guarantees the practitioner can
    proceed manually meanwhile.
    """
    ddl = _table_ddl("jobs")
    assert "ck_jobs__generation_not_retried" in ddl
    assert "job_class <> 'generation' OR max_attempts = 1" in ddl


def test_attempt_count_cannot_exceed_max_attempts() -> None:
    """A job that ran more times than it was allowed to is a retry-loop bug, and
    the database is where it should be caught rather than in a dashboard."""
    ddl = _table_ddl("jobs")
    assert "ck_jobs__attempt_count_bounded" in ddl


# ─── RLS disposition ─────────────────────────────────────────────────────


@pytest.mark.parametrize("table", _QUEUE_PLATFORM_TABLES)
def test_queue_tables_have_no_rls(migration_source: str, table: str) -> None:
    """🔒 Deliberate, and unlike every other Pattern D table in the schema.

    `tenants` and `operators` have no RLS because they are unreachable from a
    tenant-facing path. `jobs` has no RLS for a stronger reason: the worker
    claims work *across* tenants with no tenant in scope, so a policy keyed on
    `current_tenant_id()` would match zero rows on every poll and the queue
    would silently never run. Nothing would error — reminders and plan
    deliveries would simply stop.

    ⚠️ If a future migration adds a policy here "for consistency", this is what
    says no.
    """
    assert not re.search(
        rf"CREATE POLICY\s+\w*\s*ON\s+{table}\b", migration_source, re.IGNORECASE
    ), (
        f"`{table}` has an RLS policy. The worker claims across tenants with no "
        "tenant in scope — this policy would make the claim query return nothing "
        "and the queue would stop without any error."
    )
    assert not re.search(
        rf"ALTER TABLE\s+{table}\s+ENABLE ROW LEVEL SECURITY", migration_source, re.IGNORECASE
    ), f"`{table}` has RLS enabled; see above."


def test_idempotency_records_is_tenant_isolated(migration_source: str) -> None:
    """🔒 The one table here that *does* need Pattern A RLS.

    It stores response bodies (ADR-A10 requires the stored response be returned),
    and it is only ever written and read inside a tenant-scoped request
    transaction — never crossed by the worker. So the policy that would break the
    queue is exactly right here.
    """
    assert "ALTER TABLE idempotency_records ENABLE ROW LEVEL SECURITY" in migration_source
    # 🔒 FORCE is not redundant with ENABLE: without it the table owner —
    # `app_migrator`, which runs migrations — bypasses every policy.
    assert "ALTER TABLE idempotency_records FORCE ROW LEVEL SECURITY" in migration_source
    assert "CREATE POLICY idempotency_records__tenant_isolation" in migration_source


def test_idempotency_policy_constrains_writes_not_only_reads(migration_source: str) -> None:
    """🔒 USING filters reads; WITH CHECK constrains writes.

    Without WITH CHECK a tenant could insert a record carrying another tenant's
    id — invisible to them afterwards, but present in the other tenant's data,
    where it would satisfy that tenant's next replay of the same key.
    """
    policy = re.search(
        r"CREATE POLICY idempotency_records__tenant_isolation.*?;",
        migration_source,
        re.DOTALL,
    )
    assert policy is not None
    body = policy.group(0)
    assert "USING (tenant_id = current_tenant_id())" in body
    assert "WITH CHECK (tenant_id = current_tenant_id())" in body


# ─── Payload discipline ──────────────────────────────────────────────────


def test_job_payload_is_jsonb_not_text() -> None:
    """The payload is queried by operators diagnosing a stuck job.

    `jsonb` rather than `text` so `payload->>'client_id'` works without a cast,
    which is what makes "find the job for this client" a one-liner rather than a
    reason to log the payload somewhere more convenient and less safe.
    """
    column = Base.metadata.tables["jobs"].columns["payload"]
    assert isinstance(column.type, postgresql.JSONB)
    assert not column.nullable


def test_idempotency_records_expire(migration_source: str) -> None:
    """🔒 API §13.2 — 24h retention bounds how long a response body is kept.

    NOT NULL rather than defaulted: a record written without an expiry would be
    retained forever, and this is the one table in the queue schema that holds
    response data rather than identifiers.
    """
    column = Base.metadata.tables["idempotency_records"].columns["expires_at"]
    assert not column.nullable, (
        "idempotency_records.expires_at must be NOT NULL. A row without an "
        "expiry keeps a stored response body indefinitely."
    )
    assert "ix_idempotency_records__expires" in migration_source


def test_job_tenant_id_has_no_foreign_key() -> None:
    """🔒 Deliberate, and the same argument `audit_log` makes.

    A job may outlive the tenant it referenced — a DPDP erasure removes the
    tenant, and a cascade would delete the record that the work was done. The
    reference is a reference, not a relationship.
    """
    column = Base.metadata.tables["jobs"].columns["tenant_id"]
    assert not column.foreign_keys, (
        "jobs.tenant_id must not carry a foreign key: a cascade would erase the "
        "evidence that platform work ran for a since-deleted tenant."
    )
    assert column.nullable, "jobs.tenant_id is NULL for platform jobs"


# ─── Revision chain ──────────────────────────────────────────────────────


def test_migration_revision_chain(migration_source: str) -> None:
    """The queue revision follows auth tokens, or `alembic upgrade head` skips it."""
    assert 'revision: str = "0005_jobs_queue"' in migration_source
    assert 'down_revision: str | None = "0004_auth_tokens"' in migration_source


def test_downgrade_drops_enum_types(migration_source: str) -> None:
    """A type created implicitly is not dropped with its column, which makes a
    rollback-then-reapply fail with "type already exists"."""
    match = re.search(r"def downgrade\(\).*", migration_source, re.DOTALL)
    assert match is not None
    downgrade = match.group(0)
    for table in ("idempotency_records", "job_runs", "jobs"):
        assert f'op.drop_table("{table}")' in downgrade
    assert "sa.Enum(name=name).drop(" in downgrade


def test_enum_types_are_created_with_their_values(migration_source: str) -> None:
    """🔒 Declared once, explicitly, so `downgrade()` can drop them and so the
    stored representation matches every `server_default` (see 0002)."""
    for type_name in ("job_class", "job_status", "job_outcome", "idempotency_state"):
        assert f'"{type_name}"' in migration_source, f"enum `{type_name}` is not declared"
    assert "sa.Enum(*values, name=name).create(" in migration_source


def test_enum_values_match_the_models(migration_source: str) -> None:
    """🔒 The migration's literal values and the Python enums must agree.

    They are written twice — once as a tuple in the migration, once as an enum
    in the models — and a mismatch produces a column that rejects a value the
    ORM will happily send.
    """
    from app.kernel.models import IdempotencyState, JobClass, JobOutcome, JobStatus

    declared = {
        "job_class": JobClass,
        "job_status": JobStatus,
        "job_outcome": JobOutcome,
        "idempotency_state": IdempotencyState,
    }
    for type_name, python_enum in declared.items():
        match = re.search(rf'"{type_name}":\s*\(([^)]*)\)', migration_source)
        assert match is not None, f"`{type_name}` is not declared in _ENUMS"
        in_migration = set(re.findall(r'"([^"]+)"', match.group(1)))
        in_model = {member.value for member in python_enum}
        assert in_migration == in_model, (
            f"`{type_name}` has drifted.\n"
            f"  only in migration: {sorted(in_migration - in_model)}\n"
            f"  only in model:     {sorted(in_model - in_migration)}"
        )


def test_timestamps_are_timezone_aware() -> None:
    """🔒 NFR-099 — a naive timestamp on a lease is a wrong-by-an-offset expiry."""
    for table_name in ("jobs", "job_runs", "idempotency_records"):
        for column in Base.metadata.tables[table_name].columns:
            if isinstance(column.type, sa.DateTime):
                assert column.type.timezone, (
                    f"{table_name}.{column.name} is TIMESTAMP WITHOUT TIME ZONE. "
                    "A naive lease expiry is wrong by the server's UTC offset."
                )
