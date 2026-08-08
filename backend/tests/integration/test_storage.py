"""Storage against a live PostgreSQL — S1 Slice F.

🔒 **Everything asserted here is invisible to a unit test**, which is why the file
exists. The revoked DELETE on ``files`` is a *grant*; the isolation of the file
index is a *policy*. No amount of care in ``platform.storage`` would stop a future
caller issuing a DELETE — what must be proven is that the database refuses it.

What this proves:

* 🔒 ``app_user`` cannot DELETE a file row (Arch §13.2) — the row is the evidence
  an object existed, and FR-M0-027 erasure must show the bytes were destroyed;
* 🔒 **RLS actually isolates the file index** — AC-M0-003 applied to this slice.
  A tenant scoped to A sees none of B's files and cannot write a row carrying
  B's id. A leak here hands out storage keys, and the object store has no policy
  of its own to fall back on;
* 🔒 ``storage_key`` is globally unique, so two rows cannot point at one object
  even across tenants;
* the live quota sum counts only confirmed, undeleted rows (FR-M0-040), so a soft
  delete frees allowance immediately and a pending upload holds none;
* the lifecycle CHECK constraint rejects a status that disagrees with its
  timestamps.

⚠️ Needs the same setup as the other integration gates — see
``tests/integration/test_tenant_isolation.py`` for the provisioning sequence.
These fail rather than skip in CI, where ``REQUIRE_LIVE_DATABASE`` is set.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.kernel.models import FileClass
from app.kernel.storage import build_storage_key
from app.platform.storage import current_usage_bytes
from tests.integration.conftest import scope_to

pytestmark = pytest.mark.asyncio


async def _insert_file(
    connection: object,
    *,
    tenant_id: uuid.UUID,
    status: str = "confirmed",
    size_bytes: int = 1024,
    clinical: bool = True,
    deleted: bool = False,
) -> uuid.UUID:
    """Insert one file row directly, returning its id.

    Raw SQL rather than the ORM: these tests are about what the *database*
    permits, so going through the service layer would put the thing under test
    behind the thing being trusted.

    ⚠️ The lifecycle timestamps are computed here rather than with a ``CASE`` in
    the statement. Reusing one bind parameter as both a ``file_status`` value and
    a member of a text ``IN`` list makes PostgreSQL unable to deduce a single
    type for it, and the statement fails with ``AmbiguousParameter`` — a
    confusing error for what is really a test-helper convenience.
    """
    effective_status = "deleted" if deleted else status
    stamped = "now()"
    confirmed_at = stamped if effective_status in ("confirmed", "deleted") else "NULL"
    deleted_at = stamped if effective_status == "deleted" else "NULL"

    key = build_storage_key(tenant_id=tenant_id, file_class=FileClass.CLIENT_DOCUMENT)
    result = await connection.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO files (tenant_id, storage_key, bucket, original_filename, "
            "  content_type, size_bytes, file_class, contains_clinical_data, "
            "  uploaded_by_actor_type, status, confirmed_at, deleted_at) "
            "VALUES (:t, :k, 'client-documents', 'report.pdf', 'application/pdf', "
            "  :size, 'client_document', :clinical, 'practitioner', "
            f"  CAST(:status AS file_status), {confirmed_at}, {deleted_at}) "
            "RETURNING id"
        ),
        {
            "t": tenant_id,
            "k": key,
            "size": size_bytes,
            "clinical": clinical,
            "status": effective_status,
        },
    )
    return result.scalar_one()


@pytest.fixture
async def clean_files(migrator_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]):
    """Remove every file row for the seeded tenants, before and after.

    ⚠️ As ``app_migrator``, because ``app_user`` cannot DELETE — the restriction
    this file asserts. Under each tenant's own scope, since FORCE means even the
    owner is subject to the policy and an unscoped DELETE would remove nothing.
    """

    async def _purge() -> None:
        async with migrator_engine.begin() as connection:
            for tenant_id in seeded_tenants:
                await scope_to(connection, tenant_id)
                await connection.execute(
                    text("DELETE FROM files WHERE tenant_id = :t"), {"t": tenant_id}
                )

    await _purge()
    yield
    await _purge()


# ─── The row survives the object (Arch §13.2) ────────────────────────────


async def test_app_user_cannot_delete_a_file_row(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_files: None
) -> None:
    """🔒 The row is the evidence that an object existed.

    FR-M0-027 erasure has to be able to show the bytes were destroyed, and a
    deleted row proves nothing. Deletion is ``deleted_at``, and the privilege to
    do otherwise must not exist.
    """
    tenant_a, _ = seeded_tenants

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        await _insert_file(connection, tenant_id=tenant_a)

    with pytest.raises(DBAPIError) as excinfo:
        async with app_engine.begin() as connection:
            await scope_to(connection, tenant_a)
            await connection.execute(
                text("DELETE FROM files WHERE tenant_id = :t"), {"t": tenant_a}
            )
    assert "permission denied" in str(excinfo.value).lower()


async def test_soft_delete_is_permitted(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_files: None
) -> None:
    """⚠️ Soft delete is an UPDATE, so revoking UPDATE would break deletion."""
    tenant_a, _ = seeded_tenants

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        file_id = await _insert_file(connection, tenant_id=tenant_a)
        await connection.execute(
            text("UPDATE files SET status = 'deleted', deleted_at = now() WHERE id = :i"),
            {"i": file_id},
        )
        status = (
            await connection.execute(text("SELECT status FROM files WHERE id = :i"), {"i": file_id})
        ).scalar_one()

    assert status == "deleted"


# ─── Isolation (AC-M0-003) ───────────────────────────────────────────────


async def test_a_tenant_cannot_see_another_tenants_files(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_files: None
) -> None:
    """🔒 A leak here hands out storage keys.

    The object store has no policy of its own — the key *is* the capability once
    a URL can be signed for it, so the file index is the whole boundary.
    """
    tenant_a, tenant_b = seeded_tenants

    for tenant_id in (tenant_a, tenant_b):
        async with app_engine.begin() as connection:
            await scope_to(connection, tenant_id)
            await _insert_file(connection, tenant_id=tenant_id)

    async with app_engine.connect() as connection:
        await scope_to(connection, tenant_a)
        rows = (await connection.execute(text("SELECT tenant_id FROM files"))).scalars().all()

    assert list(rows) == [tenant_a]


async def test_a_tenant_cannot_write_a_row_for_another_tenant(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_files: None
) -> None:
    """🔒 ``WITH CHECK`` — the direction a read-only policy misses entirely."""
    tenant_a, tenant_b = seeded_tenants

    with pytest.raises(DBAPIError) as excinfo:
        async with app_engine.begin() as connection:
            await scope_to(connection, tenant_a)
            await _insert_file(connection, tenant_id=tenant_b)
    assert "row-level security" in str(excinfo.value).lower()


async def test_an_unscoped_read_returns_nothing(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_files: None
) -> None:
    """🔒 No tenant in scope must mean no rows, never all rows.

    ⚠️ The failure direction matters: a missing scope that returned everything
    would be a silent cross-tenant read on any path that forgot to set it.
    """
    tenant_a, _ = seeded_tenants

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        await _insert_file(connection, tenant_id=tenant_a)

    async with app_engine.connect() as connection:
        rows = (await connection.execute(text("SELECT id FROM files"))).scalars().all()

    assert list(rows) == []


async def test_the_owner_is_subject_to_the_policy_too(
    migrator_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_files: None
) -> None:
    """🔒 FORCE ROW LEVEL SECURITY — the V1 failure ADR-02 exists to prevent.

    ``app_migrator`` owns the table, and without FORCE an owner bypasses every
    policy while ``pg_policies`` still shows the policy present.
    """
    tenant_a, _ = seeded_tenants

    async with migrator_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        await _insert_file(connection, tenant_id=tenant_a)

    async with migrator_engine.connect() as connection:
        rows = (await connection.execute(text("SELECT id FROM files"))).scalars().all()

    assert list(rows) == [], "the table owner bypassed RLS: FORCE is missing"


# ─── Uniqueness ──────────────────────────────────────────────────────────


async def test_one_object_cannot_have_two_rows(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_files: None
) -> None:
    """🔒 Two rows pointing at one object means deleting either orphans the other."""
    tenant_a, _ = seeded_tenants
    key = build_storage_key(tenant_id=tenant_a, file_class=FileClass.PLAN_PDF)

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        for _ in range(2):
            statement = text(
                "INSERT INTO files (tenant_id, storage_key, bucket, original_filename, "
                "  content_type, size_bytes, file_class, contains_clinical_data, "
                "  uploaded_by_actor_type, status, confirmed_at) "
                "VALUES (:t, :k, 'plans', 'plan.pdf', 'application/pdf', 10, "
                "  'plan_pdf', true, 'practitioner', 'confirmed', now())"
            )
            if _ == 0:
                await connection.execute(statement, {"t": tenant_a, "k": key})
            else:
                with pytest.raises(IntegrityError):
                    await connection.execute(statement, {"t": tenant_a, "k": key})


# ─── Quota (FR-M0-040) ───────────────────────────────────────────────────


async def test_quota_counts_only_confirmed_undeleted_files(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_files: None
) -> None:
    """🔒 The predicate must match ``ix_files__tenant_live`` exactly.

    A pending upload holding quota would let an abandoned upload hold a tenant's
    allowance hostage until the reaper ran; a soft-deleted file still counted
    would mean deleting a file never gives the space back.
    """
    tenant_a, _ = seeded_tenants
    sessionmaker = async_sessionmaker(app_engine, expire_on_commit=False)

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        await _insert_file(connection, tenant_id=tenant_a, size_bytes=1000, status="confirmed")
        await _insert_file(connection, tenant_id=tenant_a, size_bytes=9999, status="pending")
        await _insert_file(connection, tenant_id=tenant_a, size_bytes=5000, deleted=True)

    async with sessionmaker() as session:
        await scope_to(session, tenant_a)
        used = await current_usage_bytes(session, tenant_id=tenant_a)

    assert used == 1000


async def test_quota_is_zero_for_a_tenant_with_no_files(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_files: None
) -> None:
    """⚠️ ``SUM`` over no rows is NULL, and NULL propagates into the entitlement
    arithmetic as an indeterminate limit rather than as zero usage."""
    _, tenant_b = seeded_tenants
    sessionmaker = async_sessionmaker(app_engine, expire_on_commit=False)

    async with sessionmaker() as session:
        await scope_to(session, tenant_b)
        assert await current_usage_bytes(session, tenant_id=tenant_b) == 0


async def test_quota_does_not_include_another_tenants_files(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_files: None
) -> None:
    """🔒 A sum that crossed tenants would bill one practice for another's files."""
    tenant_a, tenant_b = seeded_tenants
    sessionmaker = async_sessionmaker(app_engine, expire_on_commit=False)

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_b)
        await _insert_file(connection, tenant_id=tenant_b, size_bytes=8000)

    async with sessionmaker() as session:
        await scope_to(session, tenant_a)
        assert await current_usage_bytes(session, tenant_id=tenant_a) == 0


