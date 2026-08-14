"""ORM models for the messaging schema — DB §11.

🔒 These live in the module, not the kernel. `messaging` owns all five tables
(DB §11: "Owner: `messaging`"), and a model in `kernel.models` would be a table
the kernel owns — which is what R6 exists to prevent one module doing to another.

⚠️ FKs to `tenants` and `users` are not R6 violations: those are kernel tables,
the spine every tenant-scoped row descends from (Arch §3.1).

🔒 **`client_id` carries no foreign key anywhere in this schema.** `clients`
belongs to another module (DB §5) and R6 forbids referencing its tables — a FK
couples the two schemas in DDL, where the import checker cannot see it.
Resolution goes through the `ClientDirectory` port (Arch §3.4b), exactly as
`enquiry_submissions.client_id` does. The consequence readers must handle: a
`client_id` can dangle after a DPDP erasure (FR-M0-027), and the delivery log
deliberately outlives the client — "did we message this person, and when" must
stay answerable to a regulator after the person is gone.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.kernel import Base
from app.kernel.clinical import DefinitionStatus
from app.kernel.db import pg_enum
from app.kernel.messaging import (
    CheckinFrequency,
    DispatchStatus,
    MessageCategory,
    ProviderTemplateStatus,
    ScheduledState,
    SuppressionReason,
)

# ⚠️ `TransportType` from `kernel.models`, not `kernel.messaging`. Both exist and
# they are deliberately different: the `models` one maps the PostgreSQL
# `transport_type` enum and therefore includes `sms`, a value the database has
# held since 0002 and `magic_links.issued_via` can still carry. The messaging
# one is the *engine's* narrower vocabulary (approved proposal #7 keeps SMS off
# the critical path). A column mapped to the narrow enum would raise `LookupError`
# on reading a legacy row rather than simply never writing one.
from app.kernel.models import TransportType


class MessageTemplate(Base):
    """One version of one message type — DB §11.1, FR-M8-002.

    🔒 **Platform-owned: no `tenant_id`.** DB §11.1 is explicit that
    practitioner-editable wording is Phase 2 (FR-M8-029). What a practitioner
    controls at MVP is *whether* a type is sent (FR-M8-027), which lives in
    :class:`NotificationPreference` — a separation that keeps one tenant from
    changing the words another tenant's clients receive.

    🔒 **Versioned, and a new version is a new row.** A `scheduled_messages` row
    points at the template id it was created against, and `message_dispatches`
    denormalises the version actually used. Editing in place would rewrite what
    the delivery log claims was sent.

    ⚠️ RLS is enabled with a read-all SELECT policy and no write policy
    (migration 0021), and `app_user` holds SELECT only. Both halves matter: the
    grant stops the application writing, and the absent write policy means even
    a restored grant could not.
    """

    __tablename__ = "message_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    code: Mapped[str] = mapped_column(Text, nullable=False, comment="e.g. plan_delivered")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    category: Mapped[MessageCategory] = mapped_column(
        pg_enum(MessageCategory, "message_category"), nullable=False
    )
    #: 🔒 DB §11.6 — exempt from quota and frequency, declared once on the
    #: template rather than decided per send. A runtime special case is a rule
    #: that holds in the call site that remembers it.
    is_essential: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    #: EC-M8-09 — which message wins when the frequency cap arbitrates. Lower
    #: sorts first, matching `jobs.priority`.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    #: 🔒 EC-M8-05. NULL = never stale.
    staleness_tolerance_minutes: Mapped[int | None] = mapped_column(Integer)
    #: Typed variable declarations: ``{name: {"type": ..., "required": bool}}``.
    #: 🔒 Declared rather than inferred, so a template asking for a value nobody
    #: supplies fails at scheduling — in the request that caused it — instead of
    #: rendering "Hi {client_name}" to a real person.
    variables: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    body_template: Mapped[str] = mapped_column(Text, nullable=False)
    default_transport: Mapped[TransportType] = mapped_column(
        pg_enum(TransportType, "transport_type"), nullable=False
    )
    #: 🔒 The name Meta approved this template under. Required for a WhatsApp
    #: template (`ck_message_templates__whatsapp_needs_provider_name`), absent
    #: for one that goes out over email.
    provider_template_name: Mapped[str | None] = mapped_column(Text)
    #: 🔒 EC-M8-03 — per template, so one revocation pauses one message type.
    provider_template_status: Mapped[ProviderTemplateStatus] = mapped_column(
        pg_enum(ProviderTemplateStatus, "provider_template_status"),
        nullable=False,
        server_default="pending",
    )
    #: FR-M8-027. ⚠️ Mutually exclusive with ``is_essential`` at the database
    #: level: a disableable magic link would let a tenant-wide toggle lock every
    #: client out of their own portal.
    is_practitioner_disableable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    status: Mapped[DefinitionStatus] = mapped_column(
        pg_enum(DefinitionStatus, "definition_status"), nullable=False, server_default="draft"
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("code", "version", name="uq_message_templates__code_version"),
        CheckConstraint("version > 0", name="ck_message_templates__version_positive"),
        CheckConstraint("priority > 0", name="ck_message_templates__priority_positive"),
        CheckConstraint(
            "length(btrim(code)) > 0 AND code ~ '^[a-z][a-z0-9_]{2,62}$'",
            name="ck_message_templates__code_shape",
        ),
        CheckConstraint(
            "staleness_tolerance_minutes IS NULL OR staleness_tolerance_minutes > 0",
            name="ck_message_templates__staleness_positive",
        ),
        CheckConstraint(
            "length(btrim(body_template)) > 0", name="ck_message_templates__body_present"
        ),
        CheckConstraint(
            "default_transport <> 'whatsapp' OR provider_template_name IS NOT NULL",
            name="ck_message_templates__whatsapp_needs_provider_name",
        ),
        CheckConstraint(
            "NOT (is_essential AND is_practitioner_disableable)",
            name="ck_message_templates__essential_not_disableable",
        ),
        Index("ix_message_templates__code_status", "code", "status"),
    )


class ScheduledMessage(Base):
    """🔒 The queue of intent — DB §11.2. What is *due*, before any send.

    🔒 **Every module that needs a message creates one of these** (FR-M8-001).
    No module owns a scheduler, a template store or a transport; the whole of
    "message M to recipient R at time T" is this row plus the dispatch engine
    that drains it.

    🔒 **Suppression is not evaluated here.** A client's stage, consent or
    tenant status can change between scheduling and sending, so
    ``suppression_reason`` is written by the dispatch path when it refuses to
    send — never at creation. DB §11.5 gives the example this design exists for:
    a check-in scheduled Monday for Friday must be suppressed if the client is
    paused on Wednesday.
    """

    __tablename__ = "scheduled_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        comment="🔒 RLS discriminator",
    )
    #: 🔒 Reference only, no foreign key — R6. NULL for a practitioner-directed
    #: message such as the new-lead notification.
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), comment="🔒 Reference only, no FK — R6"
    )
    #: Set when the recipient is a practitioner rather than a client.
    recipient_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id")
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("message_templates.id"), nullable=False
    )
    #: ⚠️ Identifiers and short display strings only (NFR-033). This row is
    #: retained, backed up and read by operators; a clinical value here would be
    #: copied into a store with different retention rules from the one it came
    #: from — the same constraint that governs `jobs.payload`.
    template_variables: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    scheduled_for: Mapped[datetime] = mapped_column(nullable=False)
    state: Mapped[ScheduledState] = mapped_column(
        pg_enum(ScheduledState, "scheduled_state"), nullable=False, server_default="pending"
    )
    #: 🔒 AC-M8-004 — retained with its reason, never deleted.
    suppression_reason: Mapped[SuppressionReason | None] = mapped_column(
        pg_enum(SuppressionReason, "suppression_reason")
    )
    #: 🔒 DB §11.4 — enforced by `uq_scheduled_messages__idempotency`, not by a
    #: read-then-write. See `kernel.messaging.build_idempotency_key`.
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: Provenance, and the handle EC-M8-08 cancels by.
    source_module: Mapped[str] = mapped_column(Text, nullable=False)
    source_record_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    #: 🔒 AC-M8-006's audit trail — the instant this was originally due, kept
    #: when quiet hours moved it. Without it, "why did this arrive at 08:00?"
    #: has no answer in the data.
    deferred_from: Mapped[datetime | None] = mapped_column()
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: When the row reached a terminal state. Read by the pending-messages view
    #: (FR-M8-028) and by the staleness report.
    resolved_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_scheduled_messages__idempotency"),
        CheckConstraint(
            "client_id IS NOT NULL OR recipient_user_id IS NOT NULL",
            name="ck_scheduled_messages__recipient_present",
        ),
        CheckConstraint(
            "(state = 'suppressed') = (suppression_reason IS NOT NULL)",
            name="ck_scheduled_messages__suppression_reason",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_scheduled_messages__attempts"),
        CheckConstraint(
            "length(btrim(source_module)) > 0", name="ck_scheduled_messages__source_present"
        ),
        Index("ix_scheduled_messages__source", "tenant_id", "source_module", "source_record_id"),
    )


class MessageDispatch(Base):
    """🔒 The immutable delivery log — DB §11.3, FR-M8-003.

    One row per *attempt*, not per message. A retry writes a second row with
    ``attempt_number = 2``; the first is never rewritten, because "we tried and
    it failed, then we tried again" is the history AC-M8-007 makes visible.

    🔒 **Append-only, enforced by grant** (migration 0021). `app_user` holds
    SELECT, INSERT and a column-level UPDATE on the fields a provider delivery
    receipt moves — status, provider id, failure detail, timestamps, cost. The
    attempt's identity is unreachable to the application, so a webhook can
    advance a status but nothing can change which template went where.

    ⚠️ ``recipient_address`` is contact data (EC-M8-08 requires it), so this
    table is subject to the tenant's retention policy like any other client
    record.
    """

    __tablename__ = "message_dispatches"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        comment="🔒 RLS discriminator",
    )
    #: 🔒 Reference only, no foreign key — R6, and it must survive the client's
    #: erasure so "on what basis did we contact this number" stays answerable.
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), comment="🔒 Reference only, no FK — R6"
    )
    #: NULL for an immediate send that never queued (DB §11.3).
    scheduled_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("scheduled_messages.id")
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("message_templates.id"), nullable=False
    )
    #: 🔒 Denormalised on purpose (DDR-11's argument): the version actually used.
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    transport: Mapped[TransportType] = mapped_column(
        pg_enum(TransportType, "transport_type"), nullable=False
    )
    recipient_address: Mapped[str] = mapped_column(Text, nullable=False, comment="🔒 EC-M8-08")
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[DispatchStatus] = mapped_column(
        pg_enum(DispatchStatus, "dispatch_status"), nullable=False, server_default="queued"
    )
    #: A stable machine code — `provider_rejected`, `no_address`, `transport_error`.
    #: ⚠️ Never the provider's raw string: that goes in ``failure_reason``.
    failure_code: Mapped[str | None] = mapped_column(Text)
    #: ⚠️ Operator-facing. Must never be shown to a client, and must never carry
    #: a clinical value — it is the field most likely to be echoed wholesale from
    #: a provider response (NFR-033).
    failure_reason: Mapped[str | None] = mapped_column(Text)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    #: 🔒 NFR-088 — per-tenant messaging cost, when the provider reports one.
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    sent_at: Mapped[datetime | None] = mapped_column()
    delivered_at: Mapped[datetime | None] = mapped_column()
    read_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("attempt_number > 0", name="ck_message_dispatches__attempt_positive"),
        CheckConstraint("template_version > 0", name="ck_message_dispatches__version_positive"),
        CheckConstraint(
            "length(btrim(recipient_address)) > 0", name="ck_message_dispatches__address_present"
        ),
        CheckConstraint(
            "status NOT IN ('failed', 'rejected') OR failure_code IS NOT NULL",
            name="ck_message_dispatches__failure_coded",
        ),
        CheckConstraint(
            "cost_amount IS NULL OR cost_amount >= 0",
            name="ck_message_dispatches__cost_non_negative",
        ),
    )


class CheckinSchedule(Base):
    """A client's recurring check-in cadence — DB §11.7, FR-M8-022…025.

    🔒 **A schedule generates `scheduled_messages`; it does not send.** One
    engine, one dispatch path. A schedule that sent directly would be the second
    scheduler M8.3 forbids, and its messages would bypass every suppression rule.

    🔒 **Stops automatically when the client leaves `active`** (FR-M8-025),
    driven by the ``ClientStageChanged`` event rather than a nightly
    reconciliation — a client paused on Wednesday must not be nudged on Friday.
    """

    __tablename__ = "checkin_schedules"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        comment="🔒 RLS discriminator",
    )
    #: 🔒 Reference only, no foreign key — R6.
    client_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, comment="🔒 Reference only, no FK — R6"
    )
    frequency: Mapped[CheckinFrequency] = mapped_column(
        pg_enum(CheckinFrequency, "checkin_frequency"), nullable=False, server_default="weekly"
    )
    #: ISO weekday, Monday = 1 … Sunday = 7 — matching PostgreSQL's `isodow` and
    #: Python's `isoweekday()`, so no translation table exists to get wrong.
    #: 🟡 FR-M8-023 defaults it to the weekday the client became active.
    day_of_week: Mapped[int | None] = mapped_column(SmallInteger)
    time_of_day: Mapped[time] = mapped_column(Time, nullable=False, server_default="09:00")
    #: 🔒 FR-M8-024 — pausable without touching the lifecycle stage.
    is_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    paused_at: Mapped[datetime | None] = mapped_column()
    #: The occurrence already turned into a `scheduled_messages` row. 🔒 The
    #: generator's idempotency handle: re-running for the same date produces the
    #: same idempotency key and the unique constraint refuses the duplicate.
    last_generated_for: Mapped[date | None] = mapped_column(Date)
    next_due_on: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("tenant_id", "client_id", name="uq_checkin_schedules__client"),
        CheckConstraint(
            "day_of_week IS NULL OR (day_of_week BETWEEN 1 AND 7)",
            name="ck_checkin_schedules__day_of_week",
        ),
        CheckConstraint(
            "is_paused = (paused_at IS NOT NULL)", name="ck_checkin_schedules__pause_timestamped"
        ),
    )


class NotificationPreference(Base):
    """Who may be messaged, how often, and when — DB §11.8.

    🔒 **Two levels.** A row with ``client_id IS NULL`` is the tenant default
    (FR-M8-027); a row naming a client is that client's override (US-M8-06).
    ``template_code IS NULL`` means "every message type". Precedence is resolved
    in ``modules.messaging.preferences``, in one place, because a rule applied
    differently by two callers is indistinguishable from no rule.

    🔒 **A client unsubscribe writes here *and* to the consent ledger.** The
    preference controls behaviour; the ledger is the legal record. Writing only
    one of them leaves either a client who still receives messages or a
    withdrawal with no lawful-basis trail.
    """

    __tablename__ = "notification_preferences"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        comment="🔒 RLS discriminator",
    )
    #: 🔒 Reference only, no foreign key — R6. NULL = the tenant default.
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), comment="🔒 Reference only, no FK — R6"
    )
    #: ⚠️ A template *code*, not a template id. A preference must survive a new
    #: version of the same message type; keyed on the id it would silently
    #: un-mute whatever the practitioner had turned off.
    template_code: Mapped[str | None] = mapped_column(Text)
    transport: Mapped[TransportType | None] = mapped_column(
        pg_enum(TransportType, "transport_type")
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    #: 🟡 FR-M8-009 — NULL inherits: client → tenant → `kernel.messaging`'s
    #: 21:00–08:00 default.
    quiet_hours_start: Mapped[time | None] = mapped_column(Time)
    quiet_hours_end: Mapped[time | None] = mapped_column(Time)
    #: FR-M8-008. NULL inherits the same way.
    max_messages_per_week: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "max_messages_per_week IS NULL OR max_messages_per_week >= 0",
            name="ck_notification_preferences__weekly_limit",
        ),
        CheckConstraint(
            "(quiet_hours_start IS NULL) = (quiet_hours_end IS NULL)",
            name="ck_notification_preferences__quiet_hours_paired",
        ),
        CheckConstraint(
            "template_code IS NULL OR length(btrim(template_code)) > 0",
            name="ck_notification_preferences__template_code_present",
        ),
    )
