"""Storage rules — FR-M0-037..040, NFR-035/036, Arch §13, ADR-12.

Pure rules, no database and no bucket. That is the point of the kernel/platform
split: every assertion below is about *what may be uploaded, what a key looks
like, and whether what landed matches what was authorized*.

🔒 The reconciliation tests are the ones that matter most. Type and size are
checked at authorization time against what the client *declared*; nothing has
seen the bytes at that point. If :func:`reconcile_upload` is wrong or is not
called, the allowlist is advisory — a client authorizes a 40 KB JPEG, uploads a
2 GB executable, and the record says "image/jpeg, 40 KB".
"""

from __future__ import annotations

import uuid

import pytest

from app.kernel.errors import ValidationError
from app.kernel.models import FileClass, FileStatus
from app.kernel.storage import (
    DOWNLOAD_URL_TTL,
    UPLOAD_URL_TTL,
    ReconcileOutcome,
    StoredObject,
    UploadGrant,
    allowed_content_types,
    build_storage_key,
    counts_towards_quota,
    is_clinical,
    is_retrievable,
    key_belongs_to_tenant,
    max_bytes_for,
    normalise_content_type,
    reconcile_upload,
    validate_upload,
)

_TENANT = uuid.uuid4()


def _grant(
    *,
    content_type: str = "image/jpeg",
    size_bytes: int = 1024,
    file_class: FileClass = FileClass.CLIENT_DOCUMENT,
    tenant_id: uuid.UUID = _TENANT,
) -> UploadGrant:
    """A grant with the boring parts filled in."""
    return UploadGrant(
        tenant_id=tenant_id,
        storage_key=build_storage_key(tenant_id=tenant_id, file_class=file_class),
        bucket="client-documents",
        file_class=file_class,
        declared_content_type=content_type,
        declared_size_bytes=size_bytes,
        upload_url="https://example.test/signed",
    )


# ─── The allowlist (NFR-036) ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "content_type",
    ["application/pdf", "image/jpeg", "image/png", "image/webp", "image/heic"],
)
def test_permitted_types_are_accepted(content_type: str) -> None:
    """The formats a nutrition practice actually exchanges."""
    validate_upload(
        content_type=content_type, size_bytes=1024, file_class=FileClass.CLIENT_DOCUMENT
    )


@pytest.mark.parametrize(
    "content_type",
    [
        # 🔒 Script-bearing formats browsers execute. SVG is the one that gets
        # waved through because "it's an image".
        "image/svg+xml",
        "text/html",
        "application/javascript",
        # 🔒 Executables. Refused by allowlist, not by extension.
        "application/x-msdownload",
        "application/x-sh",
        "application/octet-stream",
        # Archives — an unbounded set of files behind one size check.
        "application/zip",
        # Office macros.
        "application/vnd.ms-excel.sheet.macroEnabled.12",
        "",
    ],
)
def test_dangerous_types_are_refused(content_type: str) -> None:
    """🔒 NFR-036 — the allowlist is exact, so nothing is admitted by a prefix.

    ``image/svg+xml`` is the case a rule written as ``image/*`` would let through,
    and it is a script-bearing format that browsers execute.
    """
    with pytest.raises(ValidationError):
        validate_upload(
            content_type=content_type, size_bytes=1024, file_class=FileClass.CLIENT_DOCUMENT
        )


def test_content_type_parameters_are_stripped_before_comparison() -> None:
    """A charset parameter must not turn a permitted type into a rejected one."""
    validate_upload(
        content_type="image/jpeg; charset=binary",
        size_bytes=1024,
        file_class=FileClass.CLIENT_DOCUMENT,
    )
    validate_upload(
        content_type="  IMAGE/JPEG  ", size_bytes=1024, file_class=FileClass.CLIENT_DOCUMENT
    )


def test_svg_is_not_admitted_by_a_case_or_whitespace_trick() -> None:
    """🔒 Normalisation must not become a bypass in the other direction."""
    for attempt in ("IMAGE/SVG+XML", " image/svg+xml ", "image/svg+xml;charset=utf-8"):
        with pytest.raises(ValidationError):
            validate_upload(content_type=attempt, size_bytes=1024, file_class=FileClass.BRANDING)


def test_refusal_names_what_is_acceptable() -> None:
    """A refusal the user cannot act on is a dead end (NFR-063)."""
    with pytest.raises(ValidationError) as raised:
        validate_upload(
            content_type="application/zip", size_bytes=1024, file_class=FileClass.CLIENT_DOCUMENT
        )
    assert raised.value.action
    assert "pdf" in raised.value.action.lower()
    assert sorted(allowed_content_types()) == raised.value.details["allowed"]


# ─── Size (FR-M0-039) ────────────────────────────────────────────────────


def test_size_cap_is_per_class() -> None:
    """A logo and a lab report have different reasonable ceilings."""
    assert max_bytes_for(FileClass.BRANDING) < max_bytes_for(FileClass.CLIENT_DOCUMENT)


