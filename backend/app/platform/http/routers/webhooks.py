"""Provider webhooks — delivery status only (API §11.3, EC-M8-07).

🔒 The contract API §11.3 sets, in the order it sets it:

1. **Verify the signature before parsing the body.** An unverified webhook is an
   unauthenticated write endpoint, and this one can move a delivery status for
   any tenant.
2. **Respond within ~2 seconds, before processing.** Providers retry
   aggressively on a slow response, which turns one receipt into a storm.
3. **Enqueue a job** for the actual work (Arch §12.5).
4. **Idempotent by provider event id** — ``uq_message_dispatches__provider_id``.

⚠️ 🔒 **Replies are accepted and discarded.** MVP handles delivery status only
(EC-M8-07): an inbound WhatsApp message reaches the practitioner's own WhatsApp,
and the two-way inbox is Phase 2 (FR-M8-030). Returning an error for one would
make Meta mark the webhook unhealthy and eventually stop sending the *status*
callbacks we do use.

⚠️ **This is the only router on ``PublicRoute`` besides the enquiry form.** Its
paths are named individually in ``EXEMPT_PATHS``; ``verify_route_authorization``
aborts startup if that is ever untrue.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import Header, Query, Request, Response, status
from pydantic import BaseModel

from app.integrations.messaging import parse_status_events, verify_signature
from app.kernel.jobs import get_job_enqueuer
from app.modules.messaging import STATUS_JOB_TYPE, tenant_for_provider_message
from app.platform.config import get_settings
from app.platform.http.pipeline import get_session, public_router
from app.platform.logging import get_logger

logger = get_logger(__name__)

router = public_router("/api/v1/public/webhooks", tags=["public"])

#: 🔒 The one provider with a webhook at MVP. An unknown provider is accepted and
#: ignored rather than 404'd — a 404 tells an unauthenticated caller which
#: integrations exist, and a provider we later add would otherwise fail loudly
#: against an older deploy mid-rollout.
_WHATSAPP = "whatsapp"

#: ⚠️ Payload values are identifiers and short codes (DB §13.1, NFR-033). A
#: provider's failure text can be a paragraph, so it is truncated well inside
#: `kernel.jobs.MAX_PAYLOAD_STRING_LENGTH` before it reaches a retained row.
_MAX_REASON_LENGTH = 120


class WebhookAck(BaseModel):
    """🔒 API §11.3 — always this shape, even for events we ignore.

    A body that varied with what we did would tell an unauthenticated caller
    which message ids exist.
    """

    received: bool = True


@router.get(
    "/{provider}",
    summary="Provider webhook verification challenge",
    operation_id="webhookVerify",
)
async def webhook_verify(
    provider: str,
    hub_mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    hub_challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
    hub_verify_token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
) -> Response:
    """Meta's one-time registration handshake.

    🔒 Echoes the challenge **only** when the token matches the configured one.
    Echoing unconditionally would let anyone register our endpoint against their
    own app and start feeding it callbacks.

    ⚠️ Returns 403 rather than 401 on a mismatch, because Meta treats 403 as
    "verification failed" and shows it in their console; a 401 is reported as a
    transport error and sends the operator looking in the wrong place.
    """
    settings = get_settings()
    expected = settings.whatsapp_verify_token

    if (
        provider != _WHATSAPP
        or hub_mode != "subscribe"
        or expected is None
        or hub_verify_token != expected.get_secret_value()
        or not hub_challenge
    ):
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    # ⚠️ Plain text, not JSON. Meta compares the body to the challenge verbatim.
    return Response(content=hub_challenge, media_type="text/plain")


@router.post(
    "/{provider}",
    summary="Provider delivery-status callback",
    operation_id="webhookReceive",
)
async def webhook_receive(
    request: Request,
    provider: str,
    x_hub_signature_256: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
) -> Response:
    """Accept a delivery receipt, queue it, and acknowledge.

    🔒 The signature is checked against the **raw body**, before it is parsed.
    JSON round-tripping changes key order and whitespace, and the signature is
    over the bytes the provider sent.

    🔒 A failure returns 401 with no body (API §11.3). No detail: an unsigned
    caller learns only that it was refused.
    """
    settings = get_settings()

    if provider != _WHATSAPP:
        # Accepted and ignored — see `_WHATSAPP`.
        return _ack()

    body = await request.body()
    secret = settings.whatsapp_webhook_secret

    if secret is None or not verify_signature(
        secret=secret.get_secret_value(), body=body, header=x_hub_signature_256
    ):
        # ⚠️ Logged at warning, without the body or the signature. A rejected
        # webhook is either a misconfiguration or a probe, and both are worth
        # seeing; neither is worth copying an unverified payload into the logs.
        logger.warning(
            "Rejected an unverified provider webhook",
            extra={"provider": provider, "has_signature": x_hub_signature_256 is not None},
        )
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        payload: Any = json.loads(body) if body else {}
    except ValueError:
        # 🔒 Signed but unparseable. Acknowledged rather than errored: the
        # provider cannot fix it by retrying, and a 4xx would count against the
        # endpoint's health.
        logger.warning("Provider webhook body was not valid JSON", extra={"provider": provider})
        return _ack()

    events = parse_status_events(payload if isinstance(payload, dict) else {})
    session = get_session(request)
    enqueue = get_job_enqueuer()
    queued = 0

    for event in events:
        # 🔒 Resolve the tenant through the narrow provider-lookup policy
        # (migration 0021 §5), then hand the work to the queue scoped to it.
        # ⚠️ An unknown id is the common, harmless case — a receipt for a message
        # another system sent, or one whose row we have purged.
        tenant_id = await tenant_for_provider_message(
            session, provider_message_id=event.provider_message_id
        )
        if tenant_id is None:
            continue

        job_id = await enqueue(
            session,
            job_type=STATUS_JOB_TYPE,
            payload={
                "provider_message_id": event.provider_message_id,
                "status": event.status,
                "occurred_at": event.occurred_at.isoformat(),
                "failure_code": event.error_code,
                "failure_reason": (event.error_detail or "")[:_MAX_REASON_LENGTH] or None,
            },
            tenant_id=tenant_id,
            # 🔒 One job per (message, status). A provider that retries the same
            # receipt — which it will — produces the same key, and
            # `uq_jobs__idempotency` refuses the duplicate. The handler is
            # idempotent as well, because two *different* receipts can still
            # describe the same transition.
            idempotency_key=f"status:{event.provider_message_id}:{event.status}",
        )
        if job_id is not None:
            queued += 1

    logger.info(
        "Provider webhook accepted",
        extra={"provider": provider, "events": len(events), "queued": queued},
    )
    return _ack()


def _ack() -> Response:
    """🔒 API §11.3's fixed acknowledgement — always the same shape."""
    return Response(
        content=WebhookAck().model_dump_json(),
        media_type="application/json",
    )
