"""Leads — the enquiry form and the raw submissions it produces.

Revision ID: 0015_leads
Revises: 0014_mobile_reversed_leakproof
Created: 2026-08-09

🔒 DB §6, FR-M2-001..011. Two tables, owned by `leads`. **No client rows are
created here** — M1.3 makes a lead a `clients` row at stage `lead`, and this
revision adds only the enquiry artefacts around it.

📌 **Why `enquiry_submissions` is separate from the client record it creates**
(DB §6.2). The client record is edited over time; the submission is evidence of
what was consented to, against a specific notice version. DPDP requires
demonstrating the consent basis (NFR-051), which means retaining the *original*
submission rather than the current state of an edited record. So the three
`submitted_*` columns are written once and never updated — enforced by grant
below, not by convention.

🔒 **Written by an unauthenticated endpoint** (Arch §15.3, API §11.2). That
governs almost every decision here:

* Every free-text column is length-bounded by CHECK. An unbounded column on a
  public write path is a column an attacker fills.
* `is_duplicate_of_existing` is recorded but **never returned** — EC-M2-02
  matching is silent, because a response that varied on it would make the
  endpoint a client-enumeration oracle (API §11.2).
* `spam_score` is recorded for submissions that were *accepted*. EC-M2-03
  requires spam to be "blocked before record creation", so a rejected submission
  leaves no row at all and cannot pollute conversion metrics.

⚠️ **`enquiry_forms` is a table rather than columns on `tenants`** (DB §6.1).
FR-M2-012 allows multiple forms per tenant in Phase 2; the table costs nothing
now and avoids moving the data later. The partial unique index enforces the MVP
rule — one *active* form per tenant — without forbidding the Phase 2 shape.

🔒 **The public read path needs RLS that a tenant-less connection can satisfy.**
`GET /public/forms/{tenant_slug}` runs with no tenant in scope, because
resolving the slug is what *establishes* the tenant. A Pattern A policy
(`tenant_id = current_tenant_id()`) would match nothing and the form would 404
for everyone. See `_ENQUIRY_FORMS_POLICY` for how this is resolved without
weakening tenant isolation for the practitioner path.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_leads"
down_revision: str | None = "0014_mobile_reversed_leakproof"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: 🔒 The vocabulary the *public form* may record — FR-M2-009.
#:
#: ⚠️ 🟡 PROPOSED. FR-M2-009 names no member set (see `kernel.leads.LeadSource`).
#: An enum rather than free text because US-M2-04 — "which channel produces
#: enquiries" — is a grouping question, and free text turns it into a spelling
#: exercise. `clients.source` stays `text` for the manual-entry case; this type
#: governs the column an unauthenticated caller writes.
_LEAD_SOURCE = (
    "instagram",
    "whatsapp",
    "referral",
    "google",
    "facebook",
    "walk_in",
    "other",
)

#: Pattern A on both — see the RLS section for the public-read exception.
_TENANT_SCOPED: tuple[str, ...] = ("enquiry_forms", "enquiry_submissions")


def _enum(name: str) -> postgresql.ENUM:
    """Reference an already-created enum without re-emitting its DDL. See 0002."""
    return postgresql.ENUM(name=name, create_type=False)


# ⚠️ Guarded on role existence, matching every migration since 0001.
_APPLY_GRANTS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        -- 🔒 DB §6.2 — "as submitted, never mutated". The submission is the
        -- evidence NFR-051 rests on; an application that can rewrite it holds
        -- no evidence at all. Same mechanism as `audit_log` and
        -- `consent_records` (DDR-15): the grant, not a convention.
        --
        -- ⚠️ 🔒 **A column-level grant, not a table-level one**, and the
        -- distinction is the whole design. FR-M2-011 has to mark an enquiry
        -- answered, so *something* must be updatable — and both obvious options
        -- are wrong:
        --
        --   * Revoking UPDATE outright makes the needs-response queue
        --     unimplementable. (That is what this migration first did; the
        --     live-database suite caught it, which no unit test could.)
        --   * Granting UPDATE on the table lets `submitted_name`,
        --     `submitted_mobile` and `primary_goal` be rewritten — exactly the
        --     immutability DB §6.2 exists to guarantee.
        --
        -- Naming the two response columns gives the feature what it needs and
        -- nothing more. PostgreSQL refuses an UPDATE touching any other column,
        -- so "as submitted, never mutated" is enforced by the database rather
        -- than by every future caller remembering it.
        --
        -- ⚠️ DELETE stays revoked, for a different reason: a DPDP erasure
        -- (FR-M0-027) legitimately removes these rows, and that pathway runs as
        -- the migrator role. The application never needs the verb.
        REVOKE UPDATE, DELETE ON TABLE enquiry_submissions FROM app_user;
        GRANT SELECT, INSERT ON TABLE enquiry_submissions TO app_user;
        GRANT UPDATE (responded_at, responded_by_user_id)
            ON TABLE enquiry_submissions TO app_user;

        -- The form itself is practitioner-editable (FR-M2-001, API §7.2's
        -- PATCH) — title, intro text and whether it is active. No DELETE: a
        -- form is deactivated, never removed, or its submissions lose their
        -- referent.
        REVOKE DELETE ON TABLE enquiry_forms FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE enquiry_forms TO app_user;
    END IF;
END
$$;
"""