def test_upload_at_exactly_the_cap_is_permitted() -> None:
    """The boundary is inclusive — a file of exactly the documented limit works."""
    cap = max_bytes_for(FileClass.PLAN_PDF)
    validate_upload(content_type="application/pdf", size_bytes=cap, file_class=FileClass.PLAN_PDF)
    with pytest.raises(ValidationError):
        validate_upload(
            content_type="application/pdf", size_bytes=cap + 1, file_class=FileClass.PLAN_PDF
        )


@pytest.mark.parametrize("size_bytes", [0, -1])
def test_empty_or_negative_uploads_are_refused(size_bytes: int) -> None:
    """🔒 A zero-byte upload is a failed upload reporting success.

    Caught here so the quota arithmetic never sees a row consuming nothing.
    """
    with pytest.raises(ValidationError):
        validate_upload(
            content_type="image/png", size_bytes=size_bytes, file_class=FileClass.BRANDING
        )


def test_oversize_error_states_the_limit_in_readable_units() -> None:
    """ "larger than 2 MB" is actionable; "larger than 2097152" is not."""
    with pytest.raises(ValidationError) as raised:
        validate_upload(
            content_type="image/png", size_bytes=99 * 1024 * 1024, file_class=FileClass.BRANDING
        )
    assert "2 MB" in str(raised.value)


# ─── Keys (Arch §13.1) ───────────────────────────────────────────────────


def test_key_is_tenant_scoped() -> None:
    """The tenant is the first path segment, so a misdirected read is visible."""
    key = build_storage_key(tenant_id=_TENANT, file_class=FileClass.CLIENT_DOCUMENT)
    assert key.startswith(f"{_TENANT}/")
    assert key_belongs_to_tenant(key, _TENANT)


def test_key_does_not_belong_to_another_tenant() -> None:
    key = build_storage_key(tenant_id=_TENANT, file_class=FileClass.CLIENT_DOCUMENT)
    assert not key_belongs_to_tenant(key, uuid.uuid4())


def test_keys_are_not_enumerable() -> None:
    """🔒 A sequential key lets anyone holding one guess the next.

    Two keys minted back to back for the same tenant and class must differ, and
    must differ in the random component rather than in a counter.
    """
    first = build_storage_key(tenant_id=_TENANT, file_class=FileClass.PLAN_PDF)
    second = build_storage_key(tenant_id=_TENANT, file_class=FileClass.PLAN_PDF)
    assert first != second
    # The final segment is a UUID, so it parses as one.
    uuid.UUID(second.rsplit("/", 1)[-1])


def test_key_never_contains_the_original_filename() -> None:
    """🔒 A key built from a filename is a traversal and an encoding problem.

    ``build_storage_key`` takes no filename at all, which is the strongest form
    of this guarantee — there is nothing to pass.
    """
    key = build_storage_key(tenant_id=_TENANT, file_class=FileClass.CLIENT_DOCUMENT)
    assert ".." not in key
    assert key.count("/") == 2


def test_a_tenant_prefix_cannot_be_spoofed_by_a_similar_id() -> None:
    """⚠️ Prefix matching must not admit a tenant whose id merely starts the same.

    The trailing slash in the comparison is what makes this hold; without it a
    tenant could match another whose UUID string it is a prefix of.
    """
    key = f"{_TENANT}extra/client_document/{uuid.uuid4()}"
    assert not key_belongs_to_tenant(key, _TENANT)


# ─── Reconciliation (ADR-12) ─────────────────────────────────────────────


def test_matching_upload_is_confirmed() -> None:
    """The happy path: what landed is what was authorized."""
    grant = _grant(content_type="image/jpeg", size_bytes=2048)
    stored = StoredObject(storage_key=grant.storage_key, size_bytes=2048, content_type="image/jpeg")
    verdict = reconcile_upload(grant, stored)
    assert verdict.outcome is ReconcileOutcome.CONFIRMED
    assert verdict.status is FileStatus.CONFIRMED


def test_size_mismatch_is_quarantined() -> None:
    """🔒 The bug this whole mechanism exists to catch.

    A client authorizes a small JPEG and uploads something enormous. Without this
    comparison the record would agree with the declaration rather than with the
    bytes.
    """
    grant = _grant(size_bytes=40 * 1024)
    stored = StoredObject(
        storage_key=grant.storage_key,
        size_bytes=2 * 1024 * 1024 * 1024,
        content_type="image/jpeg",
    )
    verdict = reconcile_upload(grant, stored)
    assert verdict.outcome is ReconcileOutcome.QUARANTINED
    assert verdict.status is FileStatus.QUARANTINED
    assert "size mismatch" in (verdict.reason or "")


def test_content_type_mismatch_is_quarantined() -> None:
    """Authorized as a PDF, arrived as something else."""
    grant = _grant(content_type="application/pdf")
    stored = StoredObject(
        storage_key=grant.storage_key,
        size_bytes=grant.declared_size_bytes,
        content_type="application/x-msdownload",
    )
    verdict = reconcile_upload(grant, stored)
    assert verdict.outcome is ReconcileOutcome.QUARANTINED
    assert "content-type mismatch" in (verdict.reason or "")


