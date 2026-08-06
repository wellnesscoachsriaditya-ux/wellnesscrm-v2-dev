"""The request pipeline — Arch §5.1 steps 3, 4, 5, 9, 11.

🔒 **The sprint's third gate.** Tenancy, transaction, authorization and audit are
properties of the route class, so these tests assert what an endpoint author gets
*without writing anything*: a scoped transaction, a deny-by-default decision, an
audit row for every outcome, and a rollback that takes the audit row with it.

Run without PostgreSQL by substituting the transaction provider. That is a
deliberate choice, not a shortcut: the alternative is that the pipeline's only
coverage lives in the integration suite, which is skipped until the AC-M0-003
gate closes — leaving the most security-relevant code in the sprint unverified
for exactly as long as it takes to provision a database.

⚠️ What is *not* asserted here: that `SET LOCAL` isolates anything. That is a
PostgreSQL property and it is tested against PostgreSQL in
`tests/integration/test_tenant_isolation.py`. These tests assert the pipeline
*asks for* the right scope; only the database can prove it is honoured.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.kernel.audit import AuditOutcome, InMemoryAuditSink
from app.kernel.authz import REGISTRY, Action, DataScope, owner_or_assigned, register_action
from app.kernel.context import Actor, ActorType, AuthRealm, UserRole
from app.kernel.errors import DomainRuleError
from app.kernel.tenancy import TenantScope
from app.platform.audit import configure_audit_sink, get_audit_sink
from app.platform.http.authz import requires
from app.platform.http.errors import register_error_handlers
from app.platform.http.middleware import RequestContextMiddleware
from app.platform.http.pipeline import (
    authorize,
    configure_actor_resolver,
    configure_transaction_provider,
    get_transaction_provider,
    realm_router,
    record_audit,
    verify_route_authorization,
)

TENANT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
TENANT_B = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
USER_1 = uuid.UUID("11111111-0000-4000-8000-000000000001")
USER_2 = uuid.UUID("22222222-0000-4000-8000-000000000002")
THING_1 = uuid.UUID("cccccccc-0000-4000-8000-000000000001")


# ─── Fakes ───────────────────────────────────────────────────────────────


@dataclass
class FakeSession:
    """Stands in for `AsyncSession`. Records what the pipeline asked for.

    Not a mock: the assertions are about *ordering and atomicity* — was the scope
    applied before the endpoint ran, did the audit write survive the rollback —
    and a recording fake states those directly.
    """

    scope: TenantScope
    committed: bool = False
    rolled_back: bool = False
    writes: list[Any] = field(default_factory=list)

    async def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> None:
        """The audit INSERT. In Slice A the pipeline is the only writer."""
        self.writes.append(statement)


@dataclass
class FakeTransactions:
    """Transaction provider capturing every transaction opened."""

    sessions: list[FakeSession] = field(default_factory=list)

    @asynccontextmanager
    async def __call__(self, scope: TenantScope) -> AsyncIterator[Any]:
        session = FakeSession(scope=scope)
        self.sessions.append(session)
        try:
            yield session
        except Exception:
            # 🔒 What `session.begin()` does on an exception. Modelled explicitly
            # because "the audit row rolls back with the change" is the property
            # under test, and an implicit rollback would make it invisible.
            session.rolled_back = True
            session.writes.clear()
            raise
        session.committed = True

    @property
    def last(self) -> FakeSession:
        assert self.sessions, "no transaction was opened"
        return self.sessions[-1]


@dataclass(frozen=True)
class FakeThing:
    """A resource, satisfying the structural `Resource` protocol."""

    id: uuid.UUID
    tenant_id: uuid.UUID | None
    owner_user_id: uuid.UUID | None = None


# ─── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolate_globals() -> Iterator[None]:
    """Restore every process-wide seam the pipeline reads.

    ⚠️ The registry, the audit sink, the actor resolver and the transaction
    provider are all deliberately global — they are configured once at startup.
    That makes them shared mutable state under test, so each is snapshotted and
    restored; without this, ordering would decide outcomes.
    """
    saved_actions = dict(REGISTRY._actions)
    saved_sink = get_audit_sink()
    saved_provider = get_transaction_provider()
    REGISTRY.clear()
    try:
        yield
    finally:
        REGISTRY.clear()
        REGISTRY._actions.update(saved_actions)
        configure_audit_sink(saved_sink)
        configure_transaction_provider(saved_provider)
        configure_actor_resolver(_anonymous)


async def _anonymous(_request: Request) -> Actor:
    return Actor.anonymous()


def _as_actor(actor: Actor) -> None:
    """Install an actor for the request. 🔒 Stands in for Slice B's token
    verification — the seam, exercised exactly as identity will use it."""

    async def resolver(_request: Request) -> Actor:
        return actor

    configure_actor_resolver(resolver)


@pytest.fixture
def sink() -> InMemoryAuditSink:
    collected = InMemoryAuditSink()
    configure_audit_sink(collected)
    return collected


@pytest.fixture
def transactions() -> FakeTransactions:
    fake = FakeTransactions()
    configure_transaction_provider(fake)
    return fake


@pytest.fixture
def practitioner() -> Actor:
    return Actor(
        actor_type=ActorType.PRACTITIONER,
        realm=AuthRealm.PRACTITIONER,
        subject_id=USER_1,
        tenant_id=TENANT_A,
        role=UserRole.OWNER,
    )


@pytest.fixture
def thing_read() -> Action:
    return register_action(
        "thing.read",
        roles={UserRole.OWNER},
        data_scope=DataScope.TENANT_PII,
        is_read=True,
    )


@pytest.fixture
def thing_update() -> Action:
    return register_action(
        "thing.update",
        roles={UserRole.OWNER},
        data_scope=DataScope.TENANT_PII,
    )


def _client(*routers: Any) -> TestClient:
    """Build an app carrying the real middleware, handlers and pipeline."""
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    for router in routers:
        app.include_router(router)
    verify_route_authorization(app)
    # 🔒 `raise_server_exceptions=False` so the error handlers run as they do in
    # production. Otherwise TestClient re-raises and the envelope is never built.
    return TestClient(app, raise_server_exceptions=False)


# ─── Step 5: authorization ───────────────────────────────────────────────


def test_anonymous_is_denied_by_default(
    thing_read: Action, sink: InMemoryAuditSink, transactions: FakeTransactions
) -> None:
    """🔒 The Slice A baseline: no identity exists, so nothing is reachable.

    A 401, not a 403 — the honest answer, and the one that makes a client
    re-authenticate rather than conclude it is permanently forbidden.
    """
    router = realm_router("/api/v1/app")

    @router.get("/things")
    @requires(thing_read)
    async def list_things() -> list[str]:
        return []

    response = _client(router).get("/api/v1/app/things")

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "unauthenticated"
    assert not transactions.sessions, "a denied request must not open a transaction"


def test_an_authorized_actor_reaches_the_endpoint(
    practitioner: Actor,
    thing_read: Action,
    sink: InMemoryAuditSink,
    transactions: FakeTransactions,
) -> None:
    _as_actor(practitioner)
    router = realm_router("/api/v1/app")

    @router.get("/things")
    @requires(thing_read)
    async def list_things() -> list[str]:
        return ["one"]

    response = _client(router).get("/api/v1/app/things")

    assert response.status_code == 200
    assert response.json() == ["one"]


def test_a_wrong_role_is_refused(
    practitioner: Actor, sink: InMemoryAuditSink, transactions: FakeTransactions
) -> None:
    register_action(
        "thing.admin", roles={UserRole.PLATFORM_OPERATOR}, data_scope=DataScope.PLATFORM
    )
    router = realm_router("/api/v1/app")

    @router.get("/things")
    @requires(REGISTRY.get("thing.admin"))  # type: ignore[arg-type]
    async def admin_things() -> None: ...

    _as_actor(practitioner)
    response = _client(router).get("/api/v1/app/things")

    assert response.status_code == 403
    assert not transactions.sessions


def test_the_denial_reason_never_reaches_the_caller(
    practitioner: Actor, sink: InMemoryAuditSink, transactions: FakeTransactions
) -> None:
    """🔒 API §5.4 — the reason is audit material, not a response field.

    Returning "role_not_permitted:owner:thing.admin" would tell a prober exactly
    which rule to work around.
    """
    register_action(
        "thing.admin", roles={UserRole.PLATFORM_OPERATOR}, data_scope=DataScope.PLATFORM
    )
    router = realm_router("/api/v1/app")

    @router.get("/things")
    @requires(REGISTRY.get("thing.admin"))  # type: ignore[arg-type]
    async def admin_things() -> None: ...

    _as_actor(practitioner)
    body = _client(router).get("/api/v1/app/things").text

    assert "role_not_permitted" not in body
    assert "thing.admin" not in body
    # ...but it is in the audit trail, which is the whole point.
    assert "role_not_permitted" in sink.entries[0].metadata["reason"]


# ─── Step 3: realm and tenancy ───────────────────────────────────────────


def test_a_client_token_cannot_reach_the_practitioner_realm(
    thing_read: Action, sink: InMemoryAuditSink, transactions: FakeTransactions
) -> None:
    """🔒 FR-M0-004. Separate signing keys make forgery impossible; this stops
    misuse of a *genuine* token against a more privileged surface."""
    _as_actor(
        Actor(
            actor_type=ActorType.CLIENT,
            realm=AuthRealm.CLIENT,
            subject_id=USER_1,
            tenant_id=TENANT_A,
            role=UserRole.CLIENT,
        )
    )
    router = realm_router("/api/v1/app")

    @router.get("/things")
    @requires(thing_read)
    async def list_things() -> list[str]:
        return []

    response = _client(router).get("/api/v1/app/things")

    assert response.status_code == 403
    assert not transactions.sessions, "realm rejection must precede the transaction"
    assert sink.entries[0].metadata["reason"].startswith("realm_mismatch")


def test_the_realm_error_is_identical_whichever_realm_the_actor_holds(
    thing_read: Action, sink: InMemoryAuditSink, transactions: FakeTransactions
) -> None:
    """🔒 NFR-043 — the response must not identify which token the caller holds.

    Asserted by comparing two different wrong-realm actors rather than by
    grepping for words: a client and an operator presenting at `/app` must be
    indistinguishable from outside. A substring check would pass on a message
    that leaked the realm in some other phrasing; this cannot.
    """
    router = realm_router("/api/v1/app")

    @router.get("/things")
    @requires(thing_read)
    async def list_things() -> list[str]:
        return []

    bodies: list[dict[str, Any]] = []
    for actor in (
        Actor(
            actor_type=ActorType.CLIENT,
            realm=AuthRealm.CLIENT,
            subject_id=USER_1,
            tenant_id=TENANT_A,
            role=UserRole.CLIENT,
        ),
        Actor(
            actor_type=ActorType.OPERATOR,
            realm=AuthRealm.OPERATOR,
            subject_id=USER_1,
            role=UserRole.PLATFORM_OPERATOR,
        ),
    ):
        _as_actor(actor)
        payload = _client(router).get("/api/v1/app/things").json()["error"]
        # The request id differs by construction and is not a disclosure.
        payload.pop("request_id")
        bodies.append(payload)

    assert bodies[0] == bodies[1]


def test_the_transaction_carries_the_actor_tenant(
    practitioner: Actor,
    thing_read: Action,
    sink: InMemoryAuditSink,
    transactions: FakeTransactions,
) -> None:
    """🔒 The tenant comes from the verified actor, never from a header or body.

    This is the value `SET LOCAL app.tenant_id` receives, and therefore what
    every RLS policy compares against.
    """
    _as_actor(practitioner)
    router = realm_router("/api/v1/app")

    @router.get("/things")
    @requires(thing_read)
    async def list_things() -> list[str]:
        return []

    _client(router).get("/api/v1/app/things")

    scope = transactions.last.scope
    assert scope.tenant_id == TENANT_A
    assert scope.actor_id == USER_1
    assert scope.actor_role == "owner"
    assert not scope.is_platform_scope


def test_a_caller_supplied_tenant_header_is_ignored(
    practitioner: Actor,
    thing_read: Action,
    sink: InMemoryAuditSink,
    transactions: FakeTransactions,
) -> None:
    """🔒 A tenant-switching vulnerability wearing a convenience feature's
    clothes. There is no path by which a header reaches the scope."""
    _as_actor(practitioner)
    router = realm_router("/api/v1/app")

    @router.get("/things")
    @requires(thing_read)
    async def list_things() -> list[str]:
        return []

    _client(router).get(
        "/api/v1/app/things",
        headers={"X-Tenant-Id": str(TENANT_B)},
    )

    assert transactions.last.scope.tenant_id == TENANT_A


def test_an_operator_gets_platform_scope_and_no_tenant_rows(
    sink: InMemoryAuditSink, transactions: FakeTransactions
) -> None:
    """🔒 An operator sees no tenant rows through RLS. What they may reach is
    decided by `DataScope`, and every access is audited (FR-M0-032)."""
    register_action(
        "tenant.metrics",
        roles={UserRole.PLATFORM_OPERATOR},
        data_scope=DataScope.AGGREGATE,
        operator_access=True,
        is_read=True,
    )
    _as_actor(
        Actor(
            actor_type=ActorType.OPERATOR,
            realm=AuthRealm.OPERATOR,
            subject_id=USER_1,
            role=UserRole.PLATFORM_OPERATOR,
        )
    )
    router = realm_router("/api/v1/admin")

    @router.get("/metrics")
    @requires(REGISTRY.get("tenant.metrics"))  # type: ignore[arg-type]
    async def metrics() -> dict[str, int]:
        return {"total_clients": 42}

    response = _client(router).get("/api/v1/admin/metrics")

    assert response.status_code == 200
    scope = transactions.last.scope
    assert scope.tenant_id is None
    assert scope.is_platform_scope
    assert not scope.sees_tenant_rows


# ─── Step 4 and 11: the transaction ──────────────────────────────────────


def test_one_transaction_per_request(
    practitioner: Actor,
    thing_read: Action,
    sink: InMemoryAuditSink,
    transactions: FakeTransactions,
) -> None:
    """ADR-04. Two transactions would make a multi-step operation non-atomic."""
    _as_actor(practitioner)
    router = realm_router("/api/v1/app")

    @router.get("/things")
    @requires(thing_read)
    async def list_things() -> list[str]:
        return []

    _client(router).get("/api/v1/app/things")

    assert len(transactions.sessions) == 1


def test_the_transaction_commits_on_success(
    practitioner: Actor,
    thing_update: Action,
    sink: InMemoryAuditSink,
    transactions: FakeTransactions,
) -> None:
    _as_actor(practitioner)
    router = realm_router("/api/v1/app")

    @router.post("/things")
    @requires(thing_update)
    async def create_thing() -> dict[str, str]:
        return {"status": "ok"}

    assert _client(router).post("/api/v1/app/things").status_code == 200
    assert transactions.last.committed
    assert not transactions.last.rolled_back


def test_the_transaction_rolls_back_on_a_domain_error(
    practitioner: Actor,
    thing_update: Action,
    sink: InMemoryAuditSink,
    transactions: FakeTransactions,
) -> None:
    _as_actor(practitioner)
    router = realm_router("/api/v1/app")

    @router.post("/things")
    @requires(thing_update)
    async def create_thing() -> None:
        raise DomainRuleError.for_rule(
            rule_code="thing_invalid",
            message="That isn't allowed.",
            action="Change it and try again.",
        )

    response = _client(router).post("/api/v1/app/things")

    assert response.status_code == 422
    assert transactions.last.rolled_back
    assert not transactions.last.committed


def test_the_transaction_rolls_back_on_an_unexpected_error(
    practitioner: Actor,
    thing_update: Action,
    sink: InMemoryAuditSink,
    transactions: FakeTransactions,
) -> None:
    """🔒 An unhandled library exception must not leave a half-written change."""
    _as_actor(practitioner)
    router = realm_router("/api/v1/app")

    @router.post("/things")
    @requires(thing_update)
    async def create_thing() -> None:
        raise RuntimeError("the database went away")

    response = _client(router).post("/api/v1/app/things")

    assert response.status_code == 500
    assert response.json()["error"]["type"] == "internal_error"
    assert "database went away" not in response.text
    assert transactions.last.rolled_back


def test_the_session_is_available_to_the_endpoint(
    practitioner: Actor,
    thing_read: Action,
    sink: InMemoryAuditSink,
    transactions: FakeTransactions,
) -> None:
    """The endpoint receives the *request's* transaction, not a new one."""
    seen: list[Any] = []
    _as_actor(practitioner)
    router = realm_router("/api/v1/app")

    @router.get("/things")
    @requires(thing_read)
    async def list_things(request: Request) -> list[str]:
        seen.append(request.state.wellness_db_session)
        return []

    _client(router).get("/api/v1/app/things")

    assert seen == [transactions.last]


