"""Lead capture — the rules of the enquiry path, and what it is allowed to say.

🔒 DB §6, FR-M2-001..011, M2.3. **Capture and convert, not pipeline management.**
The whole of M2's MVP is: get an enquiry into the system without typing, and let
the practitioner move it forward. Everything a kanban board would need is
deliberately absent (FR-M2-013, Phase 2).

🔒 **There is no lead entity.** M1.3 and DB §5.1 settle it: a lead is a
``clients`` row at stage ``lead``. This module owns the *enquiry* — the raw
submission and the form that produced it — and nothing else. That split is what
makes AC-M2-006 ("a lead converted to Active requires no re-entry") true by
construction: conversion changes a column on a row that already holds every
captured field.

Three properties, each answering a specific failure the public surface invites:

1. 🔒 **The response never varies on whether the mobile matched** (API §11.2,
   EC-M2-02). :func:`acknowledgement` takes no argument that could carry the
   answer — the signature *is* the guarantee. A "we already have you" message
   would turn this endpoint into a client-enumeration oracle against a
   practitioner's list, which API §11.2 names as "the most serious privacy leak
   available on the public surface".
2. 🔒 **Consent is a precondition, not a field** (FR-M2-004, EC-M2-04).
   :func:`assert_consent_granted` refuses before anything is created, so a
   declined consent leaves no record at all rather than an orphan to clean up.
3. 🔒 **Leads are never metered** (FR-M1-003, EC-M2-06). Nothing here consults an
   entitlement, and that is the rule rather than an omission: a tenant at their
   client limit still accepts enquiries, because the limit binds at conversion.

This module holds rules only. Persistence lives in ``app.modules.leads`` — the
split is what lets every rule below be tested without a database.
"""

from __future__ import annotations

import enum
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.kernel.errors import ConsentError, ValidationError
from app.kernel.events import DomainEvent, register_event

# ─── Lead source (FR-M2-009) ─────────────────────────────────────────────


class LeadSource(str, enum.Enum):
    """Where an enquiry came from — FR-M2-009, US-M2-04.

    ⚠️ 🟡 **PROPOSED.** FR-M2-009 requires "a lead source, either selected by the
    prospect or derived from a link parameter" and names no vocabulary; DB §5.1
    types ``clients.source`` as free text. These members are derived from §3.1's
    competitive set and P1's own described channels — Instagram DMs, WhatsApp and
    referrals — plus the two a form cannot avoid needing.

    🔒 **A closed enum here, stored as text.** The column stays ``text`` (DB §5.1)
    because a practitioner-entered source on a manual lead is legitimate and a
    PostgreSQL enum would need a migration per channel. The closed set governs
    what the *public form* may record — which is the half an attacker controls —
    while :func:`normalise_source` keeps a hand-typed source from becoming a
    second spelling of an existing one.

    ⚠️ ``OTHER`` is a real answer, not a fallback for parse failure. US-M2-04 is
    "which channel produces enquiries"; a bucket that silently absorbs
    unrecognised values would make that question unanswerable exactly when a new
    channel started working.
    """

    INSTAGRAM = "instagram"
    WHATSAPP = "whatsapp"
    REFERRAL = "referral"
    GOOGLE = "google"
    FACEBOOK = "facebook"
    WALK_IN = "walk_in"
    OTHER = "other"


#: 🔒 The longest a source string may be. Bounds both the link parameter and the
#: practitioner's own typing — ``source`` is written by an unauthenticated
#: endpoint, so an unbounded value is a free text column an attacker fills.
MAX_SOURCE_LENGTH = 40

#: 🔒 The longest ``source_detail`` may be — the campaign or referrer name behind
#: the source. Longer than the source itself because "referred by Dr. Meera at
#: Sunrise Clinic" is the real shape of the answer, and short enough that it
#: cannot become a note.
MAX_SOURCE_DETAIL_LENGTH = 120

#: Characters a source token may contain once normalised. Anything else is
#: collapsed to an underscore, so ``Instagram Ads`` and ``instagram-ads`` do not
#: become two channels in the same report.
_SOURCE_STRIP = re.compile(r"[^a-z0-9]+")


def normalise_source(source: str | None) -> str | None:
    """Fold a submitted or typed source into a stable token — FR-M2-009.

    ⚠️ Returns ``None`` for an absent or empty source rather than defaulting to
    ``OTHER``. "We do not know where this came from" and "the prospect chose
    other" are different facts, and collapsing them would inflate one channel
    with every enquiry whose link carried no parameter.

    🔒 Truncates rather than raising. This runs on the public path, and refusing a
    submission because a campaign parameter was long would lose a real enquiry
    over a marketing URL nobody controls.
    """
    if source is None:
        return None

    folded = _SOURCE_STRIP.sub("_", source.strip().lower()).strip("_")
    return folded[:MAX_SOURCE_LENGTH] or None


