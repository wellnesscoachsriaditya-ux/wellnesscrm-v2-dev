"""Observability wiring — Sentry, with PII scrubbing enforced at the boundary.

NFR-080: unhandled errors captured with enough context to diagnose.
🔒 NFR-033: and with no clinical data whatsoever.

Two defences, both required:

1. ``send_default_pii=False`` stops the SDK collecting personal data itself.
2. ``before_send`` scrubs what *our own code* attached — extras, tags,
   breadcrumbs, exception messages.

Sentry is optional. Without a DSN the application runs normally and errors go to
the logs, which are scrubbed by the same rules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from app.kernel.scrubbing import sentry_before_send
from app.platform.config import Environment, Settings
from app.platform.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)


def configure_observability(settings: Settings, *, component: str) -> None:
    """Initialise error reporting.

    Args:
        settings: Application settings.
        component: ``"web"`` or ``"worker"`` — the two processes report
            separately so a worker failure is not mistaken for a request
            failure (ADR-01).
    """
    if not settings.sentry_dsn:
        logger.info(
            "Error reporting not configured; errors will appear in logs only",
            extra={"component": component},
        )
        return

    try:
        import sentry_sdk
    except ImportError:  # pragma: no cover - dependency is pinned
        logger.warning("sentry-sdk not installed; error reporting disabled")
        return

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.app_env.value,
        release=_release_identifier(),
        # 🔒 NFR-033 — never collect PII automatically.
        send_default_pii=False,
        # 🔒 NFR-033 — and scrub whatever our code attached.
        #
        # The cast is the price of keeping the scrubber SDK-agnostic. Sentry
        # types this hook against its own ``Event`` TypedDict; ``sentry_before_send``
        # takes a plain dict so that ``kernel.scrubbing`` has no dependency on
        # sentry-sdk and its NFR-033 tests run without one. The shapes are
        # structurally identical — Event *is* a TypedDict over str keys — so this
        # narrows a nominal mismatch at the one boundary where the SDK is known,
        # rather than leaking the SDK's types into the kernel.
        before_send=cast("Callable[[Any, Any], Any]", sentry_before_send),
        traces_sample_rate=settings.sentry_traces_sample_rate,
        # Breadcrumbs record recent activity; capped so an error report cannot
        # accumulate a long trail of request data.
        max_breadcrumbs=30,
        attach_stacktrace=True,
    )
    sentry_sdk.set_tag("component", component)

    logger.info("Error reporting configured", extra={"component": component})


def _release_identifier() -> str | None:
    """Best-effort release identifier for grouping errors by deploy."""
    import os

    return os.getenv("GIT_COMMIT_SHA") or os.getenv("RELEASE_VERSION")


def is_production_like(settings: Settings) -> bool:
    """Whether production-grade behaviour applies (JSON logs, strict checks)."""
    return settings.app_env is not Environment.LOCAL