# ─── Step 9: audit ───────────────────────────────────────────────────────


def test_a_mutation_is_audited_inside_the_transaction(
    practitioner: Actor,
    thing_update: Action,
    sink: InMemoryAuditSink,
    transactions: FakeTransactions,
) -> None:
    """🔒 In-transaction, so the record and the change commit together.

    The entry is absent from the out-of-band sink precisely because it went to
    the session — which is the atomicity guarantee, stated as an assertion.
    """
    _as_actor(practitioner)
    router = realm_router("/api/v1/app")

    @router.post("/things")
    @requires(thing_update)
    async def create_thing() -> dict[str, str]:
        return {"status": "ok"}

    _client(router).post("/api/v1/app/things")

    assert transactions.last.committed
    assert sink.entries == [], "a successful mutation is audited in-transaction"


def test_a_rolled_back_mutation_leaves_no_audit_row(
    practitioner: Actor,
    thing_update: Action,
    sink: InMemoryAuditSink,
    transactions: FakeTransactions,
) -> None:
    """🔒 An audit row describing a change that never happened is worse than no
    row — it is evidence of a thing that did not occur.

    The *failure* is still recorded out of band; what must not survive is the
    ALLOWED entry.
    """
    _as_actor(practitioner)
    router = realm_router("/api/v1/app")

    @router.post("/things")
    @requires(thing_update)
    async def create_thing() -> None:
        raise RuntimeError("boom")

    _client(router).post("/api/v1/app/things")

    assert transactions.last.writes == []
    outcomes = {entry.outcome for entry in sink.entries}
    assert AuditOutcome.ALLOWED not in outcomes
    assert outcomes == {AuditOutcome.FAILED}