def normalise_source_detail(detail: str | None) -> str | None:
    """Trim the free-text detail behind a source — FR-M2-009.

    ⚠️ Not tokenised, unlike :func:`normalise_source`. The detail is a campaign
    name or a referrer's name shown back to the practitioner, so folding its case
    would make it read wrong; only its length is bounded.
    """
    if detail is None:
        return None
    trimmed = detail.strip()
    return trimmed[:MAX_SOURCE_DETAIL_LENGTH] or None


def is_known_source(source: str | None) -> bool:
    """Whether a normalised source is one of the vocabulary's own members.

    Used by the public endpoint to decide whether to keep the value or record it
    as :attr:`LeadSource.OTHER` with the raw text in ``source_detail`` — which
    keeps the reporting axis clean without discarding what the link said.
    """
    if source is None:
        return False
    return source in _SOURCE_VALUES


_SOURCE_VALUES: frozenset[str] = frozenset(member.value for member in LeadSource)


# ─── Submitted values (API §11.2, FR-M2-003) ─────────────────────────────

#: 🔒 API §11.2 — ``primary_goal`` is required (FR-M2-003) and is free text from
#: an unauthenticated caller. Bounded so the column cannot become an essay, and
#: long enough for the real answer: "lose 8kg before my sister's wedding in
#: November".
MAX_PRIMARY_GOAL_LENGTH = 500

#: The form's own presentation text, editable by the practitioner (FR-M2-001).
MAX_FORM_TITLE_LENGTH = 120
MAX_FORM_INTRO_LENGTH = 1000


def validate_primary_goal(goal: str) -> str:
    """Trim and bound the prospect's stated goal — FR-M2-003.

    🔒 Required, and required to be non-empty after trimming. FR-M2-003 names
    exactly three minimum fields (name, mobile, primary goal); a whitespace-only
    goal satisfies the form and not the requirement, and the practitioner's first
    question would be the one the form was supposed to have asked.

    Raises:
        ValidationError: On an empty or over-long goal.
    """
    trimmed = goal.strip()
    if not trimmed:
        raise ValidationError(
            "Tell your practitioner what you would like help with.",
            action="Describe your main goal in a sentence.",
            details={"field": "primary_goal"},
        )
    if len(trimmed) > MAX_PRIMARY_GOAL_LENGTH:
        raise ValidationError(
            "That is longer than this form accepts.",
            action=f"Use {MAX_PRIMARY_GOAL_LENGTH} characters or fewer.",
            details={"max_length": str(MAX_PRIMARY_GOAL_LENGTH)},
        )
    return trimmed


# ─── Consent (FR-M2-004, EC-M2-04) ───────────────────────────────────────


def assert_consent_granted(*, granted: bool) -> None:
    """🔒 Refuse a submission that did not carry consent — EC-M2-04, FR-M2-004.

    ⚠️ **403, and no record is created.** API §11.2 is explicit. The temptation is
    to store the submission and mark it unconsented "for the practitioner to
    follow up" — which would be processing personal data on no lawful basis at
    all, and is precisely what DPDP makes unavailable.

    🔒 Checked before the client is matched or created, so a declined consent
    leaves nothing behind: no client row, no submission, no consent entry. The
    prospect's name and number are discarded with the request.

    Raises:
        ConsentError: 403 — the caller declined.
    """
    if not granted:
        raise ConsentError(
            "Your practitioner needs your permission to hold your details.",
            action=(
                "Tick the consent box to send your enquiry, or contact the "
                "practice directly instead."
            ),
            details={"requirement": "FR-M2-004"},
        )


def assert_notice_current(*, submitted_notice_id: uuid.UUID, notice_in_force: uuid.UUID) -> None:
    """🔒 Refuse consent given against a superseded notice — API §11.2, NFR-051.

    The form carries the notice id it displayed. If the notice was superseded
    between the page load and the submit, the consent on record would name text
    the prospect never saw — and NFR-051 ("produce the consent basis for any
    client") would produce the wrong basis.

    ⚠️ Rare, and deliberately not silently tolerated. The window is the seconds
    between render and submit; accepting the mismatch would mean the one case
    where the ledger is wrong is the one nobody is watching.

    Raises:
        ValidationError: On a stale notice id. The UI reloads the form.
    """
    if submitted_notice_id != notice_in_force:
        raise ValidationError(
            "This form has been updated since you opened it.",
            action="Reload the page and send your enquiry again.",
            details={"reason": "notice_superseded"},
        )


# ─── Spam protection (FR-M2-008, EC-M2-03) ───────────────────────────────

