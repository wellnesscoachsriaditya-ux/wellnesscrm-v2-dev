"""ORM models for the clinical workspace — DB §7.

🔒 These live in the module, not the kernel. ``clinical`` owns these tables
(DB §7: "Writers: `clinical`"), and a model in ``kernel.models`` would be a table
the kernel owns — which is what R6 exists to prevent.

⚠️ The FKs to ``tenants``, ``users`` and ``files`` are **not** an R6 violation:
those are kernel tables, and the kernel is the one thing every module may depend
on (Arch §3.1). ``client_id`` is the interesting case — see :class:`Measurement`.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.kernel import Base
from app.kernel.clients import DietaryClass, SexType
from app.kernel.clinical import (
    ActivityLevel,
    DefinitionStatus,
    GoalType,
    MeasurementSource,
    ResponseStatus,
)
from app.kernel.context import ActorType
from app.kernel.db import pg_enum


class AssessmentDefinition(Base):
    """A versioned assessment structure — DB §7.2, DDR-07, FR-M3-002.

    🔒 **The structure is data, not code.** That is the whole of FR-M3-002:
    "defined as versioned data, not as fixed application logic, so its structure
    can change without a code change or data migration". Adding a question is an
    INSERT of a new version.

    🔒 **A published definition is immutable** — enforced by
    ``kernel.clinical.assert_publishable`` and by the grant in migration 0016,
    which lets `app_user` UPDATE only `status` and `published_at`. That is what
    makes FR-M3-003 ("responses remain readable under the structure in force
    when they were captured") true rather than aspirational: the structure a
    response points at cannot move under it.

    ⚠️ **``tenant_id`` is nullable** — NULL means platform-authored, which is
    every definition at MVP. FR-M3-010 (fully custom tenant forms) is Phase 3;
    the column exists now so that arriving does not need a migration. This is
    the same nullable-tenant pattern DB §8 uses for the food catalogue.
    """

    __tablename__ = "assessment_definitions"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        comment="🔒 NULL = platform-authored. Phase 3 (FR-M3-010) fills it in.",
    )
    code: Mapped[str] = mapped_column(Text, nullable=False, comment="e.g. `nutrition_core`")
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="🔒 Monotonic per `code` (DB §7.2)"
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    schema: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        comment="Sections, fields, types, validation — parsed by kernel.clinical.parse_schema",
    )
    calculation_bindings: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        comment="🔒 DDR-08 — which fields project into client_nutrition_profile",
    )
    status: Mapped[DefinitionStatus] = mapped_column(
        pg_enum(DefinitionStatus, "definition_status"),
        nullable=False,
        server_default=DefinitionStatus.DRAFT.value,
    )
    published_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        # 🔒 DB §7.2. `tenant_id` is in the key so a tenant's future custom v1
        # cannot collide with the platform's `nutrition_core` v1.
        UniqueConstraint(
            "code", "version", "tenant_id", name="uq_assessment_definitions__code_version_tenant"
        ),
        Index("ix_assessment_definitions__code_status", "code", "status"),
        CheckConstraint("version > 0", name="ck_assessment_definitions__version_positive"),
        # 🔒 A published definition has a publication time. Without this a
        # version could be published with no record of when, and FR-M3-003's
        # "the structure in force when they were captured" becomes unanswerable.
        CheckConstraint(
            "(status <> 'published' OR published_at IS NOT NULL)",
            name="ck_assessment_definitions__published_timestamped",
        ),
    )


class AssessmentResponse(Base):
    """One administration of an assessment — DB §7.3, FR-M3-004/005/007.

    🔒 **``definition_id`` pins the version** and is never updated. That single
    FK is what makes both FR-M3-003 and EC-M3-03 true: a client mid-completion
    when v2 publishes finishes under v1, because the row already points at v1
    and nothing moves it.

    🔒 **Multiple responses per client are expected** (FR-M3-007) — there is
    deliberately no unique constraint on ``client_id``. Repeat administration is
    the feature; AC-M3-007 requires both to be viewable side by side.

    ⚠️ ``completed_by`` distinguishes FR-M3-004's two paths — the client via a
    link, or the practitioner on their behalf. It is not "who typed it" in a
    forensic sense; the audit log holds that.
    """

    __tablename__ = "assessment_responses"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        comment="🔒 RLS discriminator",
    )
    client_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        comment="🔒 Reference only, no FK — R6. Resolved via the ClientDirectory port.",
    )
    definition_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("assessment_definitions.id"),
        nullable=False,
        comment="🔒 Pins the structure version (FR-M3-003, EC-M3-03)",
    )
    answers: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        comment="Keyed by field id — DB §7.3",
    )
    status: Mapped[ResponseStatus] = mapped_column(
        pg_enum(ResponseStatus, "response_status"),
        nullable=False,
        server_default=ResponseStatus.IN_PROGRESS.value,
    )
    completed_sections: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'::text[]"),
        comment="🔒 Resumability (FR-M3-005) and explicit skipping (FR-M3-006)",
    )
    completed_by: Mapped[ActorType | None] = mapped_column(
        pg_enum(ActorType, "actor_type"),
        comment="Client or practitioner — FR-M3-004. NULL while in progress.",
    )
    started_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    completed_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        # 🔒 DB §7.3 — the trend read and "most recent completed assessment".
        Index(
            "ix_assessment_responses__client_completed",
            "client_id",
            text("completed_at DESC"),
        ),
        # Resume looks for the client's open response.
        Index("ix_assessment_responses__client_status", "client_id", "status"),
        # 🔒 A completed response has a completion time and a completer. Both
        # halves matter: FR-M3-007 compares administrations by date, and
        # FR-M3-004's two paths are only distinguishable if `completed_by` is set.
        CheckConstraint(
            "(status <> 'completed' OR (completed_at IS NOT NULL AND completed_by IS NOT NULL))",
            name="ck_assessment_responses__completion_recorded",
        ),
    )


class ClientNutritionProfile(Base):
    """The typed projection of an assessment — DB §7.4, DDR-08.

    🔒 **The bridge between flexible assessment and deterministic calculation**,
    and the reason DDR-07's ``jsonb`` trade-off is acceptable. `nutrition` and
    `ai_drafting` read these columns through a kernel port and never parse an
    assessment document — which is what stops assessment-schema knowledge
    leaking into the nutrition module and coupling them permanently.

    🔒 **One row per client** (UNIQUE on ``client_id``): this is the *current*
    profile, not a history. The history is the assessment responses themselves,
    each of which keeps its own answers; ``source_response_id`` says which one
    produced this.

    ⚠️ **``allergen_food_ids`` is safety-critical** (FR-M5-006). It feeds the AI
    candidate-set filter (ADR-10), and DB §7.4 is explicit that it must be
    populated by explicit food selection and **never by free-text parsing** — a
    missed allergen is a clinical incident. `kernel.clinical` enforces the
    `food_ref` field type for exactly this reason.
    """

    __tablename__ = "client_nutrition_profile"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        comment="🔒 RLS discriminator",
    )
    client_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        comment="🔒 Reference only, no FK — R6. Resolved via the ClientDirectory port.",
    )
    source_response_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("assessment_responses.id"),
        comment="Provenance — which administration produced this",
    )

    date_of_birth: Mapped[date | None] = mapped_column(Date)
    sex: Mapped[SexType | None] = mapped_column(pg_enum(SexType, "sex_type"))
    height_cm: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    activity_level: Mapped[ActivityLevel | None] = mapped_column(
        pg_enum(ActivityLevel, "activity_level")
    )
    primary_goal: Mapped[GoalType | None] = mapped_column(pg_enum(GoalType, "goal_type"))
    dietary_class: Mapped[DietaryClass | None] = mapped_column(
        pg_enum(DietaryClass, "dietary_class"),
        comment="🔒 Mirrors clients.dietary_class (DB §7.4)",
    )
    excludes_onion_garlic: Mapped[bool] = mapped_column(
        nullable=False,
        server_default=text("false"),
        comment="🔒 Jain/observant — PRD §9.9",
    )
    excludes_root_vegetables: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    allergen_food_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)),
        nullable=False,
        server_default=text("'{}'::uuid[]"),
        comment="🔒 Safety-critical (FR-M5-006). Food ids only, never parsed prose.",
    )
    excluded_food_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    fasting_patterns: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    staple_grain: Mapped[str | None] = mapped_column(Text)
    region_cuisine: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("client_id", name="uq_client_nutrition_profile__client"),
        Index("ix_client_nutrition_profile__tenant", "tenant_id"),
    )


class Measurement(Base):
    """A dated body measurement — DB §7.5, FR-M3-011…015.

    🔒 **BMI and waist-hip ratio are absent, deliberately** (FR-M3-012). They are
    derived on read by ``kernel.clinical``. Storing them would create a second
    source of truth that drifts the moment a weight is corrected.

    🔒 **No uniqueness on ``(client_id, measured_on)``** — EC-M3-05 permits a
    practitioner and a client to record different weights on the same date, and
    requires both to be retained with source attribution. The display-precedence
    rule lives in ``kernel.clinical.display_precedence``, not in a constraint.

    ⚠️ ``is_flagged_implausible`` records EC-M3-02's outcome: the value was
    outside the plausible range, the person confirmed it, and it was stored
    anyway. It is a prompt for practitioner review, never a refusal — a real
    180 kg client exists, and a system that will not record them is useless
    exactly when the record matters most.
    """

    __tablename__ = "measurements"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        comment="🔒 RLS discriminator",
    )
    client_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        comment="🔒 Reference only, no FK — R6. Resolved via the ClientDirectory port.",
    )
    measured_on: Mapped[date] = mapped_column(Date, nullable=False)
    weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    height_cm: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    waist_cm: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    hip_cm: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    body_fat_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(4, 1), comment="Practitioner-entered; device-dependent"
    )
    source: Mapped[MeasurementSource] = mapped_column(
        pg_enum(MeasurementSource, "measurement_source"), nullable=False
    )
    recorded_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id")
    )
    is_flagged_implausible: Mapped[bool] = mapped_column(
        nullable=False,
        server_default=text("false"),
        comment="EC-M3-02 — accepted after confirmation, flagged for review",
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        # 🔒 DB §7.5 — the trend read (FR-M3-014).
        Index("ix_measurements__client_date", "client_id", text("measured_on DESC")),
        # 🔒 DB §7.5's CHECK bounds. `kernel.clinical.assert_storable` refuses
        # the same values earlier and names the field; this is what holds if a
        # future caller forgets to ask.
        CheckConstraint(
            "weight_kg IS NULL OR (weight_kg >= 2 AND weight_kg <= 500)",
            name="ck_measurements__weight_range",
        ),
        CheckConstraint(
            "height_cm IS NULL OR (height_cm >= 30 AND height_cm <= 275)",
            name="ck_measurements__height_range",
        ),
        CheckConstraint(
            "body_fat_pct IS NULL OR (body_fat_pct >= 0 AND body_fat_pct <= 75)",
            name="ck_measurements__body_fat_range",
        ),
        # 🔒 A row recording nothing is not a measurement. Without this, an empty
        # submit creates a dated row that appears on the trend as a gap with a
        # date, which reads as data loss rather than as a mistake.
        CheckConstraint(
            "weight_kg IS NOT NULL OR height_cm IS NOT NULL OR waist_cm IS NOT NULL"
            " OR hip_cm IS NOT NULL OR body_fat_pct IS NOT NULL",
            name="ck_measurements__at_least_one_value",
        ),
    )


class ConsultationNote(Base):
    """A dated note from a consultation — DB §7.6, FR-M3-018…021.

    🔒 **Never client-visible** (FR-M3-021, AC-M3-006). Enforced the same way
    ``client_notes`` is: by the *absence* of a client-realm RLS policy, which is
    stronger than a condition because there is no clause to get wrong. Migration
    0016 grants the client role nothing on this table at all.

    ⚠️ Distinct from ``client_notes`` (S2 Slice C), and the distinction is real:
    a client note is a working annotation on the record, while this is the
    record of a consultation that happened on a date and may be tied to an
    appointment. FR-M3-018 asks for the latter specifically. Merging them would
    mean either giving every quick note a consultation date or losing the link
    to the appointment.

    ⏳ ``appointment_id`` is nullable and unconstrained until M6 (S8) creates the
    table. A FK to a table that does not exist cannot be declared; the column is
    here now so that arriving is a constraint, not a migration of existing rows.
    """

    __tablename__ = "consultation_notes"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        comment="🔒 RLS discriminator",
    )
    client_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        comment="🔒 Reference only, no FK — R6. Resolved via the ClientDirectory port.",
    )
    appointment_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), comment="⏳ FK deferred until M6 creates `appointments`"
    )
    note_date: Mapped[date] = mapped_column(Date, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    structured: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="🟡 PROPOSED structured mode — FR-M3-019"
    )
    author_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    archived_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        Index("ix_consultation_notes__client_date", "client_id", text("note_date DESC")),
        CheckConstraint("length(btrim(body)) > 0", name="ck_consultation_notes__body_present"),
    )


class ClientDocument(Base):
    """A document attached to a client — DB §7.6, FR-M3-024…027.

    🔒 **Metadata only.** The bytes live in object storage and never pass
    through this process (ADR-12); ``file_id`` points at the ``files`` row that
    is both the authorization record and the index DPDP erasure traverses
    (Arch §13.2).

    ⚠️ ``uploaded_by`` is the *realm*, not the user — FR-M3-025 makes client
    upload a first-class path, and "did the client or the practice put this
    here" is the question the UI asks. The user is on the ``files`` row.
    """

    __tablename__ = "client_documents"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        comment="🔒 RLS discriminator",
    )
    client_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        comment="🔒 Reference only, no FK — R6. Resolved via the ClientDirectory port.",
    )
    file_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("files.id"),
        nullable=False,
        comment="🔒 The bytes, the authorization record, and the erasure index",
    )
    document_type: Mapped[str] = mapped_column(
        Text, nullable=False, comment="A label — lab report, prescription, photo"
    )
    document_date: Mapped[date | None] = mapped_column(Date)
    uploaded_by: Mapped[ActorType] = mapped_column(
        pg_enum(ActorType, "actor_type"), nullable=False, comment="Practitioner or client realm"
    )
    description: Mapped[str | None] = mapped_column(Text)
    archived_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        Index("ix_client_documents__client_created", "client_id", text("created_at DESC")),
        # One document row per file. Without this a retry could attach the same
        # object twice and the list would show a duplicate the practitioner
        # cannot tell apart.
        UniqueConstraint("file_id", name="uq_client_documents__file"),
        CheckConstraint(
            "length(btrim(document_type)) > 0", name="ck_client_documents__type_present"
        ),
    )