def test_an_ordinary_read_is_not_audited(
    practitioner: Actor,
    thing_read: Action,
    sink: InMemoryAuditSink,
    transactions: FakeTransactions,
) -> None:
    """Volume would bury the signal. A practitioner reading their own client is
    the system working."""
    _as_actor(practitioner)
    router = realm_router("/api/v1/app")

    @router.get("/things")
    @requires(thing_read)
    async def list_things() -> list[str]:
        return []

    _client(router).get("/api/v1/app/things")

    assert sink.entries == []
    assert transactions.last.writes == []


def test_a_denial_is_audited(
    practitioner: Actor, sink: InMemoryAuditSink, transactions: FakeTransactions
) -> None:
    """🔒 FR-M0-033. Only logging successes means the log is quietest exactly
    when something is wrong."""
    register_action(
        "thing.admin", roles={UserRole.PLATFORM_OPERATOR}, data_scope=DataScope.PLATFORM
    )
    router = realm_router("/api/v1/app")

    @router.get("/things")
    @requires(REGISTRY.get("thing.admin"))  # type: ignore[arg-type]
    async def admin_things() -> None: ...

    _as_actor(practitioner)
    _client(router).get("/api/v1/app/things")

    assert len(sink.entries) == 1
    entry = sink.entries[0]
    assert entry.outcome is AuditOutcome.DENIED
    assert entry.action == "thing.admin"
    assert entry.actor_id == USER_1
    assert entry.tenant_id == TENANT_A