#: 🔒 The score at or above which a submission is refused — FR-M2-008.
#:
#: ⚠️ EC-M2-03 requires spam to be "blocked before record creation; not counted
#: in metrics". The threshold is therefore a *rejection* boundary, not a flag on
#: a stored row: a submission at or above it never becomes a client, so it cannot
#: pollute the conversion numbers US-M2-04 rests on.
SPAM_REJECT_THRESHOLD = 1.0

#: 🔒 The minimum time a genuine person takes to complete the form. AC-M2-001
#: budgets 60 seconds for a real submission on a mid-range Android over 4G; a
#: fraction of that is a script, not a fast typist.
#:
#: ⚠️ Deliberately generous. A threshold tuned to "no human could be this fast"
#: rejects a returning prospect with autofill, and a lost enquiry costs
#: ₹2,500–4,000/month of recurring revenue (M2.2) while a spam row costs a click.
MIN_COMPLETION_SECONDS = 3


@dataclass(frozen=True, slots=True)
class SpamSignals:
    """What the server can observe about how a submission was made.

    🔒 **Signals, not a verdict.** Each field is a fact the request itself
    carries; :func:`score_submission` is the only place they become a decision,
    so the policy is one function to change rather than a condition spread across
    the endpoint.

    ⚠️ Nothing here is a CAPTCHA verdict, and that is the point — see
    :func:`score_submission`. These are the checks that work without a third
    party, which matters because the third party is not wired at MVP.
    """

    #: 🔒 The honeypot field's value. Present in the rendered form, hidden from
    #: humans by CSS, and irresistible to a naive bot that fills every input.
    #: A non-empty value is the single strongest signal available here.
    honeypot_filled: bool = False
    #: How long the form was open, from the token the form issues. ``None`` when
    #: the form did not carry one — an older cached page, or a direct POST.
    elapsed_seconds: float | None = None
    #: Whether the caller presented a verified CAPTCHA token (FR-M2-008).
    #: ⏳ Always ``False`` at MVP — no provider is wired. See
    #: :func:`score_submission` for why that does not make this decorative.
    captcha_verified: bool = False
    #: Whether the prospect's own text is empty of anything but links — the
    #: cheapest content signal, and the one spam submissions fail most often.
    goal_is_link_only: bool = False


def score_submission(signals: SpamSignals) -> float:
    """Score a submission's likelihood of being automated — FR-M2-008.

    🔒 **Additive, and capped at 1.0.** A single decisive signal (the honeypot)
    reaches the threshold alone; weaker ones must combine. That shape is what
    keeps a slow-loading page on a bad connection from being read as a bot.

    ⏳ **CAPTCHA is a signal, not the gate.** API §11.2 lists ``captcha_token`` as
    required and FR-M2-008 requires protection against automated submission. No
    provider is wired at MVP (that is S5's transport work), so verifying one
    would be a call to nothing. Rather than ship an endpoint that *claims* to
    check a token and does not, the score treats a verified token as strong
    negative evidence and stands on the honeypot and timing checks meanwhile —
    both of which are real, run server-side, and cost no third party.

    ⚠️ 🔒 **The scoring never sees the prospect's identity.** No name, mobile or
    email is a parameter. A spam score that keyed on "this number looks fake"
    would encode a numbering-plan assumption into a rejection the prospect cannot
    argue with — and EC-M2-01 already rejects a malformed mobile by validation,
    with a message that names the field.
    """
    if signals.captcha_verified:
        # A verified human. The remaining signals are noise against it — a slow
        # page load or a link-only goal from a real person is not spam.
        return 0.0

    score = 0.0

    if signals.honeypot_filled:
        # 🔒 Decisive on its own. A field no human can see was filled in.
        score += 1.0

    if signals.elapsed_seconds is not None and signals.elapsed_seconds < MIN_COMPLETION_SECONDS:
        score += 0.6

    if signals.goal_is_link_only:
        score += 0.6

    return min(score, 1.0)


def is_spam(score: float) -> bool:
    """Whether a scored submission must be refused — EC-M2-03."""
    return score >= SPAM_REJECT_THRESHOLD


#: A goal consisting only of URLs — the classic link-spam payload.
_LINK_ONLY = re.compile(r"^(?:\s*(?:https?://|www\.)\S+\s*)+$", re.IGNORECASE)


def looks_like_link_spam(goal: str) -> bool:
    """Whether the stated goal is nothing but links.

    ⚠️ Only when it is *entirely* links. A real prospect may legitimately paste
    the article that prompted them to enquire, and refusing that would be a lost
    lead — so a goal containing prose alongside a link scores nothing.
    """
    return bool(_LINK_ONLY.fullmatch(goal.strip()))


