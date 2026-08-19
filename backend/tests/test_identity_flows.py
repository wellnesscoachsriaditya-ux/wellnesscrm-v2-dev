"""The authentication flows.

🔒 **Slice B's gate.** Registration, verification, sign-in, refresh rotation,
logout, password reset and magic-link redemption, exercised as flows against a
dict-backed repository.

Tested at the *service* layer rather than through HTTP, deliberately: the
verification and reset tokens are returned inside the process and delivered by
email (S5), never in a response body. Driving these through HTTP would mean
either guessing a 256-bit token or leaking it into the API — and the second is
the thing the design exists to prevent.

⚠️ What is **not** asserted here: that the SQL is right. The doubles below
implement the *observable behaviour* of each statement — notably the two
conditional updates that make single use and rotation atomic — so a wrong
`WHERE` clause would still pass. That needs PostgreSQL, and it is why
`tests/integration/` exists.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.kernel.errors import AuthenticationError, ValidationError
from app.kernel.identity import SessionSnapshot
from app.kernel.models import (
    AuthRealm,
    AuthTokenPurpose,
    LinkPurpose,
    TransportType,
    UserRole,
    UserStatus,
)
from app.platform.identity import repository as repo
from app.platform.identity import service
from app.platform.identity.credentials import LocalCredentialStore
from app.platform.identity.repository import PractitionerIdentity, RedeemedLink
from app.platform.identity.tokens import hash_for_storage

GOOD_PASSWORD = "correct-horse-battery"
OTHER_PASSWORD = "a-different-long-one"
EMAIL = "ada@example.com"
NOW = datetime.now(UTC).replace(microsecond=0)


# ─── The double ──────────────────────────────────────────────────────────


@dataclass
class SessionRow:
    id: uuid.UUID
    realm: AuthRealm
    subject_id: uuid.UUID
    tenant_id: uuid.UUID | None
    refresh_token_hash: str
    previous_token_hash: str | None
    expires_at: datetime
    revoked_at: datetime | None = None
    revocation_reason: str | None = None


@dataclass
class FakeDatabase:
    users: dict[str, PractitionerIdentity] = field(default_factory=dict)
    emails: dict[str, str] = field(default_factory=dict)
    sessions: dict[uuid.UUID, SessionRow] = field(default_factory=dict)
    # token_hash -> (subject, purpose, expires_at, consumed)
    auth_tokens: dict[str, tuple[str, AuthTokenPurpose, datetime, bool]] = field(
        default_factory=dict
    )
    # token_hash -> (tenant, client, expires_at, consumed, purpose, target_ref)
    magic_links: dict[str, tuple[uuid.UUID, uuid.UUID, datetime, bool, str, str | None]] = field(
        default_factory=dict
    )
    active_grants: set[uuid.UUID] = field(default_factory=set)


@pytest.fixture
def db() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def store() -> LocalCredentialStore:
    return LocalCredentialStore()


@pytest.fixture(autouse=True)
def fake_repo(db: FakeDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace each repository function with a dict-backed equivalent.

    Patched on the `repo` module object, which is what `service` holds a
    reference to — so the service under test is entirely unmodified.
    """

    async def find_user_by_subject(_s: Any, auth_subject_id: str) -> Any:
        return db.users.get(auth_subject_id)

    async def find_user_by_id(_s: Any, user_id: uuid.UUID) -> Any:
        return next((u for u in db.users.values() if u.user_id == user_id), None)

    async def find_subject_by_email(_s: Any, email: str) -> str | None:
        return db.emails.get(email)

    async def create_tenant_with_owner(
        _s: Any, *, auth_subject_id: str, email: str, **_kw: Any
    ) -> tuple[uuid.UUID, uuid.UUID]:
        tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
        db.users[auth_subject_id] = PractitionerIdentity(
            user_id=user_id,
            tenant_id=tenant_id,
            role=UserRole.OWNER,
            # 🔒 INVITED, not ACTIVE — verification precedes access.
            status=UserStatus.INVITED,
            is_archived=False,
        )
        db.emails[email] = auth_subject_id
        return tenant_id, user_id

    async def activate_user(_s: Any, auth_subject_id: str) -> uuid.UUID | None:
        found = db.users.get(auth_subject_id)
        if found is None:
            return None
        db.users[auth_subject_id] = PractitionerIdentity(
            user_id=found.user_id,
            tenant_id=found.tenant_id,
            role=found.role,
            status=UserStatus.ACTIVE,
            is_archived=found.is_archived,
        )
        return found.user_id

    async def touch_last_active(_s: Any, _user_id: uuid.UUID, _when: datetime) -> None:
        return None

    async def store_auth_token(
        _s: Any,
        *,
        auth_subject_id: str,
        token_hash: str,
        purpose: AuthTokenPurpose,
        expires_at: datetime,
    ) -> None:
        db.auth_tokens[token_hash] = (auth_subject_id, purpose, expires_at, False)

    async def invalidate_auth_tokens(
        _s: Any, *, auth_subject_id: str, purpose: AuthTokenPurpose, now: datetime
    ) -> None:
        for key, (subject, stored, expires, consumed) in list(db.auth_tokens.items()):
            if subject == auth_subject_id and stored is purpose and not consumed:
                db.auth_tokens[key] = (subject, stored, expires, True)

    async def consume_auth_token(
        _s: Any, *, token_hash: str, purpose: AuthTokenPurpose, now: datetime
    ) -> str | None:
        found = db.auth_tokens.get(token_hash)
        if found is None:
            return None
        subject, stored_purpose, expires_at, consumed = found
        # 🔒 The `WHERE consumed_at IS NULL AND purpose = :p AND expires_at > now`
        # predicate — all three, so purpose confusion and replay both fail here.
        if consumed or stored_purpose is not purpose or expires_at <= now:
            return None
        db.auth_tokens[token_hash] = (subject, stored_purpose, expires_at, True)
        return subject

    async def create_session(
        _s: Any,
        *,
        realm: AuthRealm,
        subject_id: uuid.UUID,
        tenant_id: uuid.UUID | None,
        refresh_token_hash: str,
        issued_at: datetime,
        expires_at: datetime,
        user_agent_hash: str | None,
    ) -> uuid.UUID:
        session_id = uuid.uuid4()
        db.sessions[session_id] = SessionRow(
            id=session_id,
            realm=realm,
            subject_id=subject_id,
            tenant_id=tenant_id,
            refresh_token_hash=refresh_token_hash,
            previous_token_hash=None,
            expires_at=expires_at,
        )
        return session_id

    async def find_session_for_refresh(_s: Any, token_hash: str) -> SessionSnapshot | None:
        for row in db.sessions.values():
            if token_hash in (row.refresh_token_hash, row.previous_token_hash):
                return SessionSnapshot(
                    id=row.id,
                    realm=service.ContextRealm(row.realm.value),
                    subject_id=row.subject_id,
                    tenant_id=row.tenant_id,
                    refresh_token_hash=row.refresh_token_hash,
                    previous_token_hash=row.previous_token_hash,
                    expires_at=row.expires_at,
                    revoked_at=row.revoked_at,
                )
        return None

    async def rotate_session(
        _s: Any,
        *,
        session_id: uuid.UUID,
        current_hash: str,
        new_hash: str,
        now: datetime,
    ) -> bool:
        row = db.sessions.get(session_id)
        # 🔒 `WHERE refresh_token_hash = :current AND revoked_at IS NULL` — a
        # second rotation from the same starting point matches nothing.
        if row is None or row.revoked_at is not None or row.refresh_token_hash != current_hash:
            return False
        row.previous_token_hash = current_hash
        row.refresh_token_hash = new_hash
        return True

    async def revoke_session(_s: Any, *, session_id: uuid.UUID, reason: str, now: datetime) -> None:
        row = db.sessions.get(session_id)
        if row is not None and row.revoked_at is None:
            row.revoked_at = now
            row.revocation_reason = reason

    async def revoke_all_for_subject(
        _s: Any, *, subject_id: uuid.UUID, reason: str, now: datetime
    ) -> int:
        count = 0
        for row in db.sessions.values():
            if row.subject_id == subject_id and row.revoked_at is None:
                row.revoked_at = now
                row.revocation_reason = reason
                count += 1
        return count

    async def is_session_live(_s: Any, *, session_id: uuid.UUID, now: datetime) -> bool:
        row = db.sessions.get(session_id)
        return row is not None and row.revoked_at is None and row.expires_at > now

    async def store_magic_link(
        _s: Any,
        *,
        tenant_id: uuid.UUID,
        client_id: uuid.UUID,
        token_hash: str,
        purpose: Any,
        target_ref: str | None,
        expires_at: datetime,
        issued_via: Any,
    ) -> None:
        db.magic_links[token_hash] = (
            tenant_id,
            client_id,
            expires_at,
            False,
            purpose.value,
            target_ref,
        )

    async def consume_magic_link(_s: Any, *, token_hash: str, now: datetime) -> RedeemedLink | None:
        found = db.magic_links.get(token_hash)
        if found is None:
            return None
        tenant_id, client_id, expires_at, consumed, purpose, target_ref = found
        # 🔒 Single use and expiry in one predicate, so an expired link and an
        # already-used one are indistinguishable to the caller.
        if consumed or expires_at <= now:
            return None
        db.magic_links[token_hash] = (
            tenant_id,
            client_id,
            expires_at,
            True,
            purpose,
            target_ref,
        )
        return RedeemedLink(
            tenant_id=tenant_id,
            client_id=client_id,
            purpose=purpose,
            target_ref=target_ref,
        )

    async def client_grant_is_active(_s: Any, *, client_id: uuid.UUID) -> uuid.UUID | None:
        return client_id if client_id in db.active_grants else None

    async def touch_grant_access(_s: Any, *, client_id: uuid.UUID, when: datetime) -> None:
        return None

    for name, value in list(locals().items()):
        if callable(value) and hasattr(repo, name) and name not in {"db", "monkeypatch"}:
            monkeypatch.setattr(repo, name, value)