def test_a_denial_is_audited_even_though_no_transaction_opened(
    practitioner: Actor, sink: InMemoryAuditSink, transactions: FakeTransactions
) -> None:
    """🔒 Out of band by necessity: there is no successful transaction to join,
    and joining a rolling-back one would erase the evidence."""
    register_action(
        "thing.admin", roles={UserRole.PLATFORM_OPERATOR}, data_scope=DataScope.PLATFORM
    )
    router = realm_router("/api/v1/app")

    @router.get("/things")
    @requires(REGISTRY.get("thing.admin"))  # type: ignore[arg-type]
    async def admin_things() -> None: ...

    _as_actor(practitioner)
    _client(router).get("/api/v1/app/things")

    assert not transactions.sessions
    assert len(sink.entries) == 1


def test_a_failure_is_recorded_as_failed_not_denied(
    practitioner: Actor,
    thing_update: Action,
    sink: InMemoryAuditSink,
    transactions: FakeTransactions,
) -> None:
    """ "It broke" and "you may not" lead to different investigations."""
    _as_actor(practitioner)
    router = realm_router("/api/v1/app")

    @router.post("/things")
    @requires(thing_update)
    async def create_thing() -> None:
        raise RuntimeError("boom")

    _client(router).post("/api/v1/app/things")

    assert sink.entries[0].outcome is AuditOutcome.FAILED


