"""Audit — the immutable record of who did what.

🔒 FR-M0-031..036, DB §15.3. Audit is a framework concern, not something each
service remembers to call. The pipeline writes the entry; a module that forgets
cannot produce an unaudited mutation, because the module is not what writes it.

Four properties make the log worth having:

1. **Immutable.** ``app_user`` is granted ``INSERT`` and ``SELECT`` on
   ``audit_log`` and nothing else (DDR-15). A log the application can rewrite is
   evidence of nothing. Enforced by grants, not by code — code is what you are
   auditing.
2. 🔒 **Field names, never values** (FR-M0-035). "``weight_kg`` changed" is the
   audit record. "``weight_kg`` changed from 78 to 82" is a clinical record
   sitting in a table with different access rules and a much longer retention
   period. :func:`build_entry` rejects the second form rather than trusting it
   not to arrive.
3. 🔒 **Denials are recorded** (FR-M0-033). A refused attempt is the signal;
   only logging successes means the log is quietest exactly when something is
   wrong.
4. **Metadata is allowlisted** (DB §15.3). A denylist fails open on whatever its
   author did not foresee. The allowlist is composed from a small kernel set
   plus keys each action declares for itself, so a module extends it without
   editing the kernel.

⚠️ Reads are not audited by default — volume would bury the signal. The
exception is a **platform operator touching tenant data** (FR-M0-032), which is
the access class that most needs a trail.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from app.kernel.authz import Action, DataScope, Decision
from app.kernel.context import Actor, ActorType, AuthRealm, RequestContext
from app.kernel.scrubbing import filter_audit_metadata

# ─── Outcome ─────────────────────────────────────────────────────────────


class AuditOutcome(StrEnum):
    """How the attempt ended (DB §15.3).

    ``DENIED`` and ``FAILED`` are distinct on purpose: "you may not" and "it
    broke" have different investigations. Collapsing them turns a security
    signal into an error-rate statistic.
    """

    ALLOWED = "allowed"
    DENIED = "denied"
    FAILED = "failed"


class AuditIntegrityError(RuntimeError):
    """An audit entry was rejected before it could be written.

    🔒 Raised for programmer error — a value where a field name belongs, an
    over-long identifier. Loud rather than best-effort: an audit log that
    silently drops malformed entries is a log you cannot reason about, and the
    entries most likely to be malformed are the unusual ones that matter.
    """


# ─── Field-name validation ───────────────────────────────────────────────

#: A column or attribute name. Anything with an ``=``, a colon, whitespace, a
#: quote or a digit-led segment is a *value* in disguise.
_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

#: Beyond this many changed fields, the entry is recording a bulk rewrite. Keep
#: the count, drop the list — an unbounded array in every row is a storage
#: problem, and a 400-element list is not something anyone reads.
_MAX_CHANGED_FIELDS = 64


def validate_changed_fields(fields: Sequence[str]) -> tuple[str, ...]:
    """🔒 Assert that ``changed_fields`` holds field *names* only (FR-M0-035).

    Raises:
        AuditIntegrityError: If any entry is not a plain snake_case identifier.
            The error names the offender and says what to do, because the fix is
            always the same shape: pass the key, not the pair.
    """
    validated: list[str] = []
    for name in fields[:_MAX_CHANGED_FIELDS]:
        if not isinstance(name, str) or not _FIELD_NAME.match(name):
            raise AuditIntegrityError(
                f"changed_fields entry {name!r} is not a field name.\n\n"
                "🔒 FR-M0-035 — the audit log records WHICH fields changed, never "
                "what they changed to. A clinical value here would sit in a table "
                "with weaker access rules and a seven-year retention period.\n\n"
                "Pass ['weight_kg'], not ['weight_kg=82'] or {'weight_kg': 82}."
            )
        validated.append(name)
    return tuple(validated)


# ─── Metadata allowlist ──────────────────────────────────────────────────

#: 🔒 Keys any action may record. Deliberately small, and identifiers or
#: outcomes only — nothing here can carry a name, a contact detail or a
#: clinical value. Modules extend this per action via
#: ``Action.audit_metadata_keys`` rather than by editing this set.
KERNEL_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "reason",
        "outcome",
        "status",
        "previous_status",
        "realm",
        "role",
        "resource_type",
        "resource_id",
        "count",
        "attempt",
        "version",
        "previous_version",
        "plan_code",
        "resource",
        "limit",
        "used",
        "error_type",
        "transport",
        "provider",
        "job_type",
        "duration_ms",
    }
)


def allowed_metadata_keys(action: Action | None) -> frozenset[str]:
    """The allowlist for one action: the kernel set plus what it declares."""
    if action is None:
        return KERNEL_METADATA_KEYS
    return KERNEL_METADATA_KEYS | action.audit_metadata_keys


# ─── The entry ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One row of ``audit_log``, validated and scrubbed.

    Frozen and built only through :func:`build_entry`, so there is no path that
    produces an entry which skipped validation.
    """

    action: str
    resource_type: str
    outcome: AuditOutcome
    actor_type: ActorType
    occurred_at: datetime
    tenant_id: uuid.UUID | None = None
    actor_realm: AuthRealm | None = None
    actor_id: uuid.UUID | None = None
    #: 🔒 A reference, not a foreign key. The audited row may be deleted under a
    #: retention policy or an erasure request; the fact that it was touched must
    #: survive that (DB §15.3). A FK would either block the deletion or cascade
    #: away the evidence.
    resource_id: uuid.UUID | None = None
    changed_fields: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    #: 🔒 Hashed, never raw — an IP address is personal data (NFR-033).
    ip_hash: str | None = None