# ─── Needs-response (FR-M2-011, AC-M2-005) ───────────────────────────────

#: 🔒 How long a lead may sit before it is "ageing" — US-M2-03's real anxiety.
#:
#: ⚠️ 🟡 PROPOSED. FR-M2-011 requires the view "ordered by age" and names no
#: threshold. M2.2 gives the reasoning that fixes it: Priya "loses some to slow
#: or forgotten follow-up", and interest decays over hours rather than days on
#: the channels §3.1 names. 24 hours is one working day — long enough that an
#: enquiry arriving overnight is not flagged before the practice opens, short
#: enough that the flag still means something.
AGEING_LEAD_HOURS = 24


def is_ageing(
    submitted_at: datetime,
    *,
    now: datetime,
    responded_at: datetime | None = None,
) -> bool:
    """Whether a lead has waited long enough to need chasing — FR-M2-011.

    🔒 **An answered enquiry is never ageing**, however old it is. That rule lives
    here rather than in the caller so there is one definition of it: the list
    projection and any future digest or nudge would otherwise each re-derive it,
    and the one that forgot would fill a working queue with completed work.

    ⚠️ A presentation rule, not a lifecycle one. Nothing changes stage because of
    it; the needs-response view sorts by age regardless (AC-M2-005) and this only
    decides what is emphasised. FR-M2-014's automated sequences are Phase 2, and
    an automatic transition invented here would pre-empt that decision.
    """
    if responded_at is not None:
        return False
    return now - submitted_at >= timedelta(hours=AGEING_LEAD_HOURS)


def age_in_hours(submitted_at: datetime, *, now: datetime) -> float:
    """How long a lead has been waiting — AC-M2-005's "with their age".

    🔒 Server-computed and sent to the client, per Principle 3: the client
    renders, never derives. A browser computing this from timestamps would
    disagree with the server across a timezone or a clock skew, and the number
    that decides which prospect is called next would differ per device.

    ⚠️ Clamped at zero. Clock skew between the application and the database is
    real, and a negative age would sort a just-arrived enquiry to the *front* of
    an oldest-first queue — ahead of one that has genuinely waited two days.
    """
    return max(0.0, (now - submitted_at).total_seconds() / 3600.0)


# ─── The acknowledgement (FR-M2-007, EC-M2-02) ───────────────────────────


def acknowledgement() -> str:
    """The message a prospect sees after submitting — FR-M2-007, AC-M2-003.

    🔒 **Takes no arguments, and that is the security property.** API §11.2: "the
    response is identical whether the mobile matches an existing client or not".
    A function with no parameter cannot vary on the match, so EC-M2-02's silent
    duplicate handling is enforced by the signature rather than by a reviewer
    noticing a branch.

    ⚠️ Phrased so it is true in both cases. "Thanks — we have your enquiry" is
    accurate for a new prospect and for one already on file; "we've added you"
    would be a lie in the second case and a hint in the first.
    """
    return "Thanks — your enquiry is with your practitioner. " "They will get back to you shortly."


# ─── Events (DDR-06 — what the timeline is materialised from) ────────────
#
# 🔒 Declared in the kernel rather than in `modules.leads`, for the reason
# `ClientStageChanged` is: the subscriber that writes the timeline row lives in
# `modules.clients`, and R3 forbids either module importing the other. The event
# class is the shared vocabulary, so it belongs to the layer both may depend on.
#
# ⚠️ Identifiers, enums and timestamps only (NFR-033). The submitted name, mobile
# and goal are deliberately absent: `kernel.events` refuses prose in a payload
# that becomes a job argument and a log line, and the timeline row says only that
# an enquiry arrived.


@register_event("enquiry.received")
@dataclass(frozen=True, slots=True)
class EnquiryReceived(DomainEvent):
    """A prospect submitted the public enquiry form — FR-M2-005, AC-M1-004.

    🔒 Published for **every accepted submission**, including one matched to an
    existing client (EC-M2-02). That is what puts a repeat enquiry on the
    existing client's timeline rather than silently appending to a record the
    practitioner never sees change — J1 and AC-M1-004 both require the enquiry to
    land on the timeline.

    ⚠️ ``is_duplicate`` is carried for the *practitioner's* benefit — it is what
    lets the notification say "an existing client enquired again". It is never
    returned to the submitter; the public response is
    :func:`acknowledgement`, which cannot express it.
    """

    client_id: uuid.UUID
    tenant_id: uuid.UUID
    submission_id: uuid.UUID
    form_id: uuid.UUID
    #: True when the submission matched a client already on file (EC-M2-02).
    is_duplicate: bool
    #: 🔒 Normalised token or ``None`` — never the raw link parameter.
    source: str | None
    received_at: datetime
