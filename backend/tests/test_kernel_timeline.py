"""The timeline's vocabulary — DDR-06, FR-M1-018/019.

🔒 No database. ``kernel.timeline`` is where the summary vocabulary lives, and
these are the rules that decide what a practitioner reads and — more importantly
— what a timeline row is structurally incapable of containing.

Two groups:

* **Summaries** — the NFR-033 guarantee. The interesting assertions are about
  what these functions *cannot* be made to do.
* **The event contract** — that each event carries what a subscriber needs and
  nothing that ``kernel.events`` would refuse.
"""

from __future__ import annotations

import uuid
from dataclasses import fields
from datetime import UTC, datetime

import pytest

from app.kernel.clients import ClientStage
from app.kernel.events import to_payload
from app.kernel.timeline import (
    MAX_SUMMARY_LENGTH,
    ClientAccessChanged,
    ClientNoteAdded,
    ClientOwnershipChanged,
    ClientTagsChanged,
    TimelineActorType,
    TimelineEventType,
    is_producible,
    stage_label,
    summarise,
    summarise_stage_change,
)

CLIENT = uuid.UUID("22222222-0000-4000-8000-000000000001")
TENANT = uuid.UUID("22222222-0000-4000-8000-000000000002")
ACTOR = uuid.UUID("22222222-0000-4000-8000-000000000003")
MOMENT = datetime(2026, 8, 9, 10, 30, tzinfo=UTC)


# ─── Summaries (DB §5.6, NFR-033) ────────────────────────────────────────


def test_a_stage_change_names_both_ends() -> None:
    """FR-M1-015 — "Lead → Active" is the whole point of the entry."""
    summary = summarise_stage_change(from_stage=ClientStage.LEAD, to_stage=ClientStage.ACTIVE)
    assert summary == "New enquiry → Active client"


def test_the_first_stage_has_no_arrow() -> None:
    """A creation has no `from_stage`, and "None → New enquiry" reads as a bug."""
    assert (
        summarise_stage_change(from_stage=None, to_stage=ClientStage.LEAD) == "Added as New enquiry"
    )


def test_every_stage_has_a_label() -> None:
    """🔒 A missing label falls back to the wire code, which would ship
    `consultation_scheduled` into a practitioner-facing timeline.

    Asserting inequality with the enum value is what makes this test fail when
    a stage is added and its label forgotten — the fallback is deliberate (an
    ugly label beats a lost event) and therefore silent, so this is the only
    thing that catches it.
    """
    for stage in ClientStage:
        assert stage_label(stage) != stage.value


def test_stage_labels_are_prose_not_wire_codes() -> None:
    assert stage_label(ClientStage.CONSULTATION_SCHEDULED) == "Consultation scheduled"
    assert "_" not in stage_label(ClientStage.CONSULTATION_SCHEDULED)


def test_every_event_type_except_stage_change_has_a_fixed_summary() -> None:
    """🔒 A type with no label would write an empty timeline row."""
    for event_type in TimelineEventType:
        if event_type is TimelineEventType.STAGE_CHANGED:
            continue
        assert summarise(event_type)


def test_a_stage_change_has_no_parameterless_summary() -> None:
    """🔒 Loud rather than defaulted.

    ``STAGE_CHANGED``'s label depends on which stages, so reaching it through
    the parameterless path is a caller bug. A silent "" would ship a blank row;
    the KeyError says which type was mishandled.
    """
    with pytest.raises(KeyError):
        summarise(TimelineEventType.STAGE_CHANGED)


def test_a_note_summary_never_says_what_the_note_said() -> None:
    """🔒 FR-M1-018 / NFR-033 — the timeline records that a note exists.

    This is the guarantee the whole module is shaped around, and it holds
    because :func:`summarise` has no parameter capable of carrying prose.
    """
    assert summarise(TimelineEventType.NOTE_ADDED) == "Note added"


def test_summarise_cannot_be_passed_a_value() -> None:
    """🔒 The NFR-033 guarantee expressed as a signature.

    A summary function taking a string would eventually be called with a note
    body. This asserts the *shape* of the API rather than an output, because
    the shape is what makes the leak impossible rather than merely absent.
    """
    with pytest.raises(TypeError):
        summarise(TimelineEventType.NOTE_ADDED, "a blood pressure reading")  # type: ignore[call-arg]


def test_a_tag_summary_never_names_the_tag() -> None:
    """⚠️ "Tags updated", not "PCOS applied".

    A tag name in a timeline row outlives the tag being renamed or retired, and
    a row naming a tag that no longer exists reads as corruption.
    """
    summary = summarise(TimelineEventType.TAG_APPLIED)
    assert summary == "Tags updated"


def test_summaries_fit_the_column() -> None:
    """The check constraint bounds `summary` at 200; every label must fit."""
    for event_type in TimelineEventType:
        if event_type is TimelineEventType.STAGE_CHANGED:
            continue
        assert len(summarise(event_type)) <= MAX_SUMMARY_LENGTH

    longest = summarise_stage_change(
        from_stage=ClientStage.CONSULTATION_SCHEDULED, to_stage=ClientStage.ARCHIVED
    )
    assert len(longest) <= MAX_SUMMARY_LENGTH


# ─── The producible set (FR-M1-019) ──────────────────────────────────────