def test_a_grant_for_a_since_revoked_type_does_not_confirm() -> None:
    """🔒 The allowlist is re-checked at confirmation, not only at authorization.

    Otherwise removing a format from the allowlist would leave a window open for
    as long as any outstanding grant survives.
    """
    # A grant that could not be issued today — as if the type were revoked
    # between authorization and confirmation.
    grant = _grant(content_type="image/svg+xml")
    stored = StoredObject(
        storage_key=grant.storage_key,
        size_bytes=grant.declared_size_bytes,
        content_type="image/svg+xml",
    )
    verdict = reconcile_upload(grant, stored)
    assert verdict.outcome is ReconcileOutcome.QUARANTINED
    assert "no longer permitted" in (verdict.reason or "")


def test_missing_object_stays_pending() -> None:
    """⚠️ Absent is not a failure — the client may still be uploading.

    Condemning a slow client would turn a 3G connection into a quarantine event.
    The reaper handles the genuinely abandoned case after 24h.
    """
    verdict = reconcile_upload(_grant(), None)
    assert verdict.outcome is ReconcileOutcome.MISSING
    assert verdict.status is FileStatus.PENDING


def test_content_type_parameters_do_not_cause_a_false_mismatch() -> None:
    """A backend that reports ``image/jpeg; charset=binary`` still matches."""
    grant = _grant(content_type="image/jpeg")
    stored = StoredObject(
        storage_key=grant.storage_key,
        size_bytes=grant.declared_size_bytes,
        content_type="image/jpeg; charset=binary",
    )
    assert reconcile_upload(grant, stored).outcome is ReconcileOutcome.CONFIRMED


# ─── Clinical classification (Arch §13.2) ────────────────────────────────


def test_client_documents_and_plans_are_clinical() -> None:
    """🔒 The erasure index. A class missed here is a file erasure walks past."""
    assert is_clinical(FileClass.CLIENT_DOCUMENT)
    assert is_clinical(FileClass.PLAN_PDF)


def test_branding_and_invoices_are_not_clinical() -> None:
    """A logo is not health data, and an invoice is a financial record."""
    assert not is_clinical(FileClass.BRANDING)
    assert not is_clinical(FileClass.INVOICE_PDF)


def test_every_file_class_has_a_size_cap_and_a_clinical_verdict() -> None:
    """🔒 A class added without both is unenforced in one direction or the other.

    A missing cap raises at authorization time; a missing clinical verdict is
    worse — it silently defaults and erasure walks past the file.
    """
    for file_class in FileClass:
        assert max_bytes_for(file_class) > 0
        assert isinstance(is_clinical(file_class), bool)


# ─── Serving and quota ───────────────────────────────────────────────────


def test_only_confirmed_undeleted_files_are_retrievable() -> None:
    assert is_retrievable(FileStatus.CONFIRMED, deleted_at_is_set=False)
    assert not is_retrievable(FileStatus.CONFIRMED, deleted_at_is_set=True)
    assert not is_retrievable(FileStatus.PENDING, deleted_at_is_set=False)


def test_a_quarantined_file_is_never_served() -> None:
    """🔒 It is retained as evidence. Serving it would hand back the bad object."""
    assert not is_retrievable(FileStatus.QUARANTINED, deleted_at_is_set=False)


def test_deleted_files_free_their_quota() -> None:
    """Soft delete releases the allowance immediately (FR-M0-040)."""
    assert counts_towards_quota(FileStatus.CONFIRMED, deleted_at_is_set=False)
    assert not counts_towards_quota(FileStatus.CONFIRMED, deleted_at_is_set=True)


def test_pending_uploads_do_not_consume_quota() -> None:
    """⚠️ Otherwise an abandoned upload would hold a tenant's allowance hostage
    until the reaper ran."""
    assert not counts_towards_quota(FileStatus.PENDING, deleted_at_is_set=False)


# ─── URL lifetimes (NFR-035) ─────────────────────────────────────────────


def test_upload_credentials_are_shorter_lived_than_download_urls() -> None:
    """🔒 A leaked write credential is worse than a leaked read one."""
    assert UPLOAD_URL_TTL < DOWNLOAD_URL_TTL


def test_url_lifetimes_are_minutes_not_hours() -> None:
    """A URL that outlives the page it was rendered on is an access-control hole
    with a long tail — it leaks into logs, history and screenshots."""
    assert DOWNLOAD_URL_TTL.total_seconds() <= 15 * 60


def test_normalise_content_type_is_idempotent() -> None:
    """Storing the normalised form means confirmation compares like with like."""
    once = normalise_content_type("IMAGE/JPEG; charset=binary")
    assert once == "image/jpeg"
    assert normalise_content_type(once) == once
