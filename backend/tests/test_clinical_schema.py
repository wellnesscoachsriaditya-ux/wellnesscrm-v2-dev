"""The clinical schema must hold its structural claims — S7/M3, DB §7.

🔒 These tests need no database. What they protect is the set of *claims*
migration 0016 and ``modules/clinical/models.py`` make, each of which is a line
someone could delete in a refactor without any other test going red:

* 🔒 **A published definition is immutable** (FR-M3-003, AC-M3-003). Enforced by a
  *column-level* UPDATE grant, so ``schema`` and ``calculation_bindings`` are
  unreachable to the application — the structure a captured response points at
  cannot move under it. That is the whole mechanism behind AC-M3-003, and it lives
  in one ``GRANT`` line.
* 🔒 **Measurements are append-only** (FR-M3-012, EC-M3-05). A trend whose history
  can be rewritten is not a record of anything.
* 🔒 **Consultation notes have no client-realm policy** (FR-M3-021, AC-M3-006) —
  the same absence-is-stronger-than-a-condition argument 0011 makes for
  ``client_notes``.
* 🔒 **``assessment_definitions`` is the one nullable-tenant table here**, and its
  widened branch must be SELECT-only: a tenant may read a platform definition and
  must not be able to write one.
* The ``definition_id`` invariant the migration *chose not to* enforce with a
  column-level grant, and explicitly deferred to this file.

⚠️ What they cannot prove: that the SQL executes, that RLS actually filters, or
that a revoke bites on a live cluster. A revoke that is present but misspelled
satisfies every assertion here. That is the isolation gate —
``tests/integration/test_clinical.py`` — which needs a real PostgreSQL.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.kernel import Base

_BACKEND = Path(__file__).resolve().parents[1]
_MIGRATION = _BACKEND / "migrations" / "versions" / "20260810_0016_clinical.py"

#: The five tables holding client data, plus the definition table. Named here
#: rather than imported so that a table dropped from the migration's own
#: ``_TENANT_SCOPED`` fails the comparison below instead of quietly shrinking it.
_CLIENT_DATA_TABLES = (
    "assessment_responses",
    "client_nutrition_profile",
    "measurements",
    "consultation_notes",
    "client_documents",
)


@pytest.fixture(scope="module")
def migration_source() -> str:
    if not _MIGRATION.is_file():
        pytest.fail(f"the clinical migration is missing: {_MIGRATION}")
    return _MIGRATION.read_text(encoding="utf-8")


# ─── Row Level Security ──────────────────────────────────────────────────


def _rls_loop_tables(source: str) -> set[str]:
    """The table names the migration's RLS loop actually iterates.

    🔒 Read from ``_TENANT_SCOPED`` in the migration's own AST rather than from a
    shared constant, matching ``test_kernel_storage_schema.py``. The statements
    are f-strings, so the literal table name never appears in the file and a
    substring search would find nothing.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign | ast.Assign):
            continue
        targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        names = {t.id for t in targets if isinstance(t, ast.Name)}
        if "_TENANT_SCOPED" not in names or node.value is None:
            continue
        return {
            element.value
            for element in getattr(node.value, "elts", [])
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        }
    pytest.fail("the migration does not declare _TENANT_SCOPED")


def test_every_clinical_table_is_tenant_scoped(migration_source: str) -> None:
    """🔒 AC-M0-003 — a table missing from the loop ships with no policy at all.

    The loop both ``ENABLE``s and ``FORCE``s RLS. A clinical table omitted here
    is readable across every tenant in the system, and no application-level test
    would notice: the module always passes a ``tenant_id`` in its WHERE clause,
    so it would keep returning the right rows right up until something didn't.
    """
    iterated = _rls_loop_tables(migration_source)
    for table in (*_CLIENT_DATA_TABLES, "assessment_definitions"):
        assert table in iterated, f"{table} would ship without an RLS policy"


