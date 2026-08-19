"""Clinical workspace — assessments, measurements, notes and documents.

Revision ID: 0016_clinical
Revises: 0015_leads
Create Date: 2026-08-10

🔒 **DDR-07 in one migration.** The assessment is *versioned data*, not columns:
`assessment_definitions.schema` holds the structure and `assessment_responses.
answers` holds what was said. That is what makes FR-M3-002 true — adding a
question is an INSERT, not a migration — and it is the whole reason this revision
creates six tables and then never needs to create another for a §9 revision.

🔒 **DDR-08 is the other half.** `client_nutrition_profile` is the typed
projection of the handful of answers nutrition calculates from. Without it,
`nutrition` would parse `jsonb` and inherit permanent knowledge of the
assessment schema; with it, the schema can change freely and the projection
contract does not.

⚠️ **`consultation_notes` gets no client-realm access at all**, in the same way
`client_notes` does not: FR-M3-021 and AC-M3-006 make notes invisible to the
client, and absence of a policy is stronger than a policy with a condition.

⚠️ **The seed is deliberately minimal.** Per the approved refinement and the
implementation plan's S4 section, `nutrition_core` v1 carries only what nutrition
calculation needs. PRD §9's clinical sections D–L are **not** seeded here: they
are pending Validation Gate G4 (ASM-05) and OD-06…14, and shipping them as
authoritative before practitioner review is exactly what the PRD's PROPOSED
marking exists to prevent. They arrive as **new definition versions** — no
migration, no release.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_clinical"
down_revision: str | None = "0015_leads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: 🔒 DB §7.2 — a definition's life. `published` is a one-way door (FR-M3-003).
_DEFINITION_STATUS = ("draft", "published", "retired")

#: 🔒 DB §7.3. Two states only — see `kernel.clinical.ResponseStatus` for why
#: "abandoned" is not one of them.
_RESPONSE_STATUS = ("in_progress", "completed")

#: 🔒 DB §7.5 — EC-M3-05's attribution. Both same-date values are kept.
_MEASUREMENT_SOURCE = ("practitioner", "client", "device")

#: 🟡 PROPOSED — DB §7.4 names the column and its type only. The labels a
#: practitioner sees are a Gate G4 review question.
_ACTIVITY_LEVEL = ("sedentary", "light", "moderate", "active", "very_active")

#: 🟡 PROPOSED — taken verbatim from PRD §9.5's own proposed list.
_GOAL_TYPE = (
    "weight_loss",
    "weight_gain",
    "muscle_gain",
    "manage_condition",
    "improve_energy",
    "improve_digestion",
    "sports_performance",
    "general_wellbeing",
    "other",
)

#: 🔒 **No table here carries a foreign key to `clients`.** R6 forbids a module
#: referencing another module's tables, and that includes in DDL — a FK couples
#: the two schemas at a layer the import checker cannot see.
#: `tools/check_boundaries.py` rejected these when they were first written.
#: Resolution goes through the `ClientDirectory` kernel port (Arch §3.4b), the
#: same way `enquiry_submissions.client_id` does.
#:
#: ⚠️ The consequence readers must handle: a `client_id` can dangle. A DPDP
#: erasure (FR-M0-027) removes the client while the clinical rows remain until
#: their own retention expires.
#
#: Pattern A RLS on all six. Every one is read on a tenant-facing path.
_TENANT_SCOPED: tuple[str, ...] = (
    "assessment_definitions",
    "assessment_responses",
    "client_nutrition_profile",
    "measurements",
    "consultation_notes",
    "client_documents",
)


def _enum(name: str) -> postgresql.ENUM:
    """Reference an already-created enum without re-emitting its DDL. See 0002."""
    return postgresql.ENUM(name=name, create_type=False)


# ⚠️ Guarded on role existence, matching every migration since 0001.
_APPLY_GRANTS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        -- 🔒 FR-M3-003 — a published definition is immutable, enforced by the
        -- grant rather than by every future caller remembering.
        --
        -- A column-level UPDATE grant, for the same reason `enquiry_submissions`
        -- has one (0015): publishing has to change *something*, and the two
        -- lifecycle columns are all it may change. `schema` and
        -- `calculation_bindings` are unreachable to the application, so the
        -- structure a response points at cannot move under it. That is what
        -- makes AC-M3-003 hold at the database rather than in a service method.
        --
        -- ⚠️ INSERT is granted: a new *version* is a new row, which is the
        -- sanctioned way to change an assessment.
        REVOKE UPDATE, DELETE ON TABLE assessment_definitions FROM app_user;
        GRANT SELECT, INSERT ON TABLE assessment_definitions TO app_user;
        GRANT UPDATE (status, published_at) ON TABLE assessment_definitions TO app_user;

        -- Responses are edited continuously while in progress (FR-M3-005), so
        -- UPDATE is granted broadly — but never DELETE: an administration is a
        -- clinical record, and FR-M3-007 keeps every one for comparison.
        --
        -- ⚠️ `definition_id` is *not* excluded from UPDATE, and it could have
        -- been. It is left updatable because a column-level grant listing
        -- fifteen columns is a list that rots; the invariant is asserted in
        -- `tests/test_clinical_schema.py` instead, where a reader can see it.
        REVOKE DELETE ON TABLE assessment_responses FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE assessment_responses TO app_user;

        -- The projection is rewritten on every completion (DDR-08).
        REVOKE DELETE ON TABLE client_nutrition_profile FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE client_nutrition_profile TO app_user;

        -- 🔒 Measurements are append-only to the application. A correction is a
        -- new dated row, not an edit: EC-M3-05 already expects several values
        -- for one date, and a trend whose history can be rewritten is not a
        -- record of anything. Erasure runs as the migrator role.
        REVOKE UPDATE, DELETE ON TABLE measurements FROM app_user;
        GRANT SELECT, INSERT ON TABLE measurements TO app_user;

        -- Notes are editable by their author (FR-M3-020) and archived, never
        -- deleted — the same disposition as `client_notes` in 0011.
        REVOKE DELETE ON TABLE consultation_notes FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE consultation_notes TO app_user;

        -- A document is detached by archiving. The bytes are destroyed by the
        -- storage purge path, which is what makes erasure traverse object
        -- storage (Arch §13.2) rather than orphan it.
        REVOKE DELETE ON TABLE client_documents FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE client_documents TO app_user;
    END IF;
END
$$;
"""

