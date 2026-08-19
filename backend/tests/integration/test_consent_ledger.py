"""The consent ledger, against a live PostgreSQL — D3.

🔒 **Everything asserted here is invisible to a unit test**, which is why the
file exists. The append-only property of ``consent_records`` is a *grant*, not a
rule: no amount of care in ``kernel.consent`` would stop a future caller issuing
an UPDATE, so what must be proven is that the privilege to do so is absent. A
fake ledger would agree with whatever the code believed, including when wrong.

What this proves:

* 🔒 ``app_user`` cannot UPDATE or DELETE a consent record (DDR-15, NFR-047) —
  the ledger is evidence *against* application code, so application code is not
  what protects it;
* 🔒 a notice's presented text cannot be edited once written (FR-M0-029,
  NFR-051), while ``superseded_at`` alone may still be stamped — the narrow
  UPDATE that supersession needs;
* 🔒 ``app_user`` cannot write the purpose catalogue: a tenant inventing its own
  processing purposes is a compliance problem, not a feature;
* the subject check constraint refuses an entry nobody can be matched to;
* 🔒 a prospect consent insert succeeds with **no tenant context set**, which is
  the pre-client enquiry path (FR-M2-004) that a Pattern A RLS policy would have
  rejected — the reason this table deliberately has no policy.

⚠️ The notice used here comes from :func:`seeded_notice`, not from a migration.
0006 seeds the purpose catalogue but deliberately no notice: notice bodies are
legal text pending the privacy review (ASM-10), and shipping invented wording as
though it had been approved would be worse than shipping none.

⚠️ Needs the same setup as the tenant-isolation gate — see
``tests/integration/test_tenant_isolation.py`` for the provisioning sequence.
These fail rather than skip in CI, where ``REQUIRE_LIVE_DATABASE`` is set.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import cast

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = pytest.mark.asyncio

#: The seeded essential purpose from migration 0006. Referenced by code rather
#: than id: the id is generated per-database, the code is the stable contract.
SERVICE_DELIVERY = "service_delivery"


async def _purpose_id(connection: AsyncConnection, code: str) -> uuid.UUID:
    result = await connection.execute(
        text("SELECT id FROM consent_purposes WHERE code = :code"), {"code": code}
    )
    row = result.first()
    assert row is not None, (
        f"Purpose {code!r} is missing. Migration 0006 seeds the catalogue — "
        "run `alembic upgrade head` against the test database."
    )
    return cast(uuid.UUID, row.id)


@pytest_asyncio.fixture
async def seeded_notice(migrator_engine: AsyncEngine) -> AsyncIterator[uuid.UUID]:
    """One notice in force, created as the migrator.

    Test data, not seed data — see the module docstring. The version is
    randomised because ``uq_consent_notices__version_locale`` is unique and a
    fixed value would collide with a row left behind by an interrupted run.
    """
    version = f"test-{uuid.uuid4().hex[:8]}"

    async with migrator_engine.begin() as connection:
        purpose_id = await _purpose_id(connection, SERVICE_DELIVERY)
        result = await connection.execute(
            text(
                """
                INSERT INTO consent_notices (
                    purpose_ids, version, locale, title, body, effective_from
                ) VALUES (
                    ARRAY[:purpose_id]::uuid[], :version, 'en-IN',
                    'Test notice', 'Body used only by the integration suite.',
                    now() - interval '1 day'
                ) RETURNING id
                """
            ),
            {"purpose_id": purpose_id, "version": version},
        )
        notice_id = cast(uuid.UUID, result.scalar_one())

    yield notice_id

    async with migrator_engine.begin() as connection:
        # Ledger rows reference the notice, so they go first. This is also how
        # the app_user inserts below get cleaned up — app_user cannot DELETE
        # them, which is precisely the property this file asserts.
        await connection.execute(
            text("DELETE FROM consent_records WHERE notice_id = :id"), {"id": notice_id}
        )
        await connection.execute(
            text("DELETE FROM consent_notices WHERE id = :id"), {"id": notice_id}
        )


@pytest_asyncio.fixture
async def seeded_record(
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
) -> tuple[uuid.UUID, int]:
    """One committed ledger entry, inserted as the migrator.

    Seeded by ``app_migrator`` rather than ``app_user`` for the same reason
    ``seeded_tenants`` is: the point of the test is what ``app_user`` may do to a
    row that already exists. Cleanup rides on ``seeded_notice``.
    """
    tenant_id = seeded_tenants[0]
    async with migrator_engine.begin() as connection:
        purpose_id = await _purpose_id(connection, SERVICE_DELIVERY)
        result = await connection.execute(
            text(
                """
                INSERT INTO consent_records (
                    tenant_id, subject_type, subject_id, purpose_id, notice_id,
                    action, captured_via, captured_by_actor_type
                ) VALUES (
                    :tenant_id, 'client', :subject_id, :purpose_id, :notice_id,
                    'granted', 'practitioner', 'practitioner'
                ) RETURNING id
                """
            ),
            {
                "tenant_id": tenant_id,
                "subject_id": uuid.uuid4(),
                "purpose_id": purpose_id,
                "notice_id": seeded_notice,
            },
        )
        record_id = int(result.scalar_one())

    return tenant_id, record_id


# ─── Append-only, enforced by grant (DDR-15, NFR-047) ────────────────────


async def test_app_user_cannot_update_a_consent_record(
    app_engine: AsyncEngine,
    seeded_record: tuple[uuid.UUID, int],
) -> None:
    """🔒 The privilege to revise a consent decision does not exist."""
    _tenant_id, record_id = seeded_record

    with pytest.raises(DBAPIError) as excinfo:
        async with app_engine.begin() as connection:
            await connection.execute(
                text("UPDATE consent_records SET action = 'withdrawn' WHERE id = :id"),
                {"id": record_id},
            )

    assert "permission denied" in str(excinfo.value).lower()


async def test_app_user_cannot_delete_a_consent_record(
    app_engine: AsyncEngine,
    seeded_record: tuple[uuid.UUID, int],
) -> None:
    """🔒 A ledger the application can prune is not a legal record."""
    _tenant_id, record_id = seeded_record

    with pytest.raises(DBAPIError) as excinfo:
        async with app_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM consent_records WHERE id = :id"), {"id": record_id}
            )

    assert "permission denied" in str(excinfo.value).lower()


async def test_app_user_can_insert_and_select(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
) -> None:
    """The two verbs it does hold — otherwise the revoke went too far."""
    tenant_id = seeded_tenants[0]
    subject_id = uuid.uuid4()

    async with app_engine.begin() as connection:
        purpose_id = await _purpose_id(connection, SERVICE_DELIVERY)
        await connection.execute(
            text(
                """
                INSERT INTO consent_records (
                    tenant_id, subject_type, subject_id, purpose_id, notice_id,
                    action, captured_via, captured_by_actor_type
                ) VALUES (
                    :tenant_id, 'client', :subject_id, :purpose_id, :notice_id,
                    'granted', 'portal', 'client'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "subject_id": subject_id,
                "purpose_id": purpose_id,
                "notice_id": seeded_notice,
            },
        )

    async with app_engine.connect() as connection:
        result = await connection.execute(
            text("SELECT action FROM consent_records WHERE subject_id = :s"),
            {"s": subject_id},
        )
        assert result.scalar_one() == "granted"


