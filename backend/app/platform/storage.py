"""Storage persistence — the file index, the quota, and the signed-URL path.

The kernel decides *what may be uploaded and whether a key is well-formed*; this
module decides *where the row goes, whether the tenant has room, and when a URL
may be issued*. Splitting them is what lets every rule in ``kernel.storage`` be
tested without a database or a bucket.

📌 **ADR-12, in three calls.** :func:`authorize_upload` checks authorization,
type, size and quota, then issues a scoped credential — **and writes nothing**.
The client uploads directly. :func:`confirm_upload` verifies what landed against
what was authorized and only then creates the row. An abandoned upload leaves an
orphan object for :func:`find_orphaned_keys` to reap, not a phantom row.

🔒 **Every retrieval is authorized before a URL exists** (FR-M0-038, NFR-035).
:func:`authorize_download` refuses first and signs second. There is no function
here that signs without checking, which is the only way to make the ordering a
property of the module rather than of each call site.

🔒 **Quota is an entitlement, counted live** (FR-M0-040). Storage is a cumulative
total, so it has no ``usage_counters`` row: :func:`current_usage_mb` sums the
tenant's live files and the result is passed to ``entitlements.load_allowance``
as ``live_used``. A monthly counter would reset every 1st and would have no way
to give bytes back on deletion.

⚠️ **Deletion is soft, and the bytes go later.** :func:`soft_delete` marks the
row; the object survives until a purge job removes it. That ordering is
deliberate — a crash between the two leaves a reclaimable orphan rather than a
row pointing at bytes that are already gone.

⚠️ Functions take an ``AsyncSession`` rather than opening one. The transaction
belongs to the request pipeline (ADR-04).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.context import ActorType
from app.kernel.entitlements import Allowance, ResourceCode, check
from app.kernel.errors import NotFoundError, ValidationError
from app.kernel.models import File, FileClass, FileStatus
from app.kernel.storage import (
    DOWNLOAD_URL_TTL,
    ORPHAN_REAP_AFTER,
    UPLOAD_URL_TTL,
    Reconciliation,
    StorageBackend,
    StoredObject,
    UploadGrant,
    build_storage_key,
    counts_towards_quota,
    is_clinical,
    is_retrievable,
    key_belongs_to_tenant,
    normalise_content_type,
    reconcile_upload,
    validate_upload,
)
from app.platform.entitlements import SubscriptionSnapshot, load_allowance
from app.platform.logging import get_logger

logger = get_logger(__name__)

#: One mebibyte, as the quota is denominated. Named because the conversion
#: appears in both the read and the check, and a stray 1000 in one of them would
#: make the two disagree by 5%.
_BYTES_PER_MB: int = 1024 * 1024


def now() -> datetime:
    """Timezone-aware current time. Centralised so every row is UTC."""
    return datetime.now(UTC)


# ─── The backend, and the absence of one ─────────────────────────────────


class _UnconfiguredBackend:
    """The default backend, which refuses every call.

    🔒 There is no Supabase Storage adapter in S1 — see the note at the foot of
    this module. A silent no-op default would let the whole upload path appear to
    work in development and fail only once a real bucket existed; refusing loudly
    means the gap is visible the first time anyone exercises it.
    """

    async def issue_upload_url(
        self, *, bucket: str, storage_key: str, content_type: str, expires_in: timedelta
    ) -> str:
        raise RuntimeError(_UNCONFIGURED)

    async def issue_download_url(
        self, *, bucket: str, storage_key: str, expires_in: timedelta
    ) -> str:
        raise RuntimeError(_UNCONFIGURED)

    async def stat(self, *, bucket: str, storage_key: str) -> StoredObject | None:
        raise RuntimeError(_UNCONFIGURED)

    async def delete(self, *, bucket: str, storage_key: str) -> None:
        raise RuntimeError(_UNCONFIGURED)


_UNCONFIGURED = (
    "FATAL: no storage backend is installed.\n\n"
    "🔒 File bytes live in object storage, never in PostgreSQL and never in this "
    "process (ADR-12). Install an adapter via `configure_storage_backend()` "
    "before any upload or download path runs. See the note at the foot of "
    "`app/platform/storage.py`."
)

_backend: StorageBackend = _UnconfiguredBackend()


def configure_storage_backend(backend: StorageBackend) -> None:
    """Install the object-store adapter. Called once, at startup."""
    global _backend
    _backend = backend


def get_storage_backend() -> StorageBackend:
    """The installed backend, or the one that refuses every call."""
    return _backend


def raise_if_storage_is_unconfigured(settings: object) -> None:
    """🔒 Refuse to start a production-like process with no storage backend.

    The failure this prevents is quiet: the process boots, every other route
    works, and the first practitioner to upload a lab report gets a 500 with no
    prior signal that the capability was never wired up.
    """
    if not isinstance(get_storage_backend(), _UnconfiguredBackend):
        return
    raise RuntimeError(_UNCONFIGURED)


# ─── Quota (FR-M0-040) ───────────────────────────────────────────────────


async def current_usage_bytes(session: AsyncSession, *, tenant_id: uuid.UUID) -> int:
    """Bytes this tenant is currently holding.

    🔒 Counted live from ``files`` rather than from a ``usage_counters`` row —
    see :meth:`ResourceCode.is_counted_live`. Storage is a cumulative total, and
    a monthly counter would reset it every 1st while having no way to give bytes
    back when a file is deleted.

    Backed by ``ix_files__tenant_live``, a partial index over exactly the rows
    this predicate admits, so the sum is an index-only scan of one tenant's live
    files rather than a scan of the table.

    ⚠️ Reads under RLS, so the ``tenant_id`` predicate here is belt-and-braces
    rather than the isolation boundary. It stays because a sum that silently
    returned every tenant's bytes would fail *open* — the tenant would appear to
    be over quota, which is the wrong direction only until someone "fixes" it by
    removing the check.
    """
    total = await session.scalar(
        select(func.coalesce(func.sum(File.size_bytes), 0)).where(
            File.tenant_id == tenant_id,
            File.status == FileStatus.CONFIRMED,
            File.deleted_at.is_(None),
        )
    )
    return int(total or 0)


async def current_usage_mb(session: AsyncSession, *, tenant_id: uuid.UUID) -> Decimal:
    """Live storage consumption in MB, as the plan denominates it."""
    used = await current_usage_bytes(session, tenant_id=tenant_id)
    return Decimal(used) / Decimal(_BYTES_PER_MB)


async def storage_allowance(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    subscription: SubscriptionSnapshot | None = None,
) -> Allowance:
    """The tenant's storage position — plan limit against live consumption."""
    return await load_allowance(
        session,
        tenant_id=tenant_id,
        resource=ResourceCode.STORAGE_MB,
        subscription=subscription,
        live_used=await current_usage_mb(session, tenant_id=tenant_id),
    )


