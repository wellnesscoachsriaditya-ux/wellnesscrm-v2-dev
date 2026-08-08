"""Clients — the rules of the spine, and the port everything else reads it through.

🔒 DB §5, FR-M1-004..016, M1.3. **Leads and clients are one entity**, separated
only by lifecycle stage — DB §5.1 calls that "the single most important
modelling decision in the schema". There is no leads table. A lead is a client
row at stage ``lead``, which is why converting one loses nothing (AC-M1-003) and
why a returning client is reactivated rather than recreated (EC-M1-02).

Two things live here, and the split matters:

1. **The rules** — contact presence, mobile format, what a stage permits. Pure
   functions over values, testable without a database, and the single definition
   of each rule so a second one cannot drift into existence.
2. **The port** (:class:`ClientDirectory`) — how *other* modules read client
   identity and stage. DB §5 lists five modules that need it and states the
   mechanism: "Readers: via kernel ports". Arch R3 forbids importing the
   ``clients`` module directly and R6 forbids reading its tables, so this
   protocol is the only sanctioned seam.

⚠️ **The port is read-only, deliberately.** Every writer of a ``clients`` row is
the ``clients`` module itself (DB §5: "Writers: `clients` only"). A port method
that created or mutated a client would let `leads` — or anything else — write
into a table it does not own, which is R6 with extra steps.

🔒 **Stage is not a status field.** Moving between stages has consequences:
entitlement checks, ``activated_at``, history rows, message cancellation. ADR-A06
makes each transition a named action rather than a PATCH, and
:func:`counts_towards_limit` is the predicate the entitlement count is built on.

This module holds rules only. Persistence lives in ``app.modules.clients`` — the
split is what lets every rule below be tested without a database.
"""

from __future__ import annotations

import enum
import re
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.consent import MINOR_AGE_THRESHOLD
from app.kernel.errors import ValidationError


class ClientStage(str, enum.Enum):
    """The lifecycle — DB §5.2, M1.4. 🟡 Values PROPOSED pending OD-01.

    🔒 One entity, seven stages. A ``lead`` and an ``active`` client are the same
    row; the stage is what differs. That is what makes AC-M1-003 ("converting a
    lead retains the original record, its identifier, and all prior history")
    true by construction rather than by careful copying.
    """

    LEAD = "lead"
    CONTACTED = "contacted"
    CONSULTATION_SCHEDULED = "consultation_scheduled"
    ACTIVE = "active"
    PAUSED = "paused"
    CHURNED = "churned"
    ARCHIVED = "archived"


class SexType(str, enum.Enum):
    """Biological sex, for requirement estimation — DB §5.1.

    ⚠️ 🟡 **PROPOSED — the value set is not specified in any approved document.**
    DB §5.1 names the column and the type and stops there. These values are my
    proposal and need confirmation.

    🔒 The column exists for one purpose: BMR equations (Mifflin-St Jeor,
    Harris-Benedict) take a male/female term. It is **nullable**, and ``OTHER``
    is a real answer rather than a gap — but neither NULL nor ``OTHER`` yields a
    defined equation, so both must fall through to the practitioner entering a
    requirement directly. Silently substituting one for the other would put a
    wrong number on a clinical plan, which is the failure this note exists to
    prevent.
    """

    MALE = "male"
    FEMALE = "female"
    OTHER = "other"


class DietaryClass(str, enum.Enum):
    """What the person eats — DB §8.5, FR-M4-035.

    🔒 Lives on the *client*, not the plan (DB §5.1): it is a property of the
    person, and it filters food search at authoring time, before a plan exists.

    ``JAIN`` is not a stricter vegetarian in a way a boolean could express — it
    excludes root vegetables, which is a different axis from animal products
    entirely, and collapsing it would put onion in a Jain client's plan.
    """

    VEGETARIAN = "vegetarian"
    EGGETARIAN = "eggetarian"
    NON_VEGETARIAN = "non_vegetarian"
    VEGAN = "vegan"
    JAIN = "jain"


# ─── Stage predicates ────────────────────────────────────────────────────

