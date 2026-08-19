"""nutrition catalogue

Revision ID: 0017_nutrition
Revises: 0016_clinical
Create Date: 2026-08-13

DDR-03: Pattern B RLS applied to `foods`, `food_portions`, `recipes`,
`supplements`. These tables have a nullable `tenant_id`.
Curated records (`tenant_id IS NULL`) are readable by all tenants but only
writable by the migrator.
Custom records (`tenant_id = current_setting(...)`) are isolated.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_nutrition"
down_revision: str | None = "0016_clinical"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_DIETARY_CLASSIFICATION = ("vegetarian", "eggetarian", "non_vegetarian", "vegan", "jain")

_PATTERN_B_TABLES = (
    "food_categories",
    "foods",
    "food_aliases",
    "food_portions",
    "recipes",
    "recipe_items",
    "supplements",
    "dietary_rules",
)

_TENANT_SCOPED = (
    "meals",
    "meal_items",
    "nutrition_targets",
    "food_search_misses",
)


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(name=name, create_type=False)


_APPLY_GRANTS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        -- DDR-15: Explicitly revoke UPDATE/DELETE where immutability is required
        -- or append-only access is intended.
        REVOKE UPDATE, DELETE ON TABLE food_categories FROM app_user;
        GRANT SELECT, INSERT ON TABLE food_categories TO app_user;

        REVOKE UPDATE, DELETE ON TABLE foods FROM app_user;
        GRANT SELECT, INSERT ON TABLE foods TO app_user;

        REVOKE UPDATE, DELETE ON TABLE food_aliases FROM app_user;
        GRANT SELECT, INSERT ON TABLE food_aliases TO app_user;

        REVOKE UPDATE, DELETE ON TABLE nutrients FROM app_user;
        GRANT SELECT ON TABLE nutrients TO app_user;

        REVOKE UPDATE, DELETE ON TABLE food_nutrients FROM app_user;
        GRANT SELECT, INSERT ON TABLE food_nutrients TO app_user;

        REVOKE UPDATE, DELETE ON TABLE measure_units FROM app_user;
        GRANT SELECT ON TABLE measure_units TO app_user;

        REVOKE UPDATE, DELETE ON TABLE food_portions FROM app_user;
        GRANT SELECT, INSERT ON TABLE food_portions TO app_user;

        REVOKE UPDATE, DELETE ON TABLE recipes FROM app_user;
        GRANT SELECT, INSERT ON TABLE recipes TO app_user;

        REVOKE UPDATE, DELETE ON TABLE recipe_items FROM app_user;
        GRANT SELECT, INSERT ON TABLE recipe_items TO app_user;

        REVOKE UPDATE, DELETE ON TABLE supplements FROM app_user;
        GRANT SELECT, INSERT ON TABLE supplements TO app_user;

        REVOKE UPDATE, DELETE ON TABLE dietary_rules FROM app_user;
        GRANT SELECT, INSERT ON TABLE dietary_rules TO app_user;

        -- Allow UPDATE on editable entities
        REVOKE DELETE ON TABLE meals FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE meals TO app_user;

        REVOKE DELETE ON TABLE meal_items FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE meal_items TO app_user;

        REVOKE DELETE ON TABLE nutrition_targets FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE nutrition_targets TO app_user;

        REVOKE UPDATE, DELETE ON TABLE food_search_misses FROM app_user;
        GRANT SELECT, INSERT ON TABLE food_search_misses TO app_user;
    END IF;
END
$$;
"""


