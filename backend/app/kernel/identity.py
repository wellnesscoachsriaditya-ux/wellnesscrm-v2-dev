"""Identity — who is acting, proven cryptographically.

🔒 Arch §6.1 / FR-M0-004. Three realms, and **a credential in one must not
authenticate into another**. That is enforced by a separate signing key per
realm, so a practitioner token presented to `/admin` fails *signature
verification* — not a claim comparison someone could forget to write, invert, or
short-circuit. AC-M11-005 is satisfied by arithmetic rather than by discipline.

This module is pure: no database, no HTTP, no configuration (R5). It knows how to
mint and verify a token, how to decide what a presented refresh token means, and
what a credential store must be able to do. It knows nothing about users,
tenants, practices or plans — those are the caller's concern, and keeping them
out is what lets the whole identity surface be tested as a table of inputs.

Three properties carry the design:

1. 🔒 **Realm is a key, not a claim.** :func:`verify_access_token` takes the
   realm it expects and uses that realm's key. There is no code path that reads
   the realm from the token and then trusts it.
2. 🔒 **Refresh tokens are opaque and stored hashed** (DDR-05). A database
   disclosure yields no usable credential, and a rotated token that comes back is
   treated as theft rather than as a retry — see :func:`evaluate_refresh`.
3. 🔒 **Passwords never reach this codebase's storage** (NFR-029). The
   :class:`CredentialStore` port is the boundary; what lives behind it is the
   identity provider's problem, and `users` has no password column by design.
"""

from __future__ import annotations

import enum
import hashlib
import hmac
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import jwt

from app.kernel.context import Actor, ActorType, AuthRealm, UserRole
from app.kernel.errors import AuthenticationError

#: 🔒 The only accepted algorithm. Passed explicitly on every decode so a token
#: whose header claims `none` — or an asymmetric algorithm we never issue — is
#: rejected before its signature is considered. Algorithm confusion is the
#: classic JWT break and this single-element list is what closes it.
JWT_ALGORITHM = "HS256"

#: Bytes of entropy in an opaque token. 256 bits: these are bearer credentials
#: with no rate limit on offline guessing if a hash ever leaks.
_OPAQUE_TOKEN_BYTES = 32

#: How the actor type follows from the realm. A realm cannot produce an actor of
#: a different type, which is what keeps "client token, practitioner powers" from
#: being expressible.
_ACTOR_TYPE_BY_REALM: Mapping[AuthRealm, ActorType] = {
    AuthRealm.PRACTITIONER: ActorType.PRACTITIONER,
    AuthRealm.CLIENT: ActorType.CLIENT,
    AuthRealm.OPERATOR: ActorType.OPERATOR,
}


# ─── Errors ──────────────────────────────────────────────────────────────


