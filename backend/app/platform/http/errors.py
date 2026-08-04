"""Translate exceptions into the approved API error envelope.

🔒 API §5.1 — one envelope, always::

    {"error": {"type", "message", "action", "request_id", "details"}}

Domain code raises :class:`~app.kernel.errors.AppError` subclasses and never
builds a response. This module is the only place that mapping happens, so the
envelope cannot drift between endpoints.

Two guarantees worth stating explicitly:

* 🔒 **Unexpected exceptions never leak.** Anything not an ``AppError`` becomes
  ``InternalError`` with a fixed message. A library exception's text — which may
  echo a clinical value or a connection string — is logged, never returned.
* 🔒 **Errors carry no clinical data** (NFR-033). Detail payloads are built by
  the error classes themselves from structured, non-clinical fields.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.kernel.context import get_context
from app.kernel.errors import (
    AppError,
    ErrorType,
    InternalError,
    LogSeverity,
    RateLimitError,
    ValidationError,
)
from app.platform.logging import get_logger

logger = get_logger(__name__)

_SEVERITY_TO_LOG = {
    LogSeverity.DEBUG: logger.debug,
    LogSeverity.INFO: logger.info,
    LogSeverity.WARNING: logger.warning,
    LogSeverity.ERROR: logger.error,
    LogSeverity.ALERT: logger.error,
}


def build_envelope(error: AppError, request_id: str) -> dict[str, Any]:
    """Build the response body. 🔒 The single definition of the envelope."""
    payload: dict[str, Any] = {
        "type": error.error_type.value,
        "message": error.message,
        "action": error.action,
        "request_id": request_id,
    }
    if error.details:
        payload["details"] = error.details
    return {"error": payload}


def _log(error: AppError, request: Request) -> None:
    """Record the failure at the severity its category declares."""
    log_at = _SEVERITY_TO_LOG.get(error.severity, logger.error)
    log_at(
        "Request failed: %s",
        error.error_type.value,
        extra={
            "error_type": error.error_type.value,
            "status_code": error.status_code,
            "endpoint": f"{request.method} {request.url.path}",
            # 🔒 The message is scrubbed by the logging filter before emission.
            "error_message": error.message,
            "is_alert": error.severity is LogSeverity.ALERT,
        },
    )


def _response(error: AppError, request: Request) -> JSONResponse:
    ctx = get_context()
    _log(error, request)

    headers: dict[str, str] = {"X-Request-Id": ctx.request_id}
    if isinstance(error, RateLimitError):
        headers["Retry-After"] = str(error.retry_after_seconds)

    return JSONResponse(
        status_code=error.status_code,
        content=build_envelope(error, ctx.request_id),
        headers=headers,
    )


async def _handle_app_error(request: Request, exc: Exception) -> JSONResponse:
    """Expected failures — the taxonomy in :mod:`app.kernel.errors`."""
    if not isinstance(exc, AppError):  # pragma: no cover - registered for this type
        return await _handle_unexpected(request, exc)
    return _response(exc, request)


async def _handle_request_validation(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI/Pydantic validation → 🔒 field-level detail (API §5.3).

    Pydantic's own error shape is not our contract, so it is translated rather
    than forwarded — and its ``input`` field is dropped, because it echoes the
    submitted value, which may be clinical data.

    FastAPI types handler arguments as ``Exception``, so the concrete type is
    narrowed explicitly. An ``assert`` would be stripped under ``python -O``,
    leaving an unchecked attribute access on a mistyped exception.
    """
    if not isinstance(exc, RequestValidationError):  # pragma: no cover
        return await _handle_unexpected(request, exc)

    fields: list[dict[str, str]] = []
    for err in exc.errors():
        location = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")]
        fields.append(
            {
                "field": ".".join(location) or "request",
                "code": str(err.get("type", "invalid")),
                # 🔒 Pydantic's message only; never `err["input"]`.
                "message": str(err.get("msg", "This value is not valid.")),
            }
        )

    return _response(ValidationError.for_fields(fields), request)


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    """Starlette's own exceptions — 404s from routing, 405s, etc.

    Mapped into the envelope so a client never has to parse two error shapes.
    """
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover
        return await _handle_unexpected(request, exc)

    known: dict[int, tuple[ErrorType, str, str]] = {
        status.HTTP_404_NOT_FOUND: (
            ErrorType.NOT_FOUND,
            "That page or resource doesn't exist.",
            "Check the address and try again.",
        ),
        status.HTTP_405_METHOD_NOT_ALLOWED: (
            ErrorType.VALIDATION_FAILED,
            "That action isn't supported here.",
            "If this came from the app, please report it.",
        ),
        status.HTTP_401_UNAUTHORIZED: (
            ErrorType.UNAUTHENTICATED,
            "You need to sign in to continue.",
            "Sign in and try again.",
        ),
        status.HTTP_403_FORBIDDEN: (
            ErrorType.FORBIDDEN,
            "You don't have access to this.",
            "If you think you should, contact the account owner.",
        ),
    }

    error_type, message, action = known.get(
        exc.status_code,
        (
            ErrorType.INTERNAL_ERROR,
            "Something went wrong.",
            "Try again in a few moments.",
        ),
    )

    ctx = get_context()
    logger.info(
        "HTTP exception",
        extra={
            "status_code": exc.status_code,
            "endpoint": f"{request.method} {request.url.path}",
        },
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "type": error_type.value,
                "message": message,
                "action": action,
                "request_id": ctx.request_id,
            }
        },
        headers={"X-Request-Id": ctx.request_id},
    )


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    """🔒 The catch-all. Nothing unexpected reaches the client.

    The exception is logged with a stack trace (scrubbed on the way out); the
    client receives a fixed message and a ``request_id``. This is what makes
    "errors never reveal internals" true rather than aspirational.
    """
    ctx = get_context()
    logger.exception(
        "Unhandled exception",
        extra={
            "endpoint": f"{request.method} {request.url.path}",
            "exception_class": type(exc).__name__,
        },
    )
    internal = InternalError()
    return JSONResponse(
        status_code=internal.status_code,
        content=build_envelope(internal, ctx.request_id),
        headers={"X-Request-Id": ctx.request_id},
    )


def register_error_handlers(app: FastAPI) -> None:
    """Register every handler. Order is specific-to-general."""
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(RequestValidationError, _handle_request_validation)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected)