SESSION: Any = object()  # the doubles ignore it


# ─── Registration ────────────────────────────────────────────────────────


async def _register(store: LocalCredentialStore, email: str = EMAIL) -> service.Registration:
    return await service.register_practitioner(
        SESSION,
        store,
        email=email,
        password=GOOD_PASSWORD,
        full_name="Ada Lovelace",
        practice_name="Ada Nutrition",
        mobile=None,
        now=NOW,
    )


async def test_registration_creates_a_tenant_and_an_invited_owner(
    store: LocalCredentialStore, db: FakeDatabase
) -> None:
    """🔒 FR-M0-001 / FR-M0-002 — the account exists but cannot yet sign in."""
    result = await _register(store)

    assert result.tenant_id is not None
    assert result.verification_token is not None
    assert not result.duplicate_email
    assert next(iter(db.users.values())).status is UserStatus.INVITED


async def test_a_duplicate_address_creates_nothing_and_says_nothing(
    store: LocalCredentialStore, db: FakeDatabase
) -> None:
    """🔒 NFR-043 — the shape is identical; only `duplicate_email` differs, and
    that flag never leaves the process."""
    await _register(store)
    second = await _register(store)

    assert second.duplicate_email
    assert second.tenant_id is None
    assert second.verification_token is None
    assert len(db.users) == 1