# ─── Authorize (ADR-12, step 1) ──────────────────────────────────────────


async def authorize_upload(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    file_class: FileClass,
    content_type: str,
    size_bytes: int,
    bucket: str,
    subscription: SubscriptionSnapshot | None = None,
) -> UploadGrant:
    """Check everything, then issue a scoped credential — and write nothing.

    🔒 Four refusals, in the order that costs least:

    1. **Type and size** (:func:`kernel.storage.validate_upload`) — pure, no I/O.
    2. **Quota** (FR-M0-040) — one indexed sum plus the plan read.
    3. ...and only then a credential from the backend.

    ⚠️ **No database row is created here** (ADR-12). The row is written at
    confirmation, so an abandoned upload leaves an orphan object rather than a
    phantom row. That is also why the quota check uses the *declared* size: it is
    the only size available before the bytes exist, and :func:`confirm_upload`
    re-checks against what actually landed.

    Raises:
        ValidationError: The type or size is not permitted.
        EntitlementError: The upload would exceed the plan's storage.
    """
    validate_upload(content_type=content_type, size_bytes=size_bytes, file_class=file_class)

    allowance = await storage_allowance(session, tenant_id=tenant_id, subscription=subscription)
    # 🔒 Charged in MB because that is how the plan is written. Fractional, so a
    # 300 KB upload against a 2 GB plan is not rounded up to a whole MB — over a
    # few thousand small files that rounding is a material overcharge.
    check(allowance, amount=Decimal(size_bytes) / Decimal(_BYTES_PER_MB))

    storage_key = build_storage_key(tenant_id=tenant_id, file_class=file_class)
    normalised = normalise_content_type(content_type)

    upload_url = await get_storage_backend().issue_upload_url(
        bucket=bucket,
        storage_key=storage_key,
        content_type=normalised,
        expires_in=UPLOAD_URL_TTL,
    )

    logger.info(
        "storage.upload_authorized",
        extra={
            "tenant_id": str(tenant_id),
            "file_class": file_class.value,
            "size_bytes": size_bytes,
        },
    )

    return UploadGrant(
        tenant_id=tenant_id,
        storage_key=storage_key,
        bucket=bucket,
        file_class=file_class,
        declared_content_type=normalised,
        declared_size_bytes=size_bytes,
        upload_url=upload_url,
    )