def upgrade() -> None:
    postgresql.ENUM(*_DIETARY_CLASSIFICATION, name="dietary_classification").create(op.get_bind())  # type: ignore[no-untyped-call]

    op.create_table(
        "food_categories",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name"),
    )

    op.create_table(
        "foods",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("dietary_class", _enum("dietary_classification"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', name)",
                persisted=True,
            ),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["category_id"], ["food_categories.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index("ix_foods_search_vector", "foods", ["search_vector"], postgresql_using="gin")

    op.create_table(
        "food_aliases",
        sa.Column("food_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["food_id"], ["foods.id"]),
        sa.PrimaryKeyConstraint("food_id", "alias"),
    )

    op.create_table(
        "nutrients",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )

    op.create_table(
        "food_nutrients",
        sa.Column("food_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("nutrient_id", sa.Uuid(), nullable=False),
        sa.Column("amount_per_100g", sa.Numeric(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["food_id"], ["foods.id"]),
        sa.ForeignKeyConstraint(["nutrient_id"], ["nutrients.id"]),
        sa.PrimaryKeyConstraint("food_id", "nutrient_id"),
    )

    op.create_table(
        "measure_units",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "food_portions",
        sa.Column("food_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("measure_unit_id", sa.Uuid(), nullable=False),
        sa.Column("gram_weight", sa.Numeric(), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default="false", nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["food_id"], ["foods.id"]),
        sa.ForeignKeyConstraint(["measure_unit_id"], ["measure_units.id"]),
        sa.PrimaryKeyConstraint("food_id", "measure_unit_id"),
    )

    op.create_table(
        "recipes",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("yield_amount", sa.Numeric(), nullable=True),
        sa.Column("yield_unit_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["yield_unit_id"], ["measure_units.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "recipe_items",
        sa.Column("recipe_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("food_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Numeric(), nullable=False),
        sa.Column("measure_unit_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"]),
        sa.ForeignKeyConstraint(["food_id"], ["foods.id"]),
        sa.ForeignKeyConstraint(["measure_unit_id"], ["measure_units.id"]),
        sa.PrimaryKeyConstraint("recipe_id", "food_id"),
    )

    op.create_table(
        "supplements",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "dietary_rules",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("condition", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "meals",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "meal_items",
        sa.Column("meal_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("food_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Numeric(), nullable=False),
        sa.Column("measure_unit_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["meal_id"], ["meals.id"]),
        sa.ForeignKeyConstraint(["food_id"], ["foods.id"]),
        sa.ForeignKeyConstraint(["measure_unit_id"], ["measure_units.id"]),
        sa.PrimaryKeyConstraint("meal_id", "food_id"),
    )

    op.create_table(
        "nutrition_targets",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("energy_kcal", sa.Numeric(), nullable=True),
        sa.Column("protein_g", sa.Numeric(), nullable=True),
        sa.Column("carbs_g", sa.Numeric(), nullable=True),
        sa.Column("fat_g", sa.Numeric(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "food_search_misses",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column(
            "timestamp", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    for table_name in _PATTERN_B_TABLES:
        op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table_name}__shared_catalogue ON {table_name}
            AS PERMISSIVE FOR ALL
            TO app_user
            USING (tenant_id IS NULL OR tenant_id = current_setting('app.tenant_id')::uuid)
            WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid)
            """
        )

    op.execute("ALTER TABLE nutrients ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE measure_units ENABLE ROW LEVEL SECURITY")

    op.execute("CREATE POLICY nutrients__read_all ON nutrients FOR SELECT TO app_user USING (true)")
    op.execute(
        "CREATE POLICY measure_units__read_all ON measure_units FOR SELECT TO app_user USING (true)"
    )

    for table_name in _TENANT_SCOPED:
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
    """Drop everything this revision created."""
    op.drop_table("food_search_misses")
    op.drop_table("nutrition_targets")
    op.drop_table("meal_items")
    op.drop_table("meals")
    op.drop_table("dietary_rules")
    op.drop_table("supplements")
    op.drop_table("recipe_items")
    op.drop_table("recipes")
    op.drop_table("food_portions")
    op.drop_table("food_nutrients")
    op.drop_table("food_aliases")

    op.drop_index("ix_foods_search_vector", table_name="foods")
    op.drop_table("foods")

    op.drop_table("nutrients")
    op.drop_table("measure_units")
    op.drop_table("food_categories")

    sa.Enum(name="dietary_classification").drop(op.get_bind(), checkfirst=False)
