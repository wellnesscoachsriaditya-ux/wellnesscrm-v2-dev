"""Request-scoped context: who is acting, in which tenant, on which request.

Arch §5.1 steps 1–3. Set once by the middleware pipeline, read everywhere.

Uses :mod:`contextvars` rather than a passed-around object because audit writing
(§15.3) and logging must reach the actor without every function signature
carrying it. That is a deliberate trade: implicit access in exchange for making
it impossible to forget.

🔒 The context holds IDENTIFIERS ONLY — never a name, email, phone number or any
clinical value (NFR-033). It is read by the logger and the audit writer, so
anything placed here is at risk of being written to a log file.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType


class AuthRealm(StrEnum):
    """The three identity realms (FR-M0-004, Arch §6.1).

    🔒 A credential in one realm must never authenticate into another. Each realm
    has its own signing key, so crossing realms fails signature verification
    rather than a claim check.
    """

    PRACTITIONER = "practitioner"
    CLIENT = "client"
    OPERATOR = "operator"


class ActorType(StrEnum):
    """Who performed an action, for audit attribution (DB §15.1)."""

    PRACTITIONER = "practitioner"
    CLIENT = "client"
    OPERATOR = "operator"
    SYSTEM = "system"
    ANONYMOUS = "anonymous"


class UserRole(StrEnum):
    """Roles within a tenant (FR-M0-016)."""

    OWNER = "owner"
    PRACTITIONER = "practitioner"
    CLIENT = "client"
    PLATFORM_OPERATOR = "platform_operator"


@dataclass(frozen=True, slots=True)
class Actor:
    """The authenticated principal for a request.

    Frozen: an actor must not be mutated mid-request. Re-authentication produces
    a new actor rather than editing one, so an authorization decision cannot be
    invalidated after it is made.

    🔒 Identifiers only. No email, name or phone (NFR-033).
    """

    actor_type: ActorType
    realm: AuthRealm | None = None
    subject_id: uuid.UUID | None = None
    tenant_id: uuid.UUID | None = None
    role: UserRole | None = None
    session_id: uuid.UUID | None = None

    @classmethod
    def anonymous(cls) -> Actor:
        """An unauthenticated caller — the public surface (Arch §15.3)."""
        return cls(actor_type=ActorType.ANONYMOUS)

    @classmethod
    def system(cls, tenant_id: uuid.UUID | None = None) -> Actor:
        """The worker acting without a user — scheduled jobs, rollups.

        🔒 System actions are audited like any other (DB §15.3). "The system did
        it" must still be attributable, or unexplained changes become
        uninvestigable.
        """
        return cls(actor_type=ActorType.SYSTEM, tenant_id=tenant_id)

    @property
    def is_authenticated(self) -> bool:
        return self.actor_type is not ActorType.ANONYMOUS

    @property
    def is_operator(self) -> bool:
        """⚠️ Operators are cross-tenant. Every read they perform is audited
        (FR-M0-032) — the only reads in the system that are."""
        return self.actor_type is ActorType.OPERATOR

    def require_tenant(self) -> uuid.UUID:
        """Return the tenant id, or fail loudly.

        Called where tenant scope is structurally required. Raises rather than
        returning ``None`` so a missing tenant can never silently widen a query.
        """
        if self.tenant_id is None:
            raise RuntimeError(
                "Tenant-scoped operation attempted without a tenant in context. "
                "This is a programming error, not a user error."
            )
        return self.tenant_id

    def require_subject(self) -> uuid.UUID:
        """Return the subject id, or fail loudly."""
        if self.subject_id is None:
            raise RuntimeError("Operation requires an authenticated subject.")
        return self.subject_id


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Everything ambient about the current request.

    🔒 ``request_id`` appears in every log line and in every error response
    (API §5.1), which is how a user-reported problem is traced to its logs
    without the logs containing anything about the user.
    """

    request_id: str
    actor: Actor
    method: str | None = None
    path: str | None = None
    realm_prefix: str | None = None

    @classmethod
    def anonymous(cls, request_id: str | None = None) -> RequestContext:
        return cls(
            request_id=request_id or new_request_id(),
            actor=Actor.anonymous(),
        )

    @classmethod
    def for_worker(cls, job_type: str, tenant_id: uuid.UUID | None = None) -> RequestContext:
        """Context for a background job (Arch §11).

        The worker shares the application's context machinery so audit and
        logging behave identically in both processes — one implementation, not
        two (NFR-072).
        """
        return cls(
            request_id=f"job_{uuid.uuid4().hex[:16]}",
            actor=Actor.system(tenant_id=tenant_id),
            path=f"worker:{job_type}",
        )

    def to_log_fields(self) -> dict[str, str]:
        """Structured fields for log lines.

        🔒 Identifiers only. Reviewed against NFR-033 — adding a field here that
        carries personal or clinical data would leak it into every log line for
        the request.
        """
        fields: dict[str, str] = {
            "request_id": self.request_id,
            "actor_type": self.actor.actor_type.value,
        }
        if self.actor.realm is not None:
            fields["realm"] = self.actor.realm.value
        if self.actor.tenant_id is not None:
            fields["tenant_id"] = str(self.actor.tenant_id)
        if self.actor.subject_id is not None:
            fields["subject_id"] = str(self.actor.subject_id)
        if self.method and self.path:
            fields["endpoint"] = f"{self.method} {self.path}"
        return fields


def new_request_id() -> str:
    """Generate a request identifier.

    Prefixed and hex-encoded so it is recognisable in a support conversation and
    safe to show a user in an error message.
    """
    return f"req_{uuid.uuid4().hex}"


_context: ContextVar[RequestContext | None] = ContextVar("request_context", default=None)


def get_context() -> RequestContext:
    """Return the current context, or an anonymous one.

    Never raises: logging must work even outside a request (startup, shutdown,
    scripts). Code needing a *guaranteed* actor calls
    :meth:`Actor.require_subject` instead.
    """
    ctx = _context.get()
    return ctx if ctx is not None else RequestContext.anonymous()


def get_actor() -> Actor:
    """Convenience accessor for the current actor."""
    return get_context().actor


def set_context(ctx: RequestContext) -> Token[RequestContext | None]:
    """Install a context, returning a token for restoration."""
    return _context.set(ctx)


def reset_context(token: Token[RequestContext | None]) -> None:
    """Restore the previous context."""
    _context.reset(token)


class context_scope:  # noqa: N801 — used as a context manager, lowercase reads better
    """Scope a context to a block.

    Used by the HTTP middleware and the worker's job runner::

        with context_scope(RequestContext.for_worker("send_message")):
            ...

    Guarantees restoration on exit, so a context cannot leak between jobs
    sharing a worker process — which would be a cross-tenant attribution bug.
    """

    __slots__ = ("_ctx", "_token")

    def __init__(self, ctx: RequestContext) -> None:
        self._ctx = ctx
        self._token: Token[RequestContext | None] | None = None

    def __enter__(self) -> RequestContext:
        self._token = set_context(self._ctx)
        return self._ctx

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._token is not None:
            reset_context(self._token)
            self._token = None