def test_rls_is_forced_not_merely_enabled(migration_source: str) -> None:
    """🔒 ``ENABLE`` alone exempts the table owner, and migrations run as the owner.

    Every one of these tables is owned by ``app_migrator``. Without ``FORCE``, a
    future job or fixture connecting as the owner would read across every tenant
    and the policy would look present while doing nothing.
    """
    assert "FORCE ROW LEVEL SECURITY" in migration_source, (
        "the clinical tables are not FORCEd; the owner bypasses every policy and "
        "the isolation gate's own fixtures connect as the owner."
    )


def test_the_definition_policy_widens_reads_only(migration_source: str) -> None:
    """🔒 A platform definition is readable by every tenant and writable by none.

    ``tenant_id IS NULL`` means platform-authored, which is every definition at
    MVP — so the ``USING`` clause must admit it or ``nutrition_core`` is invisible
    and the module is dead on arrival. The ``WITH CHECK`` clause must *not*, or a
    tenant could author, edit or publish a definition every other tenant then
    answers questions from.

    ⚠️ The asymmetry is the assertion. A policy with the NULL branch in both
    clauses reads identically at a glance and is a cross-tenant write.
    """
    policy = re.search(
        r"CREATE POLICY assessment_definitions__tenant_isolation(.*?);",
        migration_source,
        re.DOTALL,
    )
    assert policy is not None, "the assessment_definitions policy is gone"
    body = policy.group(1)

    using = re.search(r"USING\s*\((.*?)\)\s*\n", body, re.DOTALL)
    check = re.search(r"WITH CHECK\s*\((.*?)\)", body, re.DOTALL)
    assert using is not None and check is not None, "the policy is missing a clause"

    assert "IS NULL" in using.group(1), (
        "the USING clause no longer admits platform-authored definitions, so "
        "`nutrition_core` is invisible to every tenant."
    )
    assert "IS NULL" not in check.group(1), (
        "the WITH CHECK clause admits `tenant_id IS NULL`, which lets a tenant "
        "write a platform definition that every other tenant would then read."
    )


def test_client_data_tables_use_the_plain_policy(migration_source: str) -> None:
    """⚠️ The nullable-tenant widening must not spread beyond definitions.

    ``_TENANT_POLICY`` is applied to the five tables holding client data. If one
    of them were given the definitions policy instead, a NULL ``tenant_id`` row
    would be readable by everyone — and every one of these tables has
    ``nullable=False`` on ``tenant_id``, so the bug would be invisible until
    something wrote a NULL.
    """
    policy = re.search(r"_TENANT_POLICY = \"\"\"(.*?)\"\"\"", migration_source, re.DOTALL)
    assert policy is not None, "_TENANT_POLICY is gone"
    assert "IS NULL" not in policy.group(1), (
        "the shared client-data policy admits a NULL tenant; that widening belongs "
        "only to the definition catalogue."
    )


def test_notes_have_no_client_realm_policy(migration_source: str) -> None:
    """🔒 FR-M3-021, AC-M3-006 — notes are never client-visible.

    The same argument 0011 makes for ``client_notes``: enforced by the *absence*
    of a policy, which is stronger than a condition because there is no clause to
    write wrongly. A Pattern C policy mentioning ``app.actor_id`` on
    ``consultation_notes`` would be the mechanism by which a client could read a
    practitioner's private note, so its absence is the assertion.
    """
    assert "actor_id" not in migration_source, (
        "migration 0016 mentions actor_id — a client-realm policy on "
        "consultation_notes would make AC-M3-006 a condition that can be written "
        "wrongly rather than an absence that cannot."
    )


# ─── Grants: what the application may not do ─────────────────────────────