class TokenRejectedError(AuthenticationError):
    """🔒 A token was not accepted. One error for every reason.

    Expired, malformed, wrong realm, bad signature, missing claim — all produce
    this, with the same message. The *reason* goes to the audit log; telling a
    caller which of those applied is a probing aid (NFR-043), and the remedy is
    identical in every case.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(
            message="Your session is no longer valid.",
            action="Sign in again to continue.",
        )
        #: Audit material. 🔒 Never returned to the caller.
        self.reason = reason


# ─── Claims ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AccessClaims:
    """The contents of an access token.

    🔒 Identifiers only (NFR-033). No email, name or phone number: a JWT is
    base64, not encryption, and every holder of the token can read it. A token
    that carries a name is a name in browser storage, in proxy logs, and in every
    crash report.
    """

    subject_id: uuid.UUID
    realm: AuthRealm
    session_id: uuid.UUID
    issued_at: datetime
    expires_at: datetime
    tenant_id: uuid.UUID | None = None
    role: UserRole | None = None

    def to_actor(self) -> Actor:
        """Build the actor the pipeline authorizes against.

        🔒 The actor type is derived from the realm, never read from the token.
        A forged `actor_type` claim would otherwise be a privilege escalation
        that survives signature verification — the signature proves the token is
        ours, not that its contents are sensible.
        """
        return Actor(
            actor_type=_ACTOR_TYPE_BY_REALM[self.realm],
            realm=self.realm,
            subject_id=self.subject_id,
            tenant_id=self.tenant_id,
            role=self.role,
            session_id=self.session_id,
        )


def issue_access_token(
    claims: AccessClaims,
    *,
    key: str,
) -> str:
    """Mint a signed access token for one realm.

    Args:
        claims: What the token asserts.
        key: 🔒 That realm's signing key. Supplied by the caller because the
            kernel must not read configuration (R5) — and because passing it
            explicitly makes "which key signed this?" answerable at every call
            site rather than hidden in a lookup.
    """
    payload: dict[str, Any] = {
        "sub": str(claims.subject_id),
        "realm": claims.realm.value,
        "sid": str(claims.session_id),
        "iat": int(claims.issued_at.timestamp()),
        "exp": int(claims.expires_at.timestamp()),
    }
    if claims.tenant_id is not None:
        payload["tid"] = str(claims.tenant_id)
    if claims.role is not None:
        payload["role"] = claims.role.value
    return jwt.encode(payload, key, algorithm=JWT_ALGORITHM)


def verify_access_token(token: str, *, realm: AuthRealm, key: str) -> AccessClaims:
    """🔒 Verify a token *against the realm it was presented to*.

    The realm comes from the request path (ADR-A01), and its key is the only key
    tried. A practitioner token presented at `/portal` is therefore verified with
    the client key and fails on the signature — the realm boundary is arithmetic,
    not a comparison.

    ⚠️ The `realm` claim inside the token is checked too, but only as a
    consistency assertion. It is not what provides the separation, and treating
    it as though it were would make the separation forgeable.

    Raises:
        TokenRejectedError: For every failure mode, with the reason recorded for audit
            and withheld from the caller.
    """
    try:
        payload = jwt.decode(
            token,
            key,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "exp", "iat", "realm", "sid"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenRejectedError("token_expired") from exc
    except jwt.InvalidSignatureError as exc:
        # 🔒 The overwhelmingly likely cause is a token from another realm.
        raise TokenRejectedError("bad_signature_or_wrong_realm") from exc
    except jwt.MissingRequiredClaimError as exc:
        raise TokenRejectedError("missing_claim") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenRejectedError("malformed_token") from exc

    claimed_realm = payload.get("realm")
    if claimed_realm != realm.value:
        # Reaching here means a token signed with this realm's key carries a
        # different realm claim — an issuing bug, not an attack, but the token
        # must not be honoured while the two disagree.
        raise TokenRejectedError("realm_claim_mismatch")

    try:
        return AccessClaims(
            subject_id=uuid.UUID(payload["sub"]),
            realm=realm,
            session_id=uuid.UUID(payload["sid"]),
            issued_at=datetime.fromtimestamp(payload["iat"], tz=UTC),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
            tenant_id=uuid.UUID(payload["tid"]) if payload.get("tid") else None,
            role=UserRole(payload["role"]) if payload.get("role") else None,
        )
    except (ValueError, KeyError) as exc:
        # A well-signed token with an unparseable identifier or an unknown role.
        raise TokenRejectedError("unusable_claims") from exc


# ─── Opaque tokens ───────────────────────────────────────────────────────


def new_opaque_token() -> str:
    """Generate a refresh token or magic-link token.

    🔒 `secrets`, never `random`. URL-safe because magic-link tokens travel in a
    WhatsApp deep link and a token that needs escaping will eventually be
    delivered broken by something in the chain.
    """
    return secrets.token_urlsafe(_OPAQUE_TOKEN_BYTES)


def hash_token(token: str, *, pepper: str) -> str:
    """🔒 Hash an opaque token for storage (DDR-04, DDR-05).

    A single SHA-256 pass, deliberately — not scrypt or argon2. Those exist to
    make *low-entropy* secrets expensive to guess. These tokens carry 256 bits
    from a CSPRNG, so brute force is not the threat and a slow KDF would only add
    latency to every refresh. The pepper covers the case that matters: a database
    disclosure without the application secret yields nothing testable.
    """
    return hashlib.sha256(f"{pepper}:{token}".encode()).hexdigest()


def tokens_equal(left: str, right: str) -> bool:
    """Constant-time comparison, for comparing a presented hash to a stored one."""
    return hmac.compare_digest(left, right)


# ─── Refresh rotation ────────────────────────────────────────────────────


class RefreshOutcome(enum.StrEnum):
    """What a presented refresh token means.

    🔒 ``REUSE_DETECTED`` is the one that matters. Everything else is a
    rejection; that one is a *revocation*, because a token which was already
    rotated coming back is the signature of a stolen credential being replayed.
    """

    ROTATE = "rotate"
    REUSE_DETECTED = "reuse_detected"
    EXPIRED = "expired"
    REVOKED = "revoked"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """The stored session, as the policy needs to see it.

    A snapshot rather than the ORM row so the policy is testable as a table and
    cannot accidentally reach the database mid-decision.
    """

    id: uuid.UUID
    realm: AuthRealm
    subject_id: uuid.UUID
    tenant_id: uuid.UUID | None
    refresh_token_hash: str
    previous_token_hash: str | None
    expires_at: datetime
    revoked_at: datetime | None


def evaluate_refresh(
    session: SessionSnapshot | None,
    presented_hash: str,
    *,
    now: datetime,
) -> RefreshOutcome:
    """🔒 Decide what to do with a presented refresh token (DDR-05).

    The caller looks up a session by *either* ``refresh_token_hash`` or
    ``previous_token_hash`` and hands over whatever it found. This function does
    not query, so the ordering of those two lookups cannot change the verdict.

    Order matters and is deliberate:

    1. **Nothing found** → ``UNKNOWN``. Not an error worth revoking anything for:
       an old token from a session that has since been deleted looks identical to
       a typo.
    2. 🔒 **Matches the previous hash** → ``REUSE_DETECTED``, *checked before
       expiry and revocation*. A stolen token replayed after the session expired
       is still a theft, and the alarm should fire on it. Checking expiry first
       would silently downgrade the most important signal in the system to a
       routine 401.
    3. **Revoked or expired** → rejected.
    4. **Matches the current hash** → ``ROTATE``.

    ⚠️ Only one generation of history is stored (``previous_token_hash``), so a
    token rotated several generations ago returns ``UNKNOWN`` rather than
    ``REUSE_DETECTED``. The realistic attack — steal a token, the victim refreshes,
    the attacker replays — is one generation deep and is caught. Detecting deeper
    replays needs a token-family table, which is a schema change and not worth it
    at this scale.
    """
    if session is None:
        return RefreshOutcome.UNKNOWN

    if session.previous_token_hash is not None and tokens_equal(
        presented_hash, session.previous_token_hash
    ):
        return RefreshOutcome.REUSE_DETECTED

    if not tokens_equal(presented_hash, session.refresh_token_hash):
        return RefreshOutcome.UNKNOWN

    if session.revoked_at is not None:
        return RefreshOutcome.REVOKED

    if session.expires_at <= now:
        return RefreshOutcome.EXPIRED

    return RefreshOutcome.ROTATE


def session_expiry(now: datetime, *, ttl_days: int) -> datetime:
    return now + timedelta(days=ttl_days)


def access_expiry(now: datetime, *, ttl_minutes: int) -> datetime:
    return now + timedelta(minutes=ttl_minutes)


# ─── The credential port ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CredentialSubject:
    """The identity provider's handle on an account.

    ``subject_id`` is a string because it belongs to the provider — GoTrue issues
    a UUID, another provider might not, and parsing it here would make the port
    provider-specific in exactly the way it exists to avoid.
    """

    subject_id: str
    email: str


class CredentialError(enum.StrEnum):
    """Why a credential operation failed. 🔒 Never surfaced to a caller."""

    ALREADY_EXISTS = "already_exists"
    NOT_FOUND = "not_found"
    BAD_PASSWORD = "bad_password"
    WEAK_PASSWORD = "weak_password"


class CredentialStore(Protocol):
    """🔒 Where passwords live — which is *not here* (NFR-029, D1).

    The whole reason this is a port: `users` has no password column, and swapping
    GoTrue for another provider must not touch a line of business logic. Every
    method deals in email, password and an opaque subject id; none knows what a
    tenant or a practitioner is.

    ⚠️ Implementations must not distinguish "no such account" from "wrong
    password" in anything a caller can observe beyond the returned
    :class:`CredentialError` — and callers must not surface that distinction
    either (NFR-043).
    """

    async def create(self, email: str, password: str) -> CredentialSubject | CredentialError:
        """Register an account. Returns ``ALREADY_EXISTS`` rather than raising —
        the caller's correct response is to look identical to success."""
        ...

    async def verify(self, email: str, password: str) -> CredentialSubject | CredentialError:
        """Check a password. 🔒 Must take similar time whether or not the account
        exists, or the response becomes an enumeration oracle."""
        ...

    async def set_password(self, subject_id: str, password: str) -> None | CredentialError:
        """Replace a password. Called after a reset token is redeemed."""
        ...