_RESTORE_DEFAULT_PRIVILEGES = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE assessment_definitions TO app_user;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE assessment_responses TO app_user;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE client_nutrition_profile TO app_user;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE measurements TO app_user;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE consultation_notes TO app_user;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE client_documents TO app_user;
    END IF;
END
$$;
"""


#: 🔒 The one definition table that is *not* purely tenant-scoped.
#:
#: A platform-authored definition has `tenant_id IS NULL` and must be readable by
#: every tenant — that is what "platform-authored" means, and at MVP it is the
#: only kind there is. A plain Pattern A policy would make `nutrition_core`
#: invisible to everyone and the whole module dead on arrival.
#:
#: ⚠️ The asymmetry is deliberate and narrow: the NULL-tenant branch widens
#: **SELECT only**. Writes still require the row's tenant to match, so a tenant
#: cannot create, edit or publish a platform definition — and since `schema` is
#: not updatable by the application at all (see the grants above), the widened
#: read exposes structure that we authored and nothing about any person.
#:
#: This is the same nullable-tenant catalogue pattern DB §8 uses for foods.
_ASSESSMENT_DEFINITIONS_POLICY = """
CREATE POLICY assessment_definitions__tenant_isolation ON assessment_definitions
    USING (tenant_id = current_tenant_id() OR tenant_id IS NULL)
    WITH CHECK (tenant_id = current_tenant_id());
"""

#: Ordinary Pattern A for the five tables that hold client data.
_TENANT_POLICY = """
CREATE POLICY {table}__tenant_isolation ON {table}
    USING (tenant_id = current_tenant_id())
    WITH CHECK (tenant_id = current_tenant_id());