# ─── Constraints ─────────────────────────────────────────────────────────


async def test_a_zero_byte_file_is_rejected(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_files: None
) -> None:
    """🔒 A zero-byte row is a failed upload recorded as a success."""
    tenant_a, _ = seeded_tenants

    with pytest.raises(IntegrityError):
        async with app_engine.begin() as connection:
            await scope_to(connection, tenant_a)
            await _insert_file(connection, tenant_id=tenant_a, size_bytes=0)


async def test_a_confirmed_file_must_carry_a_confirmation_time(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_files: None
) -> None:
    """🔒 Status and timestamp are two representations of one fact.

    If they can disagree, the quota sum and the retrievability check disagree —
    one reads ``status``, the other reads ``deleted_at``.
    """
    tenant_a, _ = seeded_tenants

    with pytest.raises(IntegrityError):
        async with app_engine.begin() as connection:
            await scope_to(connection, tenant_a)
            await connection.execute(
                text(
                    "INSERT INTO files (tenant_id, storage_key, bucket, original_filename, "
                    "  content_type, size_bytes, file_class, contains_clinical_data, "
                    "  uploaded_by_actor_type, status) "
                    "VALUES (:t, :k, 'b', 'f.pdf', 'application/pdf', 10, "
                    "  'client_document', true, 'practitioner', 'confirmed')"
                ),
                {
                    "t": tenant_a,
                    "k": build_storage_key(
                        tenant_id=tenant_a, file_class=FileClass.CLIENT_DOCUMENT
                    ),
                },
            )


