"""Worker process entry point.

🔒 ADR-01 — background work runs here, never in the web process. Same codebase,
same modules, different entry point.

Why the separation exists from day one:

* A 30-second AI call or a headless-browser PDF render would occupy a web worker
  on a ₹600/month instance and starve request capacity.
* A web deploy would kill in-flight jobs.
* NFR-095 requires background work to be *separable* without re-architecture —
  separating it immediately makes that true by construction.

The scheduler and job dispatch loop are wired here; the claiming and execution
logic itself lives in :mod:`app.platform.job_runner`, so this module stays about
process lifecycle — start, poll, drain, stop — and the queue semantics are
testable without spawning a process.

🔒 Jobs run with worker context (:meth:`RequestContext.for_worker`), so audit and
logging behave identically in both processes — one implementation, not two
(NFR-072).
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from types import FrameType

from app.kernel.events import configure_deferred_enqueuer, deferred_job_types
from app.kernel.jobs import configure_job_enqueuer, verify_handlers_exist
from app.modules.clients import register_subscribers
from app.modules.messaging import active_tenant_ids, configure_link_base_url, sweep_tenant
from app.modules.messaging import register_jobs as register_messaging_jobs
from app.modules.nutrition import register_jobs as register_nutrition_jobs
from app.platform.config import get_settings
from app.platform.db import dispose_engine, transaction
from app.platform.job_runner import JobRunner
from app.platform.jobs import enqueue, enqueue_for_event
from app.platform.logging import configure_logging, get_logger
from app.platform.messaging_wiring import configure_messaging
from app.platform.observability import configure_observability, is_production_like

logger = get_logger(__name__)


class Worker:
    """The background job runner.

    Owns the process lifecycle: start, poll, drain, stop. The claiming and
    execution logic lives in :class:`~app.platform.job_runner.JobRunner`; this
    class is the loop and the signal handling around it.
    """

    def __init__(
        self,
        *,
        poll_interval_seconds: int,
        runner: JobRunner | None = None,
        run_scheduler: bool = True,
    ) -> None:
        self._poll_interval = poll_interval_seconds
        self._shutdown = asyncio.Event()
        self._current_work: asyncio.Task[None] | None = None
        self._runner = runner or JobRunner()
        #: 🔒 M8's single scheduler (FR-M8-001). It runs *here*, in the one
        #: background process, rather than as a self-perpetuating job: a job that
        #: re-enqueues itself stops forever the first time it dead-letters, and
        #: nothing would notice until a practitioner asked why check-ins had
        #: stopped. Turned off only by tests that drive the sweep directly.
        self._run_scheduler = run_scheduler

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
            extra={
                "poll_interval_seconds": self._poll_interval,
                "worker_id": self._runner.worker_id,
            },
        )

        while not self._shutdown.is_set():
            found_work = False
            try:
                found_work = await self._tick()
            except Exception:
                # 🔒 The loop must survive any single failure. A crashed worker
                # stops every reminder, plan delivery and rollup in the system —
                # a far worse outcome than one failed job.
                logger.exception("Worker tick failed; continuing")

            # 🔒 Poll again immediately when the last tick found work. Sleeping
            # the full interval after every tick would drain a backlog at one
            # batch per minute, so a burst of enqueues after an outage would take
            # hours to clear while the queue sat idle between polls.
            if found_work:
                continue

            # Wake early on shutdown rather than sleeping out the interval.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._shutdown.wait(),
                    timeout=self._poll_interval,
                )

        await self._drain()
        logger.info("Worker stopped")

    async def _tick(self) -> bool:
        """Sweep for due messages, then recover leases, claim and execute.

        🔒 The sweep runs **before** the claim, so a message that becomes due
        this second is dispatched in the same tick rather than waiting a full
        poll interval — which is most of NFR-009's 60-second budget.

        Returns:
            True when the tick did something, so the loop polls again rather
            than sleeping.
        """
        swept = await self._sweep_messages() if self._run_scheduler else 0
        result = await self._runner.tick()

        if result.claimed or result.recovered:
            logger.info(
                "Tick complete",
                extra={
                    "claimed": result.claimed,
                    "succeeded": result.succeeded,
                    "failed": result.failed,
                    "dead": result.dead,
                    "recovered": result.recovered,
                },
            )

        return result.did_work or swept > 0

    async def _sweep_messages(self) -> int:
        """Queue every due message, one tenant at a time — M8's scheduler.

        ⚠️ **One transaction per tenant**, because RLS scopes a transaction to a
        single tenant and there is no cross-tenant "what is due" query for the
        application role — nor should there be. At the 50–200 tenants this
        product is sized for that is a few hundred cheap indexed queries a
        minute; `modules.messaging.scheduler` records the revisit trigger.

        🔒 A failure for one tenant must not stop the others. A tenant whose
        sweep raises is logged and skipped, and the next tick tries again.
        """
        queued = 0
        async with transaction() as session:
            tenant_ids = await active_tenant_ids(session)

        for tenant_id in tenant_ids:
            try:
                async with transaction(tenant_id=tenant_id) as session:
                    result = await sweep_tenant(session, tenant_id=tenant_id)
                queued += result.dispatches_queued + result.checkins_generated
            except Exception:
                logger.exception(
                    "Message sweep failed for one tenant; continuing",
                    extra={"tenant_id": str(tenant_id)},
                )

        return queued

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

    # 🔒 The same two lines as `create_app`, and both are needed here too.
    # A job handler may itself publish an event with a deferred subscriber, so
    # the worker process must be able to enqueue; and it must refuse to start
    # for the same reason the web process does — a job type nothing can run
    # would dead-letter every row that reached it.
    configure_deferred_enqueuer(enqueue_for_event)
    # 🔒 The direct-enqueue seam — see `main.create_app` for why both exist. The
    # worker needs it because the sweep runs here.
    configure_job_enqueuer(enqueue)

    # 🔒 M8 — the transports and the deep-link base URL. The worker is where
    # messages are actually sent, so a worker without this configured would
    # dispatch nothing.
    configure_messaging(settings)
    configure_link_base_url(settings.app_base_url)

    verify_handlers_exist(deferred_job_types())

    # 🔒 DDR-06 — and needed here for a reason easy to miss. Subscriptions are
    # process-global state, so a job handler that changes a client's stage would
    # publish into a registry with no timeline subscriber in it: the change would
    # commit and the timeline would silently lack the entry. Registering in both
    # entry points is what makes "every event produces a row" true regardless of
    # which process published it. Idempotent by handler identity.
    register_subscribers()
    register_nutrition_jobs()
    # 🔒 The dispatch handler, the webhook-status handler and the four producers.
    register_messaging_jobs()

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