# ─── Pre-client capture with no tenant context (FR-M2-004) ───────────────


async def test_prospect_consent_inserts_without_tenant_context(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
) -> None:
    """🔒 The insert a Pattern A policy would have rejected.

    The enquiry form captures consent before any client record or session
    exists, so no tenant context is set. This table therefore has no RLS policy
    — and this test is what would fail if someone added one.
    """
    tenant_id = seeded_tenants[0]

    async with app_engine.begin() as connection:
        purpose_id = await _purpose_id(connection, SERVICE_DELIVERY)
        # Deliberately no `SET LOCAL app.current_tenant_id`.
        await connection.execute(
            text(
                """
                INSERT INTO consent_records (
                    tenant_id, subject_type, subject_mobile_hash, purpose_id,
                    notice_id, action, captured_via, captured_by_actor_type
                ) VALUES (
                    :tenant_id, 'prospect', :mobile_hash, :purpose_id,
                    :notice_id, 'granted', 'enquiry_form', 'anonymous'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "mobile_hash": uuid.uuid4().hex,
                "purpose_id": purpose_id,
                "notice_id": seeded_notice,
            },
        )


async def test_a_record_needs_a_subject(
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
) -> None:
    """An entry nobody can be matched to is not evidence of anything."""
    tenant_id = seeded_tenants[0]

    with pytest.raises(DBAPIError) as excinfo:
        async with migrator_engine.begin() as connection:
            purpose_id = await _purpose_id(connection, SERVICE_DELIVERY)
            await connection.execute(
                text(
                    """
                    INSERT INTO consent_records (
                        tenant_id, subject_type, purpose_id, notice_id,
                        action, captured_via, captured_by_actor_type
                    ) VALUES (
                        :tenant_id, 'client', :purpose_id, :notice_id,
                        'granted', 'portal', 'client'
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "purpose_id": purpose_id,
                    "notice_id": seeded_notice,
                },
            )

    assert "ck_consent_records__subject_identified" in str(excinfo.value)


# ─── Notice immutability (FR-M0-029, NFR-051) ────────────────────────────


async def test_notice_body_cannot_be_edited(
    migrator_engine: AsyncEngine,
    seeded_notice: uuid.UUID,
) -> None:
    """🔒 Rewriting a notice rewrites the basis of every consent given under it.

    Asserted as the *migrator*, deliberately. ``app_user`` keeps UPDATE on this
    table for supersession, so a grant cannot express this rule — the trigger
    must hold even for the role that owns the schema.
    """
    with pytest.raises(DBAPIError) as excinfo:
        async with migrator_engine.begin() as connection:
            await connection.execute(
                text("UPDATE consent_notices SET body = 'rewritten' WHERE id = :id"),
                {"id": seeded_notice},
            )

    assert "immutable" in str(excinfo.value).lower()


async def test_notice_can_be_superseded(
    migrator_engine: AsyncEngine,
    seeded_notice: uuid.UUID,
) -> None:
    """The one UPDATE supersession needs — stamping ``superseded_at``."""
    async with migrator_engine.begin() as connection:
        await connection.execute(
            text("UPDATE consent_notices SET superseded_at = now() WHERE id = :id"),
            {"id": seeded_notice},
        )

    async with migrator_engine.connect() as connection:
        result = await connection.execute(
            text("SELECT superseded_at FROM consent_notices WHERE id = :id"),
            {"id": seeded_notice},
        )
        assert result.scalar_one() is not None


async def test_app_user_cannot_delete_a_notice(
    app_engine: AsyncEngine,
    seeded_notice: uuid.UUID,
) -> None:
    """Removing a notice orphans the basis of every consent given under it."""
    with pytest.raises(DBAPIError) as excinfo:
        async with app_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM consent_notices WHERE id = :id"), {"id": seeded_notice}
            )

    assert "permission denied" in str(excinfo.value).lower()


# ─── The catalogue is the platform's (DB §16.2) ──────────────────────────


async def test_app_user_cannot_write_the_purpose_catalogue(app_engine: AsyncEngine) -> None:
    """🔒 A tenant inventing processing purposes is a compliance problem."""
    with pytest.raises(DBAPIError) as excinfo:
        async with app_engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO consent_purposes (code, name, description, is_essential)
                    VALUES ('invented', 'Invented', 'Not ours to define', false)
                    """
                )
            )

    assert "permission denied" in str(excinfo.value).lower()


async def test_app_user_can_read_the_purpose_catalogue(app_engine: AsyncEngine) -> None:
    """Pattern D — tenants read the platform catalogue, they just cannot write."""
    async with app_engine.connect() as connection:
        result = await connection.execute(
            text("SELECT count(*) FROM consent_purposes WHERE is_active")
        )
        assert result.scalar_one() > 0
