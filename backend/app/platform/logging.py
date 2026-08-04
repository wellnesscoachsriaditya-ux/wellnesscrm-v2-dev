"""Structured logging with mandatory scrubbing.

NFR-033 and NFR-080. Every log record passes through the scrubber
(:mod:`app.kernel.scrubbing`) before it is emitted — there is no unscrubbed
path, because a scrubber that can be bypassed will be bypassed.

JSON output in production so records are queryable; human-readable locally
because a solo developer reading JSON in a terminal is a waste of attention.

Request context (:mod:`app.kernel.context`) is attached automatically, so a log
line can be traced to its request without any call site remembering to include
the identifier.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from pythonjsonlogger.json import JsonFormatter

from app.kernel.context import get_context
from app.kernel.scrubbing import is_sensitive_key, scrub_text, scrub_value

_CONFIGURED = False


class ContextFilter(logging.Filter):
    """Attach request context to every record.

    A filter rather than an adapter so it applies to third-party loggers too —
    SQLAlchemy and uvicorn records get the same treatment as ours.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in get_context().to_log_fields().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class ScrubbingFilter(logging.Filter):
    """🔒 Scrub every record. The last line of defence before emission.

    Applied to the *handler* rather than a logger, so it cannot be sidestepped
    by obtaining a logger some other way.
    """

    _RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__)

    def filter(self, record: logging.LogRecord) -> bool:
        # The formatted message: catches interpolated values.
        if record.args:
            record.msg = record.getMessage()
            record.args = ()
        if isinstance(record.msg, str):
            record.msg = scrub_text(record.msg)

        # Structured extras: catches `logger.info("...", extra={...})`.
        for key, value in list(record.__dict__.items()):
            if key in self._RESERVED or key.startswith("_"):
                continue
            if is_sensitive_key(key):
                record.__dict__[key] = "[redacted]"
            elif isinstance(value, dict | list):
                record.__dict__[key] = scrub_value(value)
            elif isinstance(value, str):
                record.__dict__[key] = scrub_text(value)

        return True


class HumanFormatter(logging.Formatter):
    """Readable local output: ``HH:MM:SS LEVEL logger — message [ctx]``."""

    _COLOURS = {
        "DEBUG": "\033[36m", "INFO": "\033[32m", "WARNING": "\033[33m",
        "ERROR": "\033[31m", "CRITICAL": "\033[35m",
    }
    _RESET = "\033[0m"
    _CONTEXT_KEYS = ("request_id", "tenant_id", "actor_type")

    def format(self, record: logging.LogRecord) -> str:
        colour = self._COLOURS.get(record.levelname, "")
        ts = self.formatTime(record, "%H:%M:%S")
        base = (
            f"{ts} {colour}{record.levelname:<8}{self._RESET} "
            f"{record.name:<28} {record.getMessage()}"
        )

        bits = [
            f"{k}={getattr(record, k)}"
            for k in self._CONTEXT_KEYS
            if getattr(record, k, None) and getattr(record, k) != "anonymous"
        ]
        if bits:
            base += f"  \033[90m[{' '.join(bits)}]{self._RESET}"

        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging(*, level: str = "INFO", json_output: bool = False) -> None:
    """Configure application logging. Idempotent.

    Args:
        level: Root log level.
        json_output: JSON in production; human-readable locally.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "timestamp", "levelname": "level"},
            timestamp=True,
        )
        if json_output
        else HumanFormatter()
    )

    # 🔒 Order matters: context is attached first, then everything is scrubbed.
    handler.addFilter(ContextFilter())
    handler.addFilter(ScrubbingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Third-party noise reduction. SQLAlchemy at INFO logs every statement,
    # and statements contain parameter values — i.e. clinical data.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a module logger.

    Use ``get_logger(__name__)``. All records are scrubbed regardless of
    origin, so this is a convenience rather than a safety boundary.
    """
    return logging.getLogger(name)


def log_extra(**fields: Any) -> dict[str, Any]:
    """Build structured log fields.

    Exists to make the intent explicit at call sites::

        logger.info("Plan issued", extra=log_extra(plan_version_id=str(pv.id)))

    🔒 Pass identifiers, not values. The scrubber will redact a value that slips
    through, but relying on it is how leaks eventually happen.
    """
    return fields