def test_a_published_definition_is_immutable(migration_source: str) -> None:
    """🔒 FR-M3-003 / AC-M3-003, and this grant is the entire mechanism.

    Publishing has to change *something*, so UPDATE cannot simply be revoked —
    it is narrowed to the two lifecycle columns. What matters is that ``schema``
    and ``calculation_bindings`` are **not** in that list: a captured response
    points at its definition, and if the structure could be edited in place then
    "responses remain readable under the structure in force when they were
    captured" would be false for every response already taken.

    ⚠️ A broad ``GRANT UPDATE`` here passes every application-level test. The
    only thing that catches it is reading the column list.
    """
    assert re.search(
        r"REVOKE UPDATE, DELETE ON TABLE assessment_definitions FROM app_user", migration_source
    ), "assessment_definitions keeps a broad UPDATE; a published structure could be rewritten."

    granted = re.search(
        r"GRANT UPDATE \(([^)]*)\) ON TABLE assessment_definitions TO app_user", migration_source
    )
    assert granted is not None, (
        "the column-level UPDATE grant is gone. Either publishing is now "
        "impossible, or UPDATE was widened back to every column."
    )
    columns = {column.strip() for column in granted.group(1).split(",")}
    assert columns == {"status", "published_at"}, (
        f"the publishable column list is {sorted(columns)}; only the two lifecycle "
        "columns may be updatable, or a definition's structure can move under the "
        "responses that pin it."
    )


def test_a_new_version_is_still_possible(migration_source: str) -> None:
    """⚠️ Immutability must not become un-editability.

    FR-M3-002's promise is that the assessment changes without a migration. That
    only works if INSERT survives — a new *version* is a new row. Revoking INSERT
    alongside UPDATE would satisfy the test above and freeze the form forever.
    """
    assert re.search(
        r"GRANT SELECT, INSERT ON TABLE assessment_definitions TO app_user", migration_source
    ), "assessment_definitions lost INSERT; a new version could never be published."


def test_measurements_are_append_only(migration_source: str) -> None:
    """🔒 FR-M3-012, EC-M3-05 — a correction is a new dated row, not an edit.

    EC-M3-05 already expects several values for one date and requires both to be
    retained with source attribution. If UPDATE were available, "practitioner
    value takes display precedence" could be implemented by overwriting the
    client's — which discards the disagreement that is itself clinically
    interesting.
    """
    assert re.search(
        r"REVOKE UPDATE, DELETE ON TABLE measurements FROM app_user", migration_source
    ), "measurements keeps UPDATE or DELETE; the trend history could be rewritten."
    assert re.search(
        r"GRANT SELECT, INSERT ON TABLE measurements TO app_user", migration_source
    ), "measurements lost INSERT; nothing could be recorded."


@pytest.mark.parametrize(
    "table",
    ["assessment_responses", "client_nutrition_profile", "consultation_notes", "client_documents"],
)
def test_clinical_records_cannot_be_deleted(migration_source: str, table: str) -> None:
    """🔒 ``ops/db/001_roles.sql`` grants all four verbs by default, so each of
    these revokes is mandatory rather than decorative.

    Every one of these is a clinical record: an administration kept for
    comparison (FR-M3-007), the projection planning reads, a record of what was
    discussed, and a pointer to bytes DPDP erasure has to traverse. Archiving is
    the disposition; a row the application can destroy is archived only by
    convention.
    """
    assert re.search(rf"REVOKE DELETE ON TABLE {table} FROM app_user", migration_source), (
        f"{table} keeps DELETE. Erasure runs as the migrator role, so the "
        "application never needs it."
    )


def test_responses_keep_update_so_a_form_can_be_filled_in(migration_source: str) -> None:
    """⚠️ The deliberate exception. FR-M3-005 saves continuously, so an
    in-progress response is written on every keystroke-batch; revoking UPDATE
    here would make resumability impossible."""
    assert re.search(
        r"GRANT SELECT, INSERT, UPDATE ON TABLE assessment_responses TO app_user", migration_source
    )


