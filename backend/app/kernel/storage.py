"""Storage — what may be uploaded, where it lands, and who may fetch it.

🔒 Arch §13, ADR-12, FR-M0-037..040, NFR-035/036. The bytes never pass through
this process. What lives here is the set of rules that decide whether an upload
is permitted, what its key will be, and whether a retrieval may be issued a URL.

Four properties, each answering a specific failure:

1. 🔒 **The signed URL is delivery, not access control** (FR-M0-038, NFR-035).
   Every retrieval is authorized *first*; the URL is short-lived and issued
   afterwards. "Unguessable" is explicitly not sufficient, because a URL leaks
   into logs, browser history, referrer headers and shared screenshots, and none
   of those revoke.
2. 🔒 **Type and size are constrained before the credential is issued**
   (FR-M0-039, NFR-036). :func:`validate_upload` is the allowlist, and it runs
   at authorization time — the only moment before the bytes exist when refusing
   costs nothing.
3. 🔒 **Keys are opaque and never derived from user input** (Arch §13.1). A key
   built from a filename is a path traversal waiting to happen; a key built from
   a sequence is an enumeration. :func:`build_storage_key` uses a UUID and
   embeds the tenant, so the key is unguessable *and* self-describing to the
   isolation check.
4. 🔒 **Confirmation compares what arrived against what was authorized.** Without
   it the allowlist is advisory: a client could authorize a 40 KB JPEG and upload
   a 2 GB executable. :func:`reconcile_upload` is that comparison, and a mismatch
   quarantines rather than deletes — a deliberate mismatch is a security event,
   and destroying the evidence is the wrong first move.

⚠️ **This module cannot enforce "authorize before issuing a URL".** The ordering
is a property of the call site, not of any function here. A caller that issues a
signed URL without first calling ``can()`` has broken NFR-035, and nothing in
this file can detect it. Reviewers should treat a URL issued outside
``platform.storage.authorize_download`` as a defect.

⚠️ **Executable content is refused by allowlist, not by extension.** The
allowlist is of *content types*, and the filename's extension is never consulted
— it is attacker-controlled and disagrees with the bytes whenever that is useful.

This module holds rules only. Persistence and the backend live in
``platform.storage`` — the split is what lets every rule below be tested without
a database or a bucket.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from app.kernel.errors import ValidationError
from app.kernel.models import FileClass, FileStatus

#: 🔒 NFR-036 — the allowlist. Content types only; the filename extension is
#: never consulted, because it is attacker-controlled and agrees with the bytes
#: only when that is convenient for the attacker.
#:
#: ⚠️ Deliberately narrow. Every addition is a new parser reachable by an
#: unauthenticated-ish path, and the formats below are the ones a nutrition
#: practice actually exchanges: lab reports as PDFs, photographs of meals and
#: documents, and a logo. SVG is **absent on purpose** — it is a script-bearing
#: format that browsers execute, and "an image" is the reason it gets waved
#: through.
_ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/heic",
    }
)

#: 🔒 Per-class size caps (FR-M0-039). Separate from the plan's storage quota,
#: which is cumulative: this bounds a *single* object, so one upload cannot
#: consume a whole tenant's allowance or exhaust the request path before the
#: quota check is even reached.
_MAX_BYTES_BY_CLASS: dict[FileClass, int] = {
    # A lab report or a scan. Generous, because a multi-page PDF from a
    # pathology lab is routinely large and refusing one is a support ticket.
    FileClass.CLIENT_DOCUMENT: 25 * 1024 * 1024,
    FileClass.PLAN_PDF: 10 * 1024 * 1024,
    FileClass.INVOICE_PDF: 5 * 1024 * 1024,
    # 🔒 A logo. Small on purpose: this one is rendered on every page of every
    # generated document, so a 20 MB "logo" is a performance problem for the
    # practitioner's own clients (NFR-002).
    FileClass.BRANDING: 2 * 1024 * 1024,
    FileClass.EXPORT: 100 * 1024 * 1024,
}

#: 🔒 Which classes carry clinical data — the erasure index (Arch §13.2).
#:
#: ⚠️ Derived from the class rather than passed in by the caller. A caller that
#: could declare its own upload non-clinical would eventually declare a lab
#: report non-clinical, and DPDP erasure would then walk past it.
_CLINICAL_CLASSES: frozenset[FileClass] = frozenset({FileClass.CLIENT_DOCUMENT, FileClass.PLAN_PDF})

#: 🔒 NFR-035 — how long an issued URL lives. Short enough that a leaked URL is
#: expired before it is useful, long enough that a slow mobile connection on
#: 3G finishes the download it started.
DOWNLOAD_URL_TTL: timedelta = timedelta(minutes=5)

#: How long an upload credential lives. Shorter than a download: the client is
#: about to use it immediately, and a long-lived write credential is a worse
#: thing to leak than a read one.
UPLOAD_URL_TTL: timedelta = timedelta(minutes=2)

#: 🔒 ADR-12 / approved proposal #9 — an upload that was authorized but never
#: confirmed leaves an orphan object. The reaper removes them after this long.
ORPHAN_REAP_AFTER: timedelta = timedelta(hours=24)


def is_clinical(file_class: FileClass) -> bool:
    """Whether this class of file is in scope for DPDP erasure — Arch §13.2."""
    return file_class in _CLINICAL_CLASSES


def max_bytes_for(file_class: FileClass) -> int:
    """The per-object size cap for this class (FR-M0-039)."""
    return _MAX_BYTES_BY_CLASS[file_class]


def allowed_content_types() -> frozenset[str]:
    """The upload allowlist (NFR-036), for surfacing in an error or a UI hint."""
    return _ALLOWED_CONTENT_TYPES


# ─── Validation (FR-M0-039, NFR-036) ─────────────────────────────────────


def validate_upload(
    *,
    content_type: str,
    size_bytes: int,
    file_class: FileClass,
) -> None:
    """Refuse an upload that is the wrong type or the wrong size.

    🔒 Runs at *authorization* time — before a credential exists and before any
    bytes move. That is the only moment where refusing is free, and it is the
    check FR-M0-039 and NFR-036 both point at.

    ⚠️ The content type is compared exactly, after lowercasing and stripping any
    ``;charset=`` parameter. No prefix matching: ``image/svg+xml`` must not be
    admitted by a rule that was written to allow ``image/*``, and that is exactly
    how script-bearing formats get in.

    Raises:
        ValidationError: With an action naming what is acceptable, because a
            refusal the user cannot act on is a dead end (NFR-063).
    """
    normalised = content_type.split(";")[0].strip().lower()

    if normalised not in _ALLOWED_CONTENT_TYPES:
        readable = ", ".join(sorted(_ALLOWED_CONTENT_TYPES))
        raise ValidationError(
            f"Files of type {normalised or 'unknown'} cannot be uploaded.",
            action=f"Upload one of these instead: {readable}.",
            details={"content_type": normalised, "allowed": sorted(_ALLOWED_CONTENT_TYPES)},
        )

    if size_bytes <= 0:
        # 🔒 A zero-byte upload is a failed upload reporting success. Caught here
        # so the quota arithmetic never sees a row that consumes nothing.
        raise ValidationError(
            "That file is empty.",
            action="Check the file opened correctly, then try again.",
            details={"size_bytes": size_bytes},
        )

    cap = max_bytes_for(file_class)
    if size_bytes > cap:
        raise ValidationError(
            f"That file is larger than the {cap // (1024 * 1024)} MB limit.",
            action="Compress it or split it into smaller files, then try again.",
            details={"size_bytes": size_bytes, "max_bytes": cap},
        )


def normalise_content_type(content_type: str) -> str:
    """The stored form of a content type — lowercased, without parameters.

    Storing the normalised form means the confirmation comparison is between two
    values in the same shape, rather than between ``image/JPEG`` and
    ``image/jpeg; charset=binary``.
    """
    return content_type.split(";")[0].strip().lower()


# ─── Keys (Arch §13.1) ───────────────────────────────────────────────────


def build_storage_key(
    *,
    tenant_id: uuid.UUID,
    file_class: FileClass,
    object_id: uuid.UUID | None = None,
) -> str:
    """Build an opaque, tenant-scoped, non-enumerable object key.

    🔒 Three properties, each load-bearing (Arch §13.1):

    * **Opaque** — a random UUID, not a counter. A sequential key lets anyone who
      has one key guess the next, which is enumeration of every tenant's files.
    * **Tenant-scoped** — the tenant id is the first path segment, so a bucket
      listing is at least partitioned by tenant and a misdirected read is visible
      as a prefix mismatch rather than as a plausible-looking key.
    * **Never derived from user input** — the original filename appears nowhere.
      A key built from a filename is a path traversal (``../``) and an encoding
      problem, and neither is worth the marginal debuggability.

    ⚠️ The class is in the path for operational legibility only. Nothing reads it
    back — ``files.file_class`` is the authority — so a key whose class segment
    disagrees with its row is untidy rather than dangerous.
    """
    return f"{tenant_id}/{file_class.value}/{object_id or uuid.uuid4()}"


def key_belongs_to_tenant(storage_key: str, tenant_id: uuid.UUID) -> bool:
    """Whether a key was minted for this tenant.

    🔒 A belt-and-braces check for the delivery path. RLS already prevents
    reading another tenant's row, so a key reaching the backend should always
    match — but the object store has no policy of its own, and this is the last
    point at which a mismatch is catchable before a URL is signed for it.
    """
    return storage_key.startswith(f"{tenant_id}/")


# ─── The upload grant (ADR-12) ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class UploadGrant:
    """Permission to upload one object, issued before any bytes move.

    🔒 **No database row exists at this point** (ADR-12). The grant is handed to
    the client, the client uploads directly to storage, and only then is a row
    written. An abandoned upload therefore leaves an orphan object for the reaper
    rather than a phantom row nothing will ever complete.

    ⚠️ ``declared_size_bytes`` and ``declared_content_type`` are what the *client
    said*. They are recorded on the grant so that confirmation can compare them
    against what actually landed — see :func:`reconcile_upload`. Treating them as
    facts before that comparison is what makes an allowlist advisory.
    """

    tenant_id: uuid.UUID
    storage_key: str
    bucket: str
    file_class: FileClass
    declared_content_type: str
    declared_size_bytes: int
    upload_url: str
    expires_in: timedelta = UPLOAD_URL_TTL


@dataclass(frozen=True, slots=True)
class StoredObject:
    """What the backend reports about an object that actually exists.

    Returned by :meth:`StorageBackend.stat`. ``checksum`` is optional because not
    every backend exposes one, and a confirmation that required it would be
    unimplementable against a backend that does not.
    """

    storage_key: str
    size_bytes: int
    content_type: str
    checksum: str | None = None


class StorageBackend(Protocol):
    """The object store, as the rest of the system is allowed to see it.

    🔒 Four operations, and deliberately no more. Swapping Supabase Storage for
    S3, R2 or a local directory must not touch a line of business logic — the
    same argument ``CredentialStore`` makes for the identity provider.

    ⚠️ Every method takes an already-authorized key. **No implementation of this
    protocol performs authorization**, and none should: authorization is a
    decision about an actor and a tenant, made in ``platform.storage`` before any
    of these are called. A backend that tried to authorize would be a second,
    weaker copy of that decision.
    """

    async def issue_upload_url(
        self, *, bucket: str, storage_key: str, content_type: str, expires_in: timedelta
    ) -> str:
        """A scoped, short-lived credential the client can PUT to (ADR-12)."""
        ...

    async def issue_download_url(
        self, *, bucket: str, storage_key: str, expires_in: timedelta
    ) -> str:
        """A short-lived URL for an *already authorized* retrieval (NFR-035)."""
        ...

    async def stat(self, *, bucket: str, storage_key: str) -> StoredObject | None:
        """What actually landed, or ``None`` if the object is absent.

        🔒 The input to :func:`reconcile_upload`. Without it, confirmation would
        be the client asserting its own upload succeeded.
        """
        ...

    async def delete(self, *, bucket: str, storage_key: str) -> None:
        """Destroy the bytes. Idempotent — a missing object is not an error.

        ⚠️ Called by the purge job and by erasure (FR-M0-027), never by the
        request path: a soft delete marks the row, and the bytes go later.
        """
        ...


# ─── Confirmation (ADR-12) ───────────────────────────────────────────────


class ReconcileOutcome(str, enum.Enum):
    """What confirmation concluded about an uploaded object."""

    #: Present, and matches what was authorized.
    CONFIRMED = "confirmed"
    #: Present, but does not match. 🔒 A security event, not a retry.
    QUARANTINED = "quarantined"
    #: Absent. The client never finished; nothing to record yet.
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """The verdict, and why — so the caller can log a reason worth reading."""

    outcome: ReconcileOutcome
    reason: str | None = None

    @property
    def status(self) -> FileStatus:
        """The file status this verdict implies.

        ``MISSING`` maps to ``PENDING``: the row stays open for a retry within
        the reaper's window rather than being condemned for a slow client.
        """
        if self.outcome is ReconcileOutcome.CONFIRMED:
            return FileStatus.CONFIRMED
        if self.outcome is ReconcileOutcome.QUARANTINED:
            return FileStatus.QUARANTINED
        return FileStatus.PENDING


def reconcile_upload(grant: UploadGrant, stored: StoredObject | None) -> Reconciliation:
    """Compare what landed against what was authorized.

    🔒 **This is what makes the allowlist real.** The grant is issued on the
    client's *declaration* of type and size; nothing before this point has seen
    the bytes. Without this comparison a client could authorize a 40 KB JPEG and
    upload a 2 GB executable, and the only record would say "image/jpeg, 40 KB".

    ⚠️ A mismatch **quarantines**, it does not delete. A file that disagrees with
    its authorization is either a broken client or a deliberate attempt, and the
    two are indistinguishable at this point — so the bytes are kept, unserved,
    for whoever investigates. Deleting would destroy the only evidence.

    ⚠️ Size is compared exactly rather than with a tolerance. A backend that
    re-encodes on ingest would break this, and none of ours does; a tolerance
    would be a window someone eventually fits something through.
    """
    if stored is None:
        return Reconciliation(ReconcileOutcome.MISSING, "the object is not in the bucket yet")

    if stored.size_bytes != grant.declared_size_bytes:
        return Reconciliation(
            ReconcileOutcome.QUARANTINED,
            f"size mismatch: authorized {grant.declared_size_bytes} bytes, "
            f"stored object is {stored.size_bytes}",
        )

    stored_type = normalise_content_type(stored.content_type)
    if stored_type != normalise_content_type(grant.declared_content_type):
        return Reconciliation(
            ReconcileOutcome.QUARANTINED,
            f"content-type mismatch: authorized {grant.declared_content_type}, "
            f"stored object is {stored.content_type}",
        )

    # 🔒 Re-checked against the allowlist, not merely against the grant. A grant
    # issued before an entry was removed from the allowlist must not still
    # confirm — otherwise revoking a format would leave a window open for as long
    # as any outstanding grant survives.
    if stored_type not in _ALLOWED_CONTENT_TYPES:
        return Reconciliation(
            ReconcileOutcome.QUARANTINED,
            f"content type {stored_type} is no longer permitted",
        )

    return Reconciliation(ReconcileOutcome.CONFIRMED)


# ─── Retrieval and deletion ──────────────────────────────────────────────


def is_retrievable(status: FileStatus, deleted_at_is_set: bool) -> bool:
    """Whether a file in this state may be served at all.

    🔒 Only a confirmed, undeleted file. In particular a **quarantined file is
    never served** — it is retained as evidence, and serving it would be handing
    back exactly the object that failed verification.
    """
    return status is FileStatus.CONFIRMED and not deleted_at_is_set


def counts_towards_quota(status: FileStatus, deleted_at_is_set: bool) -> bool:
    """Whether this file consumes the tenant's storage entitlement (FR-M0-040).

    ⚠️ Confirmed and undeleted only, which is the same predicate as
    :func:`is_retrievable` today and deliberately a separate function. They
    answer different questions and will diverge: a quarantined object still
    occupies bytes we pay for, and if we ever bill for that, this is the one that
    changes.
    """
    return status is FileStatus.CONFIRMED and not deleted_at_is_set