def test_s2_event_types_are_producible() -> None:
    """The filter list must offer what the system can actually emit."""
    for event_type in (
        TimelineEventType.STAGE_CHANGED,
        TimelineEventType.NOTE_ADDED,
        TimelineEventType.TAG_APPLIED,
        TimelineEventType.OWNERSHIP_CHANGED,
        TimelineEventType.ACCESS_CHANGED,
        TimelineEventType.CLIENT_ARCHIVED,
        TimelineEventType.CLIENT_RESTORED,
    ):
        assert is_producible(event_type)


def test_future_event_types_are_not_offered_as_filters() -> None:
    """🔒 A filter that always returns nothing reads as a broken timeline.

    These values exist in the enum so the S3–S6 producers need no migration;
    offering them as filters now would be a promise the build cannot keep.
    """
    for event_type in (
        TimelineEventType.PLAN_ISSUED,
        TimelineEventType.MEASUREMENT_RECORDED,
        TimelineEventType.APPOINTMENT_SCHEDULED,
        TimelineEventType.MESSAGE_SENT,
        TimelineEventType.DOCUMENT_UPLOADED,
        TimelineEventType.ASSESSMENT_COMPLETED,
        TimelineEventType.CLIENT_ACTIVITY,
        TimelineEventType.ENQUIRY_RECEIVED,
    ):
        assert not is_producible(event_type)


def test_the_system_actor_is_distinguishable() -> None:
    """🔒 "The system archived them" and "Priya archived them" are different
    facts, and a timeline rendering both as a name misattributes automation."""
    assert TimelineActorType.SYSTEM.value == "system"
    assert len(set(TimelineActorType)) == 3


# ─── The event contract (NFR-033, kernel.events) ─────────────────────────


def test_a_note_event_carries_no_body() -> None:
    """🔒 The strongest form of the FR-M1-018 guarantee.

    The body is not merely unused by the subscriber — it is absent from the
    event, so no future subscriber can put it in a timeline row either.
    """
    names = {field.name for field in fields(ClientNoteAdded)}
    assert "body" not in names
    assert "note_id" in names


def test_every_timeline_event_survives_payload_encoding() -> None:
    """🔒 `kernel.events.to_payload` refuses anything that is not an identifier.

    An event failing this would abort the publish *inside* the publisher's
    transaction, rolling back the practitioner's action — so the contract is
    asserted here rather than discovered at runtime.
    """
    events = [
        ClientNoteAdded(
            client_id=CLIENT,
            tenant_id=TENANT,
            note_id=uuid.uuid4(),
            author_user_id=ACTOR,
            created_at=MOMENT,
        ),
        ClientTagsChanged(
            client_id=CLIENT,
            tenant_id=TENANT,
            tag_id=uuid.uuid4(),
            attached=True,
            actor_user_id=ACTOR,
            changed_at=MOMENT,
        ),
        ClientOwnershipChanged(
            client_id=CLIENT,
            tenant_id=TENANT,
            from_user_id=ACTOR,
            to_user_id=uuid.uuid4(),
            actor_user_id=ACTOR,
            changed_at=MOMENT,
        ),
        ClientAccessChanged(
            client_id=CLIENT,
            tenant_id=TENANT,
            grantee_user_id=uuid.uuid4(),
            granted=True,
            actor_user_id=ACTOR,
            changed_at=MOMENT,
        ),
    ]

    for event in events:
        payload = to_payload(event)
        assert payload["_event"] == type(event).event_name
        assert payload["tenant_id"] == str(TENANT)


def test_a_tag_change_records_its_direction() -> None:
    """One event for both directions, so the subscriber needs the flag."""
    applied = ClientTagsChanged(
        client_id=CLIENT,
        tenant_id=TENANT,
        tag_id=uuid.uuid4(),
        attached=True,
        actor_user_id=ACTOR,
        changed_at=MOMENT,
    )
    assert applied.attached is True
    assert to_payload(applied)["attached"] is True


def test_an_ownership_change_carries_both_ends() -> None:
    """🔒 "Who was this client moved away from" is the question a handover
    audit asks, and a timeline carrying only the destination cannot answer it."""
    previous, incoming = uuid.uuid4(), uuid.uuid4()
    event = ClientOwnershipChanged(
        client_id=CLIENT,
        tenant_id=TENANT,
        from_user_id=previous,
        to_user_id=incoming,
        actor_user_id=ACTOR,
        changed_at=MOMENT,
    )
    assert event.from_user_id == previous
    assert event.to_user_id == incoming


def test_timeline_events_are_frozen() -> None:
    """🔒 An event is a statement about the past. `kernel.events` relies on this
    so one transactional handler cannot mutate what the next receives."""
    event = ClientAccessChanged(
        client_id=CLIENT,
        tenant_id=TENANT,
        grantee_user_id=ACTOR,
        granted=True,
        actor_user_id=ACTOR,
        changed_at=MOMENT,
    )
    with pytest.raises(AttributeError):
        event.granted = False  # type: ignore[misc]


def test_a_system_actor_event_carries_no_user() -> None:
    """⚠️ `actor_user_id=None` means unattended work, not "we failed to
    identify them" — every practitioner path carries the subject."""
    event = ClientTagsChanged(
        client_id=CLIENT,
        tenant_id=TENANT,
        tag_id=uuid.uuid4(),
        attached=False,
        actor_user_id=None,
        changed_at=MOMENT,
    )
    assert to_payload(event)["actor_user_id"] is None
