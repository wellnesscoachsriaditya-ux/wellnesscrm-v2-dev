"""ORM models for the client spine — DB §5.1, §5.3.

🔒 These live in the module, not the kernel. ``clients`` owns these tables
(DB §5: "Writers: `clients` only"), and a model in ``kernel.models`` would be a
table the kernel owns — which is what R6 exists to prevent one module doing to
another.

⚠️ The FKs to ``tenants`` and ``users`` are **not** an R6 violation. Those are
kernel tables, and the kernel is the one thing every module may depend on
(Arch §3.1) — the spine every tenant-scoped row descends from. R6 forbids
referencing another *module's* tables.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.kernel import Base
from app.kernel.clients import ClientStage, DietaryClass, SexType
from app.kernel.clients import is_minor as derive_is_minor
from app.kernel.collaboration import TagColour
from app.kernel.db import pg_enum
from app.kernel.timeline import TimelineActorType, TimelineEventType


class Client(Base):
    """A person the practice is engaged with — lead or client, one row.

    🔒 **The spine** (DB §5.1). M1.3 — leads and clients are one entity
    distinguished by ``stage``, which is "the single most important modelling
    decision in the schema". Converting a lead changes a column; it does not move
    a record, which is why AC-M1-003 (identity and history retained) is true by
    construction.

    **Pattern A RLS**, forced. Read on every tenant-facing path.

    🔒 **Soft delete only** (FR-M1-010): ``archived_at``, never a DELETE.
    Migration 0009 revokes the privilege. FR-M1-011 puts hard deletion behind the
    DPDP erasure pathway, which runs as the migrator role.

    ⚠️ **No unique constraint on ``mobile``** (DB §5.1, EC-M1-01). Family members
    sharing a handset is a real and common pattern in the launch market;
    duplicate detection is a warning (FR-M1-024), never a constraint.
    """

    __tablename__ = "clients"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        comment="🔒 RLS discriminator",
    )
    stage: Mapped[ClientStage] = mapped_column(
        pg_enum(ClientStage, "client_stage"),
        nullable=False,
        server_default="lead",
        comment="🔒 M1.3 — lead and client are one entity, separated by this",
    )
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    mobile: Mapped[str | None] = mapped_column(
        Text, comment="🔒 E.164 (NFR-100). Not unique — EC-M1-01"
    )
    email: Mapped[str | None] = mapped_column(Text)
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    sex: Mapped[SexType | None] = mapped_column(pg_enum(SexType, "sex_type"))
    city: Mapped[str | None] = mapped_column(Text)
    preferred_language: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="en", comment="NFR-096"
    )
    source: Mapped[str | None] = mapped_column(Text, comment="FR-M2-009")
    source_detail: Mapped[str | None] = mapped_column(Text, comment="Link parameter")
    owner_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="FR-M1-009"
    )
    dietary_class: Mapped[DietaryClass | None] = mapped_column(
        pg_enum(DietaryClass, "dietary_class"),
        comment="🔒 Drives plan filtering (FR-M4-035)",
    )
    # 🔒 FR-M0-028 — minor status is NOT a column. See `is_minor` below.
    activated_at: Mapped[datetime | None] = mapped_column(
        comment="First entry to active — the check-in anchor (FR-M8-023)"
    )
    archived_at: Mapped[datetime | None] = mapped_column(comment="Soft delete (FR-M1-010)")
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    # ⚠️ 🔒 **`search_vector` is deliberately NOT mapped here.** Migration 0009
    # adds it with a raw `ALTER TABLE ... ADD COLUMN ... GENERATED ALWAYS AS`,
    # because SQLAlchemy cannot express a generated tsvector in a column
    # definition. Mapping it anyway would break the model↔migration parity that
    # `tests/test_kernel_schema.py` enforces — that test reads `op.create_table`
    # and cannot see an ALTER, so a mapped column would look like drift.
    #
    # Weakening the test to accommodate one column is the wrong trade: it is the
    # only thing keeping a hand-written migration in step with the models. The
    # search predicate therefore names the column explicitly — see
    # `discovery._SEARCH_VECTOR`.

    @property
    def is_minor(self) -> bool | None:
        """🔒 FR-M0-028 — derived, never stored.

        DB §5.1 specifies a generated column here. It is not implementable —
        PostgreSQL requires generation expressions to be IMMUTABLE and age is
        not — and a stored flag would be silently wrong the day a client turns
        18, which is the day it matters most. See
        :func:`app.kernel.clients.is_minor`.
        """
        return derive_is_minor(self.date_of_birth)

    __table_args__ = (
        # 🔒 FR-M1-004 / EC-M1-08 — at least one way to reach them.
        CheckConstraint(
            "mobile IS NOT NULL OR email IS NOT NULL", name="ck_clients__contact_present"
        ),
        CheckConstraint(
            "mobile IS NULL OR mobile ~ '^\\+[1-9][0-9]{7,14}$'",
            name="ck_clients__mobile_e164",
        ),
        CheckConstraint(
            "stage <> 'active' OR owner_user_id IS NOT NULL", name="ck_clients__stage_owner"
        ),
        CheckConstraint(
            "activated_at IS NOT NULL OR stage <> 'active'",
            name="ck_clients__active_has_activated_at",
        ),
        # ⚠️ The partial indexes and the generated `search_vector` are created in
        # migration 0009 with raw SQL — SQLAlchemy cannot express a `GENERATED
        # ALWAYS AS ... STORED` tsvector or a `gin_trgm_ops` operator class in a
        # column definition. The migration is authoritative for those.
        Index("ix_clients__tenant_id", "tenant_id"),
    )


class ClientStageHistory(Base):
    """🔒 Every lifecycle transition, append-only — DB §5.3, FR-M1-015.

    🔒 **Separate from ``audit_log``, deliberately.** This is queryable domain
    history: it feeds the timeline (FR-M1-018) and conversion metrics
    (FR-M9-006). The audit log is compliance evidence with different retention
    and immutability rules, and conflating the two would force compliance-grade
    retention onto operational data.

    ``app_user`` holds INSERT and SELECT only (migration 0009), and the table is
    registered in ``ops/db/002_verify_grants.sql``.
    """

    __tablename__ = "client_stage_history"

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
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_stage: Mapped[ClientStage | None] = mapped_column(
        pg_enum(ClientStage, "client_stage"), comment="NULL on creation"
    )
    to_stage: Mapped[ClientStage] = mapped_column(
        pg_enum(ClientStage, "client_stage"), nullable=False
    )
    changed_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), comment="NULL when system-driven"
    )
    reason: Mapped[str | None] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        Index("ix_client_stage_history__tenant_id", "tenant_id"),
        CheckConstraint(
            "from_stage IS NULL OR from_stage <> to_stage",
            name="ck_client_stage_history__actual_transition",
        ),
    )


class ClientNote(Base):
    """A practitioner's free-text note about a client — DB §5.4, FR-M1-007.

    🔒 **Never client-visible** (FR-M3-021). Enforced three ways, and the
    redundancy is the point: no client-realm route reaches it, the authorization
    actions permit practitioner roles only, and migration 0011 gives the table
    **no client-realm RLS policy at all** — DB §17.1 notes that an absent policy
    is stronger than a condition in one, because there is nothing to write
    wrongly.

    🔒 Author-editable (FR-M3-020). ``author_user_id`` is NOT NULL because it is
    what that rule keys off; an unattributed note is one nobody can be asked
    about. See ``kernel.collaboration.assert_may_edit_note``.
    """

    __tablename__ = "client_notes"

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
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    author_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
        comment="🔒 FR-M3-020 — only this user may edit the body",
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    archived_at: Mapped[datetime | None] = mapped_column(comment="Soft delete (DB §22.2)")

    __table_args__ = (
        CheckConstraint("length(btrim(body)) > 0", name="ck_client_notes__body_not_blank"),
        Index("ix_client_notes__tenant_id", "tenant_id"),
    )


class Tag(Base):
    """A label in the tenant's own vocabulary — DB §5.4, FR-M1-008.

    🔒 Tenant-scoped and practitioner-defined. Uniqueness is **case-insensitive**
    and lives in migration 0011 as a partial unique index on
    ``(tenant_id, lower(name))``: SQLAlchemy cannot express a functional partial
    index in a column definition, so the migration is authoritative for it.
    ``kernel.collaboration.tag_match_key`` computes the same value.
    """

    __tablename__ = "tags"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        comment="🔒 RLS discriminator",
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    colour: Mapped[TagColour] = mapped_column(
        pg_enum(TagColour, "tag_colour"),
        nullable=False,
        server_default="slate",
        comment="🔒 A closed palette, not free hex — ADR-03 and NFR-060",
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    archived_at: Mapped[datetime | None] = mapped_column(comment="Soft delete (DB §22.2)")

    __table_args__ = (
        CheckConstraint("length(btrim(name)) > 0", name="ck_tags__name_not_blank"),
        CheckConstraint("length(name) <= 40", name="ck_tags__name_length"),
    )


class ClientTag(Base):
    """The junction — DB §5.4.

    ⚠️ Untagging is a real DELETE, not a soft delete. The row records no event:
    it is the assertion "this client carries this label", and once withdrawn
    there is nothing to keep. A tombstone would also complicate the primary key
    that makes "is this client tagged X" a single lookup.
    """

    __tablename__ = "client_tags"

    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        comment="🔒 RLS discriminator",
    )
    client_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tag_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tagged_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    tagged_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id")
    )

    __table_args__ = (Index("ix_client_tags__tenant_id", "tenant_id"),)


class ClientAssignment(Base):
    """An *additional* practitioner granted access to a client — DB §5.5, EC-M0-04.

    🔒 **Additional only.** The owning practitioner is ``clients.owner_user_id``
    and stays the single answer to "whose client is this". One owner, N grants is
    what keeps accountability unambiguous while supporting shared care.

    🔒 **Revoked, never deleted.** EC-M1-04 requires assignment history to survive
    a practitioner leaving — "who could see this client last March" is a question
    a DPDP access request can ask, and a deleted row cannot answer it. That is
    why ``granted_at`` is part of the primary key: one pair legitimately recurs
    over time, and the two-column key DB §5.5 sketches would refuse a re-grant
    after a revoke.

    A partial unique index in migration 0011 keeps at most **one live grant** per
    pair — two would make a revoke appear to do nothing.
    """

    __tablename__ = "client_assignments"

    client_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    granted_at: Mapped[datetime] = mapped_column(
        primary_key=True,
        server_default=text("now()"),
        comment="🔒 Part of the key — see the class docstring",
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        comment="🔒 RLS discriminator",
    )
    granted_by_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column()
    revoked_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id")
    )

    __table_args__ = (
        CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by_user_id IS NULL)",
            name="ck_client_assignments__revocation_complete",
        ),
        Index("ix_client_assignments__tenant_id", "tenant_id"),
    )


class TimelineEvent(Base):
    """One entry in a client's unified timeline — DB §5.6, FR-M1-018.

    🔒 **A materialised projection, written by event subscribers** (DDR-06). The
    alternative — a UNION across every module's tables at read time — violates R6
    and grows a join per new event type, which is how NFR-006's 800 ms budget
    gets spent. Here a timeline load is one indexed query regardless of how many
    modules feed it.

    ⚠️ **Derived, never a source of truth.** Every row restates something a
    module already owns. That is what makes the table safe to rebuild if it
    drifts, and it is why nothing reads *from* here to make a decision.

    🔒 **Append-only in practice, and by grant.** Migration 0012 revokes UPDATE
    and DELETE from ``app_user``: a timeline is a history, and a history that can
    be rewritten by the application is not one. An edited note appends a new row
    rather than revising the old one — the fact that something *was* recorded and
    later changed is usually the interesting part.

    🔒 ``summary`` **must not contain clinical values** (DB §5.6, NFR-033). It is
    written only by :func:`app.kernel.timeline.summarise` and its stage-aware
    sibling, both of which take enums and return fixed labels. The check
    constraint bounds the length; the function signature is what actually keeps
    prose out.
    """

    __tablename__ = "timeline_events"

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
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[TimelineEventType] = mapped_column(
        pg_enum(TimelineEventType, "timeline_event_type"),
        nullable=False,
        comment="🔒 Filtering — FR-M1-019",
    )
    occurred_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    source_module: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Provenance — which module produced this"
    )
    source_record_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        comment="Deep link to the underlying record. NULL when it has none.",
    )
    summary: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="🔒 Non-clinical label only — DB §5.6",
    )
    actor_type: Mapped[TimelineActorType] = mapped_column(
        pg_enum(TimelineActorType, "timeline_actor_type"), nullable=False
    )
    #: ⚠️ No FK to ``users``. An actor may be a client or the system, neither of
    #: which has a ``users`` row, and a nullable FK that is only sometimes a user
    #: would be a constraint that cannot be trusted either way.
    actor_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))

    __table_args__ = (
        # 🔒 NFR-006 (≤800 ms for 20 events) rests on this index and nothing
        # else. `tenant_id` leads it because RLS adds that predicate to every
        # query — an index starting at `client_id` would still work, but leaves
        # the tenant filter as a post-scan check on the largest table in S2.
        Index(
            "ix_timeline_events__client_occurred",
            "tenant_id",
            "client_id",
            text("occurred_at DESC"),
            text("id DESC"),
        ),
        CheckConstraint("length(btrim(summary)) > 0", name="ck_timeline_events__summary_not_blank"),
        CheckConstraint("length(summary) <= 200", name="ck_timeline_events__summary_length"),
        # 🔒 The system acts as nobody. A `system` row carrying an actor id would
        # attribute an automated action to a person, which is precisely the
        # misreading `actor_type` exists to prevent.
        CheckConstraint(
            "actor_type <> 'system' OR actor_id IS NULL",
            name="ck_timeline_events__system_has_no_actor",
        ),
    )