async def test_a_duplicate_address_is_detected_case_insensitively(
    store: LocalCredentialStore, db: FakeDatabase
) -> None:
    await _register(store, "Ada@Example.com")
    second = await _register(store, "ada@example.com")

    assert second.duplicate_email
    assert len(db.users) == 1


async def test_a_weak_password_is_refused(store: LocalCredentialStore) -> None:
    with pytest.raises(ValidationError):
        await service.register_practitioner(
            SESSION,
            store,
            email=EMAIL,
            password="password123",
            full_name="Ada",
            practice_name="Ada Nutrition",
            mobile=None,
            now=NOW,
        )


async def test_slugs_do_not_collide_for_identical_practice_names(
    store: LocalCredentialStore,
) -> None:
    """🔒 Random suffix, not a counter. A counter publishes how many practices
    exist and invites guessing the neighbours."""
    slugs = {service._slugify("Ada Nutrition") for _ in range(50)}

    assert len(slugs) > 45
    assert all(s.startswith("ada-nutrition-") for s in slugs)


# ─── Email verification ──────────────────────────────────────────────────


async def test_verification_activates_the_user_and_returns_tokens(
    store: LocalCredentialStore, db: FakeDatabase
) -> None:
    registered = await _register(store)
    assert registered.verification_token is not None

    tokens = await service.verify_email(SESSION, token=registered.verification_token, now=NOW)

    assert tokens.access_token
    assert tokens.refresh_token
    assert next(iter(db.users.values())).status is UserStatus.ACTIVE