async def test_a_deleted_file_must_carry_a_deletion_time(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_files: None
) -> None:
    tenant_a, _ = seeded_tenants

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        file_id = await _insert_file(connection, tenant_id=tenant_a)

    with pytest.raises(IntegrityError):
        async with app_engine.begin() as connection:
            await scope_to(connection, tenant_a)
            await connection.execute(
                text("UPDATE files SET status = 'deleted' WHERE id = :i"), {"i": file_id}
            )


# ─── Erasure traversal (Arch §13.2) ──────────────────────────────────────


async def test_clinical_files_are_findable_for_erasure(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_files: None
) -> None:
    """🔒 Arch §13.2 — "deleting a database row while its file persists in object
    storage is a compliance failure". This is the query that stops that being an
    assumption.

    ⚠️ Soft-deleted rows are included deliberately: a file marked deleted whose
    bytes were never purged is exactly the case erasure must catch.
    """
    from app.platform.storage import clinical_files_for_erasure

    tenant_a, _ = seeded_tenants
    sessionmaker = async_sessionmaker(app_engine, expire_on_commit=False)

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        await _insert_file(connection, tenant_id=tenant_a, clinical=True)
        await _insert_file(connection, tenant_id=tenant_a, clinical=True, deleted=True)
        await _insert_file(connection, tenant_id=tenant_a, clinical=False)

    async with sessionmaker() as session:
        await scope_to(session, tenant_a)
        found = await clinical_files_for_erasure(session, tenant_id=tenant_a)

    assert len(found) == 2, "erasure would walk past a clinical file"


