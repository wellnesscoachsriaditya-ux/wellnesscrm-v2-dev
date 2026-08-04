"""The exception taxonomy — one definition for the whole application.

API §5 and Arch §17.1. Every failure in the system is one of these thirteen
categories. There is no other way to fail a request.

Two rules make this taxonomy worth having:

1.  **NFR-063 — every error states what happened AND what to do next.**
    ``message`` and ``action`` are both required by the constructor. An error
    without a next step is an incomplete error, so the type system refuses it.

2.  **Errors never leak internals.** No stack traces, table names, provider
    payloads or clinical values reach a client. ``InternalError`` deliberately
    carries no caller-supplied message.

Handlers translate these into the response envelope; see
``app.platform.http.error_handlers``. Domain code raises them and never
constructs an HTTP response itself.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorType(StrEnum):
    """Stable machine-readable error identifiers.

    🔒 These strings are part of the API contract (API §16.1). The frontend
    branches on them, so renaming one is a breaking change. ``message`` is
    localisable and may change freely; ``type`` may not.
    """

    VALIDATION_FAILED = "validation_failed"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    ENTITLEMENT_EXCEEDED = "entitlement_exceeded"
    CONSENT_REQUIRED = "consent_required"
    CONFLICT = "conflict"
    PRECONDITION_REQUIRED = "precondition_required"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    RATE_LIMITED = "rate_limited"
    DOMAIN_RULE_VIOLATED = "domain_rule_violated"
    INTEGRATION_UNAVAILABLE = "integration_unavailable"
    INTERNAL_ERROR = "internal_error"


class LogSeverity(StrEnum):
    """How an error is recorded. Distinct from HTTP status.

    ``ALERT`` means the operator is notified (NFR-085), not merely that a line
    is written. Reserved for conditions a solo operator must act on.
    """

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    ALERT = "alert"


class AppError(Exception):
    """Base class for every expected failure.

    Anything not derived from this is an unexpected failure and becomes an
    ``InternalError`` at the boundary — so an unhandled library exception can
    never leak its message to a client.
    """

    error_type: ErrorType = ErrorType.INTERNAL_ERROR
    status_code: int = 500
    severity: LogSeverity = LogSeverity.ERROR

    #: Whether this failure is written to the audit log (FR-M0-031, §15.3).
    #: Authorization and consent refusals are security-relevant events, not
    #: merely failed requests.
    is_auditable: bool = False

    def __init__(
        self,
        message: str,
        action: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        """
        Args:
            message: What went wrong, in plain language, for the end user.
                🔒 Must never contain a clinical value, credential or internal
                identifier (NFR-033).
            action: What the user can do next. 🔒 Required — NFR-063.
            details: Structured, type-specific context for the frontend.
                🔒 Must never contain clinical data.
        """
        if not message:
            raise ValueError("AppError requires a message")
        if not action:
            # NFR-063 is enforced here rather than reviewed later.
            raise ValueError(
                f"{type(self).__name__} requires an 'action' — NFR-063: every error "
                "must state what to do next, not only what went wrong"
            )

        super().__init__(message)
        self.message = message
        self.action = action
        self.details = details or {}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(type={self.error_type.value!r}, message={self.message!r})"


# ─── 4xx — the caller can fix it ─────────────────────────────────────────


class ValidationError(AppError):
    """Input failed validation. Field-level detail is expected (API §5.3)."""

    error_type = ErrorType.VALIDATION_FAILED
    status_code = 422
    severity = LogSeverity.DEBUG

    @classmethod
    def for_fields(cls, fields: list[dict[str, str]]) -> ValidationError:
        """Build a field-level validation error.

        Args:
            fields: One entry per invalid field, each with ``field``, ``code``
                and ``message`` keys.
        """
        return cls(
            message="Some of the information provided needs correcting.",
            action="Review the highlighted fields and try again.",
            details={"fields": fields},
        )


class AuthenticationError(AppError):
    """Not authenticated, or credentials rejected.

    🔒 NFR-043 — the message must never reveal whether an account exists.
    Every authentication failure looks identical from outside.
    """

    error_type = ErrorType.UNAUTHENTICATED
    status_code = 401
    severity = LogSeverity.INFO


class AuthorizationError(AppError):
    """Authenticated, but not permitted.

    🔒 Audited (Arch §17.1) — a denial is a security-relevant event.

    ⚠️ Do NOT raise this for a resource in another tenant. Use
    :class:`NotFoundError` instead: a 403 confirms the resource exists, which
    leaks across the tenant boundary (API §5.4).
    """

    error_type = ErrorType.FORBIDDEN
    status_code = 403
    severity = LogSeverity.WARNING
    is_auditable = True


class NotFoundError(AppError):
    """The resource does not exist, or is not visible to this actor.

    🔒 Deliberately ambiguous between "absent" and "not yours" (API §5.4).
    Cross-tenant access returns this, while the attempt is still audited
    (AC-M0-002).
    """

    error_type = ErrorType.NOT_FOUND
    status_code = 404
    severity = LogSeverity.INFO


class EntitlementError(AppError):
    """A plan limit has been reached.

    🔒 FR-M0-045 — the response must name the limit and the upgrade path, so the
    UI needs no second call. Use :meth:`for_limit`.

    🔒 402, not 429. Rate limiting means "too fast"; this means "your plan does
    not include more of this". Telling a paying customer to wait when they need
    to upgrade is a real product failure.
    """

    error_type = ErrorType.ENTITLEMENT_EXCEEDED
    status_code = 402
    severity = LogSeverity.INFO

    @classmethod
    def for_limit(
        cls,
        *,
        resource: str,
        limit: int,
        used: int,
        plan_code: str,
        human_resource: str,
        upgrade_to: str | None = None,
        remedy: str | None = None,
    ) -> EntitlementError:
        """Build a limit error carrying everything the UI needs (API §5.5).

        Args:
            resource: Machine resource code, e.g. ``active_clients``.
            limit: The plan's limit.
            used: Current consumption.
            plan_code: The tenant's current plan.
            human_resource: Readable noun, e.g. "active clients".
            upgrade_to: Next plan code, if one exists.
            remedy: An alternative to upgrading — e.g. pausing a client. Offered
                first, because it is free.
        """
        parts: list[str] = []
        if remedy:
            parts.append(remedy)
        if upgrade_to:
            parts.append(f"upgrade to {upgrade_to.title()}")
        action = (
            f"You can {' or '.join(parts)}."
            if parts
            else "Contact support to increase this limit."
        )

        return cls(
            message=f"You've reached {limit} {human_resource} on the {plan_code.title()} plan.",
            action=action,
            details={
                "resource": resource,
                "limit": limit,
                "used": used,
                "plan_code": plan_code,
                "upgrade_to": upgrade_to,
            },
        )


class ConsentError(AppError):
    """Processing is not permitted under the recorded consent.

    🔒 DPDP: consent is per purpose (FR-M0-022), and withdrawal halts processing
    without deleting records the practitioner may lawfully retain (FR-M0-025).
    Audited, because it is a lawful-basis decision.
    """

    error_type = ErrorType.CONSENT_REQUIRED
    status_code = 403
    severity = LogSeverity.INFO
    is_auditable = True


class ConflictError(AppError):
    """The resource changed since it was read (ADR-14), or state forbids this.

    🔒 EC-M4-07 — concurrent edits must never silently discard each other's work.
    ``details.current_state`` lets the UI offer a reload.
    """

    error_type = ErrorType.CONFLICT
    status_code = 409
    severity = LogSeverity.INFO


class PreconditionRequiredError(AppError):
    """A guarded resource was mutated without ``If-Match`` (API §4.4)."""

    error_type = ErrorType.PRECONDITION_REQUIRED
    status_code = 428
    severity = LogSeverity.INFO

    def __init__(self, resource: str) -> None:
        super().__init__(
            message="This record must be re-loaded before it can be changed.",
            action="Refresh and try again.",
            details={"resource": resource},
        )


class IdempotencyConflictError(AppError):
    """An idempotency key was reused with a different payload (API §13.2).

    🔒 Loud by design. Silently accepting it would mask a client bug in exactly
    the operations where duplicates matter most — issuing plans, taking payment.
    """

    error_type = ErrorType.IDEMPOTENCY_CONFLICT
    status_code = 409
    severity = LogSeverity.WARNING

    def __init__(self) -> None:
        super().__init__(
            message="This request was already submitted with different information.",
            action="Start the action again rather than retrying.",
        )


class RateLimitError(AppError):
    """Too many requests (NFR-039).

    ⚠️ Not an entitlement failure. See :class:`EntitlementError`.
    """

    error_type = ErrorType.RATE_LIMITED
    status_code = 429
    severity = LogSeverity.INFO

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            message="Too many requests in a short period.",
            action=f"Wait {retry_after_seconds} seconds and try again.",
            details={"retry_after_seconds": retry_after_seconds},
        )
        self.retry_after_seconds = retry_after_seconds


class DomainRuleError(AppError):
    """A business rule was violated.

    🔒 Used for *hard* dietary rules (DB §8.15) — allergens, dietary-class
    violations — which block the write. Soft rules return warnings in the
    response body instead and never block: practitioner judgement prevails
    (EC-M4-05).
    """

    error_type = ErrorType.DOMAIN_RULE_VIOLATED
    status_code = 422
    severity = LogSeverity.INFO

    @classmethod
    def for_rule(
        cls,
        *,
        rule_code: str,
        message: str,
        action: str,
        severity_label: str = "hard",
    ) -> DomainRuleError:
        return cls(
            message=message,
            action=action,
            details={"rule_code": rule_code, "severity": severity_label},
        )


# ─── 5xx — the caller cannot fix it ──────────────────────────────────────


class IntegrationError(AppError):
    """An external service failed or is unavailable.

    🔒 Arch §17.3 — degrade, never block. The practitioner's work is queued or
    remains available; it is never lost. Alerts the operator (NFR-085).
    """

    error_type = ErrorType.INTEGRATION_UNAVAILABLE
    status_code = 503
    severity = LogSeverity.ALERT

    def __init__(
        self,
        provider: str,
        *,
        message: str | None = None,
        action: str | None = None,
    ) -> None:
        """
        Args:
            provider: Internal provider name, for logs and alerting.
                🔒 Not echoed to the client — an external vendor's identity is
                not the user's concern, and provider error text is untrusted.
        """
        super().__init__(
            message=message or "This couldn't be completed just now.",
            action=action or "Nothing was lost — try again in a few moments.",
        )
        self.provider = provider


class InternalError(AppError):
    """An unexpected failure.

    🔒 Carries no caller-supplied message: the client receives a fixed string
    plus a ``request_id`` for support. Detail stays in the logs, where it is
    scrubbed of clinical data (NFR-033).
    """

    error_type = ErrorType.INTERNAL_ERROR
    status_code = 500
    severity = LogSeverity.ALERT

    def __init__(self) -> None:
        super().__init__(
            message="Something went wrong on our side.",
            action="Try again. If it keeps happening, contact support with the reference below.",
        )