# ─── Confirm (ADR-12, step 2) ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ConfirmedFile:
    """The outcome of a confirmation, and the row it produced."""

    file_id: uuid.UUID
    status: FileStatus
    reconciliation: Reconciliation

    @property
    def is_usable(self) -> bool:
        """Whether the caller may now reference this file."""
        return self.status is FileStatus.CONFIRMED


async def confirm_upload(
    session: AsyncSession,
    *,
    grant: UploadGrant,
    original_filename: str,
    uploaded_by_actor_type: ActorType,
    uploaded_by_actor_id: uuid.UUID | None = None,
    checksum: str | None = None,
) -> ConfirmedFile:
    """Verify what landed, then create the row — ADR-12.

    🔒 **The verification is the point.** The grant was issued on the client's
    *declaration* of type and size; nothing before this has seen the bytes.
    Without :func:`kernel.storage.reconcile_upload` a client could authorize a
    40 KB JPEG, upload a 2 GB executable, and the record would say
    "image/jpeg, 40 KB".

    ⚠️ The row is created in whatever state reconciliation concludes, including
    ``quarantined``. A mismatch is recorded rather than discarded: the bytes are
    in the bucket either way, and a quarantined row is what tells the purge job
    and whoever investigates that they are there.

    ⚠️ ``original_filename`` is stored for display only and is never used to
    build a path — the key was minted at authorization time.

    Raises:
        ValidationError: The object is not in the bucket yet, so there is
            nothing to confirm.
    """
    stored = await get_storage_backend().stat(bucket=grant.bucket, storage_key=grant.storage_key)
    verdict = reconcile_upload(grant, stored)

    if stored is None:
        # 🔒 No row at all. The client has not finished, and writing a `pending`
        # row here would resurrect exactly the phantom ADR-12 avoids — the
        # orphan reaper handles the abandoned case.
        logger.warning(
            "storage.confirm_before_upload",
            extra={"tenant_id": str(grant.tenant_id), "reason": verdict.reason},
        )
        raise ValidationError(
            "That file has not finished uploading.",
            action="Wait for the upload to complete, then try again.",
        )

    if verdict.status is FileStatus.QUARANTINED:
        # 🔒 Logged at error: a mismatch is a security event, not a user error.
        logger.error(
            "storage.upload_quarantined",
            extra={
                "tenant_id": str(grant.tenant_id),
                "storage_key": grant.storage_key,
                "reason": verdict.reason,
            },
        )

    moment = now()
    file_row = File(
        tenant_id=grant.tenant_id,
        storage_key=grant.storage_key,
        bucket=grant.bucket,
        original_filename=original_filename,
        # 🔒 What actually landed, not what was declared. Storing the declared
        # value on a quarantined row would make the record agree with the lie.
        content_type=normalise_content_type(stored.content_type),
        size_bytes=stored.size_bytes,
        checksum=checksum or stored.checksum,
        file_class=grant.file_class,
        # 🔒 Derived from the class, never taken from the caller (Arch §13.2).
        contains_clinical_data=is_clinical(grant.file_class),
        uploaded_by_actor_type=uploaded_by_actor_type,
        uploaded_by_actor_id=uploaded_by_actor_id,
        status=verdict.status,
        confirmed_at=moment if verdict.status is FileStatus.CONFIRMED else None,
    )
    session.add(file_row)
    await session.flush()

    return ConfirmedFile(file_id=file_row.id, status=verdict.status, reconciliation=verdict)


