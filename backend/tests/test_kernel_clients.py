"""The rules of the client spine — DB §5, FR-M1-004..016, M1.3.

🔒 No database. ``kernel.clients`` is deliberately split from the persistence in
``app.modules.clients`` so that every rule below is testable as a pure function —
and these are the rules the rest of the product reads a client *through*, so a
drift here is a drift everywhere.

Four groups, each with a different reason to exist:

* **Stage predicates** — M1.5's entitlement predicate and FR-M1-014's engagement
  predicate. They overlap but are not derivable from one another, and the tests
  pin the case where they disagree.
* **Mobile normalisation** — NFR-100. Generous about input, strict about output.
* **Minor status** — FR-M0-028. Guardian consent gates on this, so the boundary
  cases are consent failures rather than rounding errors.
* **The port** — the seam five modules read clients through (DB §5).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import get_args

import pytest

from app.kernel.clients import (
    SELECTABLE_STAGES,
    ClientDirectory,
    ClientIdentity,
    ClientStage,
    ContactDetails,
    DietaryClass,
    SelectableStage,
    age_in_years,
    assert_not_archived,
    assert_transition_allowed,
    configure_client_directory,
    consumes_entitlement_on_entry,
    counts_towards_limit,
    get_client_directory,
    is_lead,
    is_minor,
    looks_like_e164,
    normalise_mobile,
    receives_engagement,
    restore_consumes_entitlement,
    validate_full_name,
)
from app.kernel.consent import MINOR_AGE_THRESHOLD
from app.kernel.errors import ValidationError

# ─── Stage predicates (M1.5, FR-M1-003, FR-M1-014) ───────────────────────


@pytest.mark.parametrize("stage", list(ClientStage))
def test_only_active_counts_towards_the_limit(stage: ClientStage) -> None:
    """🔒 M1.5 / DB §5.2 — "only `active` counts toward the plan limit".

    Parametrised over the whole enum rather than spot-checked: the failure this
    guards against is a *new* stage being added and quietly landing on the
    metered side, which a test naming three stages by hand would not notice.
    """
    assert counts_towards_limit(stage, archived_at_is_set=False) is (
        stage is ClientStage.ACTIVE
    ), f"{stage.value} disagrees with M1.5's single-predicate rule"


def test_leads_are_never_metered_at_any_pre_active_stage() -> None:
    """🔒 FR-M1-003 / EC-M2-06 — a tenant at their limit still accepts leads.

    The limit binds at conversion, not at capture. If any lead stage metered, a
    practitioner on the ₹799 tier would stop being able to take enquiries — the
    exact failure EC-M2-06 names.
    """
    for stage in (ClientStage.LEAD, ClientStage.CONTACTED, ClientStage.CONSULTATION_SCHEDULED):
        assert not counts_towards_limit(stage, archived_at_is_set=False)


def test_archiving_releases_the_entitlement() -> None:
    """🔒 AC-M1-005 / FR-M1-010 — both halves of the predicate are required.

    An archived row keeps ``stage = 'active'`` (archiving is a soft delete, not a
    transition), so without the ``archived_at`` half a practitioner would go on
    paying for clients they archived.
    """
    assert counts_towards_limit(ClientStage.ACTIVE, archived_at_is_set=False)
    assert not counts_towards_limit(ClientStage.ACTIVE, archived_at_is_set=True)


def test_engagement_and_metering_are_not_the_same_predicate() -> None:
    """🔒 FR-M1-014 — the case that proves one cannot be derived from the other.

    A ``contacted`` lead is **not metered** but **is** legitimately contacted;
    a ``paused`` client is also not metered but must receive nothing. Deriving
    either predicate from the other would make one of these two wrong, and the
    symptom would be messages sent to a paused client.
    """
    assert not counts_towards_limit(ClientStage.CONTACTED, archived_at_is_set=False)
    assert receives_engagement(ClientStage.CONTACTED, archived_at_is_set=False)

    assert not counts_towards_limit(ClientStage.PAUSED, archived_at_is_set=False)
    assert not receives_engagement(ClientStage.PAUSED, archived_at_is_set=False)


@pytest.mark.parametrize("stage", [ClientStage.PAUSED, ClientStage.CHURNED, ClientStage.ARCHIVED])
def test_dormant_stages_receive_nothing(stage: ClientStage) -> None:
    """FR-M1-014 — no plans, no scheduled messages, no check-in nudges."""
    assert not receives_engagement(stage, archived_at_is_set=False)


def test_archived_client_receives_nothing_whatever_its_stage() -> None:
    """An archived `active` client is still archived. Soft delete means silent."""
    assert not receives_engagement(ClientStage.ACTIVE, archived_at_is_set=True)


def test_consultation_scheduled_is_still_a_lead() -> None:
    """FR-M2-011 / FR-M9-006 — nobody has been billed and no service delivered.

    Conversion metrics count the crossing out of this set, so misclassifying it
    would move the denominator rather than raise an error.
    """
    assert is_lead(ClientStage.CONSULTATION_SCHEDULED)
    assert is_lead(ClientStage.LEAD)
    assert is_lead(ClientStage.CONTACTED)
    assert not is_lead(ClientStage.ACTIVE)


# ─── Transitions (ADR-A06, FR-M1-002, FR-M1-015) ─────────────────────────


def test_selectable_stages_is_every_stage_but_archived() -> None:
    """🔒 Archiving is a soft delete with its own action, not a stage.

    ``archived_at`` is the archive flag (FR-M1-010), and DB §5.2's entitlement
    predicate reads *both* it and ``stage`` — which only makes sense if a row can
    be archived while its stage still says ``active``. Two encodings of one fact
    is one too many; migration 0010 refuses the stage at the table.
    """
    assert set(SELECTABLE_STAGES) == set(ClientStage) - {ClientStage.ARCHIVED}


def test_selectable_stage_type_matches_the_tuple() -> None:
    """🔒 The two spellings of "selectable" cannot drift apart.

    ``SelectableStage`` is a ``Literal`` because a type must be static — mypy and
    the OpenAPI generator both read it at rest — while ``SELECTABLE_STAGES`` is
    derived. That is a deliberate duplication, and this is the check that makes
    it safe: adding a stage to the enum without adding it to the ``Literal``
    would silently make it unreachable through the API.
    """
    assert set(get_args(SelectableStage)) == set(SELECTABLE_STAGES)


@pytest.mark.parametrize(
    ("from_stage", "to_stage"),
    [
        (ClientStage.LEAD, ClientStage.ACTIVE),
        (ClientStage.LEAD, ClientStage.CONTACTED),
        (ClientStage.CHURNED, ClientStage.ACTIVE),
        (ClientStage.ACTIVE, ClientStage.PAUSED),
        (ClientStage.PAUSED, ClientStage.ACTIVE),
        (ClientStage.CONSULTATION_SCHEDULED, ClientStage.CHURNED),
    ],
)
def test_any_stage_may_reach_any_other(from_stage: ClientStage, to_stage: ClientStage) -> None:
    """🔒 Deliberately permissive — the PRD constrains no ordering.

    ``lead → active`` is AC-M1-003's conversion and ``churned → active`` is
    EC-M1-02's reactivation, but the backwards and sideways moves matter just as
    much: a practitioner must be able to record what actually happened, and
    FR-M1-017 puts *rule-driven* transitions in Phase 3 — which only makes sense
    if MVP transitions are practitioner-driven.
    """
    assert_transition_allowed(from_stage, to_stage)


def test_a_transition_to_the_same_stage_is_refused() -> None:
    """A no-op cannot be recorded, so it must not be accepted.

    ``ck_client_stage_history__actual_transition`` rejects a row whose
    ``from_stage`` equals its ``to_stage``, and an unrecorded transition breaks
    FR-M1-015. Refusing here names the field instead of surfacing an integrity
    error three frames up.
    """
    with pytest.raises(ValidationError) as excinfo:
        assert_transition_allowed(ClientStage.ACTIVE, ClientStage.ACTIVE)

    assert excinfo.value.details["current_stage"] == "active"


def test_archived_is_not_a_selectable_target() -> None:
    """🔒 The refusal that keeps the archive encoding single-valued."""
    with pytest.raises(ValidationError) as excinfo:
        assert_transition_allowed(ClientStage.ACTIVE, ClientStage.ARCHIVED)

    assert "archived" not in excinfo.value.details["allowed_stages"]


def test_an_archived_client_cannot_change_stage() -> None:
    """FR-M1-010 — a soft-deleted client is not a lifecycle participant.

    Moving one would edit a record the practitioner has removed from their lists,
    and any entitlement it consumed would be spent on a client nobody can see.
    """
    assert_not_archived(archived_at_is_set=False)
    with pytest.raises(ValidationError):
        assert_not_archived(archived_at_is_set=True)


@pytest.mark.parametrize(
    ("from_stage", "to_stage", "consumes"),
    [
        # 🔒 FR-M1-002 — the only transition that takes a slot.
        (ClientStage.LEAD, ClientStage.ACTIVE, True),
        (ClientStage.CHURNED, ClientStage.ACTIVE, True),
        (ClientStage.PAUSED, ClientStage.ACTIVE, True),
        # 🔒 FR-M1-003 — no stage below `active` is metered, so moving between
        # them is free. A tenant at their limit still works their funnel.
        (ClientStage.LEAD, ClientStage.CONTACTED, False),
        (ClientStage.CONTACTED, ClientStage.CONSULTATION_SCHEDULED, False),
        # Leaving `active` releases a slot; it never takes one.
        (ClientStage.ACTIVE, ClientStage.PAUSED, False),
        (ClientStage.ACTIVE, ClientStage.CHURNED, False),
    ],
)
def test_only_entry_to_active_consumes_an_entitlement(
    from_stage: ClientStage, to_stage: ClientStage, consumes: bool
) -> None:
    """🔒 M1.5 — asks about the *transition*, not the destination.

    ⚠️ A check keyed on ``to_stage is ACTIVE`` alone would also fire on
    transitions that leave the count unchanged, refusing a practitioner an action
    that costs them nothing.
    """
    assert consumes_entitlement_on_entry(from_stage, to_stage) is consumes


@pytest.mark.parametrize(
    ("stage", "consumes"),
    [
        (ClientStage.ACTIVE, True),
        (ClientStage.PAUSED, False),
        (ClientStage.LEAD, False),
        (ClientStage.CHURNED, False),
    ],
)
def test_restore_is_metered_only_for_an_active_client(stage: ClientStage, consumes: bool) -> None:
    """🔒 EC-M1-06 — archiving frees a slot, so restoring takes one back.

    Without this a practitioner could archive their way under a limit and undo it
    for free, which makes the limit advisory.
    """
    assert restore_consumes_entitlement(stage) is consumes


# ─── Mobile normalisation (NFR-100, FR-M1-006) ───────────────────────────


@pytest.mark.parametrize(
    "typed",
    [
        "9876543210",
        "98765 43210",
        "098765-43210",
        "+91 98765 43210",
        "+919876543210",
        "0091 98765 43210",
        "919876543210",
        "(98765) 43210",
        "  9876543210  ",
        "+91-98765-43210",
        "98765.43210",
    ],
)
def test_every_way_an_indian_number_is_typed_normalises_to_one_form(typed: str) -> None:
    """🔒 NFR-100 — stored in E.164, always.

    All eleven inputs are the same human number. If any normalised differently,
    duplicate detection (FR-M1-024) and WhatsApp delivery would both key off a
    value that depends on how the practitioner happened to type it.
    """
    assert normalise_mobile(typed) == "+919876543210"


def test_trunk_prefix_before_the_country_code_is_currently_refused() -> None:
    """⚠️ 🟡 **Pins a gap, not a guarantee** — ``091XXXXXXXXXX`` is rejected.

    :func:`normalise_mobile` handles the international prefix (``0091…``) and the
    trunk prefix on a national number (``09876543210``), but not a trunk prefix
    written *before* the country code. That form is common in Indian phone books
    and contact exports, so it will arrive during import (FR-M1-013, Phase 2).

    It is refused rather than accepted today, which is the safe direction — the
    practitioner sees "that does not look like a valid mobile number" and can
    retype it, rather than a wrong number being stored silently. Recorded here so
    the behaviour is a decision rather than an oversight; widening it is a
    one-branch change and a question for the user, not a test to quietly rewrite.
    """
    with pytest.raises(ValidationError):
        normalise_mobile("0919876543210")


@pytest.mark.parametrize(
    ("bad", "why"),
    [
        ("5876543210", "no Indian mobile prefix is below 6"),
        ("1234567890", "landline-shaped, the common data-entry error"),
        ("987654321", "nine digits"),
        ("98765432101", "eleven digits"),
        ("+1 415 555 2671", "valid E.164, not Indian — GCC/UK are Phase 2/3"),
        ("", "empty"),
        ("not a number", "free text"),
        ("+91987654321a", "trailing letter"),
    ],
)
def test_invalid_mobiles_are_refused_by_name(bad: str, why: str) -> None:
    """🔒 FR-M1-006 — refused here, where the field can be named.

    ⚠️ The prefix check is a real check, not decoration: a landline typed into
    the mobile field is the common error, and it makes WhatsApp delivery fail
    silently much later, in a place that cannot explain why.
    """
    with pytest.raises(ValidationError) as caught:
        normalise_mobile(bad)
    assert caught.value.status_code == 422, why


def test_normalised_output_always_satisfies_the_database_check() -> None:
    """🔒 The two halves must agree — ``ck_clients__mobile_e164`` (migration 0009).

    The column CHECK asserts E.164 *shape*; :func:`normalise_mobile` is what
    produces it. If normalisation could emit something the CHECK rejects, the
    failure would surface as an integrity error three frames above the field.
    """
    assert looks_like_e164(normalise_mobile("98765 43210"))


# ─── Contact details (FR-M1-004, EC-M1-08) ───────────────────────────────


def test_a_client_needs_at_least_one_contact_method() -> None:
    """🔒 FR-M1-004 / EC-M1-08 — mirrors ``ck_clients__contact_present``."""
    with pytest.raises(ValidationError):
        ContactDetails(mobile=None, email=None)


def test_email_only_client_is_legitimate_but_loses_whatsapp() -> None:
    """🔒 EC-M1-08 — the limitation the UI must state rather than hide.

    "At least one" rather than "mobile" is the whole point: the record is
    accepted, and the system says what it cannot do with it.
    """
    contact = ContactDetails(email="asha@example.test")
    assert contact.can_receive_whatsapp is False

    assert ContactDetails(mobile="+919876543210").can_receive_whatsapp is True


# ─── Names (FR-M1-004) ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "Asha",  # mononym
        "R. Krishnan",  # initial
        "अंजलि शर्मा",  # Devanagari
        "ஆனந்தி",  # Tamil
        "Mary-Jane O'Neill",  # punctuation
    ],
)
def test_real_names_in_the_launch_market_are_accepted(name: str) -> None:
    """🔒 FR-M1-004 — the only universally true rule is that a name is non-empty.

    India has mononyms, initials and four scripts in common use. Every format
    rule beyond "not empty" rejects somebody's real name.
    """
    assert validate_full_name(name) == name


def test_names_are_trimmed_and_blank_is_refused() -> None:
    assert validate_full_name("  Asha Menon  ") == "Asha Menon"
    with pytest.raises(ValidationError):
        validate_full_name("   ")


def test_name_length_matches_the_api_contract_and_the_column() -> None:
    """120 characters — API §11.2. Long enough for any name, short enough that
    the field cannot become a free-text note."""
    assert validate_full_name("क" * 120)
    with pytest.raises(ValidationError) as caught:
        validate_full_name("क" * 121)
    assert caught.value.details == {"max_length": 120}


# ─── Minor status (FR-M0-028, DPDP) ──────────────────────────────────────


def test_age_is_completed_years_not_divided_days() -> None:
    """⚠️ Not ``(today - dob).days // 365``.

    That drifts by a day every leap year. The day before an eighteenth birthday
    the answer must still be 17 — guardian consent gates on it, so being a day
    early is a consent failure, not a rounding error.
    """
    born = date(2008, 3, 1)
    assert age_in_years(born, as_of=date(2026, 2, 28)) == 17
    assert age_in_years(born, as_of=date(2026, 3, 1)) == 18
    assert age_in_years(born, as_of=date(2026, 3, 2)) == 18


def test_leap_day_birthday_does_not_drift() -> None:
    """A 29 February birth date in a non-leap year: the birthday has not occurred
    on 28 February, and has on 1 March."""
    born = date(2008, 2, 29)
    assert age_in_years(born, as_of=date(2026, 2, 28)) == 17
    assert age_in_years(born, as_of=date(2026, 3, 1)) == 18


def test_minor_status_flips_exactly_on_the_eighteenth_birthday() -> None:
    """🔒 FR-M0-028 — the threshold is DPDP's, held in one place."""
    born = date(2008, 6, 15)
    assert is_minor(born, as_of=date(2026, 6, 14)) is True
    assert is_minor(born, as_of=date(2026, 6, 15)) is False
    assert MINOR_AGE_THRESHOLD == 18


