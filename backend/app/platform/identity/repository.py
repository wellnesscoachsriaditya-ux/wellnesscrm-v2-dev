"""Identity persistence — sessions, users, one-time tokens.

Every statement that identity needs, in one place. The service layer above holds
the flow; this holds the SQL, so a change to how a session is rotated is a change
to one function rather than to a handler.

🔒 Two statements here are security mechanisms rather than data access, and both
are written as **conditional updates returning the affected row**:

* :func:`consume_auth_token` and :func:`consume_magic_link` — single use
  (DDR-04). ``UPDATE ... WHERE consumed_at IS NULL`` lets the database decide
  which of two concurrent redemptions wins. A read-then-write in Python has a
  window in which both succeed, and that window is exactly what a replayed link
  exploits.
* :func:`rotate_session` — the refresh rotation (DDR-05), which must move the
  current hash into the previous slot atomically.

⚠️ These take an ``AsyncSession`` as an argument rather than importing one. The
transaction belongs to the request pipeline (ADR-04); a repository that opened
its own would commit independently of the change it accompanies.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.context import AuthRealm as ContextRealm
from app.kernel.identity import SessionSnapshot
from app.kernel.models import (
    AuthRealm,
    AuthToken,
    AuthTokenPurpose,
    ClientAccessGrant,
    MagicLink,
    Session,
    Tenant,
    User,
    UserRole,
    UserStatus,
)


@dataclass(frozen=True, slots=True)
class PractitionerIdentity:
    """A practitioner, resolved. 🔒 Identifiers and status only.

    No email or name: this is what the token pipeline consumes, and the pipeline
    writes into audit rows and log lines (NFR-033).
    """

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: UserRole
    status: UserStatus
    is_archived: bool

    @property
    def can_sign_in(self) -> bool:
        """🔒 Only an active, unarchived user may hold a session.

        Checked at sign-in *and* on every refresh: a user disabled at 10:00 must
        not keep working until their 30-day refresh token expires.
        """
        return self.status is UserStatus.ACTIVE and not self.is_archived


# ─── Users ───────────────────────────────────────────────────────────────


def _practitioner_from(row: User) -> PractitionerIdentity:
    return PractitionerIdentity(
        user_id=row.id,
        tenant_id=row.tenant_id,
        role=row.role,
        status=row.status,
        is_archived=row.archived_at is not None,
    )


async def find_user_by_subject(
    session: AsyncSession, auth_subject_id: str
) -> PractitionerIdentity | None:
    """Resolve the identity provider's subject to our user row."""
    row = (
        await session.execute(select(User).where(User.auth_subject_id == auth_subject_id))
    ).scalar_one_or_none()
    return _practitioner_from(row) if row is not None else None


async def find_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> PractitionerIdentity | None:
    row = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    return _practitioner_from(row) if row is not None else None


async def find_subject_by_email(session: AsyncSession, email: str) -> str | None:
    """Find the identity-provider subject behind an address, if any.

    🔒 Used by the password-reset request path, whose response is identical
    whether or not this returns something.
    """
    return (
        await session.execute(
            select(User.auth_subject_id)
            .where(and_(User.email == email, User.archived_at.is_(None)))
            .limit(1)
        )
    ).scalar_one_or_none()


async def email_is_registered(session: AsyncSession, email: str) -> bool:
    """⚠️ Used only to decide which *notification* to send, never what to return.

    Registration responds identically either way (NFR-043); the difference is
    whether the recipient gets "confirm your address" or "someone tried to
    register with your address".
    """
    found = (
        await session.execute(
            select(User.id).where(and_(User.email == email, User.archived_at.is_(None))).limit(1)
        )
    ).scalar_one_or_none()
    return found is not None