_RESTORE_DEFAULT_PRIVILEGES = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE enquiry_submissions TO app_user;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE enquiry_forms TO app_user;
    END IF;
END
$$;
"""


#: 🔒 **The one policy in the codebase that admits a tenant-less read**, and the
#: reasoning matters because it looks like a hole.
#:
#: `GET /public/forms/{tenant_slug}` (API §11.1) is unauthenticated. The slug is
#: what identifies the tenant, so no `app.tenant_id` can be set before the row is
#: read — a Pattern A policy would return nothing and every public form would
#: 404. The alternatives were considered and rejected:
#:
#: * Reading the form as the migrator role — hands DDL rights to a public path.
#: * Resolving the slug outside RLS, then re-querying in scope — two round trips
#:   and the first one is still an unscoped read of `tenants`.
#:
#: What makes this safe is what the row *is*: API §11.1 states the response is
#: "effectively public information", and `tenants.slug` is publicly enumerable by
#: design (DB §4.1). The columns exposed are a title, some intro prose and an
#: active flag — the practitioner published them on purpose. There is no client
#: data on this table and no path from it to any.
#:
#: 🔒 Two policies rather than one permissive condition, so the practitioner path
#: keeps full tenant isolation:
#:
#: * `SELECT` is permitted when the tenant matches **or** when no tenant is in
#:   scope at all — the public read. An anonymous caller still sees only active
#:   forms, enforced by the `is_active` clause.
#: * Every write requires the tenant to match, unconditionally. The public path
#:   cannot create or edit a form, so the WITH CHECK has no anonymous branch.
_ENQUIRY_FORMS_POLICY = """
-- 🔒 The practitioner path: ordinary Pattern A, for every verb.
CREATE POLICY enquiry_forms__tenant_isolation ON enquiry_forms
    USING (tenant_id = current_tenant_id())
    WITH CHECK (tenant_id = current_tenant_id());