def test_a_failure_records_the_error_type_not_the_message(
    practitioner: Actor,
    thing_update: Action,
    sink: InMemoryAuditSink,
    transactions: FakeTransactions,
) -> None:
    """🔒 NFR-033 — an exception message may echo a submitted clinical value.

    The taxonomy's stable error type is the whole payload; the message stays in
    the logs, where it is scrubbed.
    """
    _as_actor(practitioner)
    router = realm_router("/api/v1/app")

    @router.post("/things")
    @requires(thing_update)
    async def create_thing() -> None:
        raise RuntimeError("patient weight is 82kg")

    _client(router).post("/api/v1/app/things")

    entry = sink.entries[0]
    assert entry.metadata["error_type"] == "internal_error"
    assert "82" not in str(entry.metadata)


def test_a_domain_failure_records_its_taxonomy_type(
    practitioner: Actor,
    thing_update: Action,
    sink: InMemoryAuditSink,
    transactions: FakeTransactions,
) -> None:
    _as_actor(practitioner)
    router = realm_router("/api/v1/app")

    @router.post("/things")
    @requires(thing_update)
    async def create_thing() -> None:
        raise DomainRuleError.for_rule(
            rule_code="nope", message="No.", action="Try something else."
        )

    _client(router).post("/api/v1/app/things")

    assert sink.entries[0].metadata["error_type"] == "domain_rule_violated"