async def create_tenant_with_owner(
    session: AsyncSession,
    *,
    practice_name: str,
    slug: str,
    auth_subject_id: str,
    email: str,
    full_name: str,
    mobile: str | None,
    region_code: str,
    timezone: str,
    currency_code: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create the tenant and its owner. Returns ``(tenant_id, user_id)``.

    🔒 The user is created ``INVITED``, not ``ACTIVE``. Email verification
    precedes access (FR-M0-002), and the status is what enforces it — not a flag
    the login handler remembers to consult.

    Both rows are written in the caller's transaction, so a failure part-way
    leaves no tenant without an owner.
    """
    tenant = Tenant(
        name=practice_name,
        slug=slug,
        region_code=region_code,
        timezone=timezone,
        currency_code=currency_code,
    )
    session.add(tenant)
    await session.flush()

    user = User(
        tenant_id=tenant.id,
        auth_subject_id=auth_subject_id,
        email=email,
        full_name=full_name,
        mobile=mobile,
        role=UserRole.OWNER,
        status=UserStatus.INVITED,
    )
    session.add(user)
    await session.flush()

    return tenant.id, user.id


async def activate_user(session: AsyncSession, auth_subject_id: str) -> uuid.UUID | None:
    """Mark a verified user active. Returns the user id, or ``None`` if absent."""
    result = await session.execute(
        update(User)
        .where(and_(User.auth_subject_id == auth_subject_id, User.archived_at.is_(None)))
        .values(status=UserStatus.ACTIVE, updated_at=datetime.now(tz=None))
        .returning(User.id)
    )
    return result.scalar_one_or_none()


async def touch_last_active(session: AsyncSession, user_id: uuid.UUID, when: datetime) -> None:
    await session.execute(update(User).where(User.id == user_id).values(last_active_at=when))


# ─── One-time tokens ─────────────────────────────────────────────────────


async def store_auth_token(
    session: AsyncSession,
    *,
    auth_subject_id: str,
    token_hash: str,
    purpose: AuthTokenPurpose,
    expires_at: datetime,
) -> None:
    session.add(
        AuthToken(
            auth_subject_id=auth_subject_id,
            token_hash=token_hash,
            purpose=purpose,
            expires_at=expires_at,
        )
    )


async def consume_auth_token(
    session: AsyncSession,
    *,
    token_hash: str,
    purpose: AuthTokenPurpose,
    now: datetime,
) -> str | None:
    """🔒 Redeem a one-time token, atomically. Returns its subject, or ``None``.

    Single use is a property of this statement, not of the code around it: the
    ``consumed_at IS NULL`` predicate is evaluated by PostgreSQL under the row
    lock it takes to perform the update, so exactly one of two concurrent
    redemptions can match.

    🔒 ``purpose`` is part of the predicate. A token minted to verify an email
    address must not be redeemable at the password-reset endpoint.
    """
    result = await session.execute(
        update(AuthToken)
        .where(
            and_(
                AuthToken.token_hash == token_hash,
                AuthToken.purpose == purpose,
                AuthToken.consumed_at.is_(None),
                AuthToken.expires_at > now,
            )
        )
        .values(consumed_at=now)
        .returning(AuthToken.auth_subject_id)
    )
    return result.scalar_one_or_none()


async def invalidate_auth_tokens(
    session: AsyncSession,
    *,
    auth_subject_id: str,
    purpose: AuthTokenPurpose,
    now: datetime,
) -> None:
    """Consume every outstanding token of a purpose.

    🔒 Called when a new one is issued, so requesting a second reset link
    invalidates the first. Otherwise every request adds a live credential and the
    account's exposure grows with the user's confusion.
    """
    await session.execute(
        update(AuthToken)
        .where(
            and_(
                AuthToken.auth_subject_id == auth_subject_id,
                AuthToken.purpose == purpose,
                AuthToken.consumed_at.is_(None),
            )
        )
        .values(consumed_at=now)
    )


# ─── Sessions ────────────────────────────────────────────────────────────


def _snapshot(row: Session) -> SessionSnapshot:
    return SessionSnapshot(
        id=row.id,
        realm=ContextRealm(row.realm.value),
        subject_id=row.subject_id,
        tenant_id=row.tenant_id,
        refresh_token_hash=row.refresh_token_hash,
        previous_token_hash=row.previous_token_hash,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )


def _session_by_either_hash(token_hash: str) -> Select[tuple[Session]]:
    """🔒 Find a session by its current *or* previously-rotated token.

    One statement rather than two lookups: the reuse-detection verdict must not
    depend on which query ran first, and `kernel.identity.evaluate_refresh`
    can only be trusted to decide if it is handed whatever exists.
    """
    return select(Session).where(
        or_(
            Session.refresh_token_hash == token_hash,
            Session.previous_token_hash == token_hash,
        )
    )


async def find_session_for_refresh(
    session: AsyncSession, token_hash: str
) -> SessionSnapshot | None:
    row = (await session.execute(_session_by_either_hash(token_hash))).scalar_one_or_none()
    return _snapshot(row) if row is not None else None


async def create_session(
    session: AsyncSession,
    *,
    realm: AuthRealm,
    subject_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    refresh_token_hash: str,
    issued_at: datetime,
    expires_at: datetime,
    user_agent_hash: str | None,
) -> uuid.UUID:
    row = Session(
        realm=realm,
        subject_id=subject_id,
        tenant_id=tenant_id,
        refresh_token_hash=refresh_token_hash,
        issued_at=issued_at,
        expires_at=expires_at,
        user_agent_hash=user_agent_hash,
    )
    session.add(row)
    await session.flush()
    return row.id


async def rotate_session(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    current_hash: str,
    new_hash: str,
    now: datetime,
) -> bool:
    """🔒 Rotate a refresh token (DDR-05). ``True`` if this call performed it.

    ``refresh_token_hash == current_hash`` is in the predicate so two concurrent
    refreshes with the same token cannot both rotate: the loser matches nothing
    and its caller re-reads a session whose previous hash is now the token it
    holds — which the next attempt correctly reads as reuse.
    """
    result = await session.execute(
        update(Session)
        .where(
            and_(
                Session.id == session_id,
                Session.refresh_token_hash == current_hash,
                Session.revoked_at.is_(None),
            )
        )
        .values(
            refresh_token_hash=new_hash,
            previous_token_hash=current_hash,
            rotated_at=now,
        )
        .returning(Session.id)
    )
    return result.scalar_one_or_none() is not None


async def revoke_session(
    session: AsyncSession, *, session_id: uuid.UUID, reason: str, now: datetime
) -> None:
    await session.execute(
        update(Session)
        .where(and_(Session.id == session_id, Session.revoked_at.is_(None)))
        .values(revoked_at=now, revocation_reason=reason)
    )


async def revoke_all_for_subject(
    session: AsyncSession, *, subject_id: uuid.UUID, reason: str, now: datetime
) -> int:
    """🔒 Revoke every live session for a subject.

    Used on reuse detection (DDR-05) and on password change (NFR-042). Both are
    "we no longer trust anything issued to this person", and revoking only the
    session in hand would leave the attacker's other tokens working.
    """
    result = await session.execute(
        update(Session)
        .where(and_(Session.subject_id == subject_id, Session.revoked_at.is_(None)))
        .values(revoked_at=now, revocation_reason=reason)
        .returning(Session.id)
    )
    return len(result.scalars().all())


async def is_session_live(session: AsyncSession, *, session_id: uuid.UUID, now: datetime) -> bool:
    """🔒 Is this session still valid?

    ⚠️ Called on every authenticated request, which is what makes logout and
    revocation take effect immediately rather than when the access token expires.
    A stateless-JWT-only design would leave a revoked session working for up to
    the access token's lifetime, and "we revoked it, but not for another quarter
    of an hour" is not revocation.
    """
    found = (
        await session.execute(
            select(Session.id)
            .where(
                and_(
                    Session.id == session_id,
                    Session.revoked_at.is_(None),
                    Session.expires_at > now,
                )
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return found is not None


# ─── Client realm ────────────────────────────────────────────────────────


async def store_magic_link(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    token_hash: str,
    purpose: object,
    target_ref: str | None,
    expires_at: datetime,
    issued_via: object,
) -> None:
    session.add(
        MagicLink(
            tenant_id=tenant_id,
            client_id=client_id,
            token_hash=token_hash,
            purpose=purpose,
            target_ref=target_ref,
            expires_at=expires_at,
            issued_via=issued_via,
        )
    )


@dataclass(frozen=True, slots=True)
class RedeemedLink:
    """What a consumed magic link yields."""

    tenant_id: uuid.UUID
    client_id: uuid.UUID
    purpose: str
    target_ref: str | None


async def consume_magic_link(
    session: AsyncSession, *, token_hash: str, now: datetime
) -> RedeemedLink | None:
    """🔒 Redeem a magic link, atomically (DDR-04). ``None`` if unusable.

    Expiry and single use are both in the predicate, so an expired link and an
    already-used one are indistinguishable to the caller — as they should be, and
    as they cannot be if either check happens in Python.
    """
    result = await session.execute(
        update(MagicLink)
        .where(
            and_(
                MagicLink.token_hash == token_hash,
                MagicLink.consumed_at.is_(None),
                MagicLink.expires_at > now,
            )
        )
        .values(consumed_at=now)
        .returning(
            MagicLink.tenant_id,
            MagicLink.client_id,
            MagicLink.purpose,
            MagicLink.target_ref,
        )
    )
    row = result.one_or_none()
    if row is None:
        return None
    return RedeemedLink(
        tenant_id=row.tenant_id,
        client_id=row.client_id,
        purpose=row.purpose.value if hasattr(row.purpose, "value") else str(row.purpose),
        target_ref=row.target_ref,
    )


async def client_grant_is_active(
    session: AsyncSession, *, client_id: uuid.UUID
) -> uuid.UUID | None:
    """🔒 Return the grant id if the client may still reach the portal.

    Checked on redemption as well as at issue: a grant revoked between the link
    being sent and opened must not still work.
    """
    found = (
        await session.execute(
            select(ClientAccessGrant.id)
            .where(
                and_(
                    ClientAccessGrant.client_id == client_id,
                    ClientAccessGrant.status == "active",
                )
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return found


async def touch_grant_access(
    session: AsyncSession, *, client_id: uuid.UUID, when: datetime
) -> None:
    await session.execute(
        update(ClientAccessGrant)
        .where(ClientAccessGrant.client_id == client_id)
        .values(last_accessed_at=when)
    )