#: 🔒 M1.5 — the entitlement predicate, and the whole of it. DB §5.2: "Only
#: `active` counts toward the plan limit ... one predicate, explainable in a
#: sentence, fully under practitioner control."
#:
#: ⚠️ FR-M1-003 is the other half: a lead must not be metered at any stage before
#: `active`. A tenant at their limit still accepts enquiries (EC-M2-06) — the
#: limit binds at conversion, not at capture.
_METERED_STAGES: frozenset[ClientStage] = frozenset({ClientStage.ACTIVE})

#: 🔒 FR-M1-014 — stages that receive no plans, scheduled messages or check-in
#: nudges. Distinct from "not metered": a `paused` client is not billed for and
#: not messaged, while a `contacted` lead is not billed for but *is* legitimately
#: contacted. Deriving one from the other would make one of the two wrong.
_DORMANT_STAGES: frozenset[ClientStage] = frozenset(
    {ClientStage.PAUSED, ClientStage.CHURNED, ClientStage.ARCHIVED}
)


def counts_towards_limit(stage: ClientStage, *, archived_at_is_set: bool) -> bool:
    """Whether this client consumes an ``active_clients`` entitlement.

    🔒 M1.5 / AC-M1-005. Both halves are required: an archived row is excluded
    even if its stage still says ``active``, because archiving is a soft delete
    (FR-M1-010) and a practitioner who archived a client would otherwise still be
    paying for them.
    """
    return stage in _METERED_STAGES and not archived_at_is_set


def receives_engagement(stage: ClientStage, *, archived_at_is_set: bool) -> bool:
    """Whether this client may receive plans, messages or nudges — FR-M1-014."""
    return stage not in _DORMANT_STAGES and not archived_at_is_set


def is_lead(stage: ClientStage) -> bool:
    """Whether this record is still a prospect rather than a client.

    Used by the needs-response view (FR-M2-011) and by conversion metrics. A
    ``consultation_scheduled`` record is still a lead: nobody has been billed and
    no service has been delivered.
    """
    return stage in (
        ClientStage.LEAD,
        ClientStage.CONTACTED,
        ClientStage.CONSULTATION_SCHEDULED,
    )


# ─── Contact details (FR-M1-004, FR-M1-006, NFR-100) ─────────────────────

#: 🔒 NFR-100 / FR-M1-006 — E.164, validated for Indian numbers at MVP.
#:
#: ``+91`` then a 10-digit number beginning 6–9. India has no assigned mobile
#: prefixes below 6, so the range is a real check rather than decoration: it
#: catches a landline typed into the mobile field, which is the common data-entry
#: error and the one that makes WhatsApp delivery fail silently later.
_INDIAN_MOBILE = re.compile(r"^\+91[6-9]\d{9}$")

#: The general shape, for numbers outside India once GCC and UK land (Phase 2/3).
#: ⚠️ Not used for validation at MVP — it is here so the eventual widening is a
#: one-line change to :func:`normalise_mobile` rather than a new concept.
_E164_ANY = re.compile(r"^\+[1-9]\d{7,14}$")

#: Characters people type into a phone field that carry no information.
_MOBILE_NOISE = re.compile(r"[\s\-().]")


def normalise_mobile(mobile: str) -> str:
    """Normalise a typed mobile number to stored E.164 form.

    🔒 NFR-100 — stored in international format, always. The normalisation is
    deliberately generous about *input* and strict about *output*: an Indian
    practitioner types ``98765 43210``, ``098765-43210`` or ``+91 98765 43210``
    and all three are the same number, but only one of those is storable.

    ⚠️ A bare 10-digit number is assumed Indian. That assumption is correct for
    the launch market and wrong the moment GCC lands, so it is written as an
    explicit branch rather than a regex default — the place to change is
    obvious.

    Raises:
        ValidationError: If the result is not a valid Indian mobile number.
    """
    cleaned = _MOBILE_NOISE.sub("", mobile.strip())

    if cleaned.startswith("00"):
        # The other international prefix. Normalised before anything else, or a
        # `0091...` number would be read as a national number with a trunk zero.
        cleaned = "+" + cleaned[2:]
    elif cleaned.startswith("91") and len(cleaned) == 12:
        cleaned = "+" + cleaned
    elif cleaned.startswith("0") and len(cleaned) == 11:
        # National format with the trunk prefix: 0 98765 43210.
        cleaned = "+91" + cleaned[1:]
    elif not cleaned.startswith("+") and len(cleaned) == 10:
        cleaned = "+91" + cleaned

    if not _INDIAN_MOBILE.fullmatch(cleaned):
        raise ValidationError(
            "That does not look like a valid Indian mobile number.",
            action="Enter a 10-digit mobile number starting with 6, 7, 8 or 9.",
            details={"expected_format": "+91XXXXXXXXXX"},
        )
    return cleaned