"""


# ─── The seed (DB §7.2, implementation plan S4) ──────────────────────────
#
# 🔒 **Minimum for nutrition calculation only.** DB §7.2 lists exactly this set:
# "date of birth, sex, height, current weight, activity level, primary goal,
# dietary class, allergies, food exclusions, religious fasting, staple grain,
# region".
#
# ⚠️ Two of those are deliberately *not* here as `food_ref` fields yet.
# `allergen_food_ids` and `excluded_food_ids` need the food catalogue S3 builds;
# a `food_ref` field with nothing to pick from is a dead control, and capturing
# allergies as free text in the meantime is precisely what DB §7.4 forbids
# ("never by free-text parsing — a missed allergen is a clinical incident").
# They arrive in v2, which is an INSERT.
#
# ⚠️ Current weight is captured but **not** projected: weight is longitudinal and
# belongs in `measurements` (FR-M3-011), which the assessment submit path writes
# through. `client_nutrition_profile` has no weight column for exactly this
# reason — one source of truth.
_SEED_SCHEMA = {
    "sections": [
        {
            "id": "about_you",
            "title": "About you",
            "description": "The basics your dietitian needs to calculate what you need.",
            "is_clinical": False,
            "fields": [
                {
                    "id": "date_of_birth",
                    "type": "date",
                    "label": "Date of birth",
                    "required": True,
                    "help_text": "Used to work out your daily requirements.",
                },
                {
                    "id": "sex",
                    "type": "choice",
                    "label": "Sex",
                    "required": True,
                    "options": ["male", "female", "other"],
                    "help_text": "Needed for energy requirement equations.",
                },
                {
                    "id": "height_cm",
                    "type": "number",
                    "label": "Height (cm)",
                    "required": True,
                    "min": 30,
                    "max": 275,
                },
                {
                    "id": "current_weight_kg",
                    "type": "number",
                    "label": "Current weight (kg)",
                    "required": True,
                    "min": 2,
                    "max": 500,
                },
                {
                    "id": "region_cuisine",
                    "type": "text",
                    "label": "Which region's food do you mostly eat?",
                    "help_text": "For example: Kerala, Punjabi, Bengali, South Indian.",
                },
            ],
        },
        {
            "id": "goal",
            "title": "Your goal",
            "is_clinical": False,
            "fields": [
                {
                    "id": "primary_goal",
                    "type": "choice",
                    "label": "What would you most like to achieve?",
                    "required": True,
                    "options": [
                        "weight_loss",
                        "weight_gain",
                        "muscle_gain",
                        "manage_condition",
                        "improve_energy",
                        "improve_digestion",
                        "sports_performance",
                        "general_wellbeing",
                        "other",
                    ],
                },
                {
                    "id": "activity_level",
                    "type": "choice",
                    "label": "How active are you on a normal day?",
                    "required": True,
                    "options": ["sedentary", "light", "moderate", "active", "very_active"],
                },
            ],
        },
        {
            "id": "how_you_eat",
            "title": "How you eat",
            "is_clinical": False,
            "fields": [
                {
                    "id": "dietary_class",
                    "type": "choice",
                    "label": "What do you eat?",
                    "required": True,
                    "options": ["vegetarian", "eggetarian", "non_vegetarian", "vegan", "jain"],
                },
                {
                    "id": "excludes_onion_garlic",
                    "type": "boolean",
                    "label": "Do you avoid onion and garlic?",
                },
                {
                    "id": "excludes_root_vegetables",
                    "type": "boolean",
                    "label": "Do you avoid root vegetables?",
                },
                {
                    "id": "staple_grain",
                    "type": "text",
                    "label": "Your everyday grain",
                    "help_text": "For example: rice, wheat roti, ragi, jowar.",
                },
                {
                    "id": "fasting_patterns",
                    "type": "multi_choice",
                    "label": "Do you fast regularly?",
                    "options": [
                        "none",
                        "weekly_day",
                        "ekadashi",
                        "navratri",
                        "ramadan",
                        "shravan",
                        "intermittent",
                        "other",
                    ],
                    "help_text": "Choose any that apply.",
                },
            ],
        },
    ]
}

#: 🔒 DDR-08 — which answers become typed columns. Every entry is checked against
#: the schema at publish time by `validate_calculation_bindings`; a binding to a
#: missing field would drop the value silently, which is the failure that matters.
_SEED_BINDINGS = {
    "date_of_birth": "date_of_birth",
    "sex": "sex",
    "height_cm": "height_cm",
    "activity_level": "activity_level",
    "primary_goal": "primary_goal",
    "dietary_class": "dietary_class",
    "excludes_onion_garlic": "excludes_onion_garlic",
    "excludes_root_vegetables": "excludes_root_vegetables",
    "fasting_patterns": "fasting_patterns",
    "staple_grain": "staple_grain",
    "region_cuisine": "region_cuisine",
}


def upgrade() -> None:
    for name, values in (
        ("definition_status", _DEFINITION_STATUS),
        ("response_status", _RESPONSE_STATUS),
        ("measurement_source", _MEASUREMENT_SOURCE),
        ("activity_level", _ACTIVITY_LEVEL),
        ("goal_type", _GOAL_TYPE),
    ):
        sa.Enum(*values, name=name).create(op.get_bind(), checkfirst=False)

    # ─── assessment_definitions (DB §7.2) ─────────────────────────────────
    op.create_table(
        "assessment_definitions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        # 🔒 Nullable: NULL is platform-authored, which is every row at MVP.
        sa.Column("tenant_id", sa.UUID(), nullable=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "calculation_bindings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", _enum("definition_status"), server_default="draft", nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "code", "version", "tenant_id", name="uq_assessment_definitions__code_version_tenant"
        ),
        sa.CheckConstraint("version > 0", name="ck_assessment_definitions__version_positive"),
        sa.CheckConstraint(
            "(status <> 'published' OR published_at IS NOT NULL)",
            name="ck_assessment_definitions__published_timestamped",
        ),
    )
    op.create_index(
        "ix_assessment_definitions__code_status", "assessment_definitions", ["code", "status"]
    )

    # ─── assessment_responses (DB §7.3) ───────────────────────────────────
    op.create_table(
        "assessment_responses",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        # 🔒 FR-M3-003, EC-M3-03 — the version this administration is bound to.
        sa.Column("definition_id", sa.UUID(), nullable=False),
        sa.Column(
            "answers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", _enum("response_status"), server_default="in_progress", nullable=False),
        sa.Column(
            "completed_sections",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("completed_by", _enum("actor_type"), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["definition_id"], ["assessment_definitions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "(status <> 'completed' OR (completed_at IS NOT NULL AND completed_by IS NOT NULL))",
            name="ck_assessment_responses__completion_recorded",
        ),
    )
    op.execute(
        "CREATE INDEX ix_assessment_responses__client_completed"
        " ON assessment_responses (client_id, completed_at DESC)"
    )
    op.create_index(
        "ix_assessment_responses__client_status", "assessment_responses", ["client_id", "status"]
    )

    # ─── client_nutrition_profile (DB §7.4, DDR-08) ───────────────────────
    op.create_table(
        "client_nutrition_profile",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("source_response_id", sa.UUID(), nullable=True),
        sa.Column("date_of_birth", sa.Date(), nullable=True),
        sa.Column("sex", _enum("sex_type"), nullable=True),
        sa.Column("height_cm", sa.Numeric(5, 1), nullable=True),
        sa.Column("activity_level", _enum("activity_level"), nullable=True),
        sa.Column("primary_goal", _enum("goal_type"), nullable=True),
        sa.Column("dietary_class", _enum("dietary_class"), nullable=True),
        sa.Column(
            "excludes_onion_garlic",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "excludes_root_vegetables",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        # 🔒 Safety-critical (FR-M5-006) — food ids, never parsed prose.
        sa.Column(
            "allergen_food_ids",
            postgresql.ARRAY(sa.UUID()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column(
            "excluded_food_ids",
            postgresql.ARRAY(sa.UUID()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column(
            "fasting_patterns",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("staple_grain", sa.Text(), nullable=True),
        sa.Column("region_cuisine", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["source_response_id"], ["assessment_responses.id"]),
        sa.PrimaryKeyConstraint("id"),
        # 🔒 One *current* profile per client. The history is the responses.
        sa.UniqueConstraint("client_id", name="uq_client_nutrition_profile__client"),
    )
    op.create_index(
        "ix_client_nutrition_profile__tenant", "client_nutrition_profile", ["tenant_id"]
    )

    # ─── measurements (DB §7.5) ───────────────────────────────────────────
    op.create_table(
        "measurements",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("measured_on", sa.Date(), nullable=False),
        sa.Column("weight_kg", sa.Numeric(5, 2), nullable=True),
        sa.Column("height_cm", sa.Numeric(5, 1), nullable=True),
        sa.Column("waist_cm", sa.Numeric(5, 1), nullable=True),
        sa.Column("hip_cm", sa.Numeric(5, 1), nullable=True),
        sa.Column("body_fat_pct", sa.Numeric(4, 1), nullable=True),
        sa.Column("source", _enum("measurement_source"), nullable=False),
        sa.Column("recorded_by_user_id", sa.UUID(), nullable=True),
        sa.Column(
            "is_flagged_implausible",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["recorded_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        # 🔒 DB §7.5's bounds. `kernel.clinical.assert_storable` refuses the same
        # values earlier and names the field; this holds if a caller forgets.
        sa.CheckConstraint(
            "weight_kg IS NULL OR (weight_kg >= 2 AND weight_kg <= 500)",
            name="ck_measurements__weight_range",
        ),
        sa.CheckConstraint(
            "height_cm IS NULL OR (height_cm >= 30 AND height_cm <= 275)",
            name="ck_measurements__height_range",
        ),
        sa.CheckConstraint(
            "body_fat_pct IS NULL OR (body_fat_pct >= 0 AND body_fat_pct <= 75)",
            name="ck_measurements__body_fat_range",
        ),
        sa.CheckConstraint(
            "weight_kg IS NOT NULL OR height_cm IS NOT NULL OR waist_cm IS NOT NULL"
            " OR hip_cm IS NOT NULL OR body_fat_pct IS NOT NULL",
            name="ck_measurements__at_least_one_value",
        ),
    )
    # 🔒 DB §7.5 — the trend read (FR-M3-014).
    op.execute(
        "CREATE INDEX ix_measurements__client_date ON measurements (client_id, measured_on DESC)"
    )

    # ─── consultation_notes (DB §7.6) ─────────────────────────────────────
    op.create_table(
        "consultation_notes",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        # ⏳ FK deferred until M6 (S8) creates `appointments`.
        sa.Column("appointment_id", sa.UUID(), nullable=True),
        sa.Column("note_date", sa.Date(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("structured", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("author_user_id", sa.UUID(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("length(btrim(body)) > 0", name="ck_consultation_notes__body_present"),
    )
    op.execute(
        "CREATE INDEX ix_consultation_notes__client_date"
        " ON consultation_notes (client_id, note_date DESC)"
    )

    # ─── client_documents (DB §7.6) ───────────────────────────────────────
    op.create_table(
        "client_documents",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("file_id", sa.UUID(), nullable=False),
        sa.Column("document_type", sa.Text(), nullable=False),
        sa.Column("document_date", sa.Date(), nullable=True),
        sa.Column("uploaded_by", _enum("actor_type"), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("file_id", name="uq_client_documents__file"),
        sa.CheckConstraint(
            "length(btrim(document_type)) > 0", name="ck_client_documents__type_present"
        ),
    )
    op.execute(
        "CREATE INDEX ix_client_documents__client_created"
        " ON client_documents (client_id, created_at DESC)"
    )

    # ─── The seed (DB §7.2) ───────────────────────────────────────────────
    #
    # 🔒 **Before RLS is enabled below, and that ordering is load-bearing.**
    # `FORCE ROW LEVEL SECURITY` applies policies to the table *owner* too, and
    # migrations run as `app_migrator`, which owns these tables. The policy's
    # WITH CHECK requires `tenant_id = current_tenant_id()`; a migration has no
    # tenant in scope, so a platform row (`tenant_id IS NULL`) is refused —
    # "new row violates row-level security policy". Seeding first is the fix;
    # the live database caught this, and no unit test could have.
    #
    # ⚠️ The refusal is also the guarantee: once FORCE is on, **no application
    # path can create a platform-authored definition**, because none of them can
    # produce a NULL tenant. Platform definitions come from migrations only, and
    # that is enforced rather than conventional.
    op.execute(
        sa.text(
            """
            INSERT INTO assessment_definitions
                (tenant_id, code, version, title, schema, calculation_bindings,
                 status, published_at)
            VALUES
                (NULL, 'nutrition_core', 1, :title, CAST(:schema AS jsonb),
                 CAST(:bindings AS jsonb), 'published', now())
            """
        ).bindparams(
            title="Nutrition assessment",
            schema=json.dumps(_SEED_SCHEMA),
            bindings=json.dumps(_SEED_BINDINGS),
        )
    )

    # ─── RLS (DB §17.1) ───────────────────────────────────────────────────
    #
    # 🔒 FORCE is not redundant with ENABLE: without it the table owner bypasses
    # every policy, and migrations run as `app_migrator`, which owns these.
    for table in _TENANT_SCOPED:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    op.execute(_ASSESSMENT_DEFINITIONS_POLICY)
    for table in _TENANT_SCOPED:
        if table != "assessment_definitions":
            op.execute(_TENANT_POLICY.format(table=table))

    # ⚠️ Last: once the revokes land, this migration's own connection keeps
    # working only because it runs as the migrator role.
    op.execute(_APPLY_GRANTS)


def downgrade() -> None:
    """Drop everything this revision created.

    ⚠️ 🔒 **Destroys clinical records.** Assessments, measurements, consultation
    notes and the index of every uploaded document go with these tables. The
    `files` rows survive (they are 0008's), so the effect is objects in storage
    whose clinical context is gone — recoverable only from the audit log.

    ⚠️ The document *bytes* are not touched. Arch §13.2 requires erasure to
    traverse object storage, and a schema downgrade is not an erasure: leaving
    the objects is correct here, and removing them would be a data-destroying
    side effect of a reversibility test.

    Reversibility exists so the chain is honestly testable in development. This
    is not a supported production operation.
    """
    op.execute(_RESTORE_DEFAULT_PRIVILEGES)

    op.drop_table("client_documents")
    op.drop_table("consultation_notes")
    op.drop_table("measurements")
    op.drop_table("client_nutrition_profile")
    op.drop_table("assessment_responses")
    op.drop_table("assessment_definitions")

    for name in (
        "goal_type",
        "activity_level",
        "measurement_source",
        "response_status",
        "definition_status",
    ):
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=False)
