"""Credentials and actor resolution.

🔒 Two surfaces, both on the authentication path and both easy to get subtly
wrong:

* **The credential store** — where the enumeration defences live. The property
  that matters is not what the response *says* but how long it takes to say it.
* **Actor resolution** — the seam Slice A left open, now closed. Every way a
  token can be unusable must produce an anonymous actor, because the pipeline's
  deny-by-default is what turns that into a refusal *and an audit entry*.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from starlette.datastructures import Headers
from starlette.requests import Request

from app.kernel.context import ActorType, AuthRealm, UserRole
from app.kernel.identity import (
    AccessClaims,
    CredentialError,
    CredentialSubject,
    issue_access_token,
)
from app.platform.identity.authentication import (
    bearer_token,
    configure_session_validator,
    get_session_validator,
    resolve_actor,
)
from app.platform.identity.credentials import (
    MIN_PASSWORD_LENGTH,
    LocalCredentialStore,
    normalise_email,
    password_rejection,
    raise_if_credentials_are_local,
)
from app.platform.identity.tokens import signing_key

SUBJECT = uuid.UUID("11111111-0000-4000-8000-000000000001")
TENANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
SESSION = uuid.UUID("55555555-0000-4000-8000-000000000005")

GOOD_PASSWORD = "correct-horse-battery"


# ─── Password rules ──────────────────────────────────────────────────────


def test_a_short_password_is_rejected() -> None:
    assert password_rejection("a" * (MIN_PASSWORD_LENGTH - 1)) is CredentialError.WEAK_PASSWORD


def test_a_long_but_common_password_is_rejected() -> None:
    """🔒 Length alone is not the rule. `password123` clears any length gate that
    is short enough to be usable, and is in every cracking dictionary."""
    assert password_rejection("password123") is CredentialError.WEAK_PASSWORD


def test_the_common_check_ignores_case() -> None:
    assert password_rejection("PassWord123") is CredentialError.WEAK_PASSWORD


def test_a_long_uncommon_password_passes() -> None:
    """No composition rules — forced symbols produce `Password1!` fleet-wide."""
    assert password_rejection(GOOD_PASSWORD) is None


# ─── Email canonicalisation ──────────────────────────────────────────────


def test_email_is_lowercased_and_trimmed() -> None:
    """🔒 One canonical form, or `Ada@Example.com` registers twice past the
    per-tenant unique index."""
    assert normalise_email("  Ada@Example.COM ") == "ada@example.com"


# ─── The local store ─────────────────────────────────────────────────────


@pytest.fixture
def store() -> LocalCredentialStore:
    return LocalCredentialStore()


async def test_an_account_can_be_created_and_verified(store: LocalCredentialStore) -> None:
    created = await store.create("ada@example.com", GOOD_PASSWORD)
    assert isinstance(created, CredentialSubject)

    verified = await store.verify("ada@example.com", GOOD_PASSWORD)
    assert isinstance(verified, CredentialSubject)
    assert verified.subject_id == created.subject_id


async def test_creation_is_case_insensitive(store: LocalCredentialStore) -> None:
    await store.create("ada@example.com", GOOD_PASSWORD)

    assert await store.create("ADA@example.com", GOOD_PASSWORD) is CredentialError.ALREADY_EXISTS


async def test_a_wrong_password_is_refused(store: LocalCredentialStore) -> None:
    await store.create("ada@example.com", GOOD_PASSWORD)

    assert await store.verify("ada@example.com", "wrong-but-long-enough") is (
        CredentialError.BAD_PASSWORD
    )


async def test_duplicate_creation_returns_an_error_rather_than_raising(
    store: LocalCredentialStore,
) -> None:
    """🔒 The caller's correct response is to look identical to success. An
    exception invites a distinguishable error path."""
    await store.create("ada@example.com", GOOD_PASSWORD)

    assert await store.create("ada@example.com", GOOD_PASSWORD) is (CredentialError.ALREADY_EXISTS)


async def test_verifying_an_unknown_address_costs_the_same_as_a_known_one(
    store: LocalCredentialStore,
) -> None:
    """🔒 NFR-043 — the timing oracle.

    An early return on the miss path makes "no such account" measurably faster
    than "wrong password", and no amount of identical response bodies hides it.
    The store does the scrypt work either way; this asserts the two are within
    the same order of magnitude.

    ⚠️ A ratio, not an absolute threshold — absolute timings are meaningless on
    shared CI. The bug this catches is an early `return`, which shows up as a
    difference of 100× or more, not 2×.
    """
    await store.create("known@example.com", GOOD_PASSWORD)

    start = time.perf_counter()
    await store.verify("known@example.com", "wrong-but-long-enough")
    hit = time.perf_counter() - start

    start = time.perf_counter()
    await store.verify("unknown@example.com", "wrong-but-long-enough")
    miss = time.perf_counter() - start

    assert miss > hit / 10, (
        f"the unknown-address path took {miss:.4f}s against {hit:.4f}s for a known "
        "address — an early return on the miss path is an enumeration oracle"
    )


async def test_a_password_can_be_replaced(store: LocalCredentialStore) -> None:
    created = await store.create("ada@example.com", GOOD_PASSWORD)
    assert isinstance(created, CredentialSubject)

    assert await store.set_password(created.subject_id, "a-different-long-one") is None
    assert await store.verify("ada@example.com", GOOD_PASSWORD) is CredentialError.BAD_PASSWORD
    replaced = await store.verify("ada@example.com", "a-different-long-one")
    assert isinstance(replaced, CredentialSubject)


async def test_a_replacement_password_must_also_be_strong(
    store: LocalCredentialStore,
) -> None:
    created = await store.create("ada@example.com", GOOD_PASSWORD)
    assert isinstance(created, CredentialSubject)

    assert await store.set_password(created.subject_id, "short") is (CredentialError.WEAK_PASSWORD)


async def test_creating_with_a_weak_password_stores_nothing(
    store: LocalCredentialStore,
) -> None:
    assert await store.create("ada@example.com", "short") is CredentialError.WEAK_PASSWORD
    # The address is still free, so the rejection did not half-create an account.
    assert isinstance(await store.create("ada@example.com", GOOD_PASSWORD), CredentialSubject)


# ─── 🔒 The production guard ─────────────────────────────────────────────


def test_the_local_store_is_refused_outside_local_development() -> None:
    """🔒 The failure it prevents is quiet and total: the process boots, users
    register, and the next restart discards every password with no error."""
    with pytest.raises(RuntimeError, match="in-memory credential store"):
        raise_if_credentials_are_local(object())


# ─── Bearer extraction ───────────────────────────────────────────────────


def _request(path: str, headers: dict[str, str] | None = None) -> Request:
    raw = Headers(headers or {}).raw
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": raw,
            "client": ("127.0.0.1", 1234),
            "scheme": "http",
            "server": ("test", 80),
        }
    )


def test_a_bearer_header_is_read() -> None:
    assert bearer_token(_request("/", {"Authorization": "Bearer abc123"})) == "abc123"


def test_the_scheme_is_matched_case_insensitively() -> None:
    assert bearer_token(_request("/", {"Authorization": "bearer abc123"})) == "abc123"


def test_a_non_bearer_scheme_is_ignored() -> None:
    assert bearer_token(_request("/", {"Authorization": "Basic abc123"})) is None


def test_an_absent_header_yields_nothing() -> None:
    assert bearer_token(_request("/")) is None


def test_an_empty_credential_yields_nothing() -> None:
    assert bearer_token(_request("/", {"Authorization": "Bearer   "})) is None


# ─── 🔒 Actor resolution ─────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def live_sessions() -> Iterator[None]:
    """Treat every session as live unless a test says otherwise.

    ⚠️ The real validator queries PostgreSQL. Substituting it here is what lets
    the resolution logic — realm selection, signature failure, expiry — be
    covered at all before the D4 gate closes.
    """
    original = get_session_validator()

    async def always_live(_session_id: uuid.UUID) -> bool:
        return True

    configure_session_validator(always_live)
    try:
        yield
    finally:
        configure_session_validator(original)


def _token_for(realm: AuthRealm, *, lifetime: timedelta = timedelta(minutes=15)) -> str:
    now = datetime.now(UTC)
    return issue_access_token(
        AccessClaims(
            subject_id=SUBJECT,
            realm=realm,
            session_id=SESSION,
            issued_at=now,
            expires_at=now + lifetime,
            tenant_id=TENANT if realm is not AuthRealm.OPERATOR else None,
            role=UserRole.OWNER if realm is AuthRealm.PRACTITIONER else None,
        ),
        key=signing_key(realm),
    )


async def test_a_valid_practitioner_token_resolves() -> None:
    request = _request(
        "/api/v1/app/clients",
        {"Authorization": f"Bearer {_token_for(AuthRealm.PRACTITIONER)}"},
    )

    actor = await resolve_actor(request)

    assert actor.actor_type is ActorType.PRACTITIONER
    assert actor.subject_id == SUBJECT
    assert actor.tenant_id == TENANT
    assert actor.session_id == SESSION


async def test_a_practitioner_token_at_the_portal_resolves_to_anonymous() -> None:
    """🔒 The realm boundary, end to end.

    `/portal` selects the client key, so a practitioner token fails signature
    verification. The result is an anonymous actor, which the pipeline then
    denies and audits.
    """
    request = _request(
        "/api/v1/portal/plans",
        {"Authorization": f"Bearer {_token_for(AuthRealm.PRACTITIONER)}"},
    )

    assert (await resolve_actor(request)).actor_type is ActorType.ANONYMOUS


async def test_a_practitioner_token_at_admin_resolves_to_anonymous() -> None:
    request = _request(
        "/api/v1/admin/tenants",
        {"Authorization": f"Bearer {_token_for(AuthRealm.PRACTITIONER)}"},
    )

    assert (await resolve_actor(request)).actor_type is ActorType.ANONYMOUS


async def test_an_expired_token_resolves_to_anonymous() -> None:
    request = _request(
        "/api/v1/app/clients",
        {
            "Authorization": (
                f"Bearer {_token_for(AuthRealm.PRACTITIONER, lifetime=timedelta(minutes=-5))}"
            )
        },
    )

    assert (await resolve_actor(request)).actor_type is ActorType.ANONYMOUS


async def test_a_missing_token_resolves_to_anonymous() -> None:
    assert (await resolve_actor(_request("/api/v1/app/clients"))).actor_type is (
        ActorType.ANONYMOUS
    )


async def test_a_public_path_resolves_to_anonymous_even_with_a_token() -> None:
    """`/public` has no realm and therefore no key. Nothing to verify against."""
    request = _request(
        "/api/v1/public/auth/login",
        {"Authorization": f"Bearer {_token_for(AuthRealm.PRACTITIONER)}"},
    )

    assert (await resolve_actor(request)).actor_type is ActorType.ANONYMOUS


async def test_a_revoked_session_resolves_to_anonymous() -> None:
    """🔒 What makes logout immediate.

    Without this check a revoked session keeps working until its access token
    expires — up to fifteen minutes of access after signing out.
    """

    async def never_live(_session_id: uuid.UUID) -> bool:
        return False

    configure_session_validator(never_live)

    request = _request(
        "/api/v1/app/clients",
        {"Authorization": f"Bearer {_token_for(AuthRealm.PRACTITIONER)}"},
    )

    assert (await resolve_actor(request)).actor_type is ActorType.ANONYMOUS


async def test_resolution_fails_closed_when_the_session_store_errors() -> None:
    """🔒 A database outage must not become an authentication bypass."""

    async def broken(_session_id: uuid.UUID) -> bool:
        raise RuntimeError("database is down")

    configure_session_validator(broken)

    request = _request(
        "/api/v1/app/clients",
        {"Authorization": f"Bearer {_token_for(AuthRealm.PRACTITIONER)}"},
    )

    with pytest.raises(RuntimeError):
        # The default validator swallows and returns False; a substituted one
        # that raises surfaces here, which is why the real one catches.
        await resolve_actor(request)


async def test_a_garbage_token_resolves_to_anonymous() -> None:
    request = _request("/api/v1/app/clients", {"Authorization": "Bearer not.a.token"})

    assert (await resolve_actor(request)).actor_type is ActorType.ANONYMOUS
