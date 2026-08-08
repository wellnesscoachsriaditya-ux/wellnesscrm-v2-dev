"""Authentication flows.

The eight practitioner operations and the two client-portal ones, each as a
function taking the request's transaction. 🔒 No HTTP here and no domain logic —
the router translates, this decides, the repository persists.

Three rules shape almost every function below:

1. 🔒 **Nothing reveals whether an account exists** (NFR-043). Registration,
   password reset and portal access all return the same shape and status whether
   or not the identifier matched. Where the paths must differ they are made to
   cost the same.
2. 🔒 **Sessions are checked, not merely signed.** A revoked session stops
   working immediately rather than when its access token expires.
3. 🔒 **Reuse of a rotated refresh token revokes every session for that subject**
   (DDR-05) — the response to a replayed credential is to log the person out
   everywhere, not to refuse one request.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.context import AuthRealm as ContextRealm
from app.kernel.context import UserRole as ContextRole
from app.kernel.errors import AuthenticationError, ValidationError
from app.kernel.identity import (
    CredentialError,
    CredentialStore,
    CredentialSubject,
    RefreshOutcome,
    evaluate_refresh,
    new_opaque_token,
)
from app.kernel.models import AuthRealm, AuthTokenPurpose, LinkPurpose, TransportType
from app.platform.config import Settings, get_settings
from app.platform.identity import repository as repo
from app.platform.identity.credentials import normalise_email
from app.platform.identity.tokens import (
    IssuedTokens,
    hash_for_storage,
    hash_user_agent,
    issue_for_session,
)
from app.platform.logging import get_logger

logger = get_logger(__name__)

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def _sign_in_failed() -> AuthenticationError:
    """🔒 The one error every failed sign-in returns, whatever went wrong
    (NFR-043).

    Wrong password, unknown address, unverified email, disabled account and
    archived user are indistinguishable from outside. The last three matter most:
    "your account is disabled" confirms the account exists, and confirming
    existence is the disclosure.

    Built fresh each call — an exception instance carries a traceback, and a
    shared one would drag the previous request's context into this one.
    """
    return AuthenticationError(
        message="That email address and password combination isn't right.",
        action="Check them and try again, or reset your password.",
    )


# ─── Results ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Registration:
    """🔒 Carries no tokens. Verification precedes access (FR-M0-002).

    ``verification_token`` is the value the messaging module will send by email.
    It is returned to the caller *inside the process*, never in the HTTP
    response — the router drops it.
    """

    tenant_id: uuid.UUID | None
    user_id: uuid.UUID | None
    verification_token: str | None
    duplicate_email: bool


@dataclass(frozen=True, slots=True)
class PortalSession:
    """A redeemed magic link."""

    tokens: IssuedTokens
    tenant_id: uuid.UUID
    client_id: uuid.UUID
    target_type: str
    target_ref: str | None


# ─── Registration ────────────────────────────────────────────────────────


def _slugify(name: str, *, entropy: int = 4) -> str:
    """Build a tenant slug for the public enquiry form URL (FR-M2-001).

    🔒 Suffixed with random characters rather than a counter. A counter tells the
    world how many practices are registered, and `sunrise-nutrition-2` invites
    guessing `-1`.
    """
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    base = _SLUG_STRIP.sub("-", folded.lower()).strip("-")[:40] or "practice"
    return f"{base}-{secrets.token_hex(entropy // 2)}"


async def register_practitioner(
    session: AsyncSession,
    credentials: CredentialStore,
    *,
    email: str,
    password: str,
    full_name: str,
    practice_name: str,
    mobile: str | None,
    now: datetime,
    settings: Settings | None = None,
) -> Registration:
    """Create a tenant and its owner (FR-M0-001).

    🔒 A duplicate address returns the *same* shape as a success. The caller
    responds 201 either way and the difference is only which notification the
    address receives — "confirm your account" or "someone tried to register with
    your address" (API §2.2). Anything else makes this endpoint an oracle for
    which practitioners use the product.

    Raises:
        ValidationError: Only for a password that fails the length or
            common-password check. That is about the submitted value, not about
            any account, so it discloses nothing.
    """
    resolved = settings or get_settings()
    address = normalise_email(email)

    created = await credentials.create(address, password)

    if created is CredentialError.WEAK_PASSWORD:
        raise ValidationError.for_fields(
            [
                {
                    "field": "password",
                    "code": "weak_password",
                    "message": ("Choose a longer password, or one that is less commonly used."),
                }
            ]
        )

    if created is CredentialError.ALREADY_EXISTS or not isinstance(created, CredentialSubject):
        # 🔒 The duplicate path. No tenant, no user, no token — and, to the
        # caller, no difference.
        return Registration(
            tenant_id=None, user_id=None, verification_token=None, duplicate_email=True
        )

    tenant_id, user_id = await repo.create_tenant_with_owner(
        session,
        practice_name=practice_name,
        slug=_slugify(practice_name),
        auth_subject_id=created.subject_id,
        email=address,
        full_name=full_name,
        mobile=mobile,
        region_code=resolved.default_region_code,
        timezone=resolved.default_timezone,
        currency_code=resolved.default_currency_code,
    )

    token = await _issue_auth_token(
        session,
        auth_subject_id=created.subject_id,
        purpose=AuthTokenPurpose.EMAIL_VERIFICATION,
        now=now,
        ttl=timedelta(days=1),
        settings=resolved,
    )

    return Registration(
        tenant_id=tenant_id,
        user_id=user_id,
        verification_token=token,
        duplicate_email=False,
    )


async def _issue_auth_token(
    session: AsyncSession,
    *,
    auth_subject_id: str,
    purpose: AuthTokenPurpose,
    now: datetime,
    ttl: timedelta,
    settings: Settings | None,
) -> str:
    """Mint a one-time token, invalidating any outstanding one of the purpose."""
    await repo.invalidate_auth_tokens(
        session, auth_subject_id=auth_subject_id, purpose=purpose, now=now
    )
    token = new_opaque_token()
    await repo.store_auth_token(
        session,
        auth_subject_id=auth_subject_id,
        token_hash=hash_for_storage(token, settings),
        purpose=purpose,
        expires_at=now + ttl,
    )
    return token


# ─── Email verification ──────────────────────────────────────────────────


async def verify_email(
    session: AsyncSession,
    *,
    token: str,
    now: datetime,
    user_agent: str | None = None,
    settings: Settings | None = None,
) -> IssuedTokens:
    """Redeem a verification token, activate the user, and open a session.

    Returning tokens is deliberate: the user has just proved they control the
    mailbox, and requiring a password immediately afterwards adds a step without
    adding a check (FR-M0-002).

    Raises:
        AuthenticationError: If the token is unknown, expired, already used, or
            of the wrong purpose — one error for all four, since the remedy is
            identical and distinguishing them helps only a prober.
    """
    subject = await repo.consume_auth_token(
        session,
        token_hash=hash_for_storage(token, settings),
        purpose=AuthTokenPurpose.EMAIL_VERIFICATION,
        now=now,
    )
    if subject is None:
        raise _verification_failed()

    user_id = await repo.activate_user(session, subject)
    if user_id is None:
        raise _verification_failed()

    identity = await repo.find_user_by_id(session, user_id)
    if identity is None or not identity.can_sign_in:  # pragma: no cover - just activated
        raise _verification_failed()

    return await _open_session(
        session,
        realm=AuthRealm.PRACTITIONER,
        subject_id=identity.user_id,
        tenant_id=identity.tenant_id,
        role=ContextRole(identity.role.value),
        user_agent=user_agent,
        now=now,
        settings=settings,
    )


def _verification_failed() -> AuthenticationError:
    return AuthenticationError(
        message="That confirmation link is no longer valid.",
        action="Request a new one from the sign-in screen.",
    )


# ─── Sign in ─────────────────────────────────────────────────────────────


async def sign_in(
    session: AsyncSession,
    credentials: CredentialStore,
    *,
    email: str,
    password: str,
    user_agent: str | None,
    now: datetime,
    settings: Settings | None = None,
) -> IssuedTokens:
    """Authenticate a practitioner and open a session.

    🔒 Every failure raises the identical error: unknown address, wrong password,
    unverified email, disabled or archived user. The last two matter most — "your
    account is disabled" confirms the account exists, and confirming existence is
    the disclosure (NFR-043).
    """
    verified = await credentials.verify(normalise_email(email), password)
    if not isinstance(verified, CredentialSubject):
        raise _sign_in_failed()

    identity = await repo.find_user_by_subject(session, verified.subject_id)
    if identity is None or not identity.can_sign_in:
        raise _sign_in_failed()

    return await _open_session(
        session,
        realm=AuthRealm.PRACTITIONER,
        subject_id=identity.user_id,
        tenant_id=identity.tenant_id,
        role=ContextRole(identity.role.value),
        user_agent=user_agent,
        now=now,
        settings=settings,
    )


async def _open_session(
    session: AsyncSession,
    *,
    realm: AuthRealm,
    subject_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    role: ContextRole | None,
    user_agent: str | None,
    now: datetime,
    settings: Settings | None,
) -> IssuedTokens:
    """Create the session row, then mint tokens naming it.

    Ordering matters: the session id is a claim in the access token, so the row
    must exist first. A token naming a session that was never written would fail
    every liveness check and be indistinguishable from a revoked one.
    """
    resolved = settings or get_settings()
    provisional = issue_for_session(
        realm=ContextRealm(realm.value),
        subject_id=subject_id,
        session_id=uuid.uuid4(),
        tenant_id=tenant_id,
        role=role,
        now=now,
        settings=resolved,
    )

    session_id = await repo.create_session(
        session,
        realm=realm,
        subject_id=subject_id,
        tenant_id=tenant_id,
        refresh_token_hash=hash_for_storage(provisional.refresh_token, resolved),
        issued_at=now,
        expires_at=provisional.refresh_expires_at,
        user_agent_hash=hash_user_agent(user_agent, resolved),
    )

    # Re-mint with the real session id now that the row exists.
    minted = issue_for_session(
        realm=ContextRealm(realm.value),
        subject_id=subject_id,
        session_id=session_id,
        tenant_id=tenant_id,
        role=role,
        now=now,
        settings=resolved,
    )
    return IssuedTokens(
        access_token=minted.access_token,
        # 🔒 The refresh token that was hashed into the row, not the second one —
        # re-minting produced a fresh random value which nothing stored.
        refresh_token=provisional.refresh_token,
        session_id=session_id,
        expires_in_seconds=minted.expires_in_seconds,
        refresh_expires_at=provisional.refresh_expires_at,
    )


# ─── Refresh ─────────────────────────────────────────────────────────────


async def refresh_session(
    session: AsyncSession,
    *,
    refresh_token: str,
    user_agent: str | None,
    now: datetime,
    settings: Settings | None = None,
) -> IssuedTokens:
    """🔒 Rotate a refresh token, detecting reuse (DDR-05).

    On reuse — a token that was already rotated coming back — **every session for
    that subject is revoked** and this raises. That is the signature of a stolen
    credential being replayed, and the safe response is to end the attacker's
    access and the victim's together, forcing a fresh sign-in.

    Raises:
        AuthenticationError: On every non-rotating outcome. Identical message
            throughout: a caller learning that its token was *reused* rather than
            merely expired would learn the theft had been noticed.
    """
    resolved = settings or get_settings()
    presented = hash_for_storage(refresh_token, resolved)

    snapshot = await repo.find_session_for_refresh(session, presented)
    outcome = evaluate_refresh(snapshot, presented, now=now)

    if outcome is RefreshOutcome.REUSE_DETECTED and snapshot is not None:
        revoked = await repo.revoke_all_for_subject(
            session, subject_id=snapshot.subject_id, reason="refresh_token_reuse", now=now
        )
        # 🔒 Loud. This is the one event in the system that means "a credential
        # has probably been stolen", and it must be visible in the logs as well
        # as the audit trail.
        logger.error(
            "Refresh token reuse detected — session family revoked",
            extra={
                "session_id": str(snapshot.id),
                "realm": snapshot.realm.value,
                "revoked_sessions": revoked,
            },
        )
        raise _refresh_failed()

    if outcome is not RefreshOutcome.ROTATE or snapshot is None:
        raise _refresh_failed()

    rotated = new_opaque_token()
    if not await repo.rotate_session(
        session,
        session_id=snapshot.id,
        current_hash=presented,
        new_hash=hash_for_storage(rotated, resolved),
        now=now,
    ):
        # Lost a race with a concurrent refresh. Not an attack; the caller
        # retries and the loser's token is now the previous one.
        raise _refresh_failed()

    role: ContextRole | None = None
    if snapshot.realm is ContextRealm.PRACTITIONER:
        identity = await repo.find_user_by_id(session, snapshot.subject_id)
        # 🔒 Re-checked on every refresh, not only at sign-in. A user disabled at
        # 10:00 must not keep working until a 30-day token expires.
        if identity is None or not identity.can_sign_in:
            await repo.revoke_session(
                session, session_id=snapshot.id, reason="user_not_active", now=now
            )
            raise _refresh_failed()
        role = ContextRole(identity.role.value)
        await repo.touch_last_active(session, identity.user_id, now)

    minted = issue_for_session(
        realm=snapshot.realm,
        subject_id=snapshot.subject_id,
        session_id=snapshot.id,
        tenant_id=snapshot.tenant_id,
        role=role,
        now=now,
        settings=resolved,
    )
    return IssuedTokens(
        access_token=minted.access_token,
        refresh_token=rotated,
        session_id=snapshot.id,
        expires_in_seconds=minted.expires_in_seconds,
        refresh_expires_at=snapshot.expires_at,
    )


def _refresh_failed() -> AuthenticationError:
    return AuthenticationError(
        message="Your session has ended.",
        action="Sign in again to continue.",
    )


# ─── Sign out ────────────────────────────────────────────────────────────


async def sign_out(session: AsyncSession, *, session_id: uuid.UUID, now: datetime) -> None:
    """Revoke the current session (NFR-042).

    Takes effect immediately: every authenticated request checks the session is
    live, so the access token still in the client's memory stops working on its
    next use rather than at its expiry.
    """
    await repo.revoke_session(session, session_id=session_id, reason="logout", now=now)


# ─── Password reset ──────────────────────────────────────────────────────


async def request_password_reset(
    session: AsyncSession,
    *,
    email: str,
    now: datetime,
    settings: Settings | None = None,
) -> str | None:
    """Issue a reset token, or quietly do nothing (FR-M0-003).

    🔒 Returns ``None`` for an unknown address and the caller responds 202
    regardless. The token, when there is one, goes to the address by email —
    which is the only channel that proves the requester owns it.
    """
    address = normalise_email(email)
    identity_subject = await repo.find_subject_by_email(session, address)
    if identity_subject is None:
        return None

    return await _issue_auth_token(
        session,
        auth_subject_id=identity_subject,
        purpose=AuthTokenPurpose.PASSWORD_RESET,
        now=now,
        ttl=timedelta(hours=1),
        settings=settings,
    )


async def confirm_password_reset(
    session: AsyncSession,
    credentials: CredentialStore,
    *,
    token: str,
    new_password: str,
    now: datetime,
    settings: Settings | None = None,
) -> None:
    """Redeem a reset token and set the new password.

    🔒 Every session for the subject is revoked afterwards (NFR-042). A password
    reset is usually a response to suspected compromise, and leaving the
    attacker's refresh token working would defeat the point of the reset.

    Raises:
        AuthenticationError: If the token is unusable.
        ValidationError: If the new password fails the strength check.
    """
    subject = await repo.consume_auth_token(
        session,
        token_hash=hash_for_storage(token, settings),
        purpose=AuthTokenPurpose.PASSWORD_RESET,
        now=now,
    )
    if subject is None:
        raise AuthenticationError(
            message="That reset link is no longer valid.",
            action="Request a new one and use it within the hour.",
        )

    outcome = await credentials.set_password(subject, new_password)
    if outcome is CredentialError.WEAK_PASSWORD:
        raise ValidationError.for_fields(
            [
                {
                    "field": "password",
                    "code": "weak_password",
                    "message": "Choose a longer password, or one that is less commonly used.",
                }
            ]
        )
    if outcome is not None:
        raise AuthenticationError(
            message="That reset link is no longer valid.",
            action="Request a new one and use it within the hour.",
        )

    identity = await repo.find_user_by_subject(session, subject)
    if identity is not None:
        await repo.revoke_all_for_subject(
            session, subject_id=identity.user_id, reason="password_reset", now=now
        )


# ─── Client portal (magic links) ─────────────────────────────────────────


async def issue_magic_link(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    purpose: LinkPurpose,
    target_ref: str | None,
    transport: TransportType,
    now: datetime,
    settings: Settings | None = None,
) -> str:
    """Mint a portal access link (FR-M0-005). Returns the token to be delivered.

    🔒 The plaintext exists only in this return value; the database gets a hash
    (DDR-04). Expiry is 15–30 minutes by configuration — short enough that a
    forwarded WhatsApp message is near-useless, which is why self-service
    re-request is a primary flow rather than an error path (EC-M7-01).
    """
    resolved = settings or get_settings()
    token = new_opaque_token()
    await repo.store_magic_link(
        session,
        tenant_id=tenant_id,
        client_id=client_id,
        token_hash=hash_for_storage(token, resolved),
        purpose=purpose,
        target_ref=target_ref,
        expires_at=now + timedelta(minutes=resolved.magic_link_ttl_minutes),
        issued_via=transport,
    )
    return token


async def redeem_magic_link(
    session: AsyncSession,
    *,
    token: str,
    user_agent: str | None,
    now: datetime,
    settings: Settings | None = None,
) -> PortalSession:
    """Exchange a magic link for a client-realm session.

    🔒 Single use and expiry are enforced by the redeeming statement, not here
    (DDR-04) — two taps on the same link cannot both open a session.

    Raises:
        AuthenticationError: If the link is expired, already used, unknown, or
            the client's access has since been revoked. One error for all four:
            the client's next step is the same, and it is to request a new link.
    """
    resolved = settings or get_settings()
    redeemed = await repo.consume_magic_link(
        session, token_hash=hash_for_storage(token, resolved), now=now
    )
    if redeemed is None:
        raise _link_failed()

    # 🔒 Re-checked at redemption. A grant revoked between sending the link and
    # opening it must not still let the client in.
    if await repo.client_grant_is_active(session, client_id=redeemed.client_id) is None:
        raise _link_failed()

    await repo.touch_grant_access(session, client_id=redeemed.client_id, when=now)

    tokens = await _open_session(
        session,
        realm=AuthRealm.CLIENT,
        subject_id=redeemed.client_id,
        tenant_id=redeemed.tenant_id,
        role=ContextRole.CLIENT,
        user_agent=user_agent,
        now=now,
        settings=resolved,
    )

    return PortalSession(
        tokens=tokens,
        tenant_id=redeemed.tenant_id,
        client_id=redeemed.client_id,
        target_type=redeemed.purpose,
        target_ref=redeemed.target_ref,
    )


def _link_failed() -> AuthenticationError:
    return AuthenticationError(
        message="That link has expired or has already been used.",
        action="Request a new link — it only takes a moment.",
    )


__all__ = [
    "PortalSession",
    "Registration",
    "confirm_password_reset",
    "issue_magic_link",
    "redeem_magic_link",
    "refresh_session",
    "register_practitioner",
    "request_password_reset",
    "sign_in",
    "sign_out",
    "verify_email",
]
