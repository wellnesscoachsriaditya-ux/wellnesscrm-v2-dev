"""nutrition plans

Revision ID: 0018_nutrition_plans
Revises: 0017_nutrition
Create Date: 2026-08-13

All tables in this migration use Pattern A (tenant-scoped) RLS.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_nutrition_plans"
down_revision: str | None = "0017_nutrition"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PLAN_STATE = ("draft", "issued", "superseded", "discarded")
_PLAN_ORIGIN = ("manual", "template", "ai_draft", "revision")
_RENDER_STATUS = ("pending", "ready", "failed")

_TENANT_SCOPED_TABLES = (
    "diet_templates",
    "template_days",
    "template_slots",
    "template_items",
    "diet_plans",
    "diet_plan_versions",
    "plan_days",
    "plan_slots",
    "plan_items",
    "plan_item_alternatives",
    "plan_snapshots",
    "plan_supplements",
)

_APPLY_GRANTS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        REVOKE DELETE ON TABLE diet_templates FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE diet_templates TO app_user;

        REVOKE DELETE ON TABLE template_days FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE template_days TO app_user;

        REVOKE DELETE ON TABLE template_slots FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE template_slots TO app_user;

        REVOKE DELETE ON TABLE template_items FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE template_items TO app_user;

        REVOKE DELETE ON TABLE diet_plans FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE diet_plans TO app_user;

        REVOKE DELETE ON TABLE diet_plan_versions FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE diet_plan_versions TO app_user;

        REVOKE DELETE ON TABLE plan_days FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE plan_days TO app_user;

        REVOKE DELETE ON TABLE plan_slots FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE plan_slots TO app_user;

        REVOKE DELETE ON TABLE plan_items FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE plan_items TO app_user;

        REVOKE DELETE ON TABLE plan_item_alternatives FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE plan_item_alternatives TO app_user;

        REVOKE DELETE ON TABLE plan_snapshots FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE plan_snapshots TO app_user;

        REVOKE DELETE ON TABLE plan_supplements FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE plan_supplements TO app_user;
    END IF;
END
$$;
"""


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    postgresql.ENUM(*_PLAN_STATE, name="plan_state").create(op.get_bind())
    postgresql.ENUM(*_PLAN_ORIGIN, name="plan_origin").create(op.get_bind())
    postgresql.ENUM(*_RENDER_STATUS, name="render_status").create(op.get_bind())

    # diet_templates
    op.create_table(
        "diet_templates",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("goal_type", sa.Text(), nullable=True),
        sa.Column("condition_tags", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("dietary_class", _enum("dietary_classification"), nullable=True),
        sa.Column("target_energy_kcal", sa.Numeric(), nullable=True),
        sa.Column("day_count", sa.Integer(), nullable=False),
        sa.Column("usage_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # template_days
    op.create_table(
        "template_days",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("day_number", sa.Integer(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["template_id"], ["diet_templates.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # template_slots
    op.create_table(
        "template_slots",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("template_day_id", sa.Uuid(), nullable=False),
        sa.Column("slot_type", sa.Text(), nullable=False),
        sa.Column("custom_label", sa.Text(), nullable=True),
        sa.Column("target_time", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["template_day_id"], ["template_days.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # template_items
    op.create_table(
        "template_items",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("template_slot_id", sa.Uuid(), nullable=False),
        sa.Column("item_type", sa.Text(), nullable=False),
        sa.Column("food_id", sa.Uuid(), nullable=True),
        sa.Column("recipe_id", sa.Uuid(), nullable=True),
        sa.Column("meal_id", sa.Uuid(), nullable=True),
        sa.Column("quantity", sa.Numeric(), nullable=False),
        sa.Column("measure_unit_id", sa.Uuid(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_locked", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "(food_id IS NOT NULL)::int + (recipe_id IS NOT NULL)::int + "
            "(meal_id IS NOT NULL)::int = 1",
            name="ck_template_items__one_reference",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["template_slot_id"], ["template_slots.id"]),
        sa.ForeignKeyConstraint(["food_id"], ["foods.id"]),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"]),
        sa.ForeignKeyConstraint(["meal_id"], ["meals.id"]),
        sa.ForeignKeyConstraint(["measure_unit_id"], ["measure_units.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "template_slot_id", "sort_order", name="uq_template_items_order", deferrable=True
        ),
    )

    # diet_plans
    op.create_table(
        "diet_plans",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("goal_type", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # diet_plan_versions
    op.create_table(
        "diet_plan_versions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("state", _enum("plan_state"), nullable=False),
        sa.Column("origin", _enum("plan_origin"), nullable=False),
        sa.Column("source_template_id", sa.Uuid(), nullable=True),
        sa.Column("source_version_id", sa.Uuid(), nullable=True),
        sa.Column("ai_generation_id", sa.Uuid(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("target_energy_kcal", sa.Numeric(), nullable=True),
        sa.Column("target_protein_g", sa.Numeric(), nullable=True),
        sa.Column("target_carbs_g", sa.Numeric(), nullable=True),
        sa.Column("target_fat_g", sa.Numeric(), nullable=True),
        sa.Column("computed_totals", postgresql.JSONB(), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issued_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("practitioner_notes", sa.Text(), nullable=True),
        sa.Column("row_version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "(state != 'issued') OR (issued_at IS NOT NULL AND computed_totals IS NOT NULL)",
            name="ck_diet_plan_versions__issued_has_totals",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["plan_id"], ["diet_plans.id"]),
        sa.ForeignKeyConstraint(["source_template_id"], ["diet_templates.id"]),
        sa.ForeignKeyConstraint(["source_version_id"], ["diet_plan_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plan_id", "version_number", name="uq_diet_plan_versions__plan_version"
        ),
    )

    # Resolve circular dependency
    op.create_foreign_key(
        "fk_diet_plans_current_version",
        "diet_plans",
        "diet_plan_versions",
        ["current_version_id"],
        ["id"],
        use_alter=True,
    )

    op.create_index(
        "uq_diet_plan_versions__one_draft",
        "diet_plan_versions",
        ["plan_id"],
        unique=True,
        postgresql_where=sa.text("state = 'draft'"),
    )

    # plan_days
    op.create_table(
        "plan_days",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("plan_version_id", sa.Uuid(), nullable=False),
        sa.Column("day_number", sa.Integer(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["plan_version_id"], ["diet_plan_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # plan_slots
    op.create_table(
        "plan_slots",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("plan_day_id", sa.Uuid(), nullable=False),
        sa.Column("slot_type", sa.Text(), nullable=False),
        sa.Column("custom_label", sa.Text(), nullable=True),
        sa.Column("target_time", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_locked", sa.Boolean(), server_default="false", nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["plan_day_id"], ["plan_days.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # plan_items
    op.create_table(
        "plan_items",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("plan_slot_id", sa.Uuid(), nullable=False),
        sa.Column("item_type", sa.Text(), nullable=False),
        sa.Column("food_id", sa.Uuid(), nullable=True),
        sa.Column("recipe_id", sa.Uuid(), nullable=True),
        sa.Column("meal_id", sa.Uuid(), nullable=True),
        sa.Column("quantity", sa.Numeric(), nullable=False),
        sa.Column("measure_unit_id", sa.Uuid(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("client_note", sa.Text(), nullable=True),
        sa.Column("is_locked", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("resolved_grams", sa.Numeric(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "(food_id IS NOT NULL)::int + (recipe_id IS NOT NULL)::int + "
            "(meal_id IS NOT NULL)::int = 1",
            name="ck_plan_items__one_reference",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["plan_slot_id"], ["plan_slots.id"]),
        sa.ForeignKeyConstraint(["food_id"], ["foods.id"]),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"]),
        sa.ForeignKeyConstraint(["meal_id"], ["meals.id"]),
        sa.ForeignKeyConstraint(["measure_unit_id"], ["measure_units.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # plan_item_alternatives
    op.create_table(
        "plan_item_alternatives",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("plan_item_id", sa.Uuid(), nullable=False),
        sa.Column("item_type", sa.Text(), nullable=False),
        sa.Column("food_id", sa.Uuid(), nullable=True),
        sa.Column("recipe_id", sa.Uuid(), nullable=True),
        sa.Column("meal_id", sa.Uuid(), nullable=True),
        sa.Column("quantity", sa.Numeric(), nullable=False),
        sa.Column("measure_unit_id", sa.Uuid(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "(food_id IS NOT NULL)::int + (recipe_id IS NOT NULL)::int + "
            "(meal_id IS NOT NULL)::int = 1",
            name="ck_plan_item_alts__one_reference",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["plan_item_id"], ["plan_items.id"]),
        sa.ForeignKeyConstraint(["food_id"], ["foods.id"]),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"]),
        sa.ForeignKeyConstraint(["meal_id"], ["meals.id"]),
        sa.ForeignKeyConstraint(["measure_unit_id"], ["measure_units.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # plan_snapshots
    op.create_table(
        "plan_snapshots",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("plan_version_id", sa.Uuid(), nullable=False),
        sa.Column("document", postgresql.JSONB(), nullable=False),
        sa.Column("document_schema_version", sa.Integer(), nullable=False),
        sa.Column("pdf_file_id", sa.Uuid(), nullable=True),
        sa.Column("pdf_generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pdf_status", _enum("render_status"), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["plan_version_id"], ["diet_plan_versions.id"]),
        sa.ForeignKeyConstraint(["pdf_file_id"], ["files.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_version_id"),
    )

    # plan_supplements
    op.create_table(
        "plan_supplements",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("plan_version_id", sa.Uuid(), nullable=False),
        sa.Column("supplement_id", sa.Uuid(), nullable=False),
        sa.Column("dosage", sa.Text(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("frequency", sa.Text(), nullable=False),
        sa.Column("timing", sa.Text(), nullable=True),
        sa.Column("duration_days", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["plan_version_id"], ["diet_plan_versions.id"]),
        sa.ForeignKeyConstraint(["supplement_id"], ["supplements.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    for table_name in _TENANT_SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table_name}__tenant_isolation ON {table_name}
            AS PERMISSIVE FOR ALL
            TO app_user
            USING (tenant_id = current_setting('app.tenant_id')::uuid)
            WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid)
            """
        )

    op.execute(_APPLY_GRANTS)


def downgrade() -> None:
    # First remove the circular dependency
    op.drop_constraint("fk_diet_plans_current_version", "diet_plans", type_="foreignkey")

    for table_name in reversed(_TENANT_SCOPED_TABLES):
        op.drop_table(table_name)

    sa.Enum(name="plan_state").drop(op.get_bind(), checkfirst=False)
    sa.Enum(name="plan_origin").drop(op.get_bind(), checkfirst=False)
    sa.Enum(name="render_status").drop(op.get_bind(), checkfirst=False)
