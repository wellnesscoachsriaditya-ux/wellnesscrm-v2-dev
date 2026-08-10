"""The public enquiry form — API §11.1 and §11.2, FR-M2-001..009.

🔒 **The only unauthenticated write in the product.** Arch §15.3 keeps the whole
public surface enumerable in one place; this file is the half of it that touches
tenant data. Four things stand between a stranger and a ``clients`` row, and each
is here rather than in the module because each needs something ``platform`` owns:

1. **Rate limiting** (API §14.2) — ``platform.http.rate_limit``.
2. **Tenant resolution from the slug**, server-side, then
   :func:`adopt_tenant_scope`. The module never sees a tenant it did not receive.
3. **Spam scoring and consent** — enforced inside ``leads.submit``, which refuses
   before creating anything (EC-M2-03, EC-M2-04).
4. **The consent ledger entry** — ``platform.consent``, appended here because R5
   forbids the module importing it.

🔒 **The response is identical in every accepted case** (API §11.2). Same status,
same body, whether the mobile matched an existing client, whether a new client was
created, or whether the submission was silently dropped as spam. Three code paths,
one observable outcome — that is what stops this endpoint being an oracle for a
practitioner's client list, and it is asserted in the integration suite rather
than left to inspection.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Request, status
from pydantic import BaseModel, EmailStr, Field

from app.kernel.consent import ConsentDecision, ConsentSubject
from app.kernel.consent import now as consent_now
from app.kernel.context import ActorType
from app.kernel.errors import NotFoundError
from app.kernel.leads import (
    MAX_PRIMARY_GOAL_LENGTH,
    MAX_SOURCE_DETAIL_LENGTH,
    MAX_SOURCE_LENGTH,
    SpamSignals,
    acknowledgement,
)
from app.kernel.models import ConsentAction, ConsentChannel, ConsentSubjectType
from app.modules.leads import (
    SpamRejectedError,
    SubmissionInput,
    load_public_form,
    submit,
)
from app.platform.config import get_settings
from app.platform.consent import append_decisions, notice_in_force
from app.platform.http import rate_limit
from app.platform.http.pipeline import adopt_tenant_scope, get_session, public_router
from app.platform.logging import get_logger

logger = get_logger(__name__)

router = public_router("/api/v1/public/forms", tags=["public"])


# ─── Schemas ─────────────────────────────────────────────────────────────


class PublicConsentNotice(BaseModel):
    """The notice a prospect must agree to — API §11.1, FR-M2-004.

    🔒 The **body** is sent, not just a reference. DPDP requires consent against
    text the person actually saw (NFR-051), and a form that linked to a notice
    elsewhere could not evidence that they saw it.
    """

    notice_id: uuid.UUID
    version: str
    title: str
    body: str


class PublicFormResponse(BaseModel):
    """API §11.1's response.

    🔒 **An allowlist, not a projection of the row.** API §11.1: "never client
    counts, plan details, or any tenant-internal state". `tenant_id` is
    deliberately absent — the submit endpoint re-resolves it from the slug rather
    than trusting one echoed back, so publishing it would buy nothing and leak an
    internal identifier.
    """

    form_id: uuid.UUID
    practice_name: str
    title: str
    intro_text: str | None
    consent: PublicConsentNotice


class EnquirySubmitRequest(BaseModel):
    """API §11.2's request body.

    ⚠️ Every constraint here is duplicated in ``kernel.leads`` and in the database
    CHECKs, and the redundancy is deliberate: this one produces a field-level 422
    the form can render inline (EC-M2-01), the kernel's is what any other caller
    would hit, and the CHECK is what holds if both are bypassed.
    """

    full_name: str = Field(min_length=1, max_length=120)
    primary_goal: str = Field(min_length=1, max_length=MAX_PRIMARY_GOAL_LENGTH)
    # 🔒 API §11.2 — "required unless email given". Expressed as two optional
    # fields plus a service-level check rather than a validator pair, so the
    # message names a field the form can highlight.
    mobile: str | None = Field(default=None, max_length=24)
    email: EmailStr | None = Field(default=None, max_length=254)
    source: str | None = Field(default=None, max_length=MAX_SOURCE_LENGTH)
    source_detail: str | None = Field(default=None, max_length=MAX_SOURCE_DETAIL_LENGTH)
    #: 🔒 Must be `true` (EC-M2-04). Not typed `Literal[True]`: that would be a
    #: 422 naming a type, where this is a 403 explaining what consent is for.
    consent_granted: bool = False
    #: 🔒 The notice the form displayed — refused if superseded since (NFR-051).
    consent_notice_id: uuid.UUID | None = None
    #: 🔒 FR-M2-008 — the honeypot. Rendered, hidden by CSS, invisible to humans.
    #: Named for what it pretends to be, not for what it is: a bot reading the
    #: schema should see a plausible optional field.
    company: str | None = Field(default=None, max_length=200)
    #: How long the form was open, in seconds. Advisory — a bot can lie, which is
    #: why it only contributes to a score rather than deciding one.
    elapsed_seconds: float | None = Field(default=None, ge=0)
    #: ⏳ FR-M2-008's CAPTCHA token. Accepted and **not verified** — no provider
    #: is wired at MVP (S5's transport work). Declared so the contract is stable
    #: and the form can start sending it; see `kernel.leads.score_submission` for
    #: what carries the weight meanwhile.
    captcha_token: str | None = Field(default=None, max_length=4096)


class EnquirySubmitResponse(BaseModel):
    """🔒 API §11.2's 202 — and the whole of what a submitter learns.

    ⚠️ **No identifiers.** Not the client id, not the submission id, not whether
    a record already existed. `message` comes from
    `kernel.leads.acknowledgement()`, which takes no arguments and therefore
    cannot vary on the match (EC-M2-02).
    """

    submitted: bool = True
    message: str


# ─── Endpoints ───────────────────────────────────────────────────────────


@router.get(
    "/{tenant_slug}",
    summary="The public enquiry form",
    operation_id="publicEnquiryForm",
)
async def read_public_form(
    request: Request,
    tenant_slug: Annotated[str, Field(min_length=1, max_length=64)],
) -> PublicFormResponse:
    """The form a shareable link resolves to — FR-M2-001, API §11.1.

    🔒 **404 for unknown, inactive and suspended alike** (EC-M2-07). API §11.1 is
    explicit that a suspended tenant returns 404 "with a neutral message — never
    'this practitioner hasn't paid'". Distinguishing the cases would publish a
    fact about someone's business that nobody asked us to publish.

    ⚠️ Runs with **no tenant scope**. Resolving the slug is what establishes the
    tenant, so the read happens under migration 0015's ``enquiry_forms__public_read``
    policy — the only tenant-less read in the codebase, and one whose row API §11.1
    calls "effectively public information".
    """
    rate_limit.check(
        "enquiry_form_read", rate_limit.client_key(request), rate_limit.ENQUIRY_FORM_READ
    )

    session = get_session(request)
    form = await load_public_form(session, tenant_slug=tenant_slug)
    if form is None:
        raise _no_such_form()

    notice = await notice_in_force(session)
    if notice is None:
        # 🔒 No notice, no lawful capture (FR-M2-004). The form is not rendered
        # at all rather than rendered without a consent box — which would collect
        # data on no basis and look like it was working.
        logger.warning("enquiry.form_unavailable_no_notice")
        raise _no_such_form()

    return PublicFormResponse(
        form_id=form.form_id,
        practice_name=form.practice_name,
        title=form.title,
        intro_text=form.intro_text,
        consent=PublicConsentNotice(
            notice_id=notice.notice_id,
            version=notice.version,
            title=notice.title,
            body=notice.body,
        ),
    )


@router.post(
    "/{tenant_slug}/submit",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit an enquiry",
    operation_id="publicEnquirySubmit",
)
async def submit_enquiry(
    request: Request,
    tenant_slug: Annotated[str, Field(min_length=1, max_length=64)],
    payload: EnquirySubmitRequest,
) -> EnquirySubmitResponse:
    """Accept an enquiry — FR-M2-005, API §11.2.

    🔒 **202 with an identical body in every accepted case.** New client, matched
    client (EC-M2-02), or silently-dropped spam (EC-M2-03) — one response. The
    two refusals that *are* visible are the ones the submitter can act on: a 403
    for declined consent (EC-M2-04) and a 422 for a malformed mobile (EC-M2-01).

    🔒 **Never metered** (EC-M2-06, FR-M1-003). A tenant at their client limit
    still accepts enquiries; the limit binds at conversion to `active`.
    """
    rate_limit.check("enquiry_submit", rate_limit.client_key(request), rate_limit.ENQUIRY_SUBMIT)

    session = get_session(request)

    # 🔒 Step 1: resolve the tenant server-side, still outside any tenant scope.
    form = await load_public_form(session, tenant_slug=tenant_slug)
    if form is None:
        raise _no_such_form()

    notice = await notice_in_force(session)
    if notice is None:
        logger.warning("enquiry.submit_rejected_no_notice")
        raise _no_such_form()

    # 🔒 Step 2: adopt the resolved tenant's scope for the writes that follow.
    # The uuid came from the SELECT above, never from the request — see
    # `adopt_tenant_scope` for why that distinction is the whole safety argument.
    await adopt_tenant_scope(request, tenant_id=form.tenant_id)

    try:
        result = await submit(
            session,
            tenant_id=form.tenant_id,
            form_id=form.form_id,
            payload=SubmissionInput(
                full_name=payload.full_name,
                primary_goal=payload.primary_goal,
                mobile=payload.mobile,
                email=payload.email,
                source=payload.source,
                source_detail=payload.source_detail,
                consent_granted=payload.consent_granted,
                consent_notice_id=payload.consent_notice_id,
                signals=SpamSignals(
                    honeypot_filled=bool(payload.company and payload.company.strip()),
                    elapsed_seconds=payload.elapsed_seconds,
                    # ⏳ Always False — no CAPTCHA provider is wired (see the
                    # request schema). Stated here rather than silently omitted
                    # so the day a provider lands, this is the line to change.
                    captcha_verified=False,
                ),
            ),
            notice_id_in_force=notice.notice_id,
            mobile_hash_secret=get_settings().audit_ip_salt.get_secret_value(),
        )
    except SpamRejectedError:
        # 🔒 EC-M2-03 — blocked before record creation, and indistinguishable
        # from success to the caller. Logged without any submitted value: the
        # operational question is "how much is being blocked", and answering it
        # must not put a spammer's payload in the log.
        logger.info("enquiry.spam_rejected", extra={"tenant_id": str(form.tenant_id)})
        return EnquirySubmitResponse(message=acknowledgement())

    # 🔒 Step 3: the ledger entry — FR-M2-004, AC-M2-004. Appended here rather
    # than in the module because R5 forbids `leads` importing `platform.consent`.
    # Same transaction (ADR-04), so the consent and the client it describes
    # commit together or not at all.
    await append_decisions(
        session,
        [
            ConsentDecision(
                subject=ConsentSubject(
                    tenant_id=form.tenant_id,
                    subject_type=ConsentSubjectType.CLIENT,
                    subject_id=result.client_id,
                    subject_mobile_hash=result.mobile_hash,
                ),
                purpose_id=purpose_id,
                notice_id=notice.notice_id,
                action=ConsentAction.GRANTED,
                captured_via=ConsentChannel.ENQUIRY_FORM,
                # 🔒 The prospect acted, and they are nobody the system knows.
                # `ANONYMOUS` is the honest actor type: attributing this to the
                # practitioner would record a decision they did not make.
                captured_by_actor_type=ActorType.ANONYMOUS,
                captured_by_actor_id=None,
                evidence={
                    "notice_version": notice.version,
                    "channel": "public_enquiry_form",
                },
                occurred_at=result.occurred_at or consent_now(),
            )
            # 🔒 One entry per purpose, itemised (FR-M0-022). A single blanket
            # "granted" is exactly what the per-purpose ledger exists to make
            # inexpressible.
            for purpose_id in notice.purpose_ids
        ],
    )

    return EnquirySubmitResponse(message=acknowledgement())


def _no_such_form() -> NotFoundError:
    """🔒 One 404 for every reason a form is unreachable — EC-M2-07, API §11.1.

    Unknown slug, deactivated form, suspended tenant, missing consent notice.
    The message names none of them: a stranger learning that a practice exists
    but has stopped accepting enquiries is a fact about that business we were
    never asked to publish.
    """
    return NotFoundError(
        "This enquiry form is not available.",
        action="Check the link, or contact the practice directly.",
    )
