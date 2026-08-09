"""The client timeline — the vocabulary a materialised projection is built from.

🔒 **DDR-06 / FR-M1-018.** A unified reverse-chronological view aggregating
events from six modules. DB §5.6 settles *how*: a materialised table written by
event subscribers, rather than a read-time UNION across module tables, which
would violate R6 and put NFR-006 (≤800 ms) at risk from the day a seventh event
type existed.

🔒 **This module is rules only.** The event classes and the summary vocabulary
live here because a subscriber in ``modules.clients`` and a producer in some
future ``clinical`` module both need them, and R3 forbids either importing the
other. The kernel is the layer both may depend on — the same argument that put
``ClientStageChanged`` in ``kernel.clients``.

⚠️ **The timeline is a projection, never a source of truth** (DB §5.6). Every
row here is derived from a record that still exists in its owning module. That
is what makes :func:`summarise` safe to change: a wording fix is a rebuild, not
a data migration, and a drifted projection can be regenerated.

🔒 **Summaries carry no clinical values** (DB §5.6, NFR-033). The timeline is the
most-viewed screen in the product and the most likely place for a measurement or
a diagnosis to leak into a log line or an error report. Every function here takes
identifiers and enums and returns a fixed label; none takes free text. That is
enforced by construction rather than by review — see :func:`summarise`, which has
no parameter capable of carrying prose.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime

from app.kernel.clients import ClientStage
from app.kernel.events import DomainEvent, register_event

# ─── The event vocabulary (FR-M1-019) ────────────────────────────────────


class TimelineEventType(enum.StrEnum):
    """What kind of thing happened — DB §5.6 ``timeline_event_type``.

    🔒 **A closed enum, because FR-M1-019 makes it a filter.** A free-text
    ``event_type`` would mean the filter list is whatever happens to be in the
    table, which changes per tenant and cannot be translated or ordered.

    ⚠️ 🟡 **The member set is PROPOSED.** DB §5.6 names the column and its type
    and stops there. These nine are derived from FR-M1-018's own list of what the
    timeline must aggregate — "stage changes, appointments, assessments,
    measurements, plans issued, messages sent, notes, documents and client-side
    activity" — mapped onto what exists today plus what S3–S6 will add.

    🔒 The members S2 cannot yet produce are declared anyway, and that is
    deliberate: ``timeline_event_type`` is a PostgreSQL enum, and adding a value
    later is a migration that cannot run inside a transaction with other DDL on
    some versions. Declaring the full vocabulary once is cheaper than nine
    migrations, and an unused value costs nothing. What must *not* happen is a
    module inventing a tenth value at runtime — hence the closed set.
    """

    # ── Produced in S2 ──
    STAGE_CHANGED = "stage_changed"
    NOTE_ADDED = "note_added"
    TAG_APPLIED = "tag_applied"
    OWNERSHIP_CHANGED = "ownership_changed"
    ACCESS_CHANGED = "access_changed"
    CLIENT_ARCHIVED = "client_archived"
    CLIENT_RESTORED = "client_restored"
    ENQUIRY_RECEIVED = "enquiry_received"

    # ── Declared for S3–S6, not yet produced ──
    #: ⚠️ No producer yet. See the class docstring for why they are declared now.
    MEASUREMENT_RECORDED = "measurement_recorded"
    ASSESSMENT_COMPLETED = "assessment_completed"
    PLAN_ISSUED = "plan_issued"
    APPOINTMENT_SCHEDULED = "appointment_scheduled"
    MESSAGE_SENT = "message_sent"
    DOCUMENT_UPLOADED = "document_uploaded"
    CLIENT_ACTIVITY = "client_activity"


class TimelineActorType(enum.StrEnum):
    """Who caused it — DB §5.6 ``actor_type``.

    🔒 Three kinds, and the distinction matters for reading the timeline rather
    than for authorization. "Priya changed the stage" and "the system archived
    them after 90 days" are different facts, and a timeline that renders both as
    a bare name misattributes automated action to a person.
    """

    PRACTITIONER = "practitioner"
    CLIENT = "client"
    #: 🔒 A scheduled job, a rule, a retention sweep. ``actor_id`` is NULL.
    SYSTEM = "system"


# ─── The summary vocabulary (DB §5.6) ────────────────────────────────────

#: 🔒 Longest permitted summary. Short by construction — a summary is a label
#: scanned in a list, not a description. The limit is a backstop: every value
#: :func:`summarise` can return is a fixed string well under it, so a row
#: exceeding this could only come from a future caller bypassing that function.
MAX_SUMMARY_LENGTH = 200

#: 🔒 How each stage reads in a timeline entry. Separate from the stage enum's
#: own value because "consultation_scheduled" is a wire code and "Consultation
#: scheduled" is what a practitioner reads.
#:
#: ⚠️ Duplicated from the frontend's `stages.ts` on purpose. The alternative is
#: shipping display strings through the API, which would make every wording fix
#: a backend deploy. The API sends the enum; both ends label it.
_STAGE_LABELS: dict[ClientStage, str] = {
    ClientStage.LEAD: "New enquiry",
    ClientStage.CONTACTED: "Contacted",
    ClientStage.CONSULTATION_SCHEDULED: "Consultation scheduled",
    ClientStage.ACTIVE: "Active client",
    ClientStage.PAUSED: "Paused",
    ClientStage.CHURNED: "Churned",
    ClientStage.ARCHIVED: "Archived",
}


def stage_label(stage: ClientStage) -> str:
    """How a stage reads in a timeline entry.

    ⚠️ Falls back to the raw enum value rather than raising. An unlabelled stage
    is a wording gap; refusing to write the timeline row over one would lose the
    event entirely, which is a far worse outcome than an ugly label.
    """
    return _STAGE_LABELS.get(stage, stage.value)


def summarise_stage_change(*, from_stage: ClientStage | None, to_stage: ClientStage) -> str:
    """The label for a lifecycle transition — FR-M1-015.

    🔒 Takes stages, not a reason. ``change_stage`` accepts practitioner free
    text and stores it on ``client_stage_history``; letting it reach the timeline
    would put prose in the one table most likely to be read in bulk. The
    practitioner reads the reason on the stage-history record, under its own
    retention rules.
    """
    if from_stage is None:
        return f"Added as {stage_label(to_stage)}"
    return f"{stage_label(from_stage)} → {stage_label(to_stage)}"


def summarise(event_type: TimelineEventType) -> str:
    """The fixed label for an event type that needs no parameters.

    🔒 **The NFR-033 guarantee, expressed as a signature.** This function takes
    an enum and nothing else, so no caller can pass it a note body, a
    measurement or a client's name. A summary that needs a value — a stage
    transition — has its own function above that takes only enums.

    Raises:
        KeyError: On an event type with no label. Loud rather than defaulted: a
            missing label here means a new event type was added without deciding
            how it reads, and a silent "" would ship an empty timeline row.
    """
    return _SUMMARIES[event_type]


#: 🔒 One label per event type. Deliberately not derived from the enum's value —
#: "note_added" is a wire code and "Note added" is a sentence, and the two drift
#: the moment a type is renamed for a technical reason.
_SUMMARIES: dict[TimelineEventType, str] = {
    TimelineEventType.NOTE_ADDED: "Note added",
    TimelineEventType.TAG_APPLIED: "Tags updated",
    TimelineEventType.OWNERSHIP_CHANGED: "Owning practitioner changed",
    TimelineEventType.ACCESS_CHANGED: "Access changed",
    TimelineEventType.CLIENT_ARCHIVED: "Client archived",
    TimelineEventType.CLIENT_RESTORED: "Client restored",
    TimelineEventType.ENQUIRY_RECEIVED: "Enquiry received",
    TimelineEventType.MEASUREMENT_RECORDED: "Measurement recorded",
    TimelineEventType.ASSESSMENT_COMPLETED: "Assessment completed",
    TimelineEventType.PLAN_ISSUED: "Plan issued",
    TimelineEventType.APPOINTMENT_SCHEDULED: "Appointment scheduled",
    TimelineEventType.MESSAGE_SENT: "Message sent",
    TimelineEventType.DOCUMENT_UPLOADED: "Document uploaded",
    TimelineEventType.CLIENT_ACTIVITY: "Client activity",
    # ⚠️ STAGE_CHANGED is absent on purpose — its label depends on which stages,
    # so it goes through `summarise_stage_change`. `summarise` raising KeyError
    # for it is the correct outcome: it means a caller took the parameterless
    # path for an event that needs parameters.
}


def is_producible(event_type: TimelineEventType) -> bool:
    """Whether any module can emit this event type today.

    🔒 Used by the read endpoint to decide which filters to offer. Offering
    "Plan issued" in S2 would be a filter that always returns nothing, which
    reads as a broken timeline rather than an unbuilt feature.
    """
    return event_type in _PRODUCIBLE


#: The subset S2 can actually write. Grows a member per slice as producers land.
_PRODUCIBLE: frozenset[TimelineEventType] = frozenset(
    {
        TimelineEventType.STAGE_CHANGED,
        TimelineEventType.NOTE_ADDED,
        TimelineEventType.TAG_APPLIED,
        TimelineEventType.OWNERSHIP_CHANGED,
        TimelineEventType.ACCESS_CHANGED,
        TimelineEventType.CLIENT_ARCHIVED,
        TimelineEventType.CLIENT_RESTORED,
    }
)


# ─── Events the collaboration surface publishes (DDR-06) ─────────────────
#
# 🔒 Declared here rather than in `kernel.collaboration` for the same reason
# `ClientStageChanged` sits in `kernel.clients`: the publisher and the subscriber
# are in different modules and R3 forbids either importing the other. The event
# class is the shared vocabulary.
#
# ⚠️ Identifiers, enums and timestamps only (NFR-033) — `kernel.events` refuses
# anything else at registration. A note's body is deliberately absent: the
# subscriber writes "Note added", and anyone wanting the text reads the note
# under its own tenant scope and its own authorization.


@register_event("client.note_added")
@dataclass(frozen=True, slots=True)
class ClientNoteAdded(DomainEvent):
    """A practitioner wrote a note about a client — FR-M1-007.

    ⚠️ Only *adding* is a timeline event. An edit is not: FR-M3-020 puts edits in
    the audit log, and a timeline that grew a row every time somebody fixed a
    typo would bury the events that matter. An archive is not either — the note
    leaving the thread does not un-happen the fact that it was written.
    """

    client_id: uuid.UUID
    tenant_id: uuid.UUID
    note_id: uuid.UUID
    author_user_id: uuid.UUID
    created_at: datetime


@register_event("client.tags_changed")
@dataclass(frozen=True, slots=True)
class ClientTagsChanged(DomainEvent):
    """A tag was applied to or removed from a client — FR-M1-008.

    ⚠️ **One event for both directions**, and one summary ("Tags updated") for
    both. Naming the tag would put the practice's own vocabulary into a row that
    survives the tag being renamed or retired, and a timeline reading "PCOS
    applied" months after that tag was deleted is worse than one reading "Tags
    updated". The tag id is carried so a deep link still resolves when it exists.
    """

    client_id: uuid.UUID
    tenant_id: uuid.UUID
    tag_id: uuid.UUID
    #: True when applied, False when removed. An enum would be over-modelling a
    #: genuine binary — there is no third thing that can happen to a tag.
    attached: bool
    actor_user_id: uuid.UUID | None
    changed_at: datetime


@register_event("client.ownership_changed")
@dataclass(frozen=True, slots=True)
class ClientOwnershipChanged(DomainEvent):
    """The owning practitioner changed — EC-M1-04.

    🔒 Both ids are carried. "Who was this client moved away from" is the
    question a handover audit actually asks, and the timeline is where a
    practitioner looks for it first.
    """

    client_id: uuid.UUID
    tenant_id: uuid.UUID
    from_user_id: uuid.UUID
    to_user_id: uuid.UUID
    actor_user_id: uuid.UUID | None
    changed_at: datetime


@register_event("client.access_changed")
@dataclass(frozen=True, slots=True)
class ClientAccessChanged(DomainEvent):
    """A colleague was granted or lost access to a client — EC-M0-04, EC-M1-04.

    🔒 On the timeline rather than only in the audit log because it is a *care*
    fact, not just a security one: a practitioner reading a client's history
    needs to know when a colleague joined, or the notes from that period read as
    though they came from nowhere.
    """

    client_id: uuid.UUID
    tenant_id: uuid.UUID
    grantee_user_id: uuid.UUID
    #: True on grant, False on revoke.
    granted: bool
    actor_user_id: uuid.UUID | None
    changed_at: datetime