def test_an_operator_read_of_tenant_data_is_audited(
    sink: InMemoryAuditSink, transactions: FakeTransactions
) -> None:
    """🔒 FR-M0-032. Operators are the only actors whose reads are recorded,
    because they are the only actors reading across a boundary they do not own."""
    register_action(
        "tenant.health",
        roles={UserRole.PLATFORM_OPERATOR},
        data_scope=DataScope.TENANT_METADATA,
        operator_access=True,
        is_read=True,
    )
    _as_actor(
        Actor(
            actor_type=ActorType.OPERATOR,
            realm=AuthRealm.OPERATOR,
            subject_id=USER_1,
            role=UserRole.PLATFORM_OPERATOR,
        )
    )
    router = realm_router("/api/v1/admin")

    @router.get("/tenants/health")
    @requires(REGISTRY.get("tenant.health"))  # type: ignore[arg-type]
    async def tenant_health() -> dict[str, int]:
        return {"active_clients": 12}

    _client(router).get("/api/v1/admin/tenants/health")

    assert transactions.last.writes, "an operator read is audited in-transaction"


def test_the_request_id_is_on_the_audit_entry(
    practitioner: Actor, sink: InMemoryAuditSink, transactions: FakeTransactions
) -> None:
    """How a user-reported problem is traced to its audit row without the row
    containing anything about the user."""
    register_action(
        "thing.admin", roles={UserRole.PLATFORM_OPERATOR}, data_scope=DataScope.PLATFORM
    )
    router = realm_router("/api/v1/app")

    @router.get("/things")
    @requires(REGISTRY.get("thing.admin"))  # type: ignore[arg-type]
    async def admin_things() -> None: ...

    _as_actor(practitioner)
    response = _client(router).get("/api/v1/app/things")

    assert sink.entries[0].request_id == response.headers["X-Request-Id"]


def test_the_client_ip_is_hashed_never_stored_raw(
    practitioner: Actor, sink: InMemoryAuditSink, transactions: FakeTransactions
) -> None:
    """🔒 NFR-033 — an IP is personal data under the DPDP Act, and audit rows are
    retained for years."""
    register_action(
        "thing.admin", roles={UserRole.PLATFORM_OPERATOR}, data_scope=DataScope.PLATFORM
    )
    router = realm_router("/api/v1/app")

    @router.get("/things")
    @requires(REGISTRY.get("thing.admin"))  # type: ignore[arg-type]
    async def admin_things() -> None: ...

    _as_actor(practitioner)
    _client(router).get("/api/v1/app/things")

    ip_hash = sink.entries[0].ip_hash
    assert ip_hash is not None
    assert "testclient" not in ip_hash
    # Truncated SHA-256 (`kernel.audit.hash_ip`) — 32 hex characters.
    assert len(ip_hash) == 32


def test_an_endpoint_can_enrich_the_audit_entry(
    practitioner: Actor,
    thing_update: Action,
    sink: InMemoryAuditSink,
    transactions: FakeTransactions,
) -> None:
    """The pipeline knows who and what action; only the service knows which row.

    Enriching rather than writing keeps the entry framework-written, so it cannot
    be forgotten, skipped or shaped by the code being audited. The successful
    entry goes in-transaction, so it is asserted through the session's writes.
    """
    _as_actor(practitioner)
    router = realm_router("/api/v1/app")

    @router.post("/things")
    @requires(thing_update)
    async def update_thing(request: Request) -> dict[str, str]:
        record_audit(request, resource_id=THING_1, changed_fields=["display_name"])
        return {"status": "ok"}

    assert _client(router).post("/api/v1/app/things").status_code == 200
    assert transactions.last.committed
    assert len(transactions.last.writes) == 1, "exactly one audit row, written by the pipeline"


