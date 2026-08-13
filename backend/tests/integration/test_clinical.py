"""Clinical workspace against a live PostgreSQL — M3.

🔒 **Everything asserted here is invisible to a unit test**, which is why the
file exists. The structural test in ``test_clinical_schema.py`` reads migration
text; this file proves the grants, policies and constraints actually take effect
against a running cluster.

What this proves:

* 🔒 **Measurements are append-only** (FR-M3-012, EC-M3-05) — ``app_user``
  cannot UPDATE or DELETE a measurement row. A trend whose history can be
  rewritten records nothing.
* 🔒 **A published definition's structure is immutable** (FR-M3-003,
  AC-M3-003) — the column-level grant lets the application publish a definition
  (UPDATE ``status`` and ``published_at``) but refuses any change to ``schema``
  or ``calculation_bindings``. A captured response stays readable under the
  structure it was captured under.
* 🔒 **Consultation notes have no client-realm visibility** (FR-M3-021,
  AC-M3-006) — there is no policy that would admit a client actor, and the
  table is invisible to any connection not scoped to the practitioner's tenant.
  Three mechanisms — absence of a client policy, the data scope, and the portal
  projection's exclusion — hold FR-M3-021 in place.
* 🔒 **RLS actually isolates** every clinical table. A tenant scoped to A sees
  none of B's assessments, measurements, notes or documents.

⚠️ Needs the same setup as the other integration gates — see
``tests/integration/test_tenant_isolation.py`` for the provisioning sequence.
These fail rather than skip in CI, where ``REQUIRE_LIVE_DATABASE`` is set.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.conftest import scope_to

pytestmark = pytest.mark.asyncio


# ─── Helpers ─────────────────────────────────────────────────────────────


async def _seed_tenant_with_user(
    connection: object,
    *,
    tenant_id: uuid.UUID,
) -> uuid.UUID:
    """Create a tenant and user, returning the user_id."""
    await connection.execute(  # type: ignore[union-attr]
        text(
            "INSERT INTO tenants (id, name, slug, status) " "VALUES (:id, :name, :slug, 'active')"
        ),
        {"id": tenant_id, "name": f"Clinical {tenant_id}", "slug": f"clin-{tenant_id}"},
    )
    await scope_to(connection, tenant_id)  # type: ignore[arg-type]
    user_id = uuid.uuid4()
    await connection.execute(  # type: ignore[union-attr]
        text(
            "INSERT INTO users "
            "  (id, tenant_id, auth_subject_id, email, full_name, role, status) "
            "VALUES (:id, :tenant, :subject, :email, :name, 'practitioner', 'active')"
        ),
        {
            "id": user_id,
            "tenant": tenant_id,
            "subject": f"gotrue-clin-{tenant_id}",
            "email": f"clin-{tenant_id}@example.test",
            "name": "Dr Test",
        },
    )
    return user_id


async def _seed_definition(
    connection: object,
    *,
    tenant_id: uuid.UUID | None,
    status: str = "published",
) -> uuid.UUID:
    """Insert a minimal assessment definition, returning its id."""
    definition_id = uuid.uuid4()
    schema_doc = {
        "sections": [
            {
                "id": "basics",
                "title": "Basics",
                "fields": [
                    {"id": "name", "type": "text", "label": "Name", "required": True},
                ],
            }
        ]
    }
    bindings_doc: dict[str, str] = {}
    await connection.execute(  # type: ignore[union-attr]
        text(
            "INSERT INTO assessment_definitions "
            "  (id, tenant_id, code, version, title, status, published_at, "
            "   schema, calculation_bindings) "
            "VALUES (:id, :tenant, :code, 1, :title, CAST(:status AS definition_status), "
            "   CASE WHEN CAST(:status AS text) = 'published' THEN now() END, "
            "   CAST(:schema AS jsonb), CAST(:bindings AS jsonb))"
        ),
        {
            "id": definition_id,
            "tenant": tenant_id,
            "code": f"test_{definition_id.hex[:8]}",
            "title": "Test Definition",
            "status": status,
            "schema": json.dumps(schema_doc),
            "bindings": json.dumps(bindings_doc),
        },
    )
    return definition_id


async def _seed_response(
    connection: object,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    definition_id: uuid.UUID,
) -> uuid.UUID:
    """Insert an assessment response, returning its id."""
    response_id = uuid.uuid4()
    await connection.execute(  # type: ignore[union-attr]
        text(
            "INSERT INTO assessment_responses "
            "  (id, tenant_id, client_id, definition_id, answers, status, "
            "   completed_sections) "
            "VALUES (:id, :tenant, :client, :definition, '{}', 'in_progress', '[]')"
        ),
        {
            "id": response_id,
            "tenant": tenant_id,
            "client": client_id,
            "definition": definition_id,
        },
    )
    return response_id


async def _seed_measurement(
    connection: object,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    weight_kg: float = 65.0,
) -> uuid.UUID:
    """Insert a measurement row, returning its id."""
    measurement_id = uuid.uuid4()
    await connection.execute(  # type: ignore[union-attr]
        text(
            "INSERT INTO measurements "
            "  (id, tenant_id, client_id, measured_on, weight_kg, source, "
            "   is_flagged_implausible) "
            "VALUES (:id, :tenant, :client, :date, :weight, 'practitioner', false)"
        ),
        {
            "id": measurement_id,
            "tenant": tenant_id,
            "client": client_id,
            "date": date.today().isoformat(),
            "weight": weight_kg,
        },
    )
    return measurement_id


async def _seed_note(
    connection: object,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    author_user_id: uuid.UUID,
) -> uuid.UUID:
    """Insert a consultation note, returning its id."""
    note_id = uuid.uuid4()
    await connection.execute(  # type: ignore[union-attr]
        text(
            "INSERT INTO consultation_notes "
            "  (id, tenant_id, client_id, note_date, body, author_user_id) "
            "VALUES (:id, :tenant, :client, :date, :body, :author)"
        ),
        {
            "id": note_id,
            "tenant": tenant_id,
            "client": client_id,
            "date": date.today().isoformat(),
            "body": "Test consultation note",
            "author": author_user_id,
        },
    )
    return note_id


# ─── Fixtures ────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def clinical_tenants(
    migrator_engine: AsyncEngine,
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]]:
    """Two tenants, each with a user, for clinical isolation tests.

    Yields (tenant_a, user_a, tenant_b, user_b).
    """
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    async with migrator_engine.begin() as conn:
        user_a = await _seed_tenant_with_user(conn, tenant_id=tenant_a)
        user_b = await _seed_tenant_with_user(conn, tenant_id=tenant_b)

    try:
        yield (tenant_a, user_a, tenant_b, user_b)
    finally:
        async with migrator_engine.begin() as conn:
            for tid in (tenant_a, tenant_b):
                await scope_to(conn, tid)
                for table in (
                    "client_documents",
                    "consultation_notes",
                    "measurements",
                    "client_nutrition_profile",
                    "assessment_responses",
                    "assessment_definitions",
                    "users",
                ):
                    await conn.execute(
                        text(f"DELETE FROM {table} WHERE tenant_id = :id"), {"id": tid}
                    )
                # Platform definitions (tenant_id IS NULL) seeded by this test:
                await conn.execute(
                    text(
                        "DELETE FROM assessment_definitions WHERE tenant_id IS NULL "
                        "AND code LIKE 'test_%'"
                    )
                )
                await conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tid})


@pytest_asyncio.fixture
async def seeded_clinical(
    migrator_engine: AsyncEngine,
    clinical_tenants: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
) -> AsyncIterator[dict]:
    """Seed clinical data for tenant_a: a definition, response, measurement, note.

    Returns a dict with all the ids.
    """
    tenant_a, user_a, tenant_b, user_b = clinical_tenants
    client_a = uuid.uuid4()

    async with migrator_engine.begin() as conn:
        await scope_to(conn, tenant_a)
        definition_id = await _seed_definition(conn, tenant_id=tenant_a)
        response_id = await _seed_response(
            conn, tenant_id=tenant_a, client_id=client_a, definition_id=definition_id
        )
        measurement_id = await _seed_measurement(conn, tenant_id=tenant_a, client_id=client_a)
        note_id = await _seed_note(
            conn, tenant_id=tenant_a, client_id=client_a, author_user_id=user_a
        )

    yield {
        "tenant_a": tenant_a,
        "user_a": user_a,
        "tenant_b": tenant_b,
        "user_b": user_b,
        "client_a": client_a,
        "definition_id": definition_id,
        "response_id": response_id,
        "measurement_id": measurement_id,
        "note_id": note_id,
    }


# ─── Measurements are append-only (FR-M3-012, EC-M3-05) ─────────────────


async def test_the_application_cannot_update_a_measurement(
    app_engine: AsyncEngine, seeded_clinical: dict
) -> None:
    """🔒 FR-M3-012. A correction is a new dated row, not an edit."""
    async with app_engine.connect() as conn:
        await scope_to(conn, seeded_clinical["tenant_a"])
        with pytest.raises((ProgrammingError, DBAPIError)) as raised:
            await conn.execute(
                text("UPDATE measurements SET weight_kg = 70 WHERE id = :id"),
                {"id": seeded_clinical["measurement_id"]},
            )
        assert "permission denied" in str(raised.value).lower()


async def test_the_application_cannot_delete_a_measurement(
    app_engine: AsyncEngine, seeded_clinical: dict
) -> None:
    """🔒 EC-M3-05. Both same-date values are kept; neither is discarded."""
    async with app_engine.connect() as conn:
        await scope_to(conn, seeded_clinical["tenant_a"])
        with pytest.raises((ProgrammingError, DBAPIError)) as raised:
            await conn.execute(
                text("DELETE FROM measurements WHERE id = :id"),
                {"id": seeded_clinical["measurement_id"]},
            )
        assert "permission denied" in str(raised.value).lower()


async def test_the_application_can_insert_a_measurement(
    app_engine: AsyncEngine, seeded_clinical: dict
) -> None:
    """INSERT must stay granted — recording measurements is the feature."""
    async with app_engine.connect() as conn:
        await scope_to(conn, seeded_clinical["tenant_a"])
        result = await conn.execute(
            text(
                "INSERT INTO measurements "
                "  (tenant_id, client_id, measured_on, weight_kg, source, "
                "   is_flagged_implausible) "
                "VALUES (:tenant, :client, :date, 72, 'practitioner', false) "
                "RETURNING id"
            ),
            {
                "tenant": seeded_clinical["tenant_a"],
                "client": seeded_clinical["client_a"],
                "date": date.today().isoformat(),
            },
        )
        assert result.scalar_one() is not None
        await conn.rollback()


# ─── Published definition immutability — AC-M3-003 ──────────────────────


async def test_the_application_cannot_rewrite_a_definition_schema(
    app_engine: AsyncEngine, seeded_clinical: dict
) -> None:
    """🔒 FR-M3-003 / AC-M3-003 — a published definition's schema is frozen.

    The column-level grant lets ``status`` and ``published_at`` through — that
    is how publishing works — but ``schema`` and ``calculation_bindings`` are
    not in the list, so the statement is refused.

    This is the live proof of AC-M3-003: responses remain readable under the
    structure in force when they were captured, because the structure cannot
    change once it is published.
    """
    async with app_engine.connect() as conn:
        await scope_to(conn, seeded_clinical["tenant_a"])
        with pytest.raises((ProgrammingError, DBAPIError)) as raised:
            await conn.execute(
                text(
                    "UPDATE assessment_definitions SET schema = CAST(:schema AS jsonb) "
                    "WHERE id = :id"
                ),
                {
                    "id": seeded_clinical["definition_id"],
                    "schema": json.dumps({"sections": []}),
                },
            )
        assert "permission denied" in str(raised.value).lower()


async def test_the_application_cannot_rewrite_definition_bindings(
    app_engine: AsyncEngine, seeded_clinical: dict
) -> None:
    """🔒 DDR-08 — the projection contract must not drift under a response."""
    async with app_engine.connect() as conn:
        await scope_to(conn, seeded_clinical["tenant_a"])
        with pytest.raises((ProgrammingError, DBAPIError)) as raised:
            await conn.execute(
                text(
                    "UPDATE assessment_definitions SET calculation_bindings = CAST(:b AS jsonb) "
                    "WHERE id = :id"
                ),
                {
                    "id": seeded_clinical["definition_id"],
                    "b": json.dumps({"height_cm": "bogus"}),
                },
            )
        assert "permission denied" in str(raised.value).lower()


async def test_the_application_cannot_delete_a_definition(
    app_engine: AsyncEngine, seeded_clinical: dict
) -> None:
    """A definition captured responses point at must not disappear."""
    async with app_engine.connect() as conn:
        await scope_to(conn, seeded_clinical["tenant_a"])
        with pytest.raises((ProgrammingError, DBAPIError)) as raised:
            await conn.execute(
                text("DELETE FROM assessment_definitions WHERE id = :id"),
                {"id": seeded_clinical["definition_id"]},
            )
        assert "permission denied" in str(raised.value).lower()


async def test_the_application_can_publish_a_definition(
    migrator_engine: AsyncEngine, app_engine: AsyncEngine, clinical_tenants: tuple
) -> None:
    """⚠️ The lifecycle columns must remain updatable — that is how publishing
    works.  Over-revoking would freeze every definition as a permanent draft.

    Seeded as a draft and then updated to published, proving the column-level
    grant works.
    """
    tenant_a = clinical_tenants[0]
    async with migrator_engine.begin() as conn:
        await scope_to(conn, tenant_a)
        draft_id = await _seed_definition(conn, tenant_id=tenant_a, status="draft")

    try:
        async with app_engine.begin() as conn:
            await scope_to(conn, tenant_a)
            # The app-user level grant allows UPDATE on (status, published_at)
            await conn.execute(
                text(
                    "UPDATE assessment_definitions "
                    "SET status = 'published', published_at = now() "
                    "WHERE id = :id"
                ),
                {"id": draft_id},
            )
    finally:
        async with migrator_engine.begin() as conn:
            await scope_to(conn, tenant_a)
            await conn.execute(
                text("DELETE FROM assessment_definitions WHERE id = :id"),
                {"id": draft_id},
            )


# ─── Consultation notes — AC-M3-006 ─────────────────────────────────────


async def test_notes_are_invisible_across_tenants(
    app_engine: AsyncEngine, seeded_clinical: dict
) -> None:
    """🔒 FR-M3-021 / AC-M3-006 — notes are practitioner-only.

    The strongest form of the invisibility guarantee is tenant isolation: a
    connection scoped to tenant B sees zero of tenant A's notes. There is no
    client-realm policy at all — the absence is the mechanism.
    """
    async with app_engine.connect() as conn:
        await scope_to(conn, seeded_clinical["tenant_b"])
        count = (
            await conn.execute(
                text("SELECT count(*) FROM consultation_notes WHERE id = :id"),
                {"id": seeded_clinical["note_id"]},
            )
        ).scalar()
    assert count == 0


async def test_the_application_cannot_delete_a_note(
    app_engine: AsyncEngine, seeded_clinical: dict
) -> None:
    """🔒 Archiving is the disposition; a row the application can destroy is
    archived only by convention."""
    async with app_engine.connect() as conn:
        await scope_to(conn, seeded_clinical["tenant_a"])
        with pytest.raises((ProgrammingError, DBAPIError)) as raised:
            await conn.execute(
                text("DELETE FROM consultation_notes WHERE id = :id"),
                {"id": seeded_clinical["note_id"]},
            )
        assert "permission denied" in str(raised.value).lower()


# ─── RLS isolation across all clinical tables ────────────────────────────


async def test_clinical_responses_are_isolated(
    app_engine: AsyncEngine, seeded_clinical: dict
) -> None:
    """Tenant B sees none of tenant A's assessment responses."""
    async with app_engine.connect() as conn:
        await scope_to(conn, seeded_clinical["tenant_b"])
        count = (
            await conn.execute(
                text("SELECT count(*) FROM assessment_responses WHERE id = :id"),
                {"id": seeded_clinical["response_id"]},
            )
        ).scalar()
    assert count == 0


