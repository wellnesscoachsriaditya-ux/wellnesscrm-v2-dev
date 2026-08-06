"""The audit framework — FR-M0-031..036, DB §15.3.

Audit is a framework concern (Arch §5.1): the pipeline writes the entry, so a
module that forgets cannot produce an unaudited mutation. These tests cover the
two halves the kernel owns — **what may appear in an entry**, and **what gets
recorded at all**. Where the entry lands is `test_platform_audit.py`; that the
database refuses to alter it is `tests/integration/test_audit_append_only.py`.

🔒 The property under test throughout is that the log cannot be *shaped* by the
code being audited. Attribution comes from the ambient context rather than a
caller argument, values are refused where field names belong, and metadata is
allowlisted rather than denylisted.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.kernel.audit import (
    KERNEL_METADATA_KEYS,
    AuditEntry,
    AuditIntegrityError,
    AuditOutcome,
    InMemoryAuditSink,
    allowed_metadata_keys,
    build_denial_entry,
    build_entry,
    hash_ip,
    should_audit,
    validate_changed_fields,
)
from app.kernel.authz import Action, DataScope, deny
from app.kernel.context import Actor, ActorType, AuthRealm, RequestContext, UserRole

TENANT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
USER_1 = uuid.UUID("11111111-0000-4000-8000-000000000001")
RESOURCE = uuid.UUID("cccccccc-0000-4000-8000-000000000003")


def _practitioner_context() -> RequestContext:
    return RequestContext(
        request_id="req_test",
        actor=Actor(
            actor_type=ActorType.PRACTITIONER,
            realm=AuthRealm.PRACTITIONER,
            subject_id=USER_1,
            tenant_id=TENANT_A,
            role=UserRole.PRACTITIONER,
        ),
    )


def _operator_context() -> RequestContext:
    return RequestContext(
        request_id="req_operator",
        actor=Actor(
            actor_type=ActorType.OPERATOR,
            realm=AuthRealm.OPERATOR,
            subject_id=USER_1,
            role=UserRole.PLATFORM_OPERATOR,
        ),
    )


def _action(**overrides: object) -> Action:
    """A registered-shaped action without touching the process-wide registry.

    `should_audit` and `allowed_metadata_keys` read an `Action`; neither needs it
    to be registered, and constructing one directly keeps these tests free of
    the registry's shared state.
    """
    defaults: dict[str, object] = {
        "name": "client.update",
        "roles": frozenset({UserRole.PRACTITIONER}),
    }
    return Action(**{**defaults, **overrides})  # type: ignore[arg-type]


# ─── Field names, never values (FR-M0-035) ───────────────────────────────


@pytest.mark.parametrize("name", ["weight_kg", "status", "a", "notes_2", "plan_version"])
def test_plain_field_names_are_accepted(name: str) -> None:
    assert validate_changed_fields([name]) == (name,)


@pytest.mark.parametrize(
    "offender",
    [
        "weight_kg=82",  # the pair, not the key — the exact leak this prevents
        "weight_kg: 82",
        "82",
        "Weight_kg",  # capitals are not a column name in this schema
        "weight kg",
        "weight-kg",
        "'weight_kg'",
        "{'weight_kg': 82}",
        "",
        "_leading",
        "x" * 64,  # beyond a plausible identifier; a value in disguise
    ],
)
def test_anything_that_is_not_a_field_name_is_refused(offender: str) -> None:
    """🔒 A clinical value here would sit in a table with weaker access rules and
    a seven-year retention period. The check refuses rather than sanitises: a
    caller passing `weight_kg=82` has a bug at the call site, and quietly
    trimming it to `weight_kg` would hide the bug while the next such value —
    shaped slightly differently — gets through."""
    with pytest.raises(AuditIntegrityError, match="not a field name"):
        validate_changed_fields([offender])


def test_a_non_string_entry_is_refused() -> None:
    """The likeliest mistake is passing a dict of before/after pairs."""
    with pytest.raises(AuditIntegrityError):
        validate_changed_fields([{"weight_kg": 82}])  # type: ignore[list-item]


def test_the_error_says_what_to_pass_instead() -> None:
    """Every occurrence has the same fix, so the message carries it."""
    with pytest.raises(AuditIntegrityError) as raised:
        validate_changed_fields(["weight_kg=82"])
    assert "Pass ['weight_kg']" in str(raised.value)


def test_a_bulk_rewrite_keeps_the_cap_not_the_list() -> None:
    """Beyond the cap the entry is recording a bulk operation. An unbounded
    array in every row is a storage problem, and nobody reads 400 names."""
    validated = validate_changed_fields([f"field_{index}" for index in range(200)])
    assert len(validated) == 64


# ─── Metadata allowlist (DB §15.3) ───────────────────────────────────────


def test_an_action_extends_the_allowlist_without_editing_the_kernel() -> None:
    """A module adds its own keys; the kernel does not enumerate modules."""
    action = _action(audit_metadata_keys=frozenset({"stage", "previous_stage"}))
    allowed = allowed_metadata_keys(action)
    assert {"stage", "previous_stage"} <= allowed
    assert allowed >= KERNEL_METADATA_KEYS


def test_the_kernel_allowlist_applies_when_no_action_is_known() -> None:
    assert allowed_metadata_keys(None) == KERNEL_METADATA_KEYS


def test_unlisted_metadata_keys_are_dropped() -> None:
    """🔒 An allowlist, not a denylist: a denylist fails open on whatever its
    author did not foresee, and the cost of that failure is a compliance
    breach. Dropping silently is deliberate — a lost key is a smaller problem
    than a leaked value, and the allowlist is reviewable in one place."""
    entry = build_entry(
        action_name="client.update",
        resource_type="client",
        outcome=AuditOutcome.ALLOWED,
        context=_practitioner_context(),
        metadata={"reason": "stage_change", "client_name": "Priya", "weight_kg": 82},
    )
    assert entry.metadata == {"reason": "stage_change"}


def test_the_kernel_allowlist_carries_no_identifying_key() -> None:
    """⚠️ A guard on the allowlist itself. Every key here is an identifier, a
    status or a count; adding a name-shaped one would leak into every row
    written from then on, and no other test would notice."""
    forbidden = {"name", "full_name", "email", "phone", "mobile", "address", "notes", "value"}
    assert KERNEL_METADATA_KEYS.isdisjoint(forbidden)


# ─── Building an entry ───────────────────────────────────────────────────


def test_attribution_comes_from_the_context_not_the_caller() -> None:
    """🔒 A caller-supplied actor is an audit-forgery vector. The record exists
    precisely because it cannot be shaped by the code being audited, so there is
    no parameter through which to shape it."""
    entry = build_entry(
        action_name="client.update",
        resource_type="client",
        outcome=AuditOutcome.ALLOWED,
        context=_practitioner_context(),
        resource_id=RESOURCE,
        changed_fields=["stage"],
    )
    assert entry.actor_id == USER_1
    assert entry.tenant_id == TENANT_A
    assert entry.actor_type is ActorType.PRACTITIONER
    assert entry.actor_realm is AuthRealm.PRACTITIONER
    assert entry.request_id == "req_test"
    assert entry.resource_id == RESOURCE
    assert entry.changed_fields == ("stage",)


def test_the_timestamp_is_timezone_aware_utc() -> None:
    """🔒 NFR-099. A trail spanning timezones must be orderable without knowing
    which one each row was written in."""
    entry = build_entry(
        action_name="client.update",
        resource_type="client",
        outcome=AuditOutcome.ALLOWED,
        context=_practitioner_context(),
    )
    assert entry.occurred_at.tzinfo is not None
    assert entry.occurred_at.utcoffset() == UTC.utcoffset(None)


def test_an_explicit_timestamp_is_honoured() -> None:
    """The worker replays jobs; "when it happened" is not always "now"."""
    moment = datetime(2026, 8, 5, 12, 30, tzinfo=UTC)
    entry = build_entry(
        action_name="job.run",
        resource_type="job",
        outcome=AuditOutcome.ALLOWED,
        context=_practitioner_context(),
        occurred_at=moment,
    )
    assert entry.occurred_at == moment


def test_a_value_in_changed_fields_stops_the_entry_being_built() -> None:
    """Validation is not a later step that something could skip."""
    with pytest.raises(AuditIntegrityError):
        build_entry(
            action_name="client.update",
            resource_type="client",
            outcome=AuditOutcome.ALLOWED,
            context=_practitioner_context(),
            changed_fields=["weight_kg=82"],
        )


def test_an_entry_is_frozen() -> None:
    """There is no supported way to alter a written entry, and none should be
    reachable by mistake."""
    entry = build_entry(
        action_name="client.update",
        resource_type="client",
        outcome=AuditOutcome.ALLOWED,
        context=_practitioner_context(),
    )
    with pytest.raises((AttributeError, TypeError)):
        entry.outcome = AuditOutcome.DENIED  # type: ignore[misc]


# ─── Denials (FR-M0-033) ─────────────────────────────────────────────────


def test_a_denial_records_the_reason_the_caller_never_sees() -> None:
    """🔒 The reason is the whole value of the entry, and it is written here
    rather than returned: telling a client which rule refused them is a probing
    aid (API §5.4). The caller gets a generic 403."""
    entry = build_denial_entry(
        action_name="client.read",
        resource_type="client",
        context=_practitioner_context(),
        decision=deny("not_assigned_to_actor"),
        resource_id=RESOURCE,
    )
    assert entry.outcome is AuditOutcome.DENIED
    assert entry.metadata == {"reason": "not_assigned_to_actor"}
    assert entry.resource_id == RESOURCE


def test_denied_and_failed_are_distinct_outcomes() -> None:
    """ "You may not" and "it broke" have different investigations. Collapsing
    them turns a security signal into an error-rate statistic."""
    assert {o.value for o in AuditOutcome} == {"allowed", "denied", "failed"}
    # Looked up by value rather than compared as literals, so the assertion is
    # about the enum's members rather than about two constants mypy can already
    # see are different.
    assert AuditOutcome("denied") is not AuditOutcome("failed")


# ─── What gets audited ───────────────────────────────────────────────────


@pytest.mark.parametrize("outcome", [AuditOutcome.DENIED, AuditOutcome.FAILED])
def test_every_denial_and_failure_is_recorded(outcome: AuditOutcome) -> None:
    """🔒 Even for a read. These are the entries an investigation starts from,
    and a log that is quietest when something is wrong is worthless."""
    action = _action(is_read=True)
    assert should_audit(action, outcome, _practitioner_context().actor)


def test_every_mutation_is_recorded() -> None:
    assert should_audit(_action(is_read=False), AuditOutcome.ALLOWED, _practitioner_context().actor)


def test_an_ordinary_read_is_not_recorded() -> None:
    """⚠️ Volume would bury the signal, and cost more storage than the data being
    audited. A practitioner reading their own client is the system working."""
    action = _action(is_read=True)
    assert not should_audit(action, AuditOutcome.ALLOWED, _practitioner_context().actor)


@pytest.mark.parametrize(
    "scope",
    [DataScope.TENANT_METADATA, DataScope.AGGREGATE, DataScope.TENANT_PII],
)
def test_operator_reads_of_tenant_data_are_audited(scope: DataScope) -> None:
    """🔒 FR-M0-032. Operators are the only actors whose reads are recorded,
    because they are the only actors reading across a boundary they do not own.
    This is the trail that answers a DPDP enquiry about platform staff access."""
    action = _action(data_scope=scope, operator_access=True, is_read=True)
    assert should_audit(action, AuditOutcome.ALLOWED, _operator_context().actor)


def test_operator_reads_of_platform_data_are_not_audited() -> None:
    """Our own subscriptions and job runs are not a tenant's data. Recording
    every operator glance at them is the volume problem without the signal."""
    action = _action(data_scope=DataScope.PLATFORM, operator_access=True, is_read=True)
    assert not should_audit(action, AuditOutcome.ALLOWED, _operator_context().actor)


def test_an_unregistered_action_reaching_the_pipeline_is_recorded() -> None:
    """Deny-by-default means it is about to be refused anyway, and an
    unregistered action arriving at all is itself worth a row."""
    assert should_audit(None, AuditOutcome.ALLOWED, _practitioner_context().actor)


# ─── IP hashing (NFR-033) ────────────────────────────────────────────────


def test_a_missing_address_hashes_to_nothing() -> None:
    assert hash_ip(None, salt="s") is None
    assert hash_ip("", salt="s") is None


def test_hashing_is_stable_for_the_same_address_and_salt() -> None:
    """Audit needs "was this the same source?" and nothing more."""
    assert hash_ip("203.0.113.7", salt="pepper") == hash_ip("203.0.113.7", salt="pepper")


def test_different_addresses_hash_differently() -> None:
    assert hash_ip("203.0.113.7", salt="pepper") != hash_ip("203.0.113.8", salt="pepper")


def test_the_salt_changes_the_digest() -> None:
    """⚠️ The whole IPv4 space is 2^32 — a few seconds of brute force — so an
    unsalted hash is not a hash, it is an encoding. The salt must be a
    deployment secret and stable for the retention period."""
    assert hash_ip("203.0.113.7", salt="a") != hash_ip("203.0.113.7", salt="b")


def test_the_digest_carries_no_recoverable_address() -> None:
    digest = hash_ip("203.0.113.7", salt="pepper")
    assert digest is not None
    assert "203.0.113.7" not in digest
    assert len(digest) == 32


# ─── The sink port ───────────────────────────────────────────────────────


async def test_the_in_memory_sink_collects_entries_in_order() -> None:
    sink = InMemoryAuditSink()
    for name in ("client.create", "client.update"):
        await sink.write(
            build_entry(
                action_name=name,
                resource_type="client",
                outcome=AuditOutcome.ALLOWED,
                context=_practitioner_context(),
            )
        )
    assert sink.actions() == ["client.create", "client.update"]


def test_the_sink_port_offers_no_way_to_alter_an_entry() -> None:
    """🔒 There is no update or delete on the interface, so no caller can reach
    for one by mistake. The database enforces the same thing by grant."""
    assert not {name for name in dir(InMemoryAuditSink) if name in {"update", "delete", "remove"}}
    assert hasattr(AuditEntry, "__slots__")
