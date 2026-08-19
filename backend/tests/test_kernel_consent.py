"""Consent rules must hold before a decision reaches the ledger — D3.

🔒 DB §16, FR-M0-021..030. What this protects:

* 🔒 **State is derived, never asserted** (DB §16.3). The fold is the only
  definition of "is consent in force", so grant → withdraw → re-grant must
  resolve to the last decision the person actually made, and ordering must
  follow ``occurred_at`` rather than insertion order.
* 🔒 **Deny by default.** A purpose with no ledger entry is not consented.
  Absence of a decision is not permission.
* 🔒 **Withdrawal is as easy as granting** (FR-M0-024) — refused only for an
  essential purpose during an active relationship, and permitted once it ends.
* 🔒 **A minor's consent needs a guardian** (FR-M0-028), and an adult's must not
  carry one.
* 🔒 **The mobile hash is keyed** (NFR-033) — an unkeyed digest of the Indian
  numbering space is exhaustible, which would make the column personal data.

⚠️ These are rule tests. That the database physically refuses an UPDATE on
``consent_records`` is a grant, not a rule, and is asserted against live
PostgreSQL in ``tests/integration/test_consent_ledger.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.kernel.consent import (
    MINOR_AGE_THRESHOLD,
    ConsentDecision,
    ConsentSubject,
    LedgerEntry,
    PurposeRule,
    derive_state,
    hash_mobile,
    is_granted,
    plan_withdrawal,
    purposes_needing_reconsent,
    require_consent,
    validate_guardian,
    withdrawal_halts_processing,
)
from app.kernel.context import ActorType
from app.kernel.errors import ConsentError, ValidationError
from app.kernel.models import ConsentAction, ConsentChannel, ConsentSubjectType

TENANT = uuid.uuid4()
PURPOSE = uuid.uuid4()
OTHER_PURPOSE = uuid.uuid4()
NOTICE_V1 = uuid.uuid4()
NOTICE_V2 = uuid.uuid4()

BASE_TIME = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)


def _entry(
    action: ConsentAction,
    *,
    at: datetime,
    purpose_id: uuid.UUID = PURPOSE,
    notice_id: uuid.UUID = NOTICE_V1,
) -> LedgerEntry:
    return LedgerEntry(purpose_id=purpose_id, action=action, occurred_at=at, notice_id=notice_id)


# ─── Derived state (DB §16.3) ────────────────────────────────────────────


def test_no_entries_means_not_consented() -> None:
    """🔒 Deny by default — absence of a decision is not permission."""
    assert derive_state([]) == {}
    assert is_granted({}, PURPOSE) is False


def test_grant_then_withdraw_resolves_to_withdrawn() -> None:
    state = derive_state(
        [
            _entry(ConsentAction.GRANTED, at=BASE_TIME),
            _entry(ConsentAction.WITHDRAWN, at=BASE_TIME + timedelta(days=1)),
        ]
    )
    assert is_granted(state, PURPOSE) is False
    assert state[PURPOSE].last_action is ConsentAction.WITHDRAWN


def test_grant_withdraw_regrant_resolves_to_granted() -> None:
    """🔒 The full cycle — a withdrawal is not permanent."""
    state = derive_state(
        [
            _entry(ConsentAction.GRANTED, at=BASE_TIME),
            _entry(ConsentAction.WITHDRAWN, at=BASE_TIME + timedelta(days=1)),
            _entry(ConsentAction.GRANTED, at=BASE_TIME + timedelta(days=2)),
        ]
    )
    assert is_granted(state, PURPOSE) is True


def test_reconfirmed_counts_as_granted() -> None:
    """`reconfirmed` is a distinct action but the same position."""
    state = derive_state([_entry(ConsentAction.RECONFIRMED, at=BASE_TIME)])
    assert is_granted(state, PURPOSE) is True
    assert state[PURPOSE].last_action is ConsentAction.RECONFIRMED


def test_state_follows_occurred_at_not_insertion_order() -> None:
    """⚠️ A backfilled decision carries the time it actually happened.

    Fed newest-first; the fold must still resolve to the later withdrawal.
    """
    state = derive_state(
        [
            _entry(ConsentAction.WITHDRAWN, at=BASE_TIME + timedelta(days=5)),
            _entry(ConsentAction.GRANTED, at=BASE_TIME),
        ]
    )
    assert is_granted(state, PURPOSE) is False


def test_simultaneous_entries_resolve_to_the_restrictive_outcome() -> None:
    """⚠️ An ambiguous ledger must not silently authorise processing."""
    state = derive_state(
        [
            _entry(ConsentAction.WITHDRAWN, at=BASE_TIME),
            _entry(ConsentAction.GRANTED, at=BASE_TIME),
        ]
    )
    assert is_granted(state, PURPOSE) is False


def test_purposes_are_tracked_independently() -> None:
    """🔒 FR-M0-022 — per purpose, never blanket."""
    state = derive_state(
        [
            _entry(ConsentAction.GRANTED, at=BASE_TIME),
            _entry(ConsentAction.WITHDRAWN, at=BASE_TIME, purpose_id=OTHER_PURPOSE),
        ]
    )
    assert is_granted(state, PURPOSE) is True
    assert is_granted(state, OTHER_PURPOSE) is False


def test_require_consent_raises_with_a_next_action() -> None:
    with pytest.raises(ConsentError) as excinfo:
        require_consent({}, PURPOSE, purpose_name="WhatsApp messages")

    # NFR-063 — every error says what to do next.
    assert excinfo.value.action
    assert "WhatsApp messages" in excinfo.value.message


def test_require_consent_passes_when_granted() -> None:
    state = derive_state([_entry(ConsentAction.GRANTED, at=BASE_TIME)])
    require_consent(state, PURPOSE, purpose_name="WhatsApp messages")


def test_withdrawal_halts_processing_only_once_withdrawn() -> None:
    granted = derive_state([_entry(ConsentAction.GRANTED, at=BASE_TIME)])
    assert withdrawal_halts_processing(granted, PURPOSE) is False

    withdrawn = derive_state(
        [
            _entry(ConsentAction.GRANTED, at=BASE_TIME),
            _entry(ConsentAction.WITHDRAWN, at=BASE_TIME + timedelta(days=1)),
        ]
    )
    assert withdrawal_halts_processing(withdrawn, PURPOSE) is True


# ─── Withdrawal (FR-M0-024) ──────────────────────────────────────────────


def test_non_essential_purpose_is_always_withdrawable() -> None:
    rule = PurposeRule(purpose_id=PURPOSE, code="marketing", is_essential=False)
    plan_withdrawal(rule, relationship_is_active=True)
    plan_withdrawal(rule, relationship_is_active=False)


def test_essential_purpose_cannot_be_withdrawn_while_active() -> None:
    rule = PurposeRule(purpose_id=PURPOSE, code="service_delivery", is_essential=True)
    with pytest.raises(ConsentError) as excinfo:
        plan_withdrawal(rule, relationship_is_active=True)

    # 🔒 The refusal must route the person somewhere, not dead-end them.
    assert excinfo.value.action


def test_essential_purpose_becomes_withdrawable_once_the_relationship_ends() -> None:
    """🔒 A consent that can never be withdrawn is not consent under DPDP."""
    rule = PurposeRule(purpose_id=PURPOSE, code="service_delivery", is_essential=True)
    plan_withdrawal(rule, relationship_is_active=False)


# ─── Minors (FR-M0-028) ──────────────────────────────────────────────────


def _decision(**overrides: object) -> ConsentDecision:
    defaults: dict[str, object] = {
        "subject": ConsentSubject(
            tenant_id=TENANT,
            subject_type=ConsentSubjectType.CLIENT,
            subject_id=uuid.uuid4(),
        ),
        "purpose_id": PURPOSE,
        "notice_id": NOTICE_V1,
        "action": ConsentAction.GRANTED,
        "captured_via": ConsentChannel.PRACTITIONER,
        "captured_by_actor_type": ActorType.PRACTITIONER,
    }
    defaults.update(overrides)
    return ConsentDecision(**defaults)  # type: ignore[arg-type]


def test_minor_without_guardian_is_refused() -> None:
    with pytest.raises(ConsentError):
        validate_guardian(_decision(), subject_age=MINOR_AGE_THRESHOLD - 1)


def test_minor_with_guardian_is_accepted() -> None:
    decision = _decision(guardian_name="A. Sharma", guardian_relationship="mother")
    validate_guardian(decision, subject_age=MINOR_AGE_THRESHOLD - 1)


def test_adult_with_guardian_details_is_refused() -> None:
    """🔒 A record asserting a guardianship nobody claimed is worse than none."""
    decision = _decision(guardian_name="A. Sharma", guardian_relationship="mother")
    with pytest.raises(ValidationError):
        validate_guardian(decision, subject_age=MINOR_AGE_THRESHOLD)


def test_unknown_age_permits_absent_guardian() -> None:
    """The enquiry form does not ask for a date of birth (FR-M2-004)."""
    validate_guardian(_decision(), subject_age=None)


def test_unknown_age_still_requires_coherent_guardian_details() -> None:
    decision = _decision(guardian_name="A. Sharma")
    with pytest.raises(ValidationError):
        validate_guardian(decision, subject_age=None)


# ─── Re-consent (FR-M0-029) ──────────────────────────────────────────────


def test_immaterial_notice_change_does_not_force_reconsent() -> None:
    """🔒 A typo fix must not push every client through a consent flow."""
    state = derive_state([_entry(ConsentAction.GRANTED, at=BASE_TIME)])
    assert (
        purposes_needing_reconsent(state, notice_in_force=NOTICE_V2, requires_reconsent=False) == ()
    )


def test_material_change_requires_reconsent_for_granted_purposes() -> None:
    state = derive_state([_entry(ConsentAction.GRANTED, at=BASE_TIME)])
    assert purposes_needing_reconsent(
        state, notice_in_force=NOTICE_V2, requires_reconsent=True
    ) == (PURPOSE,)


def test_reconsent_is_not_asked_of_someone_who_withdrew() -> None:
    """🔒 Re-asking a person who declined is badgering, not consent."""
    state = derive_state(
        [
            _entry(ConsentAction.GRANTED, at=BASE_TIME),
            _entry(ConsentAction.WITHDRAWN, at=BASE_TIME + timedelta(days=1)),
        ]
    )
    assert (
        purposes_needing_reconsent(state, notice_in_force=NOTICE_V2, requires_reconsent=True) == ()
    )


def test_consent_against_the_current_notice_needs_no_reconsent() -> None:
    state = derive_state([_entry(ConsentAction.GRANTED, at=BASE_TIME, notice_id=NOTICE_V2)])
    assert (
        purposes_needing_reconsent(state, notice_in_force=NOTICE_V2, requires_reconsent=True) == ()
    )


# ─── Subject identification ──────────────────────────────────────────────


def test_subject_requires_an_identifier() -> None:
    """🔒 A ledger entry nobody can be matched to is not evidence."""
    with pytest.raises(ValidationError):
        ConsentSubject(tenant_id=TENANT, subject_type=ConsentSubjectType.CLIENT)


def test_prospect_cannot_carry_a_client_id() -> None:
    with pytest.raises(ValidationError):
        ConsentSubject(
            tenant_id=TENANT,
            subject_type=ConsentSubjectType.PROSPECT,
            subject_id=uuid.uuid4(),
        )


def test_prospect_is_identified_by_mobile_hash() -> None:
    """🔒 FR-M2-004 — consent captured before a client record exists."""
    subject = ConsentSubject(
        tenant_id=TENANT,
        subject_type=ConsentSubjectType.PROSPECT,
        subject_mobile_hash=hash_mobile("+91 98765 43210", secret="test-secret"),
    )
    assert subject.subject_id is None


# ─── Mobile hashing (NFR-033) ────────────────────────────────────────────


def test_hash_is_stable_across_formatting() -> None:
    """A returning prospect must be findable despite formatting drift."""
    secret = "test-secret"
    assert hash_mobile("+91 98765 43210", secret=secret) == hash_mobile(
        "+919876543210", secret=secret
    )
    assert hash_mobile("+91-98765-43210", secret=secret) == hash_mobile(
        "+919876543210", secret=secret
    )


def test_hash_is_keyed_not_a_bare_digest() -> None:
    """🔒 An unkeyed digest of a 10-digit space is exhaustible (NFR-033)."""
    assert hash_mobile("+919876543210", secret="secret-a") != hash_mobile(
        "+919876543210", secret="secret-b"
    )


def test_hash_refuses_an_empty_secret() -> None:
    """Failing loudly beats silently producing a reversible digest."""
    with pytest.raises(ValueError, match="secret"):
        hash_mobile("+919876543210", secret="")


def test_hash_refuses_an_empty_mobile() -> None:
    with pytest.raises(ValidationError):
        hash_mobile("", secret="test-secret")