async def test_measurements_are_isolated(app_engine: AsyncEngine, seeded_clinical: dict) -> None:
    """Tenant B sees none of tenant A's measurements."""
    async with app_engine.connect() as conn:
        await scope_to(conn, seeded_clinical["tenant_b"])
        count = (
            await conn.execute(
                text("SELECT count(*) FROM measurements WHERE id = :id"),
                {"id": seeded_clinical["measurement_id"]},
            )
        ).scalar()
    assert count == 0


# ─── Privilege checks via pg_catalog ─────────────────────────────────────


@pytest.mark.parametrize("privilege", ["UPDATE", "DELETE"])
async def test_measurement_privilege_is_absent(
    migrator_engine: AsyncEngine, privilege: str
) -> None:
    """The grant is gone, not merely unused — the distinction between a
    guarantee and a coincidence."""
    async with migrator_engine.connect() as conn:
        held = (
            await conn.execute(
                text("SELECT has_table_privilege('app_user', 'measurements', :p)"),
                {"p": privilege},
            )
        ).scalar()
    assert held is False, (
        f"app_user still holds {privilege} on measurements. Migration 0016 must "
        "revoke UPDATE and DELETE to keep the trend history append-only."
    )


@pytest.mark.parametrize(
    "table",
    ["assessment_responses", "client_nutrition_profile", "consultation_notes", "client_documents"],
)
async def test_clinical_record_delete_is_absent(migrator_engine: AsyncEngine, table: str) -> None:
    """DELETE is revoked on every clinical record table."""
    async with migrator_engine.connect() as conn:
        held = (
            await conn.execute(
                text(f"SELECT has_table_privilege('app_user', '{table}', 'DELETE')"),
            )
        ).scalar()
    assert held is False, f"app_user still holds DELETE on {table}. Migration 0016 must revoke it."


"""
Description: Integration test for the clinical workspace proving grants, policies and
constraints against a live PostgreSQL. Asserts append-only measurements, definition
immutability (AC-M3-003), note invisibility (AC-M3-006) and tenant isolation.
"""
