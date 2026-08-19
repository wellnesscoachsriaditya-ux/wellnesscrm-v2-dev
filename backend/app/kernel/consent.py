"""Consent — what we may process, for which purpose, on whose authority.

🔒 DB §16, FR-M0-021..030, NFR-044..053. **The DPDP Act 2023 governs at launch,
not HIPAA.** The practical consequence runs through this whole module: DPDP makes
*consent itself* the artefact to be evidenced — per purpose, against the exact
notice text presented, at a point in time.

Four properties, each answering a specific failure:

1. 🔒 **Per purpose, never blanket** (FR-M0-022). A decision is always about one
   :class:`~app.kernel.models.ConsentPurpose`. There is no API here that grants
   "everything", because a single "I agree" checkbox is what the ledger exists to
   make inexpressible.
2. 🔒 **Append-only** (NFR-047). Every decision is a new entry. Nothing in this
   module updates or deletes one, and the database will not permit it either —
   ``app_user`` holds INSERT and SELECT on ``consent_records`` and nothing more
   (DDR-15). Code is what the ledger is evidence *against*, so code is not what
   protects it.
3. 🔒 **Current state is derived, never stored** (DB §16.3). :func:`derive_state`
   folds a subject's history into a verdict. No ``is_consented`` column exists,
   because a mutable flag and an append-only ledger eventually disagree — and at
   that point the flag is what code reads while the ledger is what a regulator
   reads.
4. 🔒 **Withdrawal is as easy as granting** (FR-M0-024). :func:`plan_withdrawal`
   refuses only for essential purposes, and only while the relationship is
   active. Every other refusal would be a dark pattern with a compliance finding
   attached.

⚠️ **Withdrawal halts processing; it does not erase** (FR-M0-025). A practitioner
may lawfully retain clinical records they have already created. Erasure is a
separate, explicit request — ``data_requests`` — with its own workflow and its own
launch gate around object storage (Arch §13.2).

⚠️ 🔒 **OD-05 is an unresolved launch blocker.** :func:`validate_guardian` checks
that guardian details are *present and internally coherent* for a minor. It
cannot check that the guardian is real, because verifiable parental consent needs
a mechanism this project has not yet been given. Nutritionists routinely see
teenage clients, so this is not a deferrable edge case — the function is
deliberately named as validation, not verification, so no caller mistakes one for
the other.

This module holds rules only. Persistence lives in ``platform.consent`` — the
split is what lets every rule below be tested without a database.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from app.kernel.context import ActorType
from app.kernel.errors import ConsentError, ValidationError
from app.kernel.models import (
    ConsentAction,
    ConsentChannel,
    ConsentSubjectType,
)

#: 🔒 FR-M0-028. The DPDP Act treats under-18s as children requiring verifiable
#: parental consent. Named rather than inlined so the threshold is one edit away
#: if a jurisdiction we expand into differs (GCC, UK, US — all differ).
MINOR_AGE_THRESHOLD: int = 18


# ─── Subject identification ──────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ConsentSubject:
    """Who a consent decision is about.

    ⚠️ Two identifications, and exactly one is available at a time. The enquiry
    form captures consent *before* a client record exists (FR-M2-004), so the
    subject is a mobile hash; once a client exists it is a ``client_id``. The
    check constraint ``ck_consent_records__subject_identified`` enforces the same
    rule in the database, because a ledger entry nobody can be matched to is not
    evidence of anything.

    🔒 The mobile is hashed, never stored plain — otherwise the ledger becomes a
    second, unprotected copy of contact data (DB §16.4).
    """

    tenant_id: UUID
    subject_type: ConsentSubjectType
    subject_id: UUID | None = None
    subject_mobile_hash: str | None = None

    def __post_init__(self) -> None:
        if self.subject_id is None and self.subject_mobile_hash is None:
            raise ValidationError(
                "A consent subject needs either a client record or a contact number.",
                action="Provide the client id or the mobile number used at enquiry.",
            )
        # A prospect by definition has no client record yet. Allowing both would
        # make "which identifier is authoritative" a question with two answers.
        if self.subject_type is ConsentSubjectType.PROSPECT and self.subject_id is not None:
            raise ValidationError(
                "A prospect cannot already have a client record.",
                action="Record this consent against the client instead of the prospect.",
            )


def hash_mobile(mobile: str, *, secret: str) -> str:
    """Hash a mobile number for use as a pre-client consent subject.

    🔒 Keyed with a deployment secret, not a bare digest. A mobile number has
    roughly ten digits of entropy in practice, so an unkeyed SHA-256 of the
    Indian numbering space is exhaustible on a laptop — the hash would be
    reversible and the column would be personal data in all but name (NFR-033).

    ⚠️ The same secret must be used for lookup as for capture, or a returning
    prospect's prior consent becomes unfindable and they will be asked again.
    Rotating it is therefore a migration, not a config change.
    """
    if not mobile:
        raise ValidationError(
            "A mobile number is required to record consent before a client record exists.",
            action="Enter the mobile number from the enquiry.",
        )
    if not secret:
        # Failing loudly here rather than hashing with an empty key, which would
        # silently produce a reversible digest.
        raise ValueError("hash_mobile requires a non-empty secret (NFR-033)")

    normalised = mobile.strip().replace(" ", "").replace("-", "")
    return hmac.new(secret.encode(), normalised.encode(), hashlib.sha256).hexdigest()


# ─── The ledger entry ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ConsentDecision:
    """One entry to append to the ledger.

    🔒 Immutable by construction, matching what it becomes once written. There is
    no method here that revises a decision — superseding one means appending
    another, which is the whole point of an append-only record.
    """

    subject: ConsentSubject
    purpose_id: UUID
    #: 🔒 The exact notice version presented. NFR-051 — "produce the consent
    #: basis for any client" — is answerable only because of this reference.
    notice_id: UUID
    action: ConsentAction
    captured_via: ConsentChannel
    captured_by_actor_type: ActorType
    captured_by_actor_id: UUID | None = None
    guardian_name: str | None = None
    guardian_relationship: str | None = None
    guardian_verification_method: str | None = None
    #: 🔒 Non-clinical only: notice hash, UI version, timestamp. Never a
    #: measurement or a diagnosis — this table has different access rules and a
    #: much longer retention period than clinical data.
    evidence: dict[str, str] | None = None
    occurred_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """A persisted ledger row, as the derivation reads it.

    A snapshot rather than the ORM model, matching ``SessionSnapshot``: the fold
    below is then testable with plain objects, and cannot accidentally trigger a
    lazy load while deciding whether processing is lawful.

    Only the four fields the derivation actually uses. Guardian details and
    evidence are part of the *record* but not of the *state* — including them
    here would invite a caller to make a consent decision on the basis of who
    signed rather than what was decided.
    """

    purpose_id: UUID
    action: ConsentAction
    occurred_at: datetime
    notice_id: UUID


# ─── Derived state (DB §16.3) ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PurposeState:
    """The current consent position for one purpose, derived from history."""

    purpose_id: UUID
    is_granted: bool
    #: The notice version the standing decision was made against. 🔒 Compared
    #: against the notice now in force to answer FR-M0-029.
    notice_id: UUID
    decided_at: datetime
    last_action: ConsentAction

    @property
    def requires_reconsent_against(self) -> UUID:
        """The notice this state is anchored to, for a materiality comparison."""
        return self.notice_id


def derive_state(entries: Iterable[LedgerEntry]) -> dict[UUID, PurposeState]:
    """Fold a subject's ledger history into the current position per purpose.

    🔒 DB §16.3 — this function is the only definition of "is consent in force".
    The latest entry per purpose wins; earlier ones are history, not state.

    ⚠️ Ordered by ``occurred_at`` and **not** by ledger id. The two agree for
    entries written in sequence, but a backfilled or imported decision carries
    the timestamp it actually happened at, and *when the person decided* is the
    legally relevant ordering — not when the row reached us.

    ⚠️ Ties are broken toward the more restrictive outcome. Two entries sharing a
    timestamp is a data problem, and resolving it toward "granted" would mean an
    ambiguous ledger silently authorises processing.
    """
    latest: dict[UUID, PurposeState] = {}

    for entry in sorted(entries, key=lambda e: e.occurred_at):
        granted = entry.action in (ConsentAction.GRANTED, ConsentAction.RECONFIRMED)
        existing = latest.get(entry.purpose_id)

        if (
            existing is not None
            and existing.decided_at == entry.occurred_at
            and existing.is_granted is False
        ):
            # Same instant, and we already hold a withdrawal. Keep it.
            continue

        latest[entry.purpose_id] = PurposeState(
            purpose_id=entry.purpose_id,
            is_granted=granted,
            notice_id=entry.notice_id,
            decided_at=entry.occurred_at,
            last_action=entry.action,
        )

    return latest


def is_granted(state: dict[UUID, PurposeState], purpose_id: UUID) -> bool:
    """Whether processing for one purpose is currently permitted.

    🔒 Deny by default, matching ``kernel.authz``. A purpose with no entry has no
    consent — absence of a decision is not permission, and treating it as such is
    how a processing activity acquires a lawful basis nobody ever gave it.
    """
    entry = state.get(purpose_id)
    return entry is not None and entry.is_granted


def require_consent(
    state: dict[UUID, PurposeState],
    purpose_id: UUID,
    *,
    purpose_name: str,
) -> None:
    """Raise :class:`ConsentError` unless consent for this purpose is in force.

    The enforcement counterpart to :func:`is_granted`, for the call sites where
    the absence of consent must stop the operation rather than shape it.
    """
    if not is_granted(state, purpose_id):
        raise ConsentError(
            f"This client has not consented to {purpose_name}.",
            action=(
                "Ask the client to consent to this in their portal, or record "
                "their consent if they have given it in person."
            ),
            details={"purpose_id": str(purpose_id)},
        )


# ─── Withdrawal (FR-M0-024, FR-M0-025) ───────────────────────────────────


@dataclass(frozen=True, slots=True)
class PurposeRule:
    """The catalogue facts needed to decide a withdrawal.

    Passed in rather than read from the database, keeping this module free of
    persistence (R1) and the rule itself trivially testable.
    """

    purpose_id: UUID
    code: str
    is_essential: bool


def plan_withdrawal(
    rule: PurposeRule,
    *,
    relationship_is_active: bool,
) -> None:
    """Check that a purpose may be withdrawn, raising if it may not.

    🔒 FR-M0-024 — withdrawal must be as easy as granting. The only permitted
    refusal is an essential purpose during an active relationship: a client
    cannot withdraw consent for service delivery while still being served,
    because the processing *is* the service.

    ⚠️ Once the relationship ends, an essential purpose becomes withdrawable —
    the justification was the service, and it no longer applies. A permanent
    refusal would be a consent that cannot be withdrawn at all, which is not
    consent under DPDP.

    ⚠️ 🔒 The essential/non-essential split is PROPOSED and legally consequential
    (ASM-10, OD-05). This function is correct for whatever the split turns out to
    be; whether the split itself is defensible is a question for the privacy
    lawyer, and every purpose added to the essential list narrows this right.
    """
    if rule.is_essential and relationship_is_active:
        raise ConsentError(
            "This permission is required to provide the service and cannot be "
            "withdrawn while the engagement is active.",
            action=(
                "To stop all processing, ask your practitioner to close your "
                "engagement, then withdraw this permission."
            ),
            details={"purpose_code": rule.code, "is_essential": "true"},
        )


def withdrawal_halts_processing(state: dict[UUID, PurposeState], purpose_id: UUID) -> bool:
    """Whether a withdrawal for this purpose should stop ongoing processing.

    ⚠️ FR-M0-025 — halting is not erasure. This answers "stop sending WhatsApp
    messages", never "delete the plans already created". Erasure is a
    ``data_requests`` workflow with its own gate around object storage.
    """
    return not is_granted(state, purpose_id)


# ─── Minors (FR-M0-028) ──────────────────────────────────────────────────


def validate_guardian(decision: ConsentDecision, *, subject_age: int | None) -> None:
    """Validate guardian details on a minor's consent.

    🔒 FR-M0-028. Two directions, both checked:

    * A minor's consent without guardian details is refused.
    * Guardian details on an adult's consent are refused — silently accepting
      them would leave a record asserting a guardianship nobody claimed.

    ⚠️ 🔒 **This is validation, not verification (OD-05).** It confirms the
    details are present and coherent. It cannot confirm the guardian is real,
    consented, or related as stated — that needs a verifiable-parental-consent
    mechanism which is an **open launch blocker**. Do not read a pass here as
    DPDP compliance for a minor.
    """
    has_guardian = bool(decision.guardian_name and decision.guardian_relationship)

    if subject_age is None:
        # Unknown age on the enquiry path. Guardian details, if offered, must
        # still be coherent — but absence cannot be an error, since the form
        # does not ask for a date of birth (FR-M2-004).
        if decision.guardian_name and not decision.guardian_relationship:
            raise ValidationError(
                "A guardian's relationship to the client is required.",
                action="Add how the guardian is related to the client.",
            )
        return

    if subject_age < MINOR_AGE_THRESHOLD and not has_guardian:
        raise ConsentError(
            "Consent for a client under 18 must be given by a parent or guardian.",
            action="Record the guardian's name and their relationship to the client.",
            details={"minor_age_threshold": str(MINOR_AGE_THRESHOLD)},
        )

    if subject_age >= MINOR_AGE_THRESHOLD and has_guardian:
        raise ValidationError(
            "Guardian details cannot be recorded for an adult client.",
            action="Remove the guardian details, or correct the date of birth.",
        )


# ─── Re-consent (FR-M0-029) ──────────────────────────────────────────────


def purposes_needing_reconsent(
    state: dict[UUID, PurposeState],
    *,
    notice_in_force: UUID,
    requires_reconsent: bool,
) -> Sequence[UUID]:
    """Which granted purposes were decided against a superseded notice.

    🔒 FR-M0-029 — a material change to a notice invalidates the consent given
    against the previous version. The check is deliberately narrow:

    * Only *granted* purposes are returned. Re-asking someone who withdrew would
      be badgering them into a decision they have already made.
    * Only when the new notice is flagged ``requires_reconsent``. A typo fix in a
      notice must not force every client through a consent flow again, or the
      flow becomes noise people click through — which costs more consent quality
      than the correction gained.
    """
    if not requires_reconsent:
        return ()

    return tuple(
        purpose_id
        for purpose_id, purpose_state in state.items()
        if purpose_state.is_granted and purpose_state.notice_id != notice_in_force
    )


def now() -> datetime:
    """Timezone-aware current time, for stamping a decision.

    Centralised so every ledger entry is UTC. A naive datetime in an append-only
    legal record is a defect that cannot be corrected later by definition.
    """
    return datetime.now(UTC)