async def test_a_verification_token_is_single_use(store: LocalCredentialStore) -> None:
    """🔒 DDR-04 — enforced by the conditional update, modelled in the double."""
    registered = await _register(store)
    assert registered.verification_token is not None
    await service.verify_email(SESSION, token=registered.verification_token, now=NOW)

    with pytest.raises(AuthenticationError):
        await service.verify_email(SESSION, token=registered.verification_token, now=NOW)


async def test_an_expired_verification_token_is_refused(
    store: LocalCredentialStore,
) -> None:
    registered = await _register(store)
    assert registered.verification_token is not None

    with pytest.raises(AuthenticationError):
        await service.verify_email(
            SESSION, token=registered.verification_token, now=NOW + timedelta(days=2)
        )


async def test_an_unknown_verification_token_is_refused() -> None:
    with pytest.raises(AuthenticationError):
        await service.verify_email(SESSION, token="not-a-real-token", now=NOW)


async def test_a_reset_token_cannot_be_redeemed_as_a_verification_token(
    store: LocalCredentialStore,
) -> None:
    """🔒 The reason `purpose` is stored and checked on redemption.

    Without it, a token minted to prove mailbox ownership would be redeemable at
    the password-reset endpoint, and vice versa — account takeover using a link
    the product itself sent.
    """
    await _verified(store)
    reset_token = await service.request_password_reset(SESSION, email=EMAIL, now=NOW)
    assert reset_token is not None

    with pytest.raises(AuthenticationError):
        await service.verify_email(SESSION, token=reset_token, now=NOW)


async def test_a_verification_token_cannot_reset_a_password(
    store: LocalCredentialStore,
) -> None:
    """The same guard in the other direction — the dangerous one."""
    registered = await _register(store)
    assert registered.verification_token is not None

    with pytest.raises(AuthenticationError):
        await service.confirm_password_reset(
            SESSION,
            store,
            token=registered.verification_token,
            new_password=OTHER_PASSWORD,
            now=NOW,
        )


# ─── Sign in ─────────────────────────────────────────────────────────────


async def _verified(store: LocalCredentialStore) -> service.Registration:
    registered = await _register(store)
    assert registered.verification_token is not None
    await service.verify_email(SESSION, token=registered.verification_token, now=NOW)
    return registered


async def test_a_verified_user_can_sign_in(store: LocalCredentialStore) -> None:
    await _verified(store)

    tokens = await service.sign_in(
        SESSION, store, email=EMAIL, password=GOOD_PASSWORD, user_agent=None, now=NOW
    )

    assert tokens.access_token
    assert tokens.session_id is not None


async def test_an_unverified_user_cannot_sign_in(store: LocalCredentialStore) -> None:
    """🔒 The status check is what enforces FR-M0-002, not a flag someone
    remembers to consult."""
    await _register(store)

    with pytest.raises(AuthenticationError):
        await service.sign_in(
            SESSION, store, email=EMAIL, password=GOOD_PASSWORD, user_agent=None, now=NOW
        )


async def test_every_sign_in_failure_looks_identical(
    store: LocalCredentialStore, db: FakeDatabase
) -> None:
    """🔒 NFR-043 — unknown address, wrong password and disabled account.

    The third matters most: "your account is disabled" confirms the account
    exists, and confirming existence is the disclosure.
    """
    await _verified(store)
    messages = set()

    for email, password in (
        ("nobody@example.com", GOOD_PASSWORD),
        (EMAIL, "wrong-but-long-enough"),
    ):
        with pytest.raises(AuthenticationError) as raised:
            await service.sign_in(
                SESSION, store, email=email, password=password, user_agent=None, now=NOW
            )
        messages.add((raised.value.message, raised.value.action))

    # ...and a disabled account.
    subject = db.emails[EMAIL]
    existing = db.users[subject]
    db.users[subject] = PractitionerIdentity(
        user_id=existing.user_id,
        tenant_id=existing.tenant_id,
        role=existing.role,
        status=UserStatus.DISABLED,
        is_archived=False,
    )
    with pytest.raises(AuthenticationError) as raised:
        await service.sign_in(
            SESSION, store, email=EMAIL, password=GOOD_PASSWORD, user_agent=None, now=NOW
        )
    messages.add((raised.value.message, raised.value.action))

    assert len(messages) == 1