def build_entry(
    *,
    action_name: str,
    resource_type: str,
    outcome: AuditOutcome,
    context: RequestContext,
    action: Action | None = None,
    resource_id: uuid.UUID | None = None,
    changed_fields: Sequence[str] = (),
    metadata: Mapping[str, Any] | None = None,
    ip_hash: str | None = None,
    occurred_at: datetime | None = None,
) -> AuditEntry:
    """Construct a validated audit entry from the ambient request context.

    🔒 Actor attribution comes from the **context**, never from a caller
    argument. A caller-supplied actor is an audit-forgery vector, and the whole
    point of the record is that it cannot be shaped by the code being audited.

    Args:
        action_name: The registered action, e.g. ``client.update``.
        resource_type: The kind of thing acted upon, e.g. ``client``. A string
            rather than an enum — the kernel does not enumerate domain types.
        outcome: Allowed, denied or failed.
        context: The ambient request context, supplying actor and request id.
        action: The registered :class:`~app.kernel.authz.Action`, when known.
            Supplies the metadata allowlist extension.
        changed_fields: 🔒 Field *names* only. Validated, not trusted.
        metadata: Filtered against the allowlist; unlisted keys are dropped
            silently, because a dropped key is a smaller problem than a leaked
            value and the allowlist is reviewable in one place.

    Raises:
        AuditIntegrityError: If ``changed_fields`` contains anything other than
            plain field names.
    """
    actor = context.actor
    filtered = filter_audit_metadata(metadata or {}, allowed_metadata_keys(action))

    return AuditEntry(
        action=action_name,
        resource_type=resource_type,
        outcome=outcome,
        actor_type=actor.actor_type,
        # 🔒 Recorded in UTC (NFR-099). A tenant's local timezone is a
        # presentation concern; an audit trail spanning timezones must be
        # orderable without knowing which one each row was written in.
        occurred_at=occurred_at or datetime.now(UTC),
        tenant_id=actor.tenant_id,
        actor_realm=actor.realm,
        actor_id=actor.subject_id,
        resource_id=resource_id,
        changed_fields=validate_changed_fields(changed_fields),
        metadata=filtered,
        request_id=context.request_id,
        ip_hash=ip_hash,
    )


def build_denial_entry(
    *,
    action_name: str,
    resource_type: str,
    context: RequestContext,
    decision: Decision,
    action: Action | None = None,
    resource_id: uuid.UUID | None = None,
    ip_hash: str | None = None,
) -> AuditEntry:
    """🔒 Record a refused attempt (FR-M0-033).

    The decision's ``reason`` is stored in metadata — it is the whole value of
    the entry, and it is written *here* rather than returned to the caller,
    which only ever sees a generic 403 (API §5.4).
    """
    return build_entry(
        action_name=action_name,
        resource_type=resource_type,
        outcome=AuditOutcome.DENIED,
        context=context,
        action=action,
        resource_id=resource_id,
        metadata={"reason": decision.reason},
        ip_hash=ip_hash,
    )


# ─── What gets audited ───────────────────────────────────────────────────


def should_audit(action: Action | None, outcome: AuditOutcome, actor: Actor) -> bool:
    """Decide whether an attempt is recorded.

    The rules, in order:

    1. 🔒 **Every denial and every failure.** These are the entries an
       investigation starts from.
    2. 🔒 **Every operator action against tenant data** (FR-M0-032), read or
       not. Operators are the only actors whose reads are recorded, because they
       are the only actors reading across a boundary they do not own.
    3. **Every mutation.**
    4. Ordinary reads: not recorded. A practitioner reading their own client is
       the system working; logging it at request volume would bury the entries
       that matter and cost more storage than the data being audited.

    An unknown action (``None``) is audited. Deny-by-default in ``authz`` means
    it is about to be refused anyway, and an unregistered action reaching the
    pipeline is itself worth a row.
    """
    if outcome is not AuditOutcome.ALLOWED:
        return True

    if action is None:
        return True

    if actor.is_operator and action.data_scope is not DataScope.PLATFORM:
        return True

    return not action.is_read


# ─── Sink ────────────────────────────────────────────────────────────────


class AuditSink(Protocol):
    """Where entries go.

    A port, so the writer can be a database session in the application, a list
    in a unit test, and — if volume ever demands — a queue, without any caller
    changing. 🔒 The interface has no update or delete: there is no supported
    way to alter a written entry, and none should be reachable by mistake.
    """

    async def write(self, entry: AuditEntry) -> None: ...


@dataclass
class InMemoryAuditSink:
    """Collects entries in a list. For tests, and for the local environment
    before a database is available."""

    entries: list[AuditEntry] = field(default_factory=list)

    async def write(self, entry: AuditEntry) -> None:
        self.entries.append(entry)

    def actions(self) -> list[str]:
        return [entry.action for entry in self.entries]


# ─── Helpers ─────────────────────────────────────────────────────────────


def hash_ip(ip_address: str | None, *, salt: str) -> str | None:
    """🔒 Hash a client IP for the audit trail (NFR-033).

    An IP address is personal data under the DPDP Act, and storing it raw would
    put an identifier in a table retained for seven years. The hash preserves
    what audit actually needs — "was this the same source?" — and discards what
    it does not.

    ⚠️ The salt must be a deployment secret and stable for the retention period.
    An unsalted hash of an IPv4 address is trivially reversed: the whole space is
    2^32, which is a few seconds of brute force. It is supplied by the caller
    because the kernel must not read configuration (R5).
    """
    if not ip_address:
        return None
    digest = hashlib.sha256(f"{salt}:{ip_address}".encode())
    return digest.hexdigest()[:32]