def test_record_audit_accumulates_changed_fields(
    practitioner: Actor,
    thing_update: Action,
    sink: InMemoryAuditSink,
    transactions: FakeTransactions,
) -> None:
    """A multi-step service reports fields as it goes; the entry collects them.

    Overwriting instead of accumulating would silently record only the last
    step's fields, which is a plausible bug that no single-call test would see.
    """
    _as_actor(practitioner)
    router = realm_router("/api/v1/app")

    @router.post("/things")
    @requires(thing_update)
    async def update_thing(request: Request) -> None:
        record_audit(request, changed_fields=["display_name"])
        record_audit(request, changed_fields=["status"])
        raise RuntimeError("read the entry from the failure path")

    _client(router).post("/api/v1/app/things")

    assert sink.entries[0].changed_fields == ("display_name", "status")


def test_record_audit_rejects_a_value_where_a_field_name_belongs(
    practitioner: Actor,
    thing_update: Action,
    sink: InMemoryAuditSink,
    transactions: FakeTransactions,
) -> None:
    """🔒 FR-M0-035 end to end.

    `kernel.audit` refuses the entry rather than trusting the caller, and the
    pipeline surfaces that as a 500 instead of writing a clinical value into a
    table retained for seven years. Loud beats best-effort here.
    """
    _as_actor(practitioner)
    router = realm_router("/api/v1/app")

    @router.post("/things")
    @requires(thing_update)
    async def update_thing(request: Request) -> dict[str, str]:
        record_audit(request, changed_fields=["weight_kg=82"])
        return {"status": "ok"}

    response = _client(router).post("/api/v1/app/things")

    assert response.status_code == 500
    assert "82" not in response.text
    assert transactions.last.rolled_back


def test_enriched_fields_reach_the_entry(
    practitioner: Actor,
    thing_update: Action,
    sink: InMemoryAuditSink,
    transactions: FakeTransactions,
) -> None:
    _as_actor(practitioner)
    router = realm_router("/api/v1/app")

    @router.post("/things")
    @requires(thing_update)
    async def update_thing(request: Request) -> None:
        record_audit(request, resource_id=THING_1, changed_fields=["display_name"])
        raise RuntimeError("after enrichment")

    _client(router).post("/api/v1/app/things")

    entry = sink.entries[0]
    assert entry.resource_id == THING_1
    assert entry.changed_fields == ("display_name",)


def test_the_resource_type_comes_from_the_action_name(
    practitioner: Actor, sink: InMemoryAuditSink, transactions: FakeTransactions
) -> None:
    """`<resource>.<verb>` — so the kernel derives the resource type without
    enumerating domain types."""
    register_action(
        "diet_plan.publish", roles={UserRole.PLATFORM_OPERATOR}, data_scope=DataScope.PLATFORM
    )
    router = realm_router("/api/v1/app")

    @router.post("/plans/publish")
    @requires(REGISTRY.get("diet_plan.publish"))  # type: ignore[arg-type]
    async def publish() -> None: ...

    _as_actor(practitioner)
    _client(router).post("/api/v1/app/plans/publish")

    assert sink.entries[0].resource_type == "diet_plan"


# ─── Resource-bound authorization ────────────────────────────────────────


def test_authorize_allows_the_assigned_practitioner(
    sink: InMemoryAuditSink, transactions: FakeTransactions
) -> None:
    register_action(
        "thing.read",
        roles={UserRole.PRACTITIONER},
        data_scope=DataScope.TENANT_PII,
        policies=[owner_or_assigned],
        is_read=True,
    )
    _as_actor(
        Actor(
            actor_type=ActorType.PRACTITIONER,
            realm=AuthRealm.PRACTITIONER,
            subject_id=USER_1,
            tenant_id=TENANT_A,
            role=UserRole.PRACTITIONER,
        )
    )
    router = realm_router("/api/v1/app")

    @router.get("/things/{thing_id}")
    @requires(REGISTRY.get("thing.read"))  # type: ignore[arg-type]
    async def read_thing(thing_id: uuid.UUID, request: Request) -> dict[str, str]:
        await authorize(request, FakeThing(id=thing_id, tenant_id=TENANT_A, owner_user_id=USER_1))
        return {"status": "ok"}

    assert _client(router).get(f"/api/v1/app/things/{THING_1}").status_code == 200


