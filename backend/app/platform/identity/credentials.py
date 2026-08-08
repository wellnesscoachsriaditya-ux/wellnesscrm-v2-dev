"""Credential adapters — where passwords actually live.

🔒 NFR-029 / D1. `users` has no password column and never will. This module holds
the implementations of :class:`~app.kernel.identity.CredentialStore`; the
application above it deals only in the port, so swapping providers touches
nothing else.

Two adapters, with very different standing:

* :class:`LocalCredentialStore` — ⚠️ **development and tests only.** Credentials
  live in process memory and vanish on restart. It exists so the whole
  authentication surface can be exercised without provisioning Supabase, not as a
  fallback anyone should reach in production. It refuses to be installed outside
  local environments.
* **GoTrue** — the production adapter, brokering to Supabase Auth. Not yet
  written; see the note at the bottom of this module. The port is shaped to
  GoTrue's semantics so that adapter is a translation rather than a redesign.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass, field

from app.kernel.identity import CredentialError, CredentialSubject
from app.platform.logging import get_logger

logger = get_logger(__name__)

#: 🔒 NFR-043 / API §2.2 — length only, checked against a common-password list
#: rather than composition rules. Forced symbols and digits produce `Password1!`
#: across an entire user base; length plus a denylist is what actually resists
#: guessing.
MIN_PASSWORD_LENGTH = 10

#: A deliberately short, illustrative denylist. ⚠️ Production must load a real
#: list (the SecLists top 10k or similar) — this is enough to make the check
#: real and its interface right, not enough to make it effective.
_COMMON_PASSWORDS: frozenset[str] = frozenset(
    {
        "password",
        "password1",
        "password123",
        "12345678",
        "123456789",
        "1234567890",
        "qwertyuiop",
        "letmein123",
        "iloveyou1",
        "admin12345",
        "welcome123",
        "wellnesscrm",
    }
)

# scrypt parameters. ⚠️ Local adapter only — production password hashing is
# GoTrue's, and these values are not a recommendation for it.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_SALT_BYTES = 16
_SCRYPT_KEY_LEN = 32


def password_rejection(password: str) -> CredentialError | None:
    """Check a password against the two rules that matter. ``None`` if it passes."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return CredentialError.WEAK_PASSWORD
    if password.lower() in _COMMON_PASSWORDS:
        return CredentialError.WEAK_PASSWORD
    return None


def normalise_email(email: str) -> str:
    """🔒 One canonical form, used for storage and for every lookup.

    Without this, `Ada@Example.com` and `ada@example.com` register as two
    accounts and the per-tenant unique index does not stop them.
    """
    return email.strip().lower()


@dataclass
class _StoredCredential:
    subject_id: str
    email: str
    salt: bytes
    hash: bytes


@dataclass
class LocalCredentialStore:
    """⚠️ In-memory credentials. Development and tests only.

    🔒 Two behaviours are load-bearing and are not test conveniences:

    * **No enumeration.** :meth:`verify` performs the same scrypt work for an
      unknown email as for a known one, so response time does not reveal which
      addresses are registered (NFR-043). Skipping the work on the miss path is
      the natural implementation and the wrong one.
    * **Returns errors, never raises.** The caller's correct response to
      "already exists" is to behave exactly as it does on success, and an
      exception invites a distinguishable error path.
    """

    _by_email: dict[str, _StoredCredential] = field(default_factory=dict)
    _by_subject: dict[str, _StoredCredential] = field(default_factory=dict)

    def _derive(self, password: str, salt: bytes) -> bytes:
        return hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=_SCRYPT_KEY_LEN,
        )

    async def create(self, email: str, password: str) -> CredentialSubject | CredentialError:
        address = normalise_email(email)
        if (weak := password_rejection(password)) is not None:
            return weak
        if address in self._by_email:
            return CredentialError.ALREADY_EXISTS

        salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
        record = _StoredCredential(
            subject_id=str(uuid.uuid4()),
            email=address,
            salt=salt,
            hash=self._derive(password, salt),
        )
        self._by_email[address] = record
        self._by_subject[record.subject_id] = record
        return CredentialSubject(subject_id=record.subject_id, email=address)

    async def verify(self, email: str, password: str) -> CredentialSubject | CredentialError:
        address = normalise_email(email)
        record = self._by_email.get(address)

        if record is None:
            # 🔒 Do the work anyway. An early return here makes the miss path
            # measurably faster than the hit path, which is an enumeration oracle
            # that no amount of identical response bodies can cover up.
            self._derive(password, b"\x00" * _SCRYPT_SALT_BYTES)
            return CredentialError.NOT_FOUND

        candidate = self._derive(password, record.salt)
        if not hmac.compare_digest(candidate, record.hash):
            return CredentialError.BAD_PASSWORD

        return CredentialSubject(subject_id=record.subject_id, email=record.email)

    async def set_password(self, subject_id: str, password: str) -> None | CredentialError:
        record = self._by_subject.get(subject_id)
        if record is None:
            return CredentialError.NOT_FOUND
        if (weak := password_rejection(password)) is not None:
            return weak

        record.salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
        record.hash = self._derive(password, record.salt)
        return None


# ─── Installation ────────────────────────────────────────────────────────

_store: LocalCredentialStore | None = None


def configure_credential_store(store: LocalCredentialStore) -> None:
    """Install the process-wide credential store."""
    global _store
    _store = store


def get_credential_store() -> LocalCredentialStore:
    """Return the installed store, creating a local one on first use.

    ⚠️ The lazy default is a development affordance. :func:`raise_if_local` is
    called at startup outside local environments, so a deployed process never
    runs on it.
    """
    global _store
    if _store is None:
        _store = LocalCredentialStore()
    return _store


def raise_if_credentials_are_local(settings: object) -> None:
    """🔒 Refuse to start a production-like process on in-memory credentials.

    The failure this prevents is quiet and total: the process boots, users
    register, and the next restart discards every password with no error
    anywhere. A GoTrue adapter must be installed before this passes.

    Raises:
        RuntimeError: If the installed store is the local one.
    """
    if not isinstance(get_credential_store(), LocalCredentialStore):
        return

    raise RuntimeError(
        "FATAL: the in-memory credential store is installed outside local development.\n\n"
        "🔒 Credentials would live in process memory — lost on restart, absent from "
        "backups, and invisible to every operational check. `users` has no password "
        "column by design (NFR-029); passwords belong to the identity provider.\n\n"
        "Install the GoTrue adapter via `configure_credential_store()` before starting "
        "in staging or production. See the note at the foot of "
        "`app/platform/identity/credentials.py`."
    )


# ─── 🔒 Remaining work: the GoTrue adapter ───────────────────────────────
#
# D1 approved a GoTrue-compatible adapter alongside the local one. It is
# deliberately NOT written here.
#
# Writing an HTTP client against a service that is not provisioned would produce
# code whose tests assert only my assumptions about GoTrue's contract — green,
# and no evidence of anything. The port above is shaped to GoTrue's actual
# semantics (opaque string subject ids, error returns rather than exceptions,
# email as the lookup key), so the adapter is a translation of four calls:
#
#   create       → POST /auth/v1/admin/users
#   verify       → POST /auth/v1/token?grant_type=password
#   set_password → PUT  /auth/v1/admin/users/{id}
#
# It should be written against a provisioned Supabase project and verified with a
# contract test, at the same time as the PostgreSQL gate (D4) closes.
