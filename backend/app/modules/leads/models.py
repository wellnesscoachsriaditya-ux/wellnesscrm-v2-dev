"""ORM models for the leads schema — DB §6.1/6.2.

🔒 These live in the module, not the kernel. `leads` owns `enquiry_forms` and
`enquiry_submissions` (DB §6: "Owner: `leads`"), and a model in `kernel.models`
would be a table the kernel owns — which is what R6 exists to prevent one module
doing to another.

⚠️ FKs to `tenants`, `clients` and `users` are not R6 violations. Those are
kernel tables, and the kernel is the one thing every module may depend on
(Arch §3.1) — the spine every tenant-scoped row descends from.

🔒 `enquiry_submissions.client_id` carries **no foreign key**. R6 forbids a
module referencing another module's tables, and that includes in DDL — a FK
couples the two schemas and crosses the boundary somewhere the import checker
cannot see. `tools/check_boundaries.py` enforces exactly this and rejected the
FK when it was written. Resolution goes through `ClientDirectory` (Arch §3.4b),
and `consent_records.subject_id` is the existing precedent.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Index, Numeric, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.kernel import Base
from app.kernel.db import pg_enum
from app.kernel.leads import LeadSource


class EnquiryForm(Base):
    """The public form a practitioner shares — DB §6.1, FR-M2-001.

    🔒 **One active form per tenant at MVP** (FR-M2-001), enforced by the partial
    unique index in migration 0015: ``uq_enquiry_forms__one_active_per_tenant``
    (WHERE is_active). Phase 2 allows multiple forms (FR-M2-012); the index
    would be dropped then without touching the schema.

    **Two RLS policies, not one** — see migration 0015 for the full reasoning.
    The public-read policy admits tenant-less connections for ``is_active`` rows;
    the tenant-isolation policy is standard Pattern A. The asymmetry is what makes
    ``GET /public/forms/{tenant_slug}`` possible without granting cross-tenant
    reads to practitioner sessions.

    🔒 **Pattern A with FORCE, plus the public-read addendum.** `app_migrator`
    owns the table, so without FORCE the owner would bypass every policy.
    """

    __tablename__ = "enquiry_forms"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        comment="🔒 RLS discriminator",
    )
    slug: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="enquiry", comment="Per-tenant URL segment"
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    intro_text: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), comment="EC-M2-07"
    )
    #: 🟡 FR-M2-012 (Phase 2) — custom questions. Ships as the column, unused.
    fields: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    #: ⚠️ Nullable — see migration 0015's column comment.
    consent_notice_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("length(btrim(title)) > 0", name="ck_enquiry_forms__title_not_blank"),
        CheckConstraint("length(title) <= 120", name="ck_enquiry_forms__title_length"),
        CheckConstraint(
            "intro_text IS NULL OR length(intro_text) <= 1000",
            name="ck_enquiry_forms__intro_length",
        ),
        CheckConstraint("length(btrim(slug)) > 0", name="ck_enquiry_forms__slug_not_blank"),
        Index("ix_enquiry_forms__tenant_id", "tenant_id"),
    )


class EnquirySubmission(Base):
    """The raw record of what a prospect submitted — DB §6.2, FR-M2-005.

    🔒 **Evidence, not a snapshot.** The client record it creates is edited over
    time; these columns record what was actually submitted, against which consent
    was given (NFR-051). ``app_user`` holds INSERT and SELECT only (migration
    0015) — the same protection as ``audit_log`` and ``consent_records``.

    🔒 ``client_id`` carries **no foreign key** (R6, and FR-M0-027). The
    submission must survive as the consent basis even after the person it
    describes is erased — a regulator asking "on what basis did you hold this
    data" must be answerable then most of all. A FK would either block the
    erasure or cascade away the proof. See the column's own note.

    🔒 **Append-only, enforced by grant.** `app_user` holds INSERT and SELECT and
    nothing else. The three ``submitted_*`` columns are immutable by that grant,
    not by a constraint — which is the stronger mechanism.

    Pattern A RLS, FORCE. **No client-realm policy** — a client must not be
    able to read their own submission directly, because the form asked for
    information in service of the practitioner, not for the portal.
    """

    __tablename__ = "enquiry_submissions"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        comment="🔒 RLS discriminator",
    )
    form_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("enquiry_forms.id"), nullable=False
    )
    #: 🔒 **Reference only, no foreign key** — R6, and the same treatment
    #: ``consent_records.subject_id`` gets for the same two reasons.
    #:
    #: ``clients`` belongs to another module (DB §5), and a FK is a read of its
    #: table by another name: it couples this schema to that one and lets the
    #: boundary be crossed in DDL where the import checker cannot see it.
    #: Resolution goes through ``ClientDirectory`` (Arch §3.4b).
    #:
    #: ⚠️ The second reason is independent of the boundary and would apply even
    #: without it: a DPDP erasure (FR-M0-027) deletes the client, and a FK would
    #: either block that or cascade away the proof that consent was ever given.
    #: The basis must outlive the record — so the column is left dangling
    #: deliberately, and readers must handle an id that resolves to nothing.
    client_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        comment="🔒 Reference only, no FK — survives erasure for NFR-051",
    )
    # 🔒 DB §6.2 — "as submitted, never mutated". UPDATE revoked in migration 0015.
    submitted_name: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_mobile: Mapped[str | None] = mapped_column(Text, comment="🔒 E.164 if present")
    submitted_email: Mapped[str | None] = mapped_column(Text)
    # FR-M2-003 — the third mandatory field.
    primary_goal: Mapped[str] = mapped_column(Text, nullable=False)
    #: 🟡 FR-M2-012 Phase 2.
    answers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    source: Mapped[LeadSource | None] = mapped_column(
        pg_enum(LeadSource, "lead_source"), comment="FR-M2-009"
    )
    source_detail: Mapped[str | None] = mapped_column(Text)
    #: 🔒 Reference-only, no FK — see migration 0015's column comment.
    consent_record_id: Mapped[int | None] = mapped_column(BigInteger)
    #: The notice version actually presented at submission (NFR-051).
    consent_notice_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    #: 🔒 EC-M2-02 — never returned to the submitter.
    is_duplicate_of_existing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    #: FR-M2-008 — only populated for accepted submissions.
    spam_score: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    #: 🔒 FR-M2-011 / AC-M2-005 — NULL = still waiting.
    responded_at: Mapped[datetime | None] = mapped_column()
    responded_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id")
    )
    submitted_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "submitted_mobile IS NOT NULL OR submitted_email IS NOT NULL",
            name="ck_enquiry_submissions__contact_present",
        ),
        CheckConstraint(
            "submitted_mobile IS NULL OR submitted_mobile ~ '^\\+[1-9][0-9]{7,14}$'",
            name="ck_enquiry_submissions__mobile_e164",
        ),
        CheckConstraint(
            "length(btrim(submitted_name)) > 0 AND length(submitted_name) <= 120",
            name="ck_enquiry_submissions__name_length",
        ),
        CheckConstraint(
            "length(btrim(primary_goal)) > 0 AND length(primary_goal) <= 500",
            name="ck_enquiry_submissions__goal_length",
        ),
        CheckConstraint(
            "source_detail IS NULL OR length(source_detail) <= 120",
            name="ck_enquiry_submissions__source_detail_length",
        ),
        CheckConstraint(
            "spam_score IS NULL OR (spam_score >= 0 AND spam_score <= 1)",
            name="ck_enquiry_submissions__spam_score_range",
        ),
        CheckConstraint(
            "(responded_at IS NULL) = (responded_by_user_id IS NULL)",
            name="ck_enquiry_submissions__response_complete",
        ),
        Index("ix_enquiry_submissions__client", "tenant_id", "client_id"),
    )