# ─── Retrieve (FR-M0-038, NFR-035) ───────────────────────────────────────


async def authorize_download(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
) -> str:
    """Authorize a retrieval, then issue a short-lived URL — in that order.

    🔒 FR-M0-038 / NFR-035 — "unguessable" is explicitly not sufficient. The row
    is read under RLS (so another tenant's file is simply not found), the state
    is checked, the key is re-checked against the tenant, and only then is a URL
    signed. There is no function in this module that signs without checking.

    🔒 A **quarantined file is never served**. It is retained as evidence, and
    serving it would hand back exactly the object that failed verification.

    ⚠️ The URL that comes back is a delivery mechanism with a short life, not an
    access-control decision. It must not be stored, logged, or embedded anywhere
    that outlives :data:`DOWNLOAD_URL_TTL`.

    Raises:
        NotFoundError: The file does not exist, belongs to another tenant, or is
            not in a servable state. 🔒 Deliberately the same error for all
            three (API §5.4) — distinguishing them would confirm the existence of
            another tenant's file.
    """
    file_row = await session.scalar(
        select(File).where(File.tenant_id == tenant_id, File.id == file_id)
    )

    if file_row is None:
        raise NotFoundError(
            "That file could not be found.",
            action="Check the link, or ask whoever shared it to send it again.",
        )

    if not is_retrievable(file_row.status, file_row.deleted_at is not None):
        logger.warning(
            "storage.download_refused",
            extra={
                "tenant_id": str(tenant_id),
                "file_id": str(file_id),
                "status": file_row.status.value,
            },
        )
        raise NotFoundError(
            "That file could not be found.",
            action="Check the link, or ask whoever shared it to send it again.",
        )

    if not key_belongs_to_tenant(file_row.storage_key, tenant_id):
        # 🔒 Unreachable while RLS holds — the row could not have been read. It
        # stays because the object store has no policy of its own, and this is
        # the last point at which a mismatched key is catchable before a URL is
        # signed for it.
        logger.error(
            "storage.key_tenant_mismatch",
            extra={"tenant_id": str(tenant_id), "file_id": str(file_id)},
        )
        raise NotFoundError(
            "That file could not be found.",
            action="Contact support if this keeps happening.",
        )

    return await get_storage_backend().issue_download_url(
        bucket=file_row.bucket,
        storage_key=file_row.storage_key,
        expires_in=DOWNLOAD_URL_TTL,
    )


# ─── Delete and reap ─────────────────────────────────────────────────────


async def soft_delete(session: AsyncSession, *, tenant_id: uuid.UUID, file_id: uuid.UUID) -> None:
    """Mark a file deleted. The bytes go later, by a purge job.

    🔒 The row is never removed — migration 0008 revokes DELETE. The record is
    what proves an object existed, and DPDP erasure (FR-M0-027) has to be able to
    show that the object a row pointed at was actually destroyed.

    ⚠️ Row first, bytes later, deliberately. A crash between the two leaves a
    reclaimable orphan; the other order leaves a row pointing at bytes that are
    already gone, which reads as corruption.

    Quota is freed immediately, because :func:`current_usage_bytes` counts only
    confirmed, undeleted rows.
    """
    moment = now()
    result = await session.execute(
        update(File)
        .where(
            File.tenant_id == tenant_id,
            File.id == file_id,
            File.deleted_at.is_(None),
        )
        .values(status=FileStatus.DELETED, deleted_at=moment, updated_at=moment)
    )

    if result.rowcount == 0:
        raise NotFoundError(
            "That file could not be found.",
            action="It may already have been deleted.",
        )