async def test_an_archived_user_cannot_sign_in(
    store: LocalCredentialStore, db: FakeDatabase
) -> None:
    await _verified(store)
    subject = db.emails[EMAIL]
    existing = db.users[subject]
    db.users[subject] = PractitionerIdentity(
        user_id=existing.user_id,
        tenant_id=existing.tenant_id,
        role=existing.role,
        status=UserStatus.ACTIVE,
        is_archived=True,
    )

    with pytest.raises(AuthenticationError):
        await service.sign_in(
            SESSION, store, email=EMAIL, password=GOOD_PASSWORD, user_agent=None, now=NOW
        )


async def test_the_stored_refresh_hash_matches_the_returned_token(
    store: LocalCredentialStore, db: FakeDatabase
) -> None:
    """⚠️ Guards a subtle bug: the session is created before the id is known, so
    tokens are minted twice. Returning the *second* random refresh token would
    hand the caller a credential nothing stored."""
    await _verified(store)

    tokens = await service.sign_in(
        SESSION, store, email=EMAIL, password=GOOD_PASSWORD, user_agent=None, now=NOW
    )

    row = db.sessions[tokens.session_id]
    assert row.refresh_token_hash == hash_for_storage(tokens.refresh_token)


# ─── 🔒 Refresh rotation and reuse (DDR-05) ──────────────────────────────


async def _signed_in(store: LocalCredentialStore) -> Any:
    await _verified(store)
    return await service.sign_in(
        SESSION, store, email=EMAIL, password=GOOD_PASSWORD, user_agent=None, now=NOW
    )


async def test_refresh_rotates_the_token(store: LocalCredentialStore) -> None:
    first = await _signed_in(store)

    second = await service.refresh_session(
        SESSION, refresh_token=first.refresh_token, user_agent=None, now=NOW
    )

    assert second.refresh_token != first.refresh_token
    assert second.session_id == first.session_id


async def test_the_old_token_stops_working_after_rotation(
    store: LocalCredentialStore,
) -> None:
    first = await _signed_in(store)
    await service.refresh_session(
        SESSION, refresh_token=first.refresh_token, user_agent=None, now=NOW
    )

    with pytest.raises(AuthenticationError):
        await service.refresh_session(
            SESSION, refresh_token=first.refresh_token, user_agent=None, now=NOW
        )


async def test_replaying_a_rotated_token_revokes_every_session(
    store: LocalCredentialStore, db: FakeDatabase
) -> None:
    """🔒 DDR-05, the property this whole mechanism exists for.

    The victim refreshed, so the old token moved to `previous`. Its reappearance
    means someone else has a copy — and the response is to end everyone's access
    and force a fresh sign-in, not to refuse one request.
    """
    first = await _signed_in(store)
    second = await service.refresh_session(
        SESSION, refresh_token=first.refresh_token, user_agent=None, now=NOW
    )

    with pytest.raises(AuthenticationError):
        await service.refresh_session(
            SESSION, refresh_token=first.refresh_token, user_agent=None, now=NOW
        )

    # 🔒 The still-current token is dead too — that is the family revocation.
    assert db.sessions[first.session_id].revoked_at is not None
    assert db.sessions[first.session_id].revocation_reason == "refresh_token_reuse"

    with pytest.raises(AuthenticationError):
        await service.refresh_session(
            SESSION, refresh_token=second.refresh_token, user_agent=None, now=NOW
        )


async def test_reuse_and_ordinary_expiry_are_indistinguishable_to_the_caller(
    store: LocalCredentialStore,
) -> None:
    """🔒 A caller learning its token was *reused* would learn the theft had been
    noticed."""
    first = await _signed_in(store)
    await service.refresh_session(
        SESSION, refresh_token=first.refresh_token, user_agent=None, now=NOW
    )

    with pytest.raises(AuthenticationError) as reuse:
        await service.refresh_session(
            SESSION, refresh_token=first.refresh_token, user_agent=None, now=NOW
        )
    with pytest.raises(AuthenticationError) as unknown:
        await service.refresh_session(
            SESSION, refresh_token="never-existed", user_agent=None, now=NOW
        )

    assert reuse.value.message == unknown.value.message
    assert reuse.value.action == unknown.value.action


