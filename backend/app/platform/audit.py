"""Audit persistence — where :class:`~app.kernel.audit.AuditEntry` rows land.

The kernel decides *what* is audited and *what may appear in an entry*; this
module decides *where it goes*. Splitting them is what lets the audit rules be
tested without a database and swapped without touching a rule.

🔒 Two write paths, because they have genuinely different atomicity needs:

**In-transaction** (:func:`write_entry`) — for mutations. The entry shares the
request's transaction, so the record and the change commit or roll back
together. An audit row describing a change that was rolled back would be worse
than no row: it is evidence of something that never happened.

**Out-of-band** (:func:`record_out_of_band`) — for denials and failures. There
is no successful transaction to join; the request's transaction is about to roll
back, and taking the entry down with it would erase exactly the evidence
(FR-M0-033) that a refusal occurred.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, cast

from sqlalchemy import Table, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.audit import AuditEntry, AuditSink, InMemoryAuditSink
from app.kernel.models import AuditLog
from app.platform.db import transaction
from app.platform.logging import get_logger

logger = get_logger(__name__)

#: 🔒 The audit table as a Core construct, not the ORM class.
#:
#: ⚠️ Against the mapped class, ``values()`` resolves keys as *mapped attributes*,
#: and ``AuditLog.metadata`` is SQLAlchemy's declarative ``MetaData`` rather than
#: the column — the model renames its attribute to ``entry_metadata`` for exactly
#: that reason. Writing through the class fails at execution time with an error
#: mentioning neither audit nor the column.
#:
#: ``DeclarativeBase.__table__`` is annotated ``FromClause``; on a mapped class it
#: is always a ``Table``.
_AUDIT_TABLE: Final[Table] = cast(Table, AuditLog.__table__)


def entry_values(entry: AuditEntry) -> dict[str, Any]:
    """Map an entry onto ``audit_log`` columns.

    A plain dict against ``insert()`` rather than an ORM instance: the write is
    append-only, has no identity map to maintain, and must not be caught up in a
    session flush ordering that could reorder it relative to the change it
    describes.
    """
    return {
        "tenant_id": entry.tenant_id,
        "actor_type": entry.actor_type.value,
        "actor_realm": entry.actor_realm.value if entry.actor_realm else None,
        "actor_id": entry.actor_id,
        "action": entry.action,
        "resource_type": entry.resource_type,
        "resource_id": entry.resource_id,
        "outcome": entry.outcome.value,
        # `None` rather than `[]`: "no fields recorded" and "nothing changed"
        # are different statements, and the column is nullable to keep them so.
        "changed_fields": list(entry.changed_fields) or None,
        "metadata": dict(entry.metadata) or None,
        "request_id": entry.request_id,
        "ip_hash": entry.ip_hash,
        "occurred_at": entry.occurred_at,
    }


async def write_entry(session: AsyncSession, entry: AuditEntry) -> None:
    """Write an entry inside the caller's transaction.

    🔒 Does not commit — ADR-04: the transaction belongs to the HTTP layer, and
    a service that commits its own audit row would be committing the change it
    describes along with it.

    Targets :data:`_AUDIT_TABLE` rather than the ORM class — see the note there.
    """
    await session.execute(insert(_AUDIT_TABLE).values(**entry_values(entry)))


@dataclass
class SqlAlchemyAuditSink:
    """Writes each entry in its own transaction. The out-of-band path."""

    async def write(self, entry: AuditEntry) -> None:
        # 🔒 Platform scope: the audit table has no RLS (Pattern D), and entries
        # are written for actors with no tenant. Passing the actor's tenant here
        # would be meaningless at best and, for an operator, wrong.
        async with transaction() as session:
            await write_entry(session, entry)


@dataclass
class LoggingAuditSink:
    """Emits entries to the log instead of the database.

    ⚠️ For local development before a database exists. It is **not** an audit
    trail: logs rotate, are mutable, and are not retained for the statutory
    period. Selected only when no database is configured, and it says so loudly
    at startup rather than quietly degrading.
    """

    async def write(self, entry: AuditEntry) -> None:
        logger.info(
            "Audit (not persisted)",
            extra={
                "action": entry.action,
                "outcome": entry.outcome.value,
                "resource_type": entry.resource_type,
                "actor_type": entry.actor_type.value,
                "request_id": entry.request_id,
            },
        )


_sink: AuditSink = InMemoryAuditSink()


def configure_audit_sink(sink: AuditSink) -> None:
    """Install the process-wide sink. Called once during startup."""
    global _sink
    _sink = sink


def get_audit_sink() -> AuditSink:
    return _sink


async def record_out_of_band(entry: AuditEntry) -> None:
    """Write an entry independently of the request's transaction.

    ⚠️ Failures are logged, never raised. A denial has already been decided; if
    recording it also fails, the caller must still receive their 403. Converting
    an audit-write failure into a 500 would turn a storage hiccup into an
    outage, and would do it on the security path first.

    The log line is the fallback trail, and it is deliberately at ``error``
    severity — a persistent inability to write audit rows is an operational
    incident (NFR-085), not a background annoyance.
    """
    try:
        await get_audit_sink().write(entry)
    except Exception:
        logger.exception(
            "Failed to write audit entry",
            extra={
                "action": entry.action,
                "outcome": entry.outcome.value,
                "request_id": entry.request_id,
            },
        )
