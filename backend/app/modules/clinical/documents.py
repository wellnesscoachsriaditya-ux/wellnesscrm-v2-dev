"""Client documents and lab reports — FR-M3-024…027, EC-M3-04, EC-M3-07.

🔒 **This module stores no bytes and signs no URLs.** ``app.platform.storage``
already owns the ADR-12 upload flow — grant, direct-to-storage upload,
reconciliation against what the client *declared*, quarantine on mismatch — and
FR-M0-038's rule that a retrieval is authorized before a URL is signed. A
document is a **clinical label on an already-confirmed file**: which client it
belongs to, what kind of document it is, and what date it reports on.

⚠️ Duplicating any of that here would give the product two upload paths, one of
which had not been through the type, size and quota checks. EC-M3-04 (unsupported
or oversized file) and EC-M3-07 (tenant over quota) are both answered by
``platform.storage.authorize_upload``, before any bytes move — which is the only
moment refusing is free.

⚠️ Functions take an ``AsyncSession`` rather than opening one (ADR-04).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.clinical import DocumentUploaded, validate_document_type
from app.kernel.context import ActorType
from app.kernel.errors import NotFoundError, ValidationError
from app.kernel.events import publish
from app.kernel.models import File, FileClass, FileStatus
from app.kernel.storage import DownloadAuthorizer
from app.modules.clinical.models import ClientDocument


def now() -> datetime:
    return datetime.now(UTC)


async def attach(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    file_id: uuid.UUID,
    document_type: str,
    document_date: date | None,
    uploaded_by: ActorType,
    uploaded_by_user_id: uuid.UUID | None,
    description: str | None = None,
) -> ClientDocument:
    """Label a confirmed file as a client document — FR-M3-024, FR-M3-025.

    🔒 **Only a ``confirmed`` file may be attached.** The file has been
    reconciled against what was declared by ``platform.storage.confirm_upload``;
    attaching one that is ``pending``, ``quarantined`` or ``deleted`` would put a
    row on a client's clinical record pointing at bytes we refuse to serve — the
    practitioner would see a document that never opens.

    🔒 **``FileClass.CLIENT_DOCUMENT`` is required.** A file uploaded as a
    practice logo or an export must not be relabelled as a lab report: the class
    decides retention and whether erasure must traverse the object (Arch §13.2).

    Raises:
        NotFoundError: No such file in this tenant.
        ValidationError: The file is not confirmed, or is not a client document.
    """
    file_row = await session.scalar(
        select(File).where(File.tenant_id == tenant_id, File.id == file_id)
    )
    if file_row is None or file_row.deleted_at is not None:
        raise NotFoundError(
            "That upload could not be found.",
            action="Upload the document again.",
        )
    if file_row.status is not FileStatus.CONFIRMED:
        raise ValidationError(
            "That upload has not finished being checked yet.",
            action="Wait a moment and try again — if it keeps failing, upload it again.",
        )
    if file_row.file_class is not FileClass.CLIENT_DOCUMENT:
        raise ValidationError(
            "That file was not uploaded as a client document.",
            action="Upload it from the client's documents area.",
        )
    if document_date is not None and document_date > now().date():
        raise ValidationError(
            "A document cannot be dated in the future.",
            action="Check the date on the report and try again.",
        )

    document = ClientDocument(
        tenant_id=tenant_id,
        client_id=client_id,
        file_id=file_id,
        document_type=validate_document_type(document_type),
        document_date=document_date,
        uploaded_by=uploaded_by,
        description=description,
    )
    session.add(document)
    await session.flush()

    await publish(
        DocumentUploaded(
            client_id=client_id,
            tenant_id=tenant_id,
            document_id=document.id,
            document_type=document.document_type,
            uploaded_by_client=uploaded_by is ActorType.CLIENT,
            occurred_at=now(),
            actor_user_id=uploaded_by_user_id,
        ),
        session,
    )
    return document


async def list_for_client(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    include_archived: bool = False,
) -> list[ClientDocument]:
    """A client's documents, newest report first — FR-M3-024.

    ⚠️ Ordered by ``document_date`` before upload time: a practitioner catching
    up on a client who scanned in six months of reports at once wants them in the
    order the *tests* happened. Undated documents sort last.
    """
    statement = select(ClientDocument).where(
        ClientDocument.tenant_id == tenant_id,
        ClientDocument.client_id == client_id,
    )
    if not include_archived:
        statement = statement.where(ClientDocument.archived_at.is_(None))
    statement = statement.order_by(
        ClientDocument.document_date.desc().nullslast(),
        ClientDocument.created_at.desc(),
    )
    return list((await session.execute(statement)).scalars().all())


async def download_url(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    authorizer: DownloadAuthorizer,
) -> str:
    """A short-lived URL for one document — FR-M3-026, FR-M0-038.

    🔒 **Delegates to the injected :class:`DownloadAuthorizer`**, implemented by
    ``platform.storage.authorize_download``: it re-reads the file under RLS,
    refuses anything not servable, re-checks the storage key against the tenant,
    and only then signs. This function adds the clinical hop (document → file)
    and nothing else; every rule about *whether* bytes may be served stays in
    one place.

    ⚠️ **Injected rather than imported** (R5). A module may not reach into
    ``platform``; the port lives in the kernel and the router passes the
    implementation in. That is the same shape as ``EntitlementGuard`` (S1-E) and
    ``ClientDirectory`` (S2-A).

    ⚠️ FR-M3-026 asks for in-browser viewing. That is a property of the
    ``Content-Type`` the object was stored with — PDFs and images render inline —
    and of the viewer, not of this URL.

    ⚠️ An archived document still resolves. Archiving detaches it from the active
    list; a practitioner following an audit entry to a document they retired last
    month still needs to open it.
    """
    document = await session.get(ClientDocument, document_id)
    if document is None or document.tenant_id != tenant_id:
        raise NotFoundError(
            "That document could not be found.",
            action="Refresh the client's documents and try again.",
        )
    return await authorizer.authorize_download(
        session, tenant_id=tenant_id, file_id=document.file_id
    )


async def archive(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
) -> ClientDocument:
    """Detach a document from the client's active list.

    ⚠️ Archive, not delete — migration 0016 revokes DELETE. The *bytes* are
    destroyed by the storage purge path on erasure (Arch §13.2), which is what
    makes a deletion request traverse object storage instead of orphaning it.
    """
    document = await session.get(ClientDocument, document_id)
    if document is None or document.tenant_id != tenant_id:
        raise NotFoundError(
            "That document could not be found.",
            action="Refresh the client's documents and try again.",
        )
    if document.archived_at is None:
        document.archived_at = now()
        await session.flush()
    return document
