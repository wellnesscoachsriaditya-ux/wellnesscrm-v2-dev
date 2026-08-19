"""Audit persistence — where an entry lands, and what happens when it cannot.

The kernel decides what is audited; `app.platform.audit` decides where it goes.
This file covers the platform half that needs no database: the column mapping,
and the failure behaviour of the out-of-band path.

🔒 The behaviour most worth pinning is the one that looks like a bug: a failed
audit write is logged, never raised. A denial has already been decided, and
converting a storage hiccup into a 500 would produce an outage on the security
path first. The append-only guarantee itself is enforced by grants and tested in
`tests/integration/test_audit_append_only.py`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert
from sqlalchemy.dialects import postgresql

from app.kernel.audit import AuditEntry, AuditOutcome, InMemoryAuditSink
from app.kernel.context import ActorType, AuthRealm
from app.kernel.models import AuditLog
from app.platform.audit import (
    configure_audit_sink,
    entry_values,
    get_audit_sink,
    record_out_of_band,
)

TENANT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
USER_1 = uuid.UUID("11111111-0000-4000-8000-000000000001")
RESOURCE = uuid.UUID("cccccccc-0000-4000-8000-000000000003")


@pytest.fixture(autouse=True)
def restore_sink() -> Iterator[None]:
    """The sink is process-wide, installed once at startup. Tests that swap it
    must put it back, or a later test audits into a previous test's list."""
    original = get_audit_sink()
    try:
        yield
    finally:
        configure_audit_sink(original)


def _entry(**overrides: object) -> AuditEntry:
    defaults: dict[str, object] = {
        "action": "client.update",
        "resource_type": "client",
        "outcome": AuditOutcome.ALLOWED,
        "actor_type": ActorType.PRACTITIONER,
        "occurred_at": datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
        "tenant_id": TENANT_A,
        "actor_realm": AuthRealm.PRACTITIONER,
        "actor_id": USER_1,
        "resource_id": RESOURCE,
        "changed_fields": ("stage",),
        "metadata": {"reason": "stage_change"},
        "request_id": "req_test",
        "ip_hash": "0" * 32,
    }
    return AuditEntry(**{**defaults, **overrides})  # type: ignore[arg-type]


# ─── Column mapping ──────────────────────────────────────────────────────


def test_every_entry_field_reaches_a_column() -> None:
    values = entry_values(_entry())
    assert values == {
        "tenant_id": TENANT_A,
        "actor_type": "practitioner",
        "actor_realm": "practitioner",
        "actor_id": USER_1,
        "action": "client.update",
        "resource_type": "client",
        "resource_id": RESOURCE,
        "outcome": "allowed",
        "changed_fields": ["stage"],
        "metadata": {"reason": "stage_change"},
        "request_id": "req_test",
        "ip_hash": "0" * 32,
        "occurred_at": datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
    }


def test_the_mapping_covers_the_whole_entry() -> None:
    """⚠️ A guard against silent drift. Adding a field to `AuditEntry` without
    mapping it means the value is built, validated, and then discarded on the
    way to the database — a loss no other test would show."""
    mapped = set(entry_values(_entry()))
    # Every entry field maps to a column of the same name.
    assert set(AuditEntry.__slots__) - mapped == set()


def test_no_recorded_fields_is_not_the_same_as_nothing_changed() -> None:
    """The column is nullable to keep the two statements distinct: NULL means
    "no field list was recorded", `[]` would claim "nothing changed"."""
    values = entry_values(_entry(changed_fields=(), metadata={}))
    assert values["changed_fields"] is None
    assert values["metadata"] is None


# ─── The statement ───────────────────────────────────────────────────────


def test_the_insert_compiles_against_the_audit_table() -> None:
    """⚠️ Regression: `values(metadata=...)` against the ORM class does not work.

    `AuditLog.metadata` is SQLAlchemy's declarative `MetaData`, not the column —
    the model renames the attribute to `entry_metadata` for exactly that reason.
    Building the statement against the ORM class therefore fails at execution
    with `'MetaData' object has no attribute '_bulk_update_tuples'`, which names
    neither audit nor the column and is reached only when a row is actually
    written.

    Compiling here rather than in the integration suite is deliberate: this is a
    statement-construction defect, and gating it behind PostgreSQL would leave it
    latent for as long as the database gate stays open — which is how it survived
    the first time.
    """
    statement = insert(AuditLog.__table__).values(**entry_values(_entry()))  # type: ignore[arg-type]
    compiled = str(statement.compile(dialect=postgresql.dialect()))

    assert "INSERT INTO audit_log" in compiled
    assert "metadata" in compiled


def test_every_mapped_key_is_a_real_column() -> None:
    """A key that matches no column is silently accepted by `values()` until the
    statement is compiled, so the check is worth making explicitly."""
    columns = set(AuditLog.__table__.columns.keys())
    assert set(entry_values(_entry())) <= columns


def test_a_platform_entry_carries_no_tenant() -> None:
    """Operators and system jobs write rows with no tenant. The column is
    nullable for exactly this, and these are the entries FR-M0-032 most needs."""
    values = entry_values(_entry(tenant_id=None, actor_type=ActorType.OPERATOR, actor_realm=None))
    assert values["tenant_id"] is None
    assert values["actor_realm"] is None
    assert values["actor_type"] == "operator"


def test_enums_are_written_as_their_string_values() -> None:
    """The columns are PostgreSQL enums, which take the declared labels."""
    values = entry_values(_entry(outcome=AuditOutcome.DENIED, actor_type=ActorType.SYSTEM))
    assert values["outcome"] == "denied"
    assert values["actor_type"] == "system"


# ─── The out-of-band path ────────────────────────────────────────────────


async def test_an_entry_reaches_the_configured_sink() -> None:
    sink = InMemoryAuditSink()
    configure_audit_sink(sink)
    await record_out_of_band(_entry(outcome=AuditOutcome.DENIED))
    assert sink.actions() == ["client.update"]


async def test_a_failing_sink_does_not_propagate(caplog: pytest.LogCaptureFixture) -> None:
    """⚠️ Reads as swallowing an error, and is deliberate.

    This path runs while a request is already being refused. Raising here would
    replace the caller's 403 with a 500 — turning a storage problem into an
    outage, on the security path first. The log line is the fallback trail.
    """

    class BrokenSink:
        async def write(self, entry: AuditEntry) -> None:
            raise RuntimeError("database unreachable")

    configure_audit_sink(BrokenSink())
    with caplog.at_level(logging.ERROR):
        await record_out_of_band(_entry(outcome=AuditOutcome.DENIED))

    assert "Failed to write audit entry" in caplog.text


async def test_the_failure_is_logged_at_error_severity(caplog: pytest.LogCaptureFixture) -> None:
    """🔒 NFR-085. A persistent inability to write audit rows is an operational
    incident, not a background annoyance — so it must clear the threshold that
    reaches the operator."""

    class BrokenSink:
        async def write(self, entry: AuditEntry) -> None:
            raise RuntimeError("database unreachable")

    configure_audit_sink(BrokenSink())
    with caplog.at_level(logging.DEBUG):
        await record_out_of_band(_entry(outcome=AuditOutcome.DENIED))

    failures = [r for r in caplog.records if "Failed to write audit entry" in r.getMessage()]
    assert failures
    assert all(record.levelno >= logging.ERROR for record in failures)