def test_the_definition_id_invariant_is_asserted_here_as_promised(
    migration_source: str,
) -> None:
    """🔒 The invariant the migration deliberately did **not** enforce with a grant.

    ``definition_id`` is left updatable because a column-level grant listing
    fifteen columns is a list that rots — and the migration says so in a comment,
    naming *this file* as where the invariant is asserted instead. That comment is
    a promise; this test is the thing that makes it true rather than a note.

    What must hold: nothing in the module ever assigns ``definition_id`` after the
    row is created. A response that changed definitions mid-flight would render a
    client's captured answers against a structure they never saw, which is exactly
    what FR-M3-003 and EC-M3-03 forbid.
    """
    assert "definition_id" in migration_source, "the pinning column is gone"

    clinical = _BACKEND / "app" / "modules" / "clinical"
    offenders: list[str] = []
    for path in sorted(clinical.glob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            # An assignment to the *attribute*, which is the mutation that would
            # move a captured response. The keyword form in the constructor is how
            # the pin is set in the first place and must stay.
            if re.search(r"\.definition_id\s*=(?!=)", line):
                offenders.append(f"{path.name}:{number}")

    assert not offenders, (
        "definition_id is reassigned after creation at "
        f"{', '.join(offenders)}. A response must finish under the version it "
        "started (FR-M3-003, EC-M3-03); migration 0016 leaves the column "
        "updatable and names this test as the enforcement."
    )


# ─── Constraints ─────────────────────────────────────────────────────────


def test_a_completed_response_records_who_and_when(migration_source: str) -> None:
    """🔒 Both halves, because each answers a different requirement.

    FR-M3-007 compares administrations by date, and FR-M3-004's two paths (client
    via a link, practitioner on their behalf) are only distinguishable if
    ``completed_by`` is set. A completed row missing either is unanswerable.
    """
    assert "ck_assessment_responses__completion_recorded" in migration_source
    assert re.search(r"completed_at\s+IS\s+NOT\s+NULL", migration_source, re.IGNORECASE)
    assert re.search(r"completed_by\s+IS\s+NOT\s+NULL", migration_source, re.IGNORECASE)


def test_a_published_definition_records_when(migration_source: str) -> None:
    """🔒 FR-M3-003's "the structure in force when they were captured" is
    unanswerable if a version can be published with no publication time."""
    assert "ck_assessment_definitions__published_timestamped" in migration_source


def test_a_measurement_must_record_something(migration_source: str) -> None:
    """🔒 An empty submit would create a dated row that appears on the trend as a
    gap with a date — which reads as data loss rather than as a mistake."""
    assert "ck_measurements__at_least_one_value" in migration_source


def test_the_database_bounds_match_the_kernel(migration_source: str) -> None:
    """🔒 DB §7.5's CHECK bounds, mirrored in ``kernel.clinical``.

    Two copies of one rule, and they must agree. ``assert_storable`` refuses
    earlier and names the field; the CHECK is what holds if a future caller
    forgets to ask. If the kernel's bound were *wider* than the database's, the
    application would accept a value and then raise an opaque ``IntegrityError``
    from the flush instead of a field-level message.
    """
    from app.kernel.clinical import (
        MAX_HEIGHT_CM,
        MAX_WEIGHT_KG,
        MIN_HEIGHT_CM,
        MIN_WEIGHT_KG,
    )

    weight = re.search(r"weight_kg\s*>=\s*(\d+)\s*AND\s*weight_kg\s*<=\s*(\d+)", migration_source)
    height = re.search(r"height_cm\s*>=\s*(\d+)\s*AND\s*height_cm\s*<=\s*(\d+)", migration_source)
    assert weight is not None and height is not None, "the range CHECKs have moved"

    assert (int(weight.group(1)), int(weight.group(2))) == (
        int(MIN_WEIGHT_KG),
        int(MAX_WEIGHT_KG),
    ), "kernel.clinical's weight bounds disagree with the database CHECK"
    assert (int(height.group(1)), int(height.group(2))) == (
        int(MIN_HEIGHT_CM),
        int(MAX_HEIGHT_CM),
    ), "kernel.clinical's height bounds disagree with the database CHECK"


def test_one_profile_per_client(migration_source: str) -> None:
    """🔒 DDR-08 — this is the *current* picture, not a history.

    Without the unique constraint, "which profile is current" becomes a query
    every future reader has to get right, and ``nutrition`` would be the one
    getting it wrong.
    """
    assert "uq_client_nutrition_profile__client" in migration_source


def test_one_document_row_per_file(migration_source: str) -> None:
    """A retried attach would otherwise show a duplicate the practitioner cannot
    tell apart."""
    assert "uq_client_documents__file" in migration_source


def test_a_definition_version_is_unique_per_tenant(migration_source: str) -> None:
    """⚠️ ``tenant_id`` is in the key so a tenant's future custom v1 (FR-M3-010)
    cannot collide with the platform's ``nutrition_core`` v1."""
    key = re.search(r'UniqueConstraint\(\s*"code",\s*"version",\s*"tenant_id"', migration_source)
    assert key is not None, (
        "the definition key no longer includes tenant_id; a tenant authoring its "
        "own v1 would collide with the platform definition."
    )


# ─── The seed (DB §7.2) ──────────────────────────────────────────────────


def test_the_seed_captures_no_allergens_as_free_text(migration_source: str) -> None:
    """🔒 DB §7.4, FR-M5-006 — a missed allergen is a clinical incident.

    Allergens must be food ids, never parsed prose. The food catalogue arrives in
    S3, so v1 deliberately captures no allergens *at all* rather than capturing
    them as text: a ``text`` field named for allergies would be populated by
    clients, look like it worked, and feed nothing into the ADR-10 candidate
    filter. Omission is the safe state; v2 adds the ``food_ref`` field.
    """
    seed = re.search(r"_SEED_SCHEMA = \{(.*?)\n\}", migration_source, re.DOTALL)
    assert seed is not None, "_SEED_SCHEMA is gone"
    body = seed.group(1)

    for field_id in ("allergen_food_ids", "excluded_food_ids"):
        if f'"id": "{field_id}"' not in body:
            continue
        pytest.fail(
            f"the seed declares `{field_id}`, but the food catalogue does not exist "
            "until S3. Either it is a food_ref with nothing to pick from (a dead "
            "control) or it captures allergies as text, which DB §7.4 forbids."
        )


def test_the_seed_is_published(migration_source: str) -> None:
    """⚠️ ``current_definition`` serves published rows only, so a draft seed makes
    every assessment 404 with a message blaming configuration.

    ⚠️ Asserted against the INSERT's own column list and VALUES, not against the
    file as a whole: ``'published'`` appears in the enum declaration and in the
    grant comments, so a substring search over the whole source would pass even
    for a seed inserted as a draft.
    """
    insert = re.search(
        r"INSERT INTO assessment_definitions(.*?)\"\"\"", migration_source, re.DOTALL
    )
    assert insert is not None, "the seed INSERT is gone; no definition would exist at all"
    statement = insert.group(1)

    assert "'published'" in statement, (
        "the seeded definition is not inserted as published; `current_definition` "
        "would refuse to serve it and no assessment could be started."
    )
    # 🔒 The CHECK constraint requires both together. A published row with no
    # publication time is rejected by the database, so the seed would abort the
    # migration rather than ship a subtly wrong row — but naming it here is what
    # explains *why* the `now()` is not decorative.
    assert "published_at" in statement and "now()" in statement, (
        "the published seed records no publication time, which "
        "ck_assessment_definitions__published_timestamped will reject."
    )


def test_every_binding_names_a_field_in_the_seed(migration_source: str) -> None:
    """🔒 DDR-08 — a binding naming a missing field drops a clinical value silently.

    ``_load`` re-validates on every read, so a bad binding fails loudly at
    runtime. This catches it at the point the seed is written instead, which is
    where it is cheap.
    """
    seed = re.search(r"_SEED_SCHEMA = \{(.*?)\n\}", migration_source, re.DOTALL)
    bindings = re.search(r"_SEED_BINDINGS[^=]*= \{(.*?)\n\}", migration_source, re.DOTALL)
    assert seed is not None, "_SEED_SCHEMA is gone"
    if bindings is None:
        pytest.skip("the seed declares no calculation bindings under that name")

    declared = set(re.findall(r'"id": "(\w+)"', seed.group(1)))
    bound = set(re.findall(r':\s*"(\w+)"', bindings.group(1)))
    missing = bound - declared
    assert not missing, (
        f"these bindings name fields the seed does not declare: {sorted(missing)}. "
        "The projection would silently drop them (DDR-08)."
    )


def test_weight_is_not_a_projection_target(migration_source: str) -> None:
    """🔒 One source of truth for weight — it is longitudinal (FR-M3-011).

    ``client_nutrition_profile`` has no weight column and the assessment's weight
    answer becomes a ``measurements`` row instead. A binding projecting weight
    into the profile would create a second figure that disagrees with the trend
    chart the moment either changed.
    """
    profile = Base.metadata.tables["client_nutrition_profile"]
    assert "weight_kg" not in profile.columns, (
        "client_nutrition_profile grew a weight column. Weight belongs in "
        "`measurements` (FR-M3-011); two copies drift."
    )


# ─── Reversibility ───────────────────────────────────────────────────────


def test_the_migration_reverses(migration_source: str) -> None:
    """Forward-only chain, reversible steps — the S0 migration policy.

    ⚠️ Children before parents in the downgrade, or the foreign keys refuse.
    ``client_nutrition_profile`` references ``assessment_responses``, which
    references ``assessment_definitions``.
    """
    downgrade = migration_source.split("def downgrade()")[1]
    order = [
        downgrade.index("client_nutrition_profile"),
        downgrade.index("assessment_responses"),
        downgrade.index("assessment_definitions"),
    ]
    assert order == sorted(order), (
        "the downgrade drops the assessment tables in an order the foreign keys "
        "will refuse: the profile references a response, which references a "
        "definition."
    )


def test_the_downgrade_restores_the_default_privileges(migration_source: str) -> None:
    """⚠️ A downgrade that leaves REVOKEs behind is not a reversal.

    The round-trip in CI would still pass — the tables are dropped and recreated
    — but a *partial* downgrade on a real cluster would leave ``app_user`` without
    privileges the next upgrade assumes it has.
    """
    assert "_RESTORE_DEFAULT_PRIVILEGES" in migration_source


# ─── The live gate cannot be dropped ─────────────────────────────────────


def test_the_clinical_schema_is_covered_by_an_executable_test() -> None:
    """⚠️ Everything above reads migration text; none of it runs SQL.

    A revoke that is present but misspelled satisfies every assertion in this
    file — ``REVOKE UPDATE ON TABLE measurments`` would pass a regex written
    against the same typo. Only PostgreSQL can refuse the statement, so the
    integration file must keep existing and must still assert the privilege
    errors.
    """
    live = _BACKEND / "tests" / "integration" / "test_clinical.py"
    assert live.is_file(), (
        "the live clinical gate is missing; the grants and policies above are "
        "unproven against a cluster."
    )
    source = live.read_text(encoding="utf-8")
    assert "permission denied" in source, (
        "the live gate no longer asserts a privilege error, so the append-only "
        "and no-delete grants would be unproven."
    )
    for criterion in ("AC-M3-003", "AC-M3-006"):
        assert criterion in source, (
            f"the live gate no longer names {criterion} — definition versioning "
            "and note invisibility are the two security-relevant criteria of this "
            "slice."
        )