async def test_a_disabled_user_cannot_refresh(
    store: LocalCredentialStore, db: FakeDatabase
) -> None:
    """🔒 Re-checked on every refresh, not only at sign-in.

    Without this, a user disabled at 10:00 keeps working until a 30-day refresh
    token expires.
    """
    tokens = await _signed_in(store)
    subject = db.emails[EMAIL]
    existing = db.users[subject]
    db.users[subject] = PractitionerIdentity(
        user_id=existing.user_id,
        tenant_id=existing.tenant_id,
        role=existing.role,
        status=UserStatus.DISABLED,
        is_archived=False,
    )

    with pytest.raises(AuthenticationError):
        await service.refresh_session(
            SESSION, refresh_token=tokens.refresh_token, user_agent=None, now=NOW
        )

    assert db.sessions[tokens.session_id].revoked_at is not None


async def test_an_expired_session_cannot_refresh(store: LocalCredentialStore) -> None:
    tokens = await _signed_in(store)

    with pytest.raises(AuthenticationError):
        await service.refresh_session(
            SESSION,
            refresh_token=tokens.refresh_token,
            user_agent=None,
            now=NOW + timedelta(days=31),
        )


# ─── Logout ──────────────────────────────────────────────────────────────


async def test_logout_revokes_the_session(store: LocalCredentialStore, db: FakeDatabase) -> None:
    tokens = await _signed_in(store)

    await service.sign_out(SESSION, session_id=tokens.session_id, now=NOW)

    assert db.sessions[tokens.session_id].revoked_at == NOW
    assert db.sessions[tokens.session_id].revocation_reason == "logout"


async def test_a_revoked_session_cannot_refresh(store: LocalCredentialStore) -> None:
    tokens = await _signed_in(store)
    await service.sign_out(SESSION, session_id=tokens.session_id, now=NOW)

    with pytest.raises(AuthenticationError):
        await service.refresh_session(
            SESSION, refresh_token=tokens.refresh_token, user_agent=None, now=NOW
        )


# ─── Password reset ──────────────────────────────────────────────────────


async def test_a_reset_request_for_an_unknown_address_yields_no_token() -> None:
    """🔒 The caller responds 202 regardless; `None` is what makes that honest."""
    assert await service.request_password_reset(SESSION, email="nobody@example.com", now=NOW) is (
        None
    )


async def test_a_reset_changes_the_password(store: LocalCredentialStore) -> None:
    await _verified(store)
    token = await service.request_password_reset(SESSION, email=EMAIL, now=NOW)
    assert token is not None

    await service.confirm_password_reset(
        SESSION, store, token=token, new_password=OTHER_PASSWORD, now=NOW
    )

    with pytest.raises(AuthenticationError):
        await service.sign_in(
            SESSION, store, email=EMAIL, password=GOOD_PASSWORD, user_agent=None, now=NOW
        )
    assert await service.sign_in(
        SESSION, store, email=EMAIL, password=OTHER_PASSWORD, user_agent=None, now=NOW
    )


async def test_a_reset_revokes_every_session(store: LocalCredentialStore, db: FakeDatabase) -> None:
    """🔒 NFR-042 — a reset is usually a response to suspected compromise, and
    leaving the attacker's refresh token working would defeat it."""
    tokens = await _signed_in(store)
    token = await service.request_password_reset(SESSION, email=EMAIL, now=NOW)
    assert token is not None

    await service.confirm_password_reset(
        SESSION, store, token=token, new_password=OTHER_PASSWORD, now=NOW
    )

    assert db.sessions[tokens.session_id].revoked_at is not None
    assert db.sessions[tokens.session_id].revocation_reason == "password_reset"


async def test_a_reset_token_is_single_use(store: LocalCredentialStore) -> None:
    await _verified(store)
    token = await service.request_password_reset(SESSION, email=EMAIL, now=NOW)
    assert token is not None
    await service.confirm_password_reset(
        SESSION, store, token=token, new_password=OTHER_PASSWORD, now=NOW
    )

    with pytest.raises(AuthenticationError):
        await service.confirm_password_reset(
            SESSION, store, token=token, new_password="yet-another-long-one", now=NOW
        )


async def test_requesting_a_second_reset_invalidates_the_first(
    store: LocalCredentialStore,
) -> None:
    """🔒 Otherwise every request adds a live credential, and the account's
    exposure grows with the user's confusion."""
    await _verified(store)
    first = await service.request_password_reset(SESSION, email=EMAIL, now=NOW)
    second = await service.request_password_reset(SESSION, email=EMAIL, now=NOW)
    assert first is not None and second is not None

    with pytest.raises(AuthenticationError):
        await service.confirm_password_reset(
            SESSION, store, token=first, new_password=OTHER_PASSWORD, now=NOW
        )

    await service.confirm_password_reset(
        SESSION, store, token=second, new_password=OTHER_PASSWORD, now=NOW
    )


