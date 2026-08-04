"""Worker process entry point.

🔒 ADR-01 — background work runs here, never in the web process. Same codebase,
same modules, different entry point.

Why the separation exists from day one:

* A 30-second AI call or a headless-browser PDF render would occupy a web worker
  on a ₹600/month instance and starve request capacity.
* A web deploy would kill in-flight jobs.
* NFR-095 requires background work to be *separable* without re-architecture —
  separating it immediately makes that true by construction.

The scheduler and job dispatch loop land in S1 with the ``jobs`` table. This
module currently establishes the process, its lifecycle and its shutdown
semantics, so S1 adds claiming logic rather than process management.

🔒 Jobs run with worker context (:meth:`RequestContext.for_worker`), so audit and
logging behave identically in both processes — one implementation, not two
(NFR-072).
"""

from __future__ import annotations

import asyncio
import signal
from types import FrameType

from app.kernel.context import RequestContext, context_scope
from app.platform.config import get_settings
from app.platform.db import dispose_engine
from app.platform.logging import configure_logging, get_logger
from app.platform.observability import configure_observability, is_production_like

logger = get_logger(__name__)


class Worker:
    """The background job runner.

    Owns the process lifecycle: start, poll, drain, stop. S1 attaches the
    Postgres-backed queue (``SKIP LOCKED``, leases, retries) to :meth:`_tick`.
    """

    def __init__(self, *, poll_interval_seconds: int) -> None:
        self._poll_interval = poll_interval_seconds
        self._shutdown = asyncio.Event()
        self._current_work: asyncio.Task[None] | None = None

    def request_shutdown(self, reason: str) -> None:
        """Signal a graceful stop.

        🔒 In-flight work is allowed to finish. A job killed mid-execution would
        rely on lease expiry and re-execution to recover (DB §13.3) — correct,
        but wasteful when a clean drain costs a few seconds.
        """
        if not self._shutdown.is_set():
            logger.info("Shutdown requested", extra={"reason": reason})
            self._shutdown.set()

    async def run(self) -> None:
        """Poll for due work until shutdown."""
        logger.info(
            "Worker started",
            extra={"poll_interval_seconds": self._poll_interval},
        )

        while not self._shutdown.is_set():
            try:
                await self._tick()
            except Exception:
                # 🔒 The loop must survive any single failure. A crashed worker
                # stops every reminder, plan delivery and rollup in the system —
                # a far worse outcome than one failed job.
                logger.exception("Worker tick failed; continuing")

            # Wake early on shutdown rather than sleeping out the interval.
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(),
                    timeout=self._poll_interval,
                )
            except TimeoutError:
                pass

        await self._drain()
        logger.info("Worker stopped")

    async def _tick(self) -> None:
        """Claim and execute due work.

        S1 implements this against the ``jobs`` table:

        1. ``SELECT ... FOR UPDATE SKIP LOCKED`` — claim with a lease.
        2. Execute inside :func:`context_scope` for correct audit attribution.
        3. Record the outcome in ``job_runs``; retry with backoff, or dead-letter.

        🔒 ``SKIP LOCKED`` makes multiple workers safe. Correct with one worker
        today and correct with five later — concurrency safety is built in now
        because retrofitting it is far harder than including it.
        """
        with context_scope(RequestContext.for_worker("poll")):
            logger.debug("Polling for due work")

    async def _drain(self) -> None:
        """Wait briefly for in-flight work to finish."""
        if self._current_work is None or self._current_work.done():
            return

        logger.info("Draining in-flight job")
        try:
            await asyncio.wait_for(self._current_work, timeout=30)
        except TimeoutError:
            # The lease will expire and the job will be re-claimed. Combined
            # with idempotency (API §13), re-execution is safe.
            logger.warning("In-flight job did not finish; its lease will expire")


async def main() -> None:
    """Configure and run the worker."""
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        json_output=is_production_like(settings),
    )
    configure_observability(settings, component="worker")

    logger.info(
        "Starting worker process",
        extra={"app_env": settings.app_env.value, "component": "worker"},
    )

    worker = Worker(poll_interval_seconds=settings.worker_poll_interval_seconds)

    loop = asyncio.get_running_loop()

    def _handle_signal(signum: int, _frame: FrameType | None = None) -> None:
        worker.request_shutdown(reason=signal.Signals(signum).name)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
        except NotImplementedError:
            # Windows does not support add_signal_handler for these.
            signal.signal(sig, _handle_signal)

    try:
        await worker.run()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