-- 🔒 The public read (API §11.1). Permissive policies are OR-ed, so this widens
-- SELECT only, and only for a connection with NO tenant scope — a scoped
-- connection is unaffected and still sees exactly its own rows.
--
-- ⚠️ `current_tenant_id() IS NULL` is the whole condition, and it is what keeps
-- this from being a cross-tenant read: a practitioner's request always has a
-- tenant set, so this branch is unreachable for them. Only the anonymous public
-- endpoint arrives with NULL.
--
-- ⚠️ `is_active` is in the policy rather than only in the query, on the same
-- reasoning as DB §22.2's archived-row predicates: a query can forget the
-- filter, a policy cannot. EC-M2-07 (a suspended tenant's form is disabled) is
-- served by deactivating the form.
CREATE POLICY enquiry_forms__public_read ON enquiry_forms
    FOR SELECT
    USING (current_tenant_id() IS NULL AND is_active);
"""

#: 🔒 `enquiry_submissions` gets **no public-read policy** — the asymmetry is
#: deliberate. The public path INSERTs and never reads, and a submission carries
#: a name, a mobile and a stated goal. An anonymous SELECT here would expose
#: every prospect who ever contacted any practice.
#:
#: ⚠️ The INSERT branch still needs a tenant, and the public endpoint sets one:
#: by the time it writes, the slug has been resolved and the request adopts that
#: tenant's scope. Reading the form is what happens outside scope; writing never
#: does.
_ENQUIRY_SUBMISSIONS_POLICY = """
CREATE POLICY enquiry_submissions__tenant_isolation ON enquiry_submissions
    USING (tenant_id = current_tenant_id())
    WITH CHECK (tenant_id = current_tenant_id());
"""


def upgrade() -> None:
    sa.Enum(*_LEAD_SOURCE, name="lead_source").create(op.get_bind(), checkfirst=False)

    # ─── enquiry_forms (DB §6.1) ──────────────────────────────────────────
    op.create_table(
        "enquiry_forms",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        # 🔒 The public URL segment *within* a tenant (DB §6.1: "unique per
        # tenant"). The shareable link of FR-M2-001 is `/{tenant_slug}`, which
        # resolves to the tenant's single active form; this exists so Phase 2's
        # multiple forms have an addressable name without a URL redesign.
        sa.Column("slug", sa.Text(), nullable=False, server_default="enquiry"),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("intro_text", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        # 🟡 FR-M2-012 (Phase 2) — custom questions. Ships as the column,
        # unused, exactly as the slice plan states. Defaulted to an empty array
        # so a Phase 2 reader never has to distinguish NULL from "no questions".
        sa.Column(
            "fields",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # ⚠️ Nullable, and **no FK**, deliberately. DB §6.1 specifies a
        # `consent_notice_id` FK; at MVP the notice in force is resolved per
        # request by locale (`platform.consent.notice_in_force`), because the
        # notice a prospect must see is the current one, not the one that was
        # current when the form was created. Pinning a version here would freeze
        # a form to superseded text and silently break FR-M0-029's re-consent.
        # The column stays for the Phase 2 case of a form that deliberately
        # pins its own notice.
        sa.Column("consent_notice_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_enquiry_forms"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_enquiry_forms__tenant"),
        # 🔒 Bounded, because the practitioner's own text is rendered on a public
        # page. The limits match `kernel.leads`.
        sa.CheckConstraint("length(btrim(title)) > 0", name="ck_enquiry_forms__title_not_blank"),
        sa.CheckConstraint("length(title) <= 120", name="ck_enquiry_forms__title_length"),
        sa.CheckConstraint(
            "intro_text IS NULL OR length(intro_text) <= 1000",
            name="ck_enquiry_forms__intro_length",
        ),
        sa.CheckConstraint("length(btrim(slug)) > 0", name="ck_enquiry_forms__slug_not_blank"),
        # 🔒 The Phase 2 shape, enforced now: a slug is unique within its tenant
        # (DB §6.1), not globally. The *tenant* slug is what is globally unique
        # (`uq_tenants__slug`, migration 0002).
        sa.UniqueConstraint("tenant_id", "slug", name="uq_enquiry_forms__tenant_slug"),
    )

    # 🔒 **At most one active form per tenant** — the MVP rule (FR-M2-001: "a
    # publicly accessible enquiry form", singular).
    #
    # ⚠️ A partial unique index rather than a constraint, so Phase 2's multiple
    # forms (FR-M2-012) need only drop this index — not restructure the table.
    # Without it, `GET /public/forms/{tenant_slug}` would have to choose between
    # two active forms arbitrarily, and the practitioner would have no way to
    # tell which link they had shared.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_enquiry_forms__one_active_per_tenant
            ON enquiry_forms (tenant_id)
            WHERE is_active;
        """
    )
    op.create_index("ix_enquiry_forms__tenant_id", "enquiry_forms", ["tenant_id"])

    # ─── enquiry_submissions (DB §6.2) ────────────────────────────────────
    op.create_table(
        "enquiry_submissions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("form_id", sa.UUID(), nullable=False),
        # 🔒 **Reference only, no foreign key** — R6, and the same treatment
        # `consent_records.subject_id` gets (migration 0006) for the same reasons.
        #
        # `clients` is another module's table (DB §5). A FK would couple the two
        # schemas and cross the boundary in DDL, where the import checker cannot
        # see it — `tools/check_boundaries.py` rejects it, correctly.
        #
        # ⚠️ The second reason stands on its own: a DPDP erasure (FR-M0-027)
        # deletes the client, and a FK would either block that or cascade away
        # the proof that consent was ever given. The basis must outlive the
        # record, so this id is left to dangle deliberately.
        sa.Column("client_id", sa.UUID(), nullable=True),
        # 🔒 DB §6.2 — "as submitted, never mutated". UPDATE is revoked below.
        sa.Column("submitted_name", sa.Text(), nullable=False),
        sa.Column("submitted_mobile", sa.Text(), nullable=True),
        sa.Column("submitted_email", sa.Text(), nullable=True),
        # FR-M2-003 — the third mandatory field.
        sa.Column("primary_goal", sa.Text(), nullable=False),
        # 🟡 FR-M2-012's answers. Empty at MVP for the same reason `fields` is.
        sa.Column(
            "answers",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("source", _enum("lead_source"), nullable=True, comment="FR-M2-009"),
        sa.Column("source_detail", sa.Text(), nullable=True),
        # 🔒 FR-M2-004 / AC-M2-004. No FK: `consent_records` uses a bigserial id
        # and is append-only with its own retention, and a FK into it would let a
        # submission block the ledger's own lifecycle. Reference only, matching
        # how `consent_records` itself references `tenants`.
        sa.Column("consent_record_id", sa.BigInteger(), nullable=True),
        # 🔒 The notice actually presented, so NFR-051 is answerable from this
        # row alone without joining the ledger.
        sa.Column("consent_notice_id", sa.UUID(), nullable=True),
        # 🔒 EC-M2-02. Recorded, and **never returned to the submitter**.
        sa.Column(
            "is_duplicate_of_existing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        # FR-M2-008. Only ever populated for *accepted* submissions — EC-M2-03
        # blocks spam before a row exists.
        sa.Column("spam_score", sa.Numeric(3, 2), nullable=True),
        # 🔒 FR-M2-011 / AC-M2-005 — the needs-response view's clock. Set when a
        # practitioner acts on the enquiry; NULL means still waiting.
        #
        # ⚠️ On the submission rather than the client, deliberately. "Has this
        # *enquiry* been answered" is not "has this client been contacted": a
        # returning prospect (EC-M2-02) creates a second submission against a
        # client who was responded to months ago, and a flag on the client would
        # show the new enquiry as already handled.
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("responded_by_user_id", sa.UUID(), nullable=True),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_enquiry_submissions"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_enquiry_submissions__tenant"
        ),
        sa.ForeignKeyConstraint(
            ["form_id"], ["enquiry_forms.id"], name="fk_enquiry_submissions__form"
        ),
        sa.ForeignKeyConstraint(
            ["responded_by_user_id"], ["users.id"], name="fk_enquiry_submissions__responder"
        ),
        # 🔒 FR-M1-004 / EC-M1-08, restated at the submission. The form requires
        # a mobile (FR-M2-003), but API §11.2 permits email instead — and the
        # client this creates carries the same rule, so accepting a submission
        # with neither would fail at `clients` after the consent was recorded.
        sa.CheckConstraint(
            "submitted_mobile IS NOT NULL OR submitted_email IS NOT NULL",
            name="ck_enquiry_submissions__contact_present",
        ),
        # 🔒 NFR-100 — the same E.164 shape assertion `clients.mobile` carries.
        sa.CheckConstraint(
            "submitted_mobile IS NULL OR submitted_mobile ~ '^\\+[1-9][0-9]{7,14}$'",
            name="ck_enquiry_submissions__mobile_e164",
        ),
        # 🔒 Bounds on every column an unauthenticated caller controls.
        sa.CheckConstraint(
            "length(btrim(submitted_name)) > 0 AND length(submitted_name) <= 120",
            name="ck_enquiry_submissions__name_length",
        ),
        sa.CheckConstraint(
            "length(btrim(primary_goal)) > 0 AND length(primary_goal) <= 500",
            name="ck_enquiry_submissions__goal_length",
        ),
        sa.CheckConstraint(
            "source_detail IS NULL OR length(source_detail) <= 120",
            name="ck_enquiry_submissions__source_detail_length",
        ),
        # EC-M2-03 — a score outside [0,1] means the scorer changed shape and
        # the threshold comparison is no longer meaningful.
        sa.CheckConstraint(
            "spam_score IS NULL OR (spam_score >= 0 AND spam_score <= 1)",
            name="ck_enquiry_submissions__spam_score_range",
        ),
        # A response has both halves or neither — the same shape as
        # `client_assignments`' revocation constraint. A `responded_at` with no
        # user would make "who answered this" unanswerable at the one moment it
        # is asked.
        sa.CheckConstraint(
            "(responded_at IS NULL) = (responded_by_user_id IS NULL)",
            name="ck_enquiry_submissions__response_complete",
        ),
    )

    # 🔒 **FR-M2-011's view, and the index it rests on** — enquiries awaiting a
    # response, oldest first (AC-M2-005).
    #
    # ⚠️ 🔒 **Leakproof by construction, per the Slice E lesson.** Under FORCE
    # RLS, PostgreSQL pushes only leakproof quals into an Index Cond; migration
    # 0014 exists because `reverse()` and `LIKE` are not, and a valid index sat
    # unused behind a full bitmap scan. Every expression here is a plain column
    # reference and the predicate is `IS NULL` — no function, nothing to check
    # `proleakproof` against.
    #
    # ⚠️ The partial predicate is `responded_at IS NULL`, and the query must
    # carry that exact clause for the planner to prove the index applies — the
    # other half of 0014's lesson, where a predicate on `mobile` could not be
    # derived from a clause about `mobile_reversed`. Here both name the same
    # column, so the proof is direct.
    op.execute(
        """
        CREATE INDEX ix_enquiry_submissions__needs_response
            ON enquiry_submissions (tenant_id, submitted_at)
            WHERE responded_at IS NULL;
        """
    )

    # The full enquiry list (API §7.2's `GET /app/enquiries`), newest first.
    # `id DESC` is the tiebreaker, load-bearing for the same reason it is on
    # `timeline_events`: submissions can share a timestamp, and a cursor without
    # a unique tiebreaker skips or repeats rows at a page boundary.
    op.execute(
        """
        CREATE INDEX ix_enquiry_submissions__tenant_submitted
            ON enquiry_submissions (tenant_id, submitted_at DESC, id DESC);
        """
    )

    # "Every enquiry this client has ever sent" — the client detail view, and
    # what makes a repeat enquiry (EC-M2-02) visible on the record it matched.
    op.create_index(
        "ix_enquiry_submissions__client",
        "enquiry_submissions",
        ["tenant_id", "client_id"],
    )

    # ─── RLS (DB §17.1) ───────────────────────────────────────────────────
    #
    # 🔒 FORCE is not redundant with ENABLE: without it the table owner bypasses
    # every policy, and migrations run as `app_migrator`, which owns these.
    for table in _TENANT_SCOPED:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    op.execute(_ENQUIRY_FORMS_POLICY)
    op.execute(_ENQUIRY_SUBMISSIONS_POLICY)

    # ⚠️ Last: once the revokes land, this migration's own connection keeps
    # working only because it runs as the migrator role.
    op.execute(_APPLY_GRANTS)


def downgrade() -> None:
    """Drop everything this revision created.

    ⚠️ 🔒 **Destroys the consent evidence for every enquiry.** The submissions are
    what NFR-051 answers "on what basis do you hold this person's data" from for
    anyone who arrived through the public form. The `clients` rows they created
    survive — they are 0009's — so the effect is client records whose lawful
    basis can no longer be produced. Reversibility exists so the chain is honestly
    testable in development; this is not a supported production operation.
    """
    op.execute(_RESTORE_DEFAULT_PRIVILEGES)
    op.drop_table("enquiry_submissions")
    op.drop_table("enquiry_forms")
    sa.Enum(name="lead_source").drop(op.get_bind(), checkfirst=False)