async def test_a_reset_password_must_be_strong(store: LocalCredentialStore) -> None:
    await _verified(store)
    token = await service.request_password_reset(SESSION, email=EMAIL, now=NOW)
    assert token is not None

    with pytest.raises(ValidationError):
        await service.confirm_password_reset(
            SESSION, store, token=token, new_password="password123", now=NOW
        )


async def test_an_expired_reset_token_is_refused(store: LocalCredentialStore) -> None:
    await _verified(store)
    token = await service.request_password_reset(SESSION, email=EMAIL, now=NOW)
    assert token is not None

    with pytest.raises(AuthenticationError):
        await service.confirm_password_reset(
            SESSION,
            store,
            token=token,
            new_password=OTHER_PASSWORD,
            now=NOW + timedelta(hours=2),
        )


# ─── Magic links (client realm) ──────────────────────────────────────────

CLIENT = uuid.UUID("cccccccc-0000-4000-8000-000000000001")
TENANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")


async def _link(db: FakeDatabase) -> str:
    db.active_grants.add(CLIENT)
    return await service.issue_magic_link(
        SESSION,
        tenant_id=TENANT,
        client_id=CLIENT,
        purpose=LinkPurpose.PORTAL,
        target_ref=None,
        transport=TransportType.WHATSAPP,
        now=NOW,
    )


async def test_a_magic_link_opens_a_client_session(db: FakeDatabase) -> None:
    token = await _link(db)

    portal = await service.redeem_magic_link(SESSION, token=token, user_agent=None, now=NOW)

    assert portal.client_id == CLIENT
    assert portal.tenant_id == TENANT
    assert portal.tokens.access_token
    assert portal.target_type == LinkPurpose.PORTAL.value


async def test_a_magic_link_is_single_use(db: FakeDatabase) -> None:
    """🔒 EC-M0-01 — two taps on the same link cannot both open a session."""
    token = await _link(db)
    await service.redeem_magic_link(SESSION, token=token, user_agent=None, now=NOW)

    with pytest.raises(AuthenticationError):
        await service.redeem_magic_link(SESSION, token=token, user_agent=None, now=NOW)


async def test_an_expired_magic_link_is_refused(db: FakeDatabase) -> None:
    """🔒 15–30 minutes (D2). Short enough that a forwarded WhatsApp message is
    near-useless — which is why self-service re-request is a primary flow."""
    token = await _link(db)

    with pytest.raises(AuthenticationError):
        await service.redeem_magic_link(
            SESSION, token=token, user_agent=None, now=NOW + timedelta(hours=1)
        )


async def test_expiry_and_replay_give_the_same_error(db: FakeDatabase) -> None:
    expired = await _link(db)
    replayed = await _link(db)
    await service.redeem_magic_link(SESSION, token=replayed, user_agent=None, now=NOW)

    with pytest.raises(AuthenticationError) as first:
        await service.redeem_magic_link(
            SESSION, token=expired, user_agent=None, now=NOW + timedelta(hours=1)
        )
    with pytest.raises(AuthenticationError) as second:
        await service.redeem_magic_link(SESSION, token=replayed, user_agent=None, now=NOW)

    assert first.value.message == second.value.message


async def test_a_revoked_grant_blocks_redemption(db: FakeDatabase) -> None:
    """🔒 Re-checked at redemption: a grant revoked between sending the link and
    opening it must not still let the client in."""
    token = await _link(db)
    db.active_grants.discard(CLIENT)

    with pytest.raises(AuthenticationError):
        await service.redeem_magic_link(SESSION, token=token, user_agent=None, now=NOW)


async def test_the_link_token_is_never_stored(db: FakeDatabase) -> None:
    """🔒 DDR-04 — a database disclosure yields no usable link."""
    token = await _link(db)

    assert token not in db.magic_links
    assert hash_for_storage(token) in db.magic_links


async def test_a_client_session_carries_the_client_realm(db: FakeDatabase) -> None:
    """🔒 So the token verifies only against the client key."""
    token = await _link(db)
    portal = await service.redeem_magic_link(SESSION, token=token, user_agent=None, now=NOW)

    assert db.sessions[portal.tokens.session_id].realm is AuthRealm.CLIENT