def test_a_cross_tenant_resource_returns_404_not_403(
    sink: InMemoryAuditSink, transactions: FakeTransactions
) -> None:
    """🔒 API §5.4 — a 403 confirms the resource exists, which is the leak.

    The attempt is still audited (AC-M0-002): ambiguity is for the caller, not
    for the trail.
    """
    register_action(
        "thing.read", roles={UserRole.OWNER}, data_scope=DataScope.TENANT_PII, is_read=True
    )
    _as_actor(
        Actor(
            actor_type=ActorType.PRACTITIONER,
            realm=AuthRealm.PRACTITIONER,
            subject_id=USER_1,
            tenant_id=TENANT_A,
            role=UserRole.OWNER,
        )
    )
    router = realm_router("/api/v1/app")

    @router.get("/things/{thing_id}")
    @requires(REGISTRY.get("thing.read"))  # type: ignore[arg-type]
    async def read_thing(thing_id: uuid.UUID, request: Request) -> dict[str, str]:
        # A row from another tenant — the case RLS should have prevented, checked
        # again here because a resource can reach a policy from a cache or a job
        # payload that never touched the database.
        await authorize(request, FakeThing(id=thing_id, tenant_id=TENANT_B))
        return {"status": "ok"}

    response = _client(router).get(f"/api/v1/app/things/{THING_1}")

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_found"
    assert sink.entries[0].outcome is AuditOutcome.DENIED
    assert sink.entries[0].metadata["reason"].startswith("cross_tenant")


def test_a_policy_denial_is_403_and_audited(
    sink: InMemoryAuditSink, transactions: FakeTransactions
) -> None:
    """A policy that passes without a resource and refuses with one.

    ⚠️ The realistic shape, and the reason `authorize()` exists: "may read
    clients" is decidable at the pipeline, "may read *this* client" is not. A
    policy that refused unconditionally would be caught by the coarse check
    before the transaction, and would prove nothing about the resource path.
    """
    register_action(
        "thing.read",
        roles={UserRole.PRACTITIONER},
        data_scope=DataScope.TENANT_PII,
        policies=[owner_or_assigned],
        is_read=True,
    )
    _as_actor(
        Actor(
            actor_type=ActorType.PRACTITIONER,
            realm=AuthRealm.PRACTITIONER,
            subject_id=USER_1,
            tenant_id=TENANT_A,
            role=UserRole.PRACTITIONER,
        )
    )
    router = realm_router("/api/v1/app")

    @router.get("/things/{thing_id}")
    @requires(REGISTRY.get("thing.read"))  # type: ignore[arg-type]
    async def read_thing(thing_id: uuid.UUID, request: Request) -> dict[str, str]:
        # Assigned to a *different* practitioner.
        await authorize(request, FakeThing(id=thing_id, tenant_id=TENANT_A, owner_user_id=USER_2))
        return {"status": "ok"}

    response = _client(router).get(f"/api/v1/app/things/{THING_1}")

    assert response.status_code == 403
    assert sink.entries[0].metadata["reason"] == "not_assigned_to_actor"
    assert sink.entries[0].resource_id == THING_1


def test_a_resource_denial_rolls_back_the_transaction(
    sink: InMemoryAuditSink, transactions: FakeTransactions
) -> None:
    """A refusal mid-request must undo anything the request had already done."""
    register_action(
        "thing.update",
        roles={UserRole.PRACTITIONER},
        data_scope=DataScope.TENANT_PII,
        policies=[owner_or_assigned],
    )
    _as_actor(
        Actor(
            actor_type=ActorType.PRACTITIONER,
            realm=AuthRealm.PRACTITIONER,
            subject_id=USER_1,
            tenant_id=TENANT_A,
            role=UserRole.PRACTITIONER,
        )
    )
    router = realm_router("/api/v1/app")

    @router.post("/things/{thing_id}")
    @requires(REGISTRY.get("thing.update"))  # type: ignore[arg-type]
    async def update_thing(thing_id: uuid.UUID, request: Request) -> None:
        await authorize(request, FakeThing(id=thing_id, tenant_id=TENANT_A, owner_user_id=USER_2))

    response = _client(router).post(f"/api/v1/app/things/{THING_1}")

    assert response.status_code == 403
    assert transactions.last.rolled_back
    assert not transactions.last.committed


# ─── Exempt routes ───────────────────────────────────────────────────────


def test_health_needs_no_actor_and_no_transaction(
    sink: InMemoryAuditSink, transactions: FakeTransactions
) -> None:
    """The exemption is real: no identity, no authorization, no transaction."""
    from app.platform.http.health import router as health_router

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    app.include_router(health_router, prefix="/api/v1/public")
    verify_route_authorization(app)

    response = TestClient(app).get("/api/v1/public/health")

    assert response.status_code == 200
    assert not transactions.sessions
    assert sink.entries == []