def looks_like_e164(value: str) -> bool:
    """Whether a stored value is well-formed E.164 — for schema-level assertions."""
    return bool(_E164_ANY.fullmatch(value))


# ─── Age and minor status (FR-M0-028) ────────────────────────────────────


def age_in_years(date_of_birth: date, *, as_of: date | None = None) -> int:
    """Completed years between a birth date and a reference date.

    ⚠️ Not ``(today - dob).days // 365``. That drifts by a day every leap year
    and puts somebody on the wrong side of eighteen for a week — which, given
    what keys off it, is a consent failure rather than a rounding error.
    """
    today = as_of or date.today()
    had_birthday = (today.month, today.day) >= (date_of_birth.month, date_of_birth.day)
    return today.year - date_of_birth.year - (0 if had_birthday else 1)


def is_minor(date_of_birth: date | None, *, as_of: date | None = None) -> bool | None:
    """Whether this person is a minor — FR-M0-028, DPDP.

    🔒 **Derived, never stored.** DB §5.1 specifies a generated ``is_minor``
    column; that is not implementable and would not be correct if it were.
    PostgreSQL requires a generation expression to be IMMUTABLE and age is a
    function of the current date, so the DDL is rejected outright — and a plain
    stored flag would be worse, because a row written while the client was 17
    keeps saying so the day they turn 18. Guardian consent gates on this
    (``kernel.consent.validate_guardian``), so a stale value means asking an
    adult for parental consent or failing to ask a minor's guardian.

    Returns ``None`` when the date of birth is unknown. 🔒 ``None`` is not
    ``False``: the enquiry form does not ask for a date of birth (FR-M2-004), and
    treating "we do not know" as "adult" is precisely the assumption that skips a
    guardian check.
    """
    if date_of_birth is None:
        return None
    return age_in_years(date_of_birth, as_of=as_of) < MINOR_AGE_THRESHOLD


@dataclass(frozen=True, slots=True)
class ContactDetails:
    """A client's reachable addresses, validated together.

    🔒 FR-M1-004 / EC-M1-08 — **at least one** of mobile or email. The database
    enforces the same rule in ``ck_clients__contact_present``, and both are
    needed: the constraint catches anything that reaches the table by another
    path, this catches it early enough to name the field.

    ⚠️ EC-M1-08 is the reason it is "at least one" rather than "mobile": a client
    with only an email is legitimate, loses WhatsApp delivery, and the system
    must say so rather than refuse the record.
    """

    mobile: str | None = None
    email: str | None = None

    def __post_init__(self) -> None:
        if not self.mobile and not self.email:
            raise ValidationError(
                "A client needs a mobile number or an email address.",
                action="Add at least one way to contact them.",
            )

    @property
    def can_receive_whatsapp(self) -> bool:
        """🔒 EC-M1-08 — the limitation the UI must state rather than hide."""
        return self.mobile is not None


def validate_full_name(full_name: str) -> str:
    """Trim and check a client's name — FR-M1-004.

    The only universally true rule about names: there is one, and it is not
    empty. No format, no minimum word count, no character class — every such
    rule rejects somebody's real name, and this is a product for a market with
    mononyms, initials and four scripts in common use.
    """
    trimmed = full_name.strip()
    if not trimmed:
        raise ValidationError(
            "A client needs a name.",
            action="Enter the client's name.",
        )
    if len(trimmed) > 120:
        # Matches the API contract (§11.2) and the column. Long enough for any
        # real name; short enough that the field is not a free-text note.
        raise ValidationError(
            "That name is too long.",
            action="Use 120 characters or fewer.",
            details={"max_length": 120},
        )
    return trimmed


