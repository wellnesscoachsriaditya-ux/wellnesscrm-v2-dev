"""Rate limiting for the public surface — API §11, §14.2, FR-M2-008.

🔒 **The public endpoints have no authentication to fall back on.** Everything
else in the system is bounded by "you must be signed in"; ``/public/*`` is
bounded by this and by the spam score, and nothing else. API §14.2 sets the
budgets — ``5/min, 20/hr`` per IP for enquiry submission — and this is where they
are enforced.

⚠️ 🔒 **In-process, fixed-window, and single-instance only.** State lives in a
dict in this process. That is honest for the deployment the plan actually
describes — one web process on a ₹600/month instance (M2.2, the ₹5,000/month
infrastructure ceiling) — and wrong the moment a second replica exists, because
each would hold its own counters and the effective limit would multiply by the
replica count.

The alternative was Redis, and it was rejected for this slice: it is a service
the budget does not have, and a rate limiter that fails open when its backing
store is unreachable is worse than one whose limitation is written down. When a
second replica is provisioned, :func:`check` is the single seam to reimplement —
the callers do not change.

⚠️ **Fixed window, not sliding.** A burst can straddle a boundary and land 2× the
budget in a moment. Acceptable here: the budget exists to stop scripted floods,
not to shape traffic, and a sliding window costs per-request memory proportional
to the limit. Named because the property is surprising, not because it is a bug.

🔒 **The IP is hashed before it becomes a key** (NFR-033). A raw address in a
process-resident dict is personal data sitting in memory with no retention rule;
the hash is keyed with the same salt the audit log uses.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from dataclasses import dataclass

from fastapi import Request

from app.kernel.errors import RateLimitError
from app.platform.config import get_settings


@dataclass(frozen=True, slots=True)
class Budget:
    """One rate-limit rule: how many requests in how long.

    ⚠️ Two are usually needed per endpoint rather than one. API §14.2 pairs a
    short window with a long one — ``5/min`` stops a burst, ``20/hr`` stops a
    patient script that stays under it. Either alone leaves the other open.
    """

    limit: int
    window_seconds: int

    @property
    def label(self) -> str:
        """How the window reads in an error message: ``per minute``, ``per hour``."""
        if self.window_seconds % 3600 == 0:
            return "per hour"
        if self.window_seconds % 60 == 0:
            return "per minute"
        return f"per {self.window_seconds}s"


#: 🔒 API §14.2 — the enquiry submission budget. The tightest on the public
#: surface after the auth endpoints, because this one writes tenant data.
ENQUIRY_SUBMIT: tuple[Budget, ...] = (
    Budget(limit=5, window_seconds=60),
    Budget(limit=20, window_seconds=3600),
)

#: API §11 — reading a form definition. Generous: it is a public page, a prospect
#: may reload it, and the response contains nothing worth harvesting.
ENQUIRY_FORM_READ: tuple[Budget, ...] = (Budget(limit=60, window_seconds=60),)


#: ``(bucket_key, window_index) -> count``. Cleared opportunistically — see
#: :func:`_prune`.
_counters: dict[tuple[str, int], int] = {}

#: 🔒 The counters are mutated from an async request path that FastAPI may run
#: on a threadpool, so the increment must be atomic. A lost update here is a
#: request that was not counted, which is the direction that matters.
_lock = threading.Lock()

#: How many entries to tolerate before pruning expired windows. Bounded so a
#: sustained attack from many addresses cannot grow the dict without limit.
_PRUNE_THRESHOLD = 10_000


def client_key(request: Request) -> str:
    """A stable, non-identifying key for the caller.

    🔒 Hashed with the audit salt (NFR-033) — see the module docstring.

    ⚠️ ``request.client.host`` is the *socket* peer. Behind a proxy that is the
    proxy's address, and every caller would share one bucket. Deployment must
    therefore either terminate TLS at the app or configure the proxy headers
    Starlette's ``ProxyHeadersMiddleware`` reads. Flagged rather than silently
    trusting ``X-Forwarded-For``, which is caller-supplied and would let anyone
    mint a fresh bucket per request by varying a header.
    """
    client = request.client
    host = client.host if client is not None else "unknown"
    salt = get_settings().audit_ip_salt.get_secret_value()
    return hmac.new(salt.encode(), host.encode(), hashlib.sha256).hexdigest()[:32]


def check(scope: str, key: str, budgets: tuple[Budget, ...]) -> None:
    """Count one request against every budget, refusing if any is exceeded.

    Args:
        scope: What is being limited — ``enquiry_submit``. Keeps one endpoint's
            budget from consuming another's.
        key: The caller's bucket, from :func:`client_key`.
        budgets: Every rule that applies. All are counted even if an earlier one
            refuses, so a caller cannot stay under the hourly limit by being
            refused on the per-minute one.

    Raises:
        RateLimitError: 429. The message names the window but not the count —
            telling a script exactly how much headroom remains is telling it how
            fast to go.
    """
    now = time.time()
    retry_after: int | None = None

    with _lock:
        _prune(now)
        for budget in budgets:
            window_index = int(now // budget.window_seconds)
            counter_key = (f"{scope}:{key}:{budget.window_seconds}", window_index)
            count = _counters.get(counter_key, 0) + 1
            _counters[counter_key] = count
            if count > budget.limit and retry_after is None:
                # ⚠️ Computed inside the lock, from the same `now` the window
                # index used. Reading the clock again outside would let the two
                # disagree across a boundary and report a nonsensical wait.
                retry_after = _seconds_until_window_ends(now, budget)

    if retry_after is not None:
        # 🔒 The envelope carries `retry_after_seconds` and nothing about how many
        # requests remain — telling a script its exact headroom is telling it how
        # fast it may go.
        raise RateLimitError(retry_after_seconds=retry_after)


def _seconds_until_window_ends(now: float, budget: Budget) -> int:
    """How long until this fixed window rolls over.

    ⚠️ At least one second. A window with 0.2s left would round to ``0``, telling
    the caller to retry immediately — useless, and an invitation to spin.
    """
    elapsed = now % budget.window_seconds
    return max(1, int(budget.window_seconds - elapsed))


def _prune(now: float) -> None:
    """Drop counters for windows that have closed.

    ⚠️ Opportunistic rather than scheduled: a background sweep would need a task
    the web process does not otherwise run, and the dict is only unbounded under
    an attack — which is exactly when the threshold trips.

    ⚠️ Called with :data:`_lock` held.
    """
    if len(_counters) < _PRUNE_THRESHOLD:
        return

    live: dict[tuple[str, int], int] = {}
    for (bucket, window_index), count in _counters.items():
        window_seconds = int(bucket.rsplit(":", 1)[-1])
        if int(now // window_seconds) == window_index:
            live[(bucket, window_index)] = count

    _counters.clear()
    _counters.update(live)


def reset() -> None:
    """Clear every counter. 🔒 For tests only.

    ⚠️ Exported because the counters are process-global: a test that submitted
    five enquiries would otherwise change the outcome of the next one, and the
    failure would depend on collection order.
    """
    with _lock:
        _counters.clear()


__all__ = [
    "ENQUIRY_FORM_READ",
    "ENQUIRY_SUBMIT",
    "Budget",
    "check",
    "client_key",
    "reset",
]
