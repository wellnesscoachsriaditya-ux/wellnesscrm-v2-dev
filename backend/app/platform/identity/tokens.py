"""Tokens — the kernel's identity policy, bound to configuration.

`kernel.identity` takes keys and TTLs as arguments so it can stay pure (R5). This
module is what supplies them: one place that knows which key belongs to which
realm, and the only place that reads identity settings.

🔒 The realm→key mapping is the mechanism behind AC-M11-005, so it is a total
mapping over the enum rather than a dictionary lookup with a default. Adding a
realm without a key becomes a type error, not a runtime fallback onto whichever
key happened to be first.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.kernel.context import AuthRealm, UserRole
from app.kernel.identity import (
    AccessClaims,
    access_expiry,
    hash_token,
    issue_access_token,
    new_opaque_token,
    session_expiry,
    verify_access_token,
)
from app.platform.config import Settings, get_settings


def signing_key(realm: AuthRealm, settings: Settings | None = None) -> str:
    """🔒 The signing key for one realm. Three keys, no shared fallback."""
    resolved = settings or get_settings()
    match realm:
        case AuthRealm.PRACTITIONER:
            return resolved.jwt_secret_practitioner.get_secret_value()
        case AuthRealm.CLIENT:
            return resolved.jwt_secret_client.get_secret_value()
        case AuthRealm.OPERATOR:
            return resolved.jwt_secret_operator.get_secret_value()


def token_pepper(settings: Settings | None = None) -> str:
    """The secret mixed into stored token hashes.

    Reuses ``audit_ip_salt``'s sibling requirement — a deployment secret, stable
    for the lifetime of the stored values. Derived from the practitioner signing
    key rather than adding a fourth secret to configure: rotating that key
    invalidates outstanding refresh tokens, which is the correct consequence of
    rotating a signing key anyway.
    """
    resolved = settings or get_settings()
    return hashlib.sha256(
        f"token-pepper:{resolved.jwt_secret_practitioner.get_secret_value()}".encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    """What a successful authentication produces.

    ⚠️ ``refresh_token`` is the only time the plaintext exists in this process.
    It is returned to the caller and hashed for storage; nothing keeps it.
    """

    access_token: str
    refresh_token: str
    session_id: uuid.UUID
    expires_in_seconds: int
    refresh_expires_at: datetime


def issue_for_session(
    *,
    realm: AuthRealm,
    subject_id: uuid.UUID,
    session_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    role: UserRole | None,
    now: datetime,
    settings: Settings | None = None,
) -> IssuedTokens:
    """Mint an access token and a refresh token for an established session."""
    resolved = settings or get_settings()
    expires_at = access_expiry(now, ttl_minutes=resolved.access_token_ttl_minutes)

    access = issue_access_token(
        AccessClaims(
            subject_id=subject_id,
            realm=realm,
            session_id=session_id,
            issued_at=now,
            expires_at=expires_at,
            tenant_id=tenant_id,
            role=role,
        ),
        key=signing_key(realm, resolved),
    )

    return IssuedTokens(
        access_token=access,
        refresh_token=new_opaque_token(),
        session_id=session_id,
        expires_in_seconds=resolved.access_token_ttl_minutes * 60,
        refresh_expires_at=session_expiry(now, ttl_days=resolved.refresh_token_ttl_days),
    )


def verify_for_realm(
    token: str, *, realm: AuthRealm, settings: Settings | None = None
) -> AccessClaims:
    """🔒 Verify a token against the realm it was presented to."""
    return verify_access_token(token, realm=realm, key=signing_key(realm, settings))


def hash_for_storage(token: str, settings: Settings | None = None) -> str:
    """Hash an opaque token for the database (DDR-04, DDR-05)."""
    return hash_token(token, pepper=token_pepper(settings))


def hash_user_agent(user_agent: str | None, settings: Settings | None = None) -> str | None:
    """🔒 Hash the user agent before storing it on a session (NFR-033).

    A user-agent string is a fingerprint. The session table needs only "is this
    the same device as last time", which a hash answers.
    """
    if not user_agent:
        return None
    return hash_token(user_agent[:512], pepper=token_pepper(settings))[:32]


def utcnow() -> datetime:
    """🔒 One clock, in UTC (NFR-099). Injected into services so time-dependent
    behaviour — expiry, rotation, revocation — is testable without sleeping."""
    return datetime.now(UTC)