def test_unknown_date_of_birth_is_none_and_not_false() -> None:
    """🔒 ``None`` is not ``False``, and the difference is a guardian check.

    The enquiry form does not ask for a date of birth (FR-M2-004). Treating "we
    do not know" as "adult" is precisely the assumption that skips the guardian
    consent DPDP requires — so the type forces the caller to handle the third
    case rather than letting it collapse into the safe-looking one.
    """
    assert is_minor(None) is None


# ─── The port (DB §5 — "Readers: via kernel ports") ──────────────────────


def test_client_identity_resolves_engagement_for_its_own_stage() -> None:
    """The convenience the five reading modules actually use."""
    identity = ClientIdentity(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        full_name="Asha Menon",
        stage=ClientStage.PAUSED,
        owner_user_id=uuid.uuid4(),
        is_archived=False,
        dietary_class=DietaryClass.JAIN,
    )
    assert identity.receives_engagement is False


def test_unwired_directory_fails_loudly_rather_than_reading_nothing() -> None:
    """🔒 An unwired port must not look like an empty database.

    A null-object directory would answer "no such client" to every question, and
    a module deciding whether to send a message would silently skip work nothing
    recorded it had skipped. Same seam and same reasoning as ``CredentialStore``.
    """
    import app.kernel.clients as kernel_clients

    installed = kernel_clients._directory
    kernel_clients._directory = None
    try:
        with pytest.raises(RuntimeError, match="No ClientDirectory is installed"):
            get_client_directory()
    finally:
        kernel_clients._directory = installed


def test_configured_directory_is_returned() -> None:
    import app.kernel.clients as kernel_clients

    installed = kernel_clients._directory

    class _Stub:
        async def find(self, session: object, /, **kwargs: object) -> None:
            return None

        async def find_by_mobile(self, session: object, /, **kwargs: object) -> list[object]:
            return []

        async def count_active(self, session: object, /, **kwargs: object) -> int:
            return 0

    stub: ClientDirectory = _Stub()  # type: ignore[assignment]
    try:
        configure_client_directory(stub)
        assert get_client_directory() is stub
    finally:
        kernel_clients._directory = installed


def test_the_port_declares_no_write_method() -> None:
    """🔒 DB §5 — "Writers: `clients` only", made structural.

    ⚠️ A ``create`` or ``update`` on the port would let any module write a table
    it does not own — R6 with extra steps, and *invisible to the boundary
    checker* because the call goes through a protocol rather than an import.
    This test is the only thing that would catch it.
    """
    surface = {name for name in vars(ClientDirectory) if not name.startswith("_")}
    assert surface == {"find", "find_by_mobile", "count_active"}, (
        "ClientDirectory has gained a method. If it writes, it violates DB §5 — "
        "every write to `clients` belongs in the clients module's service."
    )
