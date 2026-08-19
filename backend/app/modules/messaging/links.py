"""Where a link in a message points — the deep-link base URL.

🔒 A seam, for the reason every seam in this codebase exists: R5 forbids a module
importing ``app.platform``, and the base URL lives in settings. The kernel is not
involved because nothing outside messaging needs it — the entry point supplies
the value, this module holds it.

⚠️ **Built from configuration, never from a request's Host header.** A message is
composed in a worker, where there is no request; and even in the web process,
trusting the Host header would let a caller poison the link a client receives
(the same argument `routers/enquiries.py` makes for the public form URL).
"""

from __future__ import annotations

import uuid

#: Set by the entry points from ``settings.app_base_url``.
_base_url: str = "http://localhost:8000"


def configure_link_base_url(base_url: str) -> None:
    """Install the public base URL. Called once, at startup."""
    global _base_url
    _base_url = base_url.rstrip("/")


def base_url() -> str:
    """The configured base, without a trailing slash."""
    return _base_url


def portal_url() -> str:
    """The client portal's landing route — the check-in nudge's destination."""
    return f"{_base_url}/portal"


def plan_url(plan_version_id: uuid.UUID) -> str:
    """The deep link to one issued plan — FR-M8-013, AC-M8-001.

    ⚠️ Points at the *portal* route rather than the API. A client opening this
    from WhatsApp is a person with a phone, not a caller with a token; the portal
    handles the magic-link redemption that turns one into the other (FR-M7-013).
    """
    return f"{_base_url}/portal/plans/{plan_version_id}"


def assessment_url(response_id: uuid.UUID) -> str:
    """The deep link to an assessment a client has been asked to complete."""
    return f"{_base_url}/portal/assessments/{response_id}"