async def test_erasure_does_not_reach_another_tenants_files(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_files: None
) -> None:
    """🔒 An erasure that crossed tenants would destroy another practice's records."""
    from app.platform.storage import clinical_files_for_erasure

    tenant_a, tenant_b = seeded_tenants
    sessionmaker = async_sessionmaker(app_engine, expire_on_commit=False)

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_b)
        await _insert_file(connection, tenant_id=tenant_b, clinical=True)

    async with sessionmaker() as session:
        await scope_to(session, tenant_a)
        assert await clinical_files_for_erasure(session, tenant_id=tenant_a) == []


# ─── The quota feeds the entitlement (FR-M0-040) ─────────────────────────


async def test_storage_allowance_reads_live_usage(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_files: None,
) -> None:
    """🔒 The Slice E/F seam — storage is counted live, not from a counter.

    A monthly ``usage_counters`` row would reset every 1st and would have no way
    to give bytes back on deletion, so a tenant could store unbounded data by
    waiting for the calendar.
    """
    from app.platform.storage import storage_allowance

    tenant_a, _ = seeded_tenants
    sessionmaker = async_sessionmaker(app_engine, expire_on_commit=False)

    async with migrator_engine.begin() as connection:
        plan_id = (
            await connection.execute(text("SELECT id FROM plan_definitions WHERE code = 'starter'"))
        ).scalar_one()
        await scope_to(connection, tenant_a)
        await connection.execute(
            text(
                "INSERT INTO subscriptions (tenant_id, plan_definition_id, status) "
                "VALUES (:t, :p, 'active') ON CONFLICT (tenant_id) DO NOTHING"
            ),
            {"t": tenant_a, "p": plan_id},
        )

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        await _insert_file(connection, tenant_id=tenant_a, size_bytes=2 * 1024 * 1024)

    try:
        async with sessionmaker() as session:
            await scope_to(session, tenant_a)
            allowance = await storage_allowance(session, tenant_id=tenant_a)

        assert allowance.is_determinate
        assert allowance.used == Decimal(2)
        assert allowance.limit is not None and allowance.limit > 0
    finally:
        async with migrator_engine.begin() as connection:
            await scope_to(connection, tenant_a)
            await connection.execute(
                text("DELETE FROM subscriptions WHERE tenant_id = :t"), {"t": tenant_a}
            )


async def test_a_pending_upload_does_not_consume_the_allowance(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_files: None
) -> None:
    """⚠️ Otherwise an abandoned upload holds a tenant's quota until the reaper runs."""
    tenant_a, _ = seeded_tenants
    sessionmaker = async_sessionmaker(app_engine, expire_on_commit=False)

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        await _insert_file(
            connection, tenant_id=tenant_a, size_bytes=50 * 1024 * 1024, status="pending"
        )

    async with sessionmaker() as session:
        await scope_to(session, tenant_a)
        assert await current_usage_bytes(session, tenant_id=tenant_a) == 0


async def test_the_orphan_reaper_finds_only_stale_pending_rows(
    app_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...], clean_files: None
) -> None:
    """ADR-12 — a slow client must not be reaped mid-upload."""
    from app.platform.storage import find_orphaned_keys

    tenant_a, _ = seeded_tenants
    sessionmaker = async_sessionmaker(app_engine, expire_on_commit=False)

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_a)
        fresh = await _insert_file(connection, tenant_id=tenant_a, status="pending")
        stale = await _insert_file(connection, tenant_id=tenant_a, status="pending")
        # Backdated past the 24h window. As migrator, since `created_at` is not
        # something the application would ever set.
        await connection.execute(
            text("UPDATE files SET created_at = now() - interval '48 hours' WHERE id = :i"),
            {"i": stale},
        )

    async with sessionmaker() as session:
        await scope_to(session, tenant_a)
        orphans = await find_orphaned_keys(session, tenant_id=tenant_a)

    assert len(orphans) == 1, f"expected only the stale row, got {orphans}"
    assert fresh is not None
