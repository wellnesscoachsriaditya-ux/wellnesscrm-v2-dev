"""The plan-authoring schema must hold its structural claims — M4, DB §8.10–8.13.

🔒 These tests need no database. What they protect is that
``modules/nutrition/models.py`` and the migrations that create its tables say the
same thing — a drift nobody notices until a write fails in production with a
``NotNullViolation``, or worse, until a column the design requires turns out
never to have existed.

Three claims in particular, each reconciled by revision 0019 and each one line
someone could delete without another test going red:

* 🔒 ``plan_snapshots.content_hash`` exists and is **NOT NULL** (DB §8.13, DDR-12).
  It is the client portal's cache-validation key; a nullable one is a cache that
  silently serves a superseded plan (EC-M7-03).
* ``plan_snapshots.created_at`` exists — a snapshot that cannot say when it was
  taken is unplaceable in a clinical record.
* 🔒 ``plan_supplements.is_locked`` exists (DB §8.10). Locking must be
  *expressible* before anything can be trusted to respect it.

⚠️ What they cannot prove: that the SQL executes, that the NOT NULL bites on a
live cluster, or that the migration reverses. That is the isolation gate —
``tests/integration/nutrition/`` plus CI's downgrade/re-apply round trip.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from sqlalchemy import Boolean, DateTime, Text

from app.kernel import Base

_BACKEND = Path(__file__).resolve().parents[1]
_VERSIONS = _BACKEND / "migrations" / "versions"
_MIGRATION_0018 = _VERSIONS / "20260813_0018_nutrition_plans.py"
_MIGRATION_0019 = _VERSIONS / "20260813_0019_plan_schema_reconciliation.py"

#: The twelve tables revision 0018 creates. Named here rather than imported from
#: the migration so that a table quietly dropped from its ``_TENANT_SCOPED_TABLES``
#: fails the comparison below instead of shrinking it unnoticed.
_PLAN_TABLES = (
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


@pytest.fixture(scope="module")
def migration_0019() -> str:
    if not _MIGRATION_0019.is_file():
        pytest.fail(f"revision 0019 is missing: {_MIGRATION_0019}")
    return _MIGRATION_0019.read_text("utf-8")


@pytest.fixture(scope="module")
def migration_0018() -> str:
    return _MIGRATION_0018.read_text("utf-8")


# ─── Every plan table has a model ────────────────────────────────────────


@pytest.mark.parametrize("table", _PLAN_TABLES)
def test_every_plan_table_has_an_orm_model(table: str) -> None:
    """A table with no model is a table only raw SQL can reach."""
    assert table in Base.metadata.tables, f"{table} has no ORM model"


# ─── The three columns revision 0019 reconciles ──────────────────────────


def test_plan_snapshots_has_a_not_null_content_hash() -> None:
    """🔒 DDR-12 — the portal's cache-validation key, and it must not be nullable.

    A nullable ``content_hash`` is worse than an absent one: a client comparing
    ``null`` to ``null`` concludes nothing changed, which is precisely the
    silently-swapped-content failure EC-M7-03 asks us to make detectable.
    """
    column = Base.metadata.tables["plan_snapshots"].columns["content_hash"]

    assert isinstance(column.type, Text)
    assert column.nullable is False
    # 🔒 No default. A placeholder hash would compare equal across two genuinely
    # different documents — see the migration's own note on why it is not `''`.
    assert column.server_default is None


def test_plan_snapshots_has_created_at() -> None:
    column = Base.metadata.tables["plan_snapshots"].columns["created_at"]

    assert isinstance(column.type, DateTime)
    assert column.type.timezone is True
    assert column.nullable is False


def test_plan_supplements_can_be_locked() -> None:
    """🔒 DB §8.10 — locking is defined on the entities a practitioner fixes."""
    column = Base.metadata.tables["plan_supplements"].columns["is_locked"]

    assert isinstance(column.type, Boolean)
    assert column.nullable is False


def test_locking_is_expressible_on_every_entity_the_design_names() -> None:
    """🔒 DB §8.10 lists four. Three shipped in 0018; the fourth in 0019."""
    for table in ("template_items", "plan_items", "plan_slots", "plan_supplements"):
        assert (
            "is_locked" in Base.metadata.tables[table].columns
        ), f"{table} cannot be locked, so nothing can be asked to respect a lock on it"


# ─── The migration and the models agree ──────────────────────────────────


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("plan_snapshots", "content_hash"),
        ("plan_snapshots", "created_at"),
        ("plan_supplements", "is_locked"),
    ],
)
def test_revision_0019_adds_the_column_the_model_declares(
    migration_0019: str, table: str, column: str
) -> None:
    """The model is the contract; the migration is what makes it true of a database."""
    pattern = rf'add_column\(\s*"{table}",\s*sa\.Column\(\s*"{column}"'
    assert re.search(
        pattern, migration_0019
    ), f"{table}.{column} is on the model but revision 0019 does not add it"


def test_revision_0019_follows_0018(migration_0019: str) -> None:
    assert 'down_revision: str | None = "0018_nutrition_plans"' in migration_0019


def test_revision_0019_is_reversible(migration_0019: str) -> None:
    """🔒 A bad deploy must be rollable back — every added column is dropped again."""
    for table, column in (
        ("plan_snapshots", "content_hash"),
        ("plan_snapshots", "created_at"),
        ("plan_supplements", "is_locked"),
    ):
        assert f'op.drop_column("{table}", "{column}")' in migration_0019


def test_revision_0019_adds_no_table_and_no_type(migration_0019: str) -> None:
    """Scope guard. 0019 reconciles columns; creating anything here is drift."""
    assert "create_table" not in migration_0019
    assert "ENUM" not in migration_0019


# ─── What 0019 deliberately does not touch ───────────────────────────────


def test_snapshots_remain_undeletable_by_the_application(migration_0018: str) -> None:
    """🔒 EC-M4-03 — an issued plan's snapshot is a clinical record.

    0019 adds columns to this table and must not have relaxed the revoke that
    keeps the row itself immutable. Table-level grants cover columns added later,
    so there was nothing to re-grant — this asserts nobody "fixed" that by
    widening the grant instead.
    """
    assert "REVOKE DELETE ON TABLE plan_snapshots FROM app_user" in migration_0018


def test_revision_0019_changes_no_grants(migration_0019: str) -> None:
    """A table-level GRANT already covers a column added afterwards.

    ⚠️ Asserted against the *executable* statements, not the file text — the
    module docstring explains why no grant is needed and naturally contains the
    word.
    """
    executed = [
        node
        for node in ast.walk(ast.parse(migration_0019))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
    ]
    assert not executed, "revision 0019 issues raw SQL; it should only add columns"
