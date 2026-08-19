"""Identity — tokens, realm separation and refresh rotation.

🔒 **The sprint's fourth gate.** `kernel.identity` is pure, so the whole of realm
separation and the whole of reuse detection are testable as tables of inputs —
no database, no HTTP, no clock. That is the point of keeping it pure: the two
mechanisms most likely to be wrong are the two cheapest to cover exhaustively.

The properties under test, in order of what they cost if broken:

1. 🔒 **A token from one realm must not verify in another.** Broken, a client
   token reaches practitioner endpoints.
2. 🔒 **A rotated refresh token coming back means theft.** Broken, a stolen
   token works indefinitely and silently.
3. 🔒 **The actor's type comes from the realm, not from the token.** Broken, a
   forged claim escalates privilege past a valid signature.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.kernel.context import ActorType, AuthRealm, UserRole
from app.kernel.identity import (
    JWT_ALGORITHM,
    AccessClaims,
    RefreshOutcome,
    SessionSnapshot,
    TokenRejectedError,
    evaluate_refresh,
    hash_token,
    issue_access_token,
    new_opaque_token,
    tokens_equal,
    verify_access_token,
)

#: ⚠️ Anchored to the real clock, not a fixed date. PyJWT rejects a token whose
#: `iat` is in the future (`ImmatureSignatureError`), so a hard-coded timestamp
#: makes these tests pass or fail depending on the hour they are run — the worst
#: kind of flake, because it looks like a code change broke them.
#: The rotation tests below are all relative to this value, so they stay
#: deterministic regardless.
NOW = datetime.now(UTC).replace(microsecond=0)
SUBJECT = uuid.UUID("11111111-0000-4000-8000-000000000001")
TENANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
SESSION = uuid.UUID("55555555-0000-4000-8000-000000000005")

# 🔒 At least 32 bytes each — RFC 7518 §3.2 for HS256, and what
# `Settings._enforce_production_safety` requires of a deployed key.
PRACTITIONER_KEY = "practitioner-key-not-used-anywhere-else-0123456789"
CLIENT_KEY = "client-key-not-used-anywhere-else-0123456789"
OPERATOR_KEY = "operator-key-not-used-anywhere-else-0123456789"

_KEYS = {
    AuthRealm.PRACTITIONER: PRACTITIONER_KEY,
    AuthRealm.CLIENT: CLIENT_KEY,
    AuthRealm.OPERATOR: OPERATOR_KEY,
}


def _claims(
    *,
    realm: AuthRealm = AuthRealm.PRACTITIONER,
    role: UserRole | None = UserRole.OWNER,
    tenant_id: uuid.UUID | None = TENANT,
    issued: datetime = NOW,
    lifetime: timedelta = timedelta(minutes=15),
) -> AccessClaims:
    return AccessClaims(
        subject_id=SUBJECT,
        realm=realm,
        session_id=SESSION,
        issued_at=issued,
        expires_at=issued + lifetime,
        tenant_id=tenant_id,
        role=role,
    )


def _token(**kwargs: object) -> str:
    claims = _claims(**kwargs)  # type: ignore[arg-type]
    return issue_access_token(claims, key=_KEYS[claims.realm])


# ─── Round trip ──────────────────────────────────────────────────────────


def test_a_token_verifies_in_its_own_realm() -> None:
    claims = verify_access_token(_token(), realm=AuthRealm.PRACTITIONER, key=PRACTITIONER_KEY)

    assert claims.subject_id == SUBJECT
    assert claims.tenant_id == TENANT
    assert claims.role is UserRole.OWNER
    assert claims.session_id == SESSION


def test_a_client_token_carries_no_tenant_when_none_is_given() -> None:
    claims = verify_access_token(
        _token(realm=AuthRealm.OPERATOR, tenant_id=None, role=UserRole.PLATFORM_OPERATOR),
        realm=AuthRealm.OPERATOR,
        key=OPERATOR_KEY,
    )

    assert claims.tenant_id is None


# ─── 🔒 Realm separation ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("issued_for", "presented_to"),
    [
        (AuthRealm.PRACTITIONER, AuthRealm.CLIENT),
        (AuthRealm.PRACTITIONER, AuthRealm.OPERATOR),
        (AuthRealm.CLIENT, AuthRealm.PRACTITIONER),
        (AuthRealm.CLIENT, AuthRealm.OPERATOR),
        (AuthRealm.OPERATOR, AuthRealm.PRACTITIONER),
        (AuthRealm.OPERATOR, AuthRealm.CLIENT),
    ],
)
def test_a_token_never_verifies_in_another_realm(
    issued_for: AuthRealm, presented_to: AuthRealm
) -> None:
    """🔒 FR-M0-004 / AC-M11-005, every ordered pair.

    The failure is a *signature* failure, not a claim comparison — which is what
    makes realm confusion impossible rather than merely checked.
    """
    token = _token(realm=issued_for, role=None, tenant_id=None)

    with pytest.raises(TokenRejectedError) as rejected:
        verify_access_token(token, realm=presented_to, key=_KEYS[presented_to])

    assert rejected.value.reason == "bad_signature_or_wrong_realm"


def test_the_rejection_message_says_nothing_about_realms() -> None:
    """🔒 NFR-043 — the reason is audit material, not a response field."""
    token = _token(realm=AuthRealm.CLIENT, role=None, tenant_id=None)

    with pytest.raises(TokenRejectedError) as rejected:
        verify_access_token(token, realm=AuthRealm.PRACTITIONER, key=PRACTITIONER_KEY)

    assert "realm" not in rejected.value.message.lower()
    assert "client" not in rejected.value.message.lower()


def test_identical_keys_would_collapse_the_realms() -> None:
    """⚠️ Documents *why* config refuses identical keys (Arch §6.1).

    With one key for two realms, the signature check passes and only the claim
    stands between them. This test exists so that the reason the configuration
    validator is not optional is written down as an executable fact.
    """
    shared = "one-key-for-everything-0123456789012345"
    token = issue_access_token(_claims(realm=AuthRealm.PRACTITIONER), key=shared)

    # The signature no longer objects...
    with pytest.raises(TokenRejectedError) as rejected:
        verify_access_token(token, realm=AuthRealm.CLIENT, key=shared)

    # ...and only the claim check catches it, which is exactly the weaker
    # position the separate-keys design exists to avoid.
    assert rejected.value.reason == "realm_claim_mismatch"


# ─── 🔒 Malformed and hostile tokens ─────────────────────────────────────


def test_an_expired_token_is_rejected() -> None:
    expired = _token(issued=NOW - timedelta(hours=2), lifetime=timedelta(minutes=15))

    with pytest.raises(TokenRejectedError) as rejected:
        verify_access_token(expired, realm=AuthRealm.PRACTITIONER, key=PRACTITIONER_KEY)

    assert rejected.value.reason == "token_expired"


def test_the_none_algorithm_is_refused() -> None:
    """🔒 The classic JWT break: an unsigned token claiming `alg: none`.

    Closed by passing `algorithms=[HS256]` explicitly on every decode.
    """
    unsigned = jwt.encode(
        {
            "sub": str(SUBJECT),
            "realm": AuthRealm.PRACTITIONER.value,
            "sid": str(SESSION),
            "iat": int(NOW.timestamp()),
            "exp": int((NOW + timedelta(hours=1)).timestamp()),
        },
        key="",
        algorithm="none",
    )

    with pytest.raises(TokenRejectedError):
        verify_access_token(unsigned, realm=AuthRealm.PRACTITIONER, key=PRACTITIONER_KEY)


def test_a_token_missing_a_required_claim_is_refused() -> None:
    """A signed token is not automatically a usable one."""
    incomplete = jwt.encode(
        {"sub": str(SUBJECT), "exp": int((NOW + timedelta(hours=1)).timestamp())},
        PRACTITIONER_KEY,
        algorithm=JWT_ALGORITHM,
    )

    with pytest.raises(TokenRejectedError) as rejected:
        verify_access_token(incomplete, realm=AuthRealm.PRACTITIONER, key=PRACTITIONER_KEY)

    assert rejected.value.reason == "missing_claim"


def test_garbage_is_refused() -> None:
    with pytest.raises(TokenRejectedError) as rejected:
        verify_access_token("not.a.token", realm=AuthRealm.PRACTITIONER, key=PRACTITIONER_KEY)

    assert rejected.value.reason == "malformed_token"


def test_an_unknown_role_is_refused_rather_than_ignored() -> None:
    """⚠️ A well-signed token naming a role we do not have.

    Dropping the claim and continuing would produce an actor with no role, which
    `can()` denies — safe, but silent. Refusing makes a rollback that removed a
    role visible instead of mysterious.
    """
    forged = jwt.encode(
        {
            "sub": str(SUBJECT),
            "realm": AuthRealm.PRACTITIONER.value,
            "sid": str(SESSION),
            "iat": int(NOW.timestamp()),
            "exp": int((NOW + timedelta(hours=1)).timestamp()),
            "role": "superuser",
        },
        PRACTITIONER_KEY,
        algorithm=JWT_ALGORITHM,
    )

    with pytest.raises(TokenRejectedError) as rejected:
        verify_access_token(forged, realm=AuthRealm.PRACTITIONER, key=PRACTITIONER_KEY)

    assert rejected.value.reason == "unusable_claims"


# ─── 🔒 Claims never carry personal data ─────────────────────────────────


def test_the_token_body_holds_identifiers_only() -> None:
    """🔒 NFR-033 — a JWT is base64, not encryption.

    Every holder can read the payload, so a name or email in it is a name or
    email in browser storage, proxy logs and crash reports.
    """
    payload = jwt.decode(
        _token(), PRACTITIONER_KEY, algorithms=[JWT_ALGORITHM], options={"verify_exp": False}
    )

    assert set(payload) == {"sub", "realm", "sid", "iat", "exp", "tid", "role"}


# ─── 🔒 Actor derivation ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("realm", "expected"),
    [
        (AuthRealm.PRACTITIONER, ActorType.PRACTITIONER),
        (AuthRealm.CLIENT, ActorType.CLIENT),
        (AuthRealm.OPERATOR, ActorType.OPERATOR),
    ],
)
def test_the_actor_type_follows_from_the_realm(realm: AuthRealm, expected: ActorType) -> None:
    """🔒 Derived, never read from the token.

    A forged `actor_type` claim would otherwise survive signature verification —
    the signature proves the token is ours, not that its contents are sensible.
    """
    actor = _claims(realm=realm, role=None, tenant_id=None).to_actor()

    assert actor.actor_type is expected
    assert actor.realm is realm


def test_the_actor_carries_the_session_so_revocation_can_bite() -> None:
    actor = _claims().to_actor()

    assert actor.session_id == SESSION


# ─── Opaque tokens ───────────────────────────────────────────────────────


def test_opaque_tokens_are_unique_and_url_safe() -> None:
    """URL-safe because a magic-link token travels in a WhatsApp deep link, and
    anything needing escaping is eventually delivered broken."""
    tokens = {new_opaque_token() for _ in range(200)}

    assert len(tokens) == 200
    assert all(t.replace("-", "").replace("_", "").isalnum() for t in tokens)
    assert all(len(t) >= 40 for t in tokens)


def test_hashing_is_stable_and_peppered() -> None:
    token = new_opaque_token()

    assert hash_token(token, pepper="p") == hash_token(token, pepper="p")
    # 🔒 A database disclosure without the application secret yields nothing
    # testable against the stored hashes.
    assert hash_token(token, pepper="p") != hash_token(token, pepper="q")


def test_tokens_equal_compares_in_constant_time() -> None:
    assert tokens_equal("abc", "abc")
    assert not tokens_equal("abc", "abd")


# ─── 🔒 Refresh rotation and reuse detection (DDR-05) ────────────────────


def _snapshot(
    *,
    current: str = "current-hash",
    previous: str | None = None,
    expires: datetime = NOW + timedelta(days=30),
    revoked: datetime | None = None,
) -> SessionSnapshot:
    return SessionSnapshot(
        id=SESSION,
        realm=AuthRealm.PRACTITIONER,
        subject_id=SUBJECT,
        tenant_id=TENANT,
        refresh_token_hash=current,
        previous_token_hash=previous,
        expires_at=expires,
        revoked_at=revoked,
    )


def test_the_current_token_rotates() -> None:
    assert evaluate_refresh(_snapshot(), "current-hash", now=NOW) is RefreshOutcome.ROTATE


def test_a_rotated_token_coming_back_is_reuse() -> None:
    """🔒 The signature of a stolen credential being replayed.

    The victim refreshed (so the token moved to `previous`), and now the old one
    arrives. Someone has a copy.
    """
    snapshot = _snapshot(current="new-hash", previous="old-hash")

    assert evaluate_refresh(snapshot, "old-hash", now=NOW) is RefreshOutcome.REUSE_DETECTED


def test_reuse_is_detected_even_after_the_session_expired() -> None:
    """🔒 Ordering matters, and this pins it.

    Checking expiry first would report `EXPIRED` and silently downgrade the most
    important signal in the system to a routine 401 — the alarm would never fire
    for a token stolen shortly before expiry.
    """
    snapshot = _snapshot(current="new-hash", previous="old-hash", expires=NOW - timedelta(days=1))

    assert evaluate_refresh(snapshot, "old-hash", now=NOW) is RefreshOutcome.REUSE_DETECTED


def test_reuse_is_detected_even_after_the_session_was_revoked() -> None:
    """Same reasoning: a logout must not mask a theft that already happened."""
    snapshot = _snapshot(current="new-hash", previous="old-hash", revoked=NOW)

    assert evaluate_refresh(snapshot, "old-hash", now=NOW) is RefreshOutcome.REUSE_DETECTED


def test_an_unknown_token_is_not_treated_as_reuse() -> None:
    """⚠️ No session found means no evidence of anything.

    Revoking on this would let anyone log a victim out by posting random strings.
    """
    assert evaluate_refresh(None, "anything", now=NOW) is RefreshOutcome.UNKNOWN


def test_a_token_matching_neither_hash_is_unknown() -> None:
    snapshot = _snapshot(current="a", previous="b")

    assert evaluate_refresh(snapshot, "c", now=NOW) is RefreshOutcome.UNKNOWN


def test_a_revoked_session_does_not_rotate() -> None:
    assert (
        evaluate_refresh(_snapshot(revoked=NOW), "current-hash", now=NOW) is RefreshOutcome.REVOKED
    )


def test_an_expired_session_does_not_rotate() -> None:
    snapshot = _snapshot(expires=NOW - timedelta(seconds=1))

    assert evaluate_refresh(snapshot, "current-hash", now=NOW) is RefreshOutcome.EXPIRED


def test_expiry_is_exclusive_at_the_boundary() -> None:
    """A token expiring exactly now is expired. Off-by-one here is a token that
    outlives its own expiry by a request."""
    assert evaluate_refresh(_snapshot(expires=NOW), "current-hash", now=NOW) is (
        RefreshOutcome.EXPIRED
    )


def test_only_one_generation_of_history_is_kept() -> None:
    """⚠️ Documents the known limit rather than hiding it.

    A token rotated twice ago matches nothing and reads as `UNKNOWN`, not
    `REUSE_DETECTED`. The realistic attack is one generation deep and is caught;
    detecting more needs a token-family table.
    """
    snapshot = _snapshot(current="gen3", previous="gen2")

    assert evaluate_refresh(snapshot, "gen1", now=NOW) is RefreshOutcome.UNKNOWN