async def find_orphaned_keys(
    session: AsyncSession, *, tenant_id: uuid.UUID, at: datetime | None = None
) -> list[str]:
    """Keys of rows stuck ``pending`` past the reap window — ADR-12.

    ⚠️ This finds *rows*, which by ADR-12 only exist once confirmation was
    attempted. The genuinely abandoned case — authorized, uploaded, never
    confirmed — leaves an object with **no row at all**, and finding those
    requires listing the bucket. That listing belongs to the maintenance job
    (approved proposal #9) rather than here, because it is a storage operation
    rather than a database one.
    """
    cutoff = (at or now()) - ORPHAN_REAP_AFTER
    result = await session.execute(
        select(File.storage_key).where(
            File.tenant_id == tenant_id,
            File.status == FileStatus.PENDING,
            File.created_at < cutoff,
        )
    )
    return list(result.scalars().all())


async def clinical_files_for_erasure(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> list[tuple[uuid.UUID, str, str]]:
    """Every clinical object for a tenant — the erasure traversal.

    🔒 Arch §13.2 / FR-M0-027 — "deleting a database row while its file persists
    in object storage is a compliance failure". This is the query that stops that
    being an assumption, and it is a **launch-gate test**, not a nicety.

    ⚠️ Includes already soft-deleted rows. A file marked deleted whose bytes were
    never purged is exactly the case erasure must catch, and filtering it out
    here would hide the failure this function exists to find.

    Returns:
        ``(file_id, bucket, storage_key)`` for each object to destroy.
    """
    result = await session.execute(
        select(File.id, File.bucket, File.storage_key).where(
            File.tenant_id == tenant_id,
            File.contains_clinical_data.is_(True),
        )
    )
    return [(row.id, row.bucket, row.storage_key) for row in result]


def quota_bytes_for(rows: list[tuple[FileStatus, bool, int]]) -> int:
    """Sum the bytes that count towards quota, given (status, deleted, size).

    A pure helper so the predicate in :func:`current_usage_bytes` can be checked
    against :func:`kernel.storage.counts_towards_quota` without a database — the
    two must agree, and nothing else would notice if they drifted.
    """
    return sum(size for status, deleted, size in rows if counts_towards_quota(status, deleted))


# ─── 🔒 Remaining work: the Supabase Storage adapter ─────────────────────
#
# S1 ships the port and no adapter. It is deliberately NOT written here.
#
# An HTTP client against a bucket that has not been provisioned would produce
# code whose tests assert only my assumptions about the provider's contract —
# green, and evidence of nothing. That is the same reasoning that defers the
# GoTrue adapter in `app/platform/identity/credentials.py`.
#
# The port is shaped to what Supabase Storage actually offers, so the adapter is
# a translation of four calls:
#
#   issue_upload_url   → POST /storage/v1/object/upload/sign/{bucket}/{key}
#   issue_download_url → POST /storage/v1/object/sign/{bucket}/{key}
#   stat               → GET  /storage/v1/object/info/{bucket}/{key}
#   delete             → DELETE /storage/v1/object/{bucket}/{key}
#
# ⚠️ Two things must be verified against the real service when it is written,
# because both are assumptions this module depends on:
#
#   1. That `info` returns the stored size and content type. `reconcile_upload`
#      is inert without them — it would confirm everything.
#   2. 🔒 That the bucket is private and has no public read policy. Arch §13.1
#      says "no public bucket exists"; a bucket created through the dashboard
#      defaults otherwise, and a public bucket makes every check in this file
#      decorative.