# ─── The port (DB §5 — "Readers: via kernel ports") ──────────────────────


@dataclass(frozen=True, slots=True)
class ClientIdentity:
    """The minimum another module may know about a client.

    🔒 Deliberately thin. Five modules read this (DB §5), and every field added
    here is a field five modules can come to depend on — at which point the
    ``clients`` module cannot change it. Clinical values are absent by design:
    a module needing a measurement asks ``clinical``, not this.
    """

    id: uuid.UUID
    tenant_id: uuid.UUID
    full_name: str
    stage: ClientStage
    owner_user_id: uuid.UUID
    is_archived: bool
    dietary_class: DietaryClass | None = None
    preferred_language: str = "en"

    @property
    def receives_engagement(self) -> bool:
        """FR-M1-014, resolved for this client."""
        return receives_engagement(self.stage, archived_at_is_set=self.is_archived)


class ClientDirectory(Protocol):
    """How other modules read client identity and stage — DB §5.

    🔒 The sanctioned seam. Arch R3 forbids importing the ``clients`` module and
    R6 forbids reading its tables, so a module that needs to know whether a
    client exists, who owns them, or what stage they are in comes through here.

    ⚠️ **Read-only, and that is the point.** DB §5: "Writers: `clients` only". A
    ``create`` or ``update`` method here would let any module write into a table
    it does not own — R6 with extra steps, and the boundary checker would not see
    it because the call goes through a protocol rather than an import.

    ⚠️ Implementations take the caller's session so their reads run inside the
    caller's transaction and under its tenant scope. A directory that opened its
    own connection would read outside the RLS scope the request established.
    """

    async def find(
        self, session: AsyncSession, /, *, tenant_id: uuid.UUID, client_id: uuid.UUID
    ) -> ClientIdentity | None:
        """One client by id, or ``None`` if absent or not this tenant's."""
        ...

    async def find_by_mobile(
        self, session: AsyncSession, /, *, tenant_id: uuid.UUID, mobile: str
    ) -> list[ClientIdentity]:
        """Every client reachable on this number — EC-M2-02 duplicate matching.

        🔒 Returns a **list**, not one record. EC-M1-01 explicitly permits family
        members sharing a mobile, and DB §5.1 has no unique constraint on the
        column for exactly that reason. A signature returning a single client
        would force an arbitrary choice between two real people.

        ⚠️ The caller must not reveal the result to an unauthenticated user. The
        enquiry endpoint matches silently (DB §6.2) — telling a stranger whether
        a number is on file discloses a practitioner's client list one probe at
        a time.
        """
        ...

    async def count_active(self, session: AsyncSession, /, *, tenant_id: uuid.UUID) -> int:
        """Live count of clients consuming the entitlement — M1.5.

        🔒 Counted from source rather than a ``usage_counters`` row: it is the
        product's most visible limit, and a drifting counter would produce a bill
        the practitioner can disprove by eye (DB §14.4).
        """
        ...


#: The installed directory. 🔒 Same seam as ``CredentialStore`` and
#: ``StorageBackend``: the kernel names the capability, the module implements it,
#: and the entry point wires the two together — because R1 forbids the kernel
#: from importing the module that satisfies it.
_directory: ClientDirectory | None = None


def configure_client_directory(directory: ClientDirectory) -> None:
    """Install the directory. Called once, at startup, by an entry point."""
    global _directory
    _directory = directory


def get_client_directory() -> ClientDirectory:
    """The installed directory.

    Raises:
        RuntimeError: If nothing is installed. 🔒 Loud rather than returning a
            null object: a module silently reading "no such client" from an
            unwired directory would skip work it was supposed to do, and nothing
            would record that it had.
    """
    if _directory is None:
        raise RuntimeError(
            "No ClientDirectory is installed. The `clients` module registers one "
            "at startup via `configure_client_directory()`. A module reading "
            "client identity before that wiring runs would silently see no "
            "clients at all."
        )
    return _directory
