"""Accepting an enquiry — the public write path.

🔒 FR-M2-005, API §11.2, EC-M2-02/03/04/06. This is the only unauthenticated
write in the module, and every decision in it is shaped by that.

**The order of operations is the design**, not an implementation detail:

1. **Spam** (FR-M2-008) — before anything is created. EC-M2-03 requires spam
   "blocked before record creation; not counted in metrics", so a refused
   submission must leave no row anywhere.
2. **Consent** (FR-M2-004, EC-M2-04) — also before anything is created. A
   declined consent creates nothing at all, so the prospect's name and number are
   discarded with the request.
3. **Match** (EC-M2-02) — silently, through the kernel port.
4. **Create or reuse** the client, then the submission, then the ledger entry,
   then the event. All in the caller's transaction (ADR-04), so a failure at any
   step leaves nothing partial.

🔒 **Nothing here reveals the match to the caller.** :func:`submit` returns a
result carrying ``is_duplicate``, and the *router* discards it — the type exists
because the practitioner's notification needs it (FR-M2-006), never the response.
API §11.2: returning "we already have you" would make this endpoint a
client-enumeration oracle against a practitioner's client list.

🔒 **Never metered** (FR-M1-003, EC-M2-06). No entitlement guard is consulted
anywhere in this file. A tenant at their client limit still accepts enquiries;
the limit binds at conversion to `active`, which is a practitioner action.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.clients import (
    ClientIdentity,
    LeadIntake,
    get_client_directory,
    get_client_intake,
    normalise_mobile,
    validate_full_name,
)
from app.kernel.consent import hash_mobile
from app.kernel.errors import ValidationError
from app.kernel.events import publish
from app.kernel.leads import (
    EnquiryReceived,
    LeadSource,
    SpamSignals,
    assert_consent_granted,
    assert_notice_current,
    is_known_source,
    is_spam,
    looks_like_link_spam,
    normalise_source,
    normalise_source_detail,
    score_submission,
    validate_primary_goal,
)
from app.modules.leads.forms import now
from app.modules.leads.models import EnquirySubmission

#: The default when a caller supplies no spam signals — an ordinary
#: submission with nothing observed against it. A module-level constant
#: rather than a `field(default_factory=...)`: `SpamSignals` is frozen, so one
#: shared instance cannot be mutated by a caller, and a fresh object per
#: submission would allocate for no reason.
_NO_SIGNALS = SpamSignals()


@dataclass(frozen=True, slots=True)
class SubmissionInput:
    """What a prospect sent — API §11.2's request body, before validation.

    ⚠️ Raw values. Every field here came from an unauthenticated caller and none
    has been trusted yet; :func:`submit` is what validates and normalises them.
    Keeping the raw shape as a distinct type is what makes "as submitted, never
    mutated" (DB §6.2) checkable — the columns are written from these fields.
    """

    full_name: str
    primary_goal: str
    mobile: str | None = None
    email: str | None = None
    source: str | None = None
    source_detail: str | None = None
    consent_granted: bool = False
    #: 🔒 The notice the form displayed. Compared against the one in force.
    consent_notice_id: uuid.UUID | None = None
    signals: SpamSignals = _NO_SIGNALS


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    """What the enquiry produced — for the *server's* use only.

    🔒 **``is_duplicate`` must not reach the response.** It is here because
    FR-M2-006's practitioner notification says "an existing client enquired
    again", and because the timeline event carries it. The router constructs its
    own response from `kernel.leads.acknowledgement()`, which takes no arguments
    and therefore cannot express this.
    """

    submission_id: uuid.UUID
    client_id: uuid.UUID
    is_duplicate: bool
    #: 🔒 Keyed HMAC of the submitted mobile, or ``None`` for an email-only
    #: enquiry. The caller records consent against it (see :func:`submit` step 6).
    mobile_hash: str | None
    #: The moment every row in this submission shares.
    occurred_at: datetime


class SpamRejectedError(Exception):
    """🔒 A submission scored as automated — EC-M2-03.

    ⚠️ **Not an ``AppError``, deliberately.** Every ``AppError`` becomes an error
    envelope naming what went wrong (API §5.1), and telling a bot which check it
    failed is how the next attempt passes. The router catches this and returns
    the **same 202 and the same body** a genuine submission receives.

    🔒 The row is never written, so EC-M2-03's "not counted in metrics" holds by
    construction rather than by filtering the metric later.
    """


async def submit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    form_id: uuid.UUID,
    payload: SubmissionInput,
    notice_id_in_force: uuid.UUID | None,
    mobile_hash_secret: str,
) -> SubmissionResult:
    """Accept an enquiry — FR-M2-005, and the whole of API §11.2.

    Args:
        notice_id_in_force: The notice the ledger entry will cite. ``None`` when
            no notice is seeded, which refuses the submission — consent recorded
            against no text is not consent (FR-M2-004).
        mobile_hash_secret: 🔒 Keys the pre-client consent subject. A mobile
            number has ~10 digits of entropy, so an unkeyed digest of the Indian
            numbering space is reversible on a laptop (NFR-033).

    Raises:
        SpamRejectedError: EC-M2-03. The caller answers 202 regardless.
        ConsentError: 403, EC-M2-04. Nothing is created.
        ValidationError: EC-M2-01 — a malformed mobile, an empty goal, or a
            stale notice id.
    """
    # ─ 1. Spam, before anything exists (EC-M2-03) ────────────────────────
    goal = validate_primary_goal(payload.primary_goal)
    signals = SpamSignals(
        honeypot_filled=payload.signals.honeypot_filled,
        elapsed_seconds=payload.signals.elapsed_seconds,
        captcha_verified=payload.signals.captcha_verified,
        # Computed here rather than trusted from the caller: it is a fact about
        # the submitted text, and the caller does not get to assert it.
        goal_is_link_only=looks_like_link_spam(goal),
    )
    score = score_submission(signals)
    if is_spam(score):
        raise SpamRejectedError

    # ─ 2. Consent, still before anything exists (EC-M2-04) ───────────────
    assert_consent_granted(granted=payload.consent_granted)

    if notice_id_in_force is None:
        # ⚠️ Not the prospect's fault and not phrased as though it were. A tenant
        # whose locale has no notice seeded cannot lawfully capture consent, and
        # accepting the enquiry anyway would create a record with no basis.
        raise ValidationError(
            "This form is not available right now.",
            action="Please contact the practice directly.",
            details={"reason": "no_consent_notice"},
        )

    if payload.consent_notice_id is not None:
        # 🔒 The form told us which version it displayed. A mismatch means the
        # notice was superseded between render and submit — see
        # `kernel.leads.assert_notice_current` for why that is refused.
        assert_notice_current(
            submitted_notice_id=payload.consent_notice_id,
            notice_in_force=notice_id_in_force,
        )

    # ─ 3. Validate and normalise the contact details (EC-M2-01) ──────────
    name = validate_full_name(payload.full_name)
    mobile = normalise_mobile(payload.mobile) if payload.mobile else None
    email = payload.email.strip() if payload.email else None

    if mobile is None and email is None:
        # FR-M1-004 / EC-M1-08, restated at the boundary so the message names a
        # field rather than surfacing an integrity error three frames up.
        raise ValidationError(
            "We need a way to reach you.",
            action="Enter your mobile number.",
            details={"field": "mobile"},
        )

    source, source_detail = _resolve_source(payload.source, payload.source_detail)

    # ─ 4. Match silently (EC-M2-02) ──────────────────────────────────────
    matched = await _match_existing(session, tenant_id=tenant_id, mobile=mobile)

    if matched is not None:
        client_id, is_duplicate = matched.id, True
    else:
        created = await get_client_intake().create_lead(
            session,
            tenant_id=tenant_id,
            intake=LeadIntake(
                full_name=name,
                mobile=mobile,
                email=email,
                source=source,
                source_detail=source_detail,
            ),
        )
        client_id, is_duplicate = created.id, False

    moment = now()

    # ─ 5. The submission — evidence, written once (DB §6.2) ──────────────
    submission = EnquirySubmission(
        tenant_id=tenant_id,
        form_id=form_id,
        client_id=client_id,
        submitted_name=name,
        submitted_mobile=mobile,
        submitted_email=email,
        primary_goal=goal,
        source=LeadSource(source) if source is not None else None,
        source_detail=source_detail,
        consent_notice_id=notice_id_in_force,
        is_duplicate_of_existing=is_duplicate,
        # 🔒 Recorded even though it passed: FR-M2-008 wants the signal
        # retained, and a column that is only ever NULL cannot be tuned against.
        spam_score=Decimal(str(round(score, 2))),
        submitted_at=moment,
    )
    session.add(submission)
    await session.flush()

    # ─ 6. The event — the timeline row and the notification (AC-M1-004) ──
    #
    # ⚠️ 🔒 **The consent ledger entry is appended by the caller, not here**, and
    # the reason is R5: `kernel.consent` holds the rules but `platform.consent`
    # owns the ledger's persistence, and a module may not import `platform`. The
    # router appends it immediately after this returns — inside the same
    # transaction (ADR-04), so the entry and the client it describes still commit
    # together or not at all.
    #
    # ⚠️ `consent_record_id` is therefore left NULL on the submission row.
    # `consent_notice_id` above is what makes NFR-051 answerable from this row:
    # it names the exact text presented, which is the fact a regulator asks for.
    # The ledger id would add a join to the same answer.
    await publish(
        EnquiryReceived(
            client_id=client_id,
            tenant_id=tenant_id,
            submission_id=submission.id,
            form_id=form_id,
            is_duplicate=is_duplicate,
            source=source,
            received_at=moment,
        ),
        session,
    )

    return SubmissionResult(
        submission_id=submission.id,
        client_id=client_id,
        is_duplicate=is_duplicate,
        #: 🔒 The subject the caller records consent against. Computed here
        #: because this is where the normalised mobile exists, and the secret is
        #: the caller's to hold.
        mobile_hash=hash_mobile(mobile, secret=mobile_hash_secret) if mobile else None,
        occurred_at=moment,
    )


def _resolve_source(raw: str | None, raw_detail: str | None) -> tuple[str | None, str | None]:
    """Fold a submitted source into the vocabulary — FR-M2-009.

    🔒 An unrecognised source becomes ``other``, with the original text kept in
    ``source_detail``. That keeps US-M2-04's grouping question answerable — a
    per-campaign proliferation of one-row "channels" would make the report
    useless — without discarding what the link actually said.
    """
    normalised = normalise_source(raw)
    detail = normalise_source_detail(raw_detail)

    if normalised is None:
        return None, detail
    if is_known_source(normalised):
        return normalised, detail
    return LeadSource.OTHER.value, detail or normalise_source_detail(raw)


async def _match_existing(
    session: AsyncSession, *, tenant_id: uuid.UUID, mobile: str | None
) -> ClientIdentity | None:
    """Find the client this enquiry is about, if we already hold them — EC-M2-02.

    🔒 **Matched on mobile only**, per EC-M2-02's own wording ("matched on
    mobile"). Matching on email as well was considered and rejected: a shared
    family email is as common as a shared handset, and an email match would
    append a stranger's enquiry to a relative's clinical record — a far worse
    outcome than a duplicate the practitioner can merge (FR-M1-024, Phase 2).

    ⚠️ **The first match wins when several share a number** (EC-M1-01 permits
    it). There is no better answer available: the submission carries a name, but
    matching on name-plus-mobile would create a second record every time somebody
    spelled their own name differently, which is the duplicate this exists to
    prevent. The practitioner sees the enquiry on the record and can move it.

    ⚠️ Reads through ``ClientDirectory`` — the sanctioned seam (DB §5). R3 forbids
    importing `clients`, and R6 forbids querying its tables.
    """
    if mobile is None:
        # 🔒 No mobile, no match. An email-only enquiry creates a new record
        # rather than guessing — see the docstring.
        return None

    candidates = await get_client_directory().find_by_mobile(
        session, tenant_id=tenant_id, mobile=mobile
    )
    if not candidates:
        return None

    # ⚠️ Archived clients are skipped. A returning prospect whose record was
    # archived should surface as a live lead the practitioner sees, not as a
    # silent append to a record excluded from every working view (DB §22.2).
    live = [candidate for candidate in candidates if not candidate.is_archived]
    return live[0] if live else None


__all__ = [
    "SpamRejectedError",
    "SubmissionInput",
    "SubmissionResult",
    "submit",
]
