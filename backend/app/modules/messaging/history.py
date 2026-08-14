"""Reading the delivery log — FR-M8-011, AC-M8-003, AC-M8-007.

🔒 **The answer to "did my client get it?"** That question is asked in support
conversations and in the practitioner's own head before they follow up, and the
whole value of an immutable per-attempt log is that it can be answered exactly:
what was sent, over what, when, and what the provider said about it.

⚠️ Reads only. Nothing here writes — status changes come from the webhook path,
and the attempt rows come from the dispatch engine. The column-level grant in
migration 0021 would refuse a write from here anyway, which is the point of
putting the guarantee in the grant.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.errors import ValidationError
from app.kernel.messaging import DispatchStatus
from app.kernel.models import TransportType
from app.modules.messaging.models import MessageDispatch, MessageTemplate

#: API §6.1's default and ceiling for this collection.
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100

#: Statuses that mean a practitioner should look — AC-M8-007's "terminal failure
#: is visible to the practitioner".
FAILED_STATUSES: tuple[DispatchStatus, ...] = (DispatchStatus.FAILED, DispatchStatus.REJECTED)


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One delivery attempt, as a practitioner reads it.

    ⚠️ ``failure_reason`` is the provider's own text and is operator-facing. The
    API layer decides whether to show it; it must never reach a *client*, and it
    must never be assumed free of provider-side detail (NFR-033).
    """

    id: uuid.UUID
    template_code: str
    template_version: int
    transport: TransportType
    recipient_address: str
    status: DispatchStatus
    attempt_number: int
    failure_code: str | None
    failure_reason: str | None
    created_at: datetime
    sent_at: datetime | None
    delivered_at: datetime | None
    read_at: datetime | None


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """One page of the delivery log."""

    items: list[HistoryEntry]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class _Cursor:
    """Where a page resumes.

    🔒 Two fields, because ``created_at`` alone is not unique: a message and its
    retry can share a timestamp to the microsecond, and a cursor keyed on time
    alone would skip or repeat whatever shared that instant. The same argument
    `TimelineCursor` makes.
    """

    created_at: datetime
    dispatch_id: uuid.UUID

    def encode(self) -> str:
        raw = f"{self.created_at.isoformat()}|{self.dispatch_id}"
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")

    @classmethod
    def decode(cls, value: str) -> _Cursor:
        """⚠️ Opaque to the caller, not signed. A cursor names a position in the
        caller's *own* tenant-scoped, authorization-checked result set — both
        checks run again on the next page, so a forged cursor buys nothing but a
        different offset into rows the caller may already read."""
        try:
            raw = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
            timestamp, identifier = raw.split("|", 1)
            return cls(
                created_at=datetime.fromisoformat(timestamp),
                dispatch_id=uuid.UUID(identifier),
            )
        except (ValueError, binascii.Error, UnicodeDecodeError) as error:
            raise ValidationError(
                "That page cursor is not valid.",
                action="Start from the first page.",
            ) from error


def _entry(dispatch: MessageDispatch, template: MessageTemplate) -> HistoryEntry:
    return HistoryEntry(
        id=dispatch.id,
        template_code=template.code,
        template_version=dispatch.template_version,
        transport=dispatch.transport,
        recipient_address=dispatch.recipient_address,
        status=dispatch.status,
        attempt_number=dispatch.attempt_number,
        failure_code=dispatch.failure_code,
        failure_reason=dispatch.failure_reason,
        created_at=dispatch.created_at,
        sent_at=dispatch.sent_at,
        delivered_at=dispatch.delivered_at,
        read_at=dispatch.read_at,
    )


async def list_for_client(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> HistoryPage:
    """A client's message history, newest first — FR-M8-011.

    🔒 **Every attempt, including the failed ones.** A history that showed only
    successes would answer "what did we send?" while hiding the case a
    practitioner most needs to see (AC-M8-007), and would make a retried message
    look like a single clean delivery.

    Matches ``ix_message_dispatches__client_created`` — ``(client_id, created_at
    DESC)`` — so the page is an index scan rather than a sort of the client's
    whole history.
    """
    page_size = max(1, min(limit, MAX_PAGE_SIZE))

    statement = (
        select(MessageDispatch, MessageTemplate)
        .join(MessageTemplate, MessageTemplate.id == MessageDispatch.template_id)
        .where(
            MessageDispatch.tenant_id == tenant_id,
            MessageDispatch.client_id == client_id,
        )
        .order_by(MessageDispatch.created_at.desc(), MessageDispatch.id.desc())
        # ⚠️ One more than asked for: the extra row is how `has_more` is known
        # without a second COUNT over the whole history.
        .limit(page_size + 1)
    )

    if cursor is not None:
        position = _Cursor.decode(cursor)
        # ⚠️ `tuple_(...)` rather than a bare Python tuple comparison: SQLAlchemy
        # builds a row-value expression only from the explicit construct, and the
        # bare form compares Python objects rather than emitting SQL.
        statement = statement.where(
            tuple_(MessageDispatch.created_at, MessageDispatch.id)
            < (position.created_at, position.dispatch_id)
        )

    rows = (await session.execute(statement)).all()
    has_more = len(rows) > page_size
    visible = rows[:page_size]

    items = [_entry(row[0], row[1]) for row in visible]
    next_cursor = (
        _Cursor(created_at=items[-1].created_at, dispatch_id=items[-1].id).encode()
        if has_more and items
        else None
    )
    return HistoryPage(items=items, next_cursor=next_cursor, has_more=has_more)


async def recent_failures(
    session: AsyncSession, *, tenant_id: uuid.UUID, limit: int = DEFAULT_PAGE_SIZE
) -> list[HistoryEntry]:
    """The tenant's recent delivery failures — AC-M8-007's practitioner surface.

    ⚠️ Tenant-wide rather than per client, deliberately. A practitioner who has
    to open each client in turn to discover a failed send will not discover it;
    EC-M8-02 ("practitioner informed after repeated failure") only works if there
    is one place that shows them.
    """
    rows = (
        await session.execute(
            select(MessageDispatch, MessageTemplate)
            .join(MessageTemplate, MessageTemplate.id == MessageDispatch.template_id)
            .where(
                MessageDispatch.tenant_id == tenant_id,
                MessageDispatch.status.in_(FAILED_STATUSES),
            )
            .order_by(MessageDispatch.created_at.desc())
            .limit(max(1, min(limit, MAX_PAGE_SIZE)))
        )
    ).all()
    return [_entry(row[0], row[1]) for row in rows]
