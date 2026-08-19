"""Reading enquiries — the list, and the queue FR-M2-011 puts at its centre.

🔒 FR-M2-011, AC-M2-005, API §7.2. Two reads with different jobs:

* :func:`list_enquiries` — everything that ever arrived, newest first. The
  archive a practitioner searches when asked "did she ever contact us?".
* :func:`list_needs_response` — 🔒 **the working queue**, oldest first. US-M2-03
  is "so none are forgotten", and M2.2 prices a forgotten enquiry at
  ₹2,500–4,000/month of recurring revenue. This is the screen that recovers it.

⚠️ **The ordering is inverted between them, deliberately.** Newest-first is right
for browsing history and wrong for a work queue: the oldest unanswered enquiry is
the most urgent one, and a queue that buries it under today's arrivals is how it
gets forgotten. AC-M2-005 says "ordered by age" and means ascending.

🔒 **Practitioner scoping is a WHERE clause, not a filter applied afterwards** —
the same rule as `clients.discovery`, and the same reason: a list filtered after
the fact pages short and leaks totals through the count.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.clients import ClientStage, get_client_directory
from app.kernel.context import UserRole
from app.kernel.leads import LeadSource, age_in_hours, is_ageing
from app.kernel.models import User
from app.modules.leads.models import EnquirySubmission

#: The default page size for an enquiry list — API §6.1.
DEFAULT_PAGE_SIZE = 25

#: 🔒 The ceiling (API §6.1). The needs-response queue is meant to be worked
#: through, not scrolled: a practitioner with more than 100 unanswered enquiries
#: has a staffing problem the UI cannot solve by showing all of them at once.
MAX_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class EnquiryListItem:
    """One row of the enquiry list — API §7.2.

    ⚠️ Carries the *submitted* values, not the client's current ones. That is the
    point of DB §6.2's separation: a practitioner looking at an enquiry needs to
    see what the prospect actually typed, which may since have been corrected on
    the client record.

    🔒 ``age_hours`` and ``is_ageing`` are **server-computed** (Principle 3). A
    browser deriving them from timestamps would disagree across a timezone or a
    clock skew, and the number deciding who gets called next would differ per
    device.
    """

    id: uuid.UUID
    client_id: uuid.UUID | None
    submitted_name: str
    submitted_mobile: str | None
    submitted_email: str | None
    primary_goal: str
    source: LeadSource | None
    source_detail: str | None
    #: 🔒 EC-M2-02 — visible to the *practitioner*, never to the submitter.
    is_duplicate_of_existing: bool
    submitted_at: datetime
    responded_at: datetime | None
    responded_by_user_id: uuid.UUID | None
    age_hours: float
    is_ageing: bool
    #: The client's current stage, so the list can show a lead that has already
    #: moved on. ``None`` when the client was erased (FR-M0-027).
    client_stage: ClientStage | None
    client_owner_user_id: uuid.UUID | None
    owner_name: str | None


@dataclass(frozen=True, slots=True)
class EnquiryPage:
    """One page, plus where to resume."""

    items: list[EnquiryListItem]
    next_cursor: str | None
    total: int | None = None

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None


@dataclass(frozen=True, slots=True)
class EnquiryCursor:
    """Where a page resumes — ADR-A05.

    🔒 Two parts, for the reason every cursor in this codebase has two: the sort
    column is not unique. Submissions arriving in the same second share a
    timestamp, and a cursor keyed on time alone skips or repeats whatever else
    shared that instant — a bug that only appears once there are enough enquiries
    to need a second page.
    """

    submitted_at: datetime
    submission_id: uuid.UUID

    def encode(self) -> str:
        """Opaque to the caller (ADR-A05), trivially decodable by us.

        ⚠️ Unsigned, like the timeline's and the client list's. A cursor names a
        position in the caller's own tenant-scoped, authorization-filtered result
        set; both checks run again on the next page, so forging one buys a
        different offset into data the caller may already read.
        """
        return f"{self.submitted_at.isoformat()}|{self.submission_id}"

    @classmethod
    def decode(cls, raw: str) -> EnquiryCursor | None:
        """Parse a cursor, or ``None`` if it is unusable.

        Returns ``None`` rather than raising: a stale bookmark should show the
        first page, not an error about a parameter nobody typed.
        """
        moment, separator, identifier = raw.rpartition("|")
        if not separator or not moment:
            return None
        try:
            return cls(
                submitted_at=datetime.fromisoformat(moment),
                submission_id=uuid.UUID(identifier),
            )
        except ValueError:
            return None


def _visible_to(
    statement: Select[tuple[EnquirySubmission]],
    *,
    actor_user_id: uuid.UUID,
    role: UserRole | None,
) -> Select[tuple[EnquirySubmission]]:
    """Restrict enquiries to those this practitioner may see — AC-M1-006.

    🔒 Scoped **through the client the enquiry created**, not by a column on the
    submission. An enquiry is about a person, and who may see it is decided by
    who may see that person — otherwise a practitioner denied a client's record
    could read the same name, mobile and goal from the enquiry that produced
    them. Same leak, different door.

    🔒 **The predicate comes from the kernel port, not from a join.** R3 forbids
    `leads` importing `clients` and R6 forbids reading its tables, so
    `ClientDirectory.visible_client_ids` hands back a SELECT this embeds as a
    subquery (Arch §3.4b). The rule then has exactly one definition — the
    `clients` module's — and an enquiry cannot become visible when its client is
    not.

    ⚠️ A subquery rather than a materialised id list. A practitioner with a
    thousand clients would otherwise produce a thousand-element ``IN`` clause,
    and the list would have to be fetched before the page could be planned.

    ⚠️ An enquiry whose client was erased (FR-M0-027 leaves ``client_id`` NULL)
    is visible to the **owner only**. It is consent evidence with no subject left
    to scope it by, and both alternatives are worse: hiding it would remove the
    record NFR-051 exists to keep, and showing it to everyone would widen access
    at exactly the moment someone asked to be forgotten.
    """
    if role is UserRole.OWNER:
        return statement

    visible = get_client_directory().visible_client_ids(actor_user_id=actor_user_id, role=role)
    return statement.where(
        EnquirySubmission.client_id.is_not(None),
        EnquirySubmission.client_id.in_(visible),
    )


async def list_enquiries(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    actor_role: UserRole | None,
    needs_response_only: bool = False,
    source: LeadSource | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    include_total: bool = False,
    now: datetime,
) -> EnquiryPage:
    """A page of enquiries — API §7.2, FR-M2-011.

    Args:
        needs_response_only: 🔒 FR-M2-011's queue. Flips the ordering to
            **oldest first** as well as filtering, because the two belong
            together: the filter without the ordering buries the enquiry that has
            waited longest, which is the one the view exists to surface.
        now: Passed in rather than read, so ``age_hours`` is computed against one
            moment for the whole page. Reading the clock per row would let two
            rows in one response disagree about what time it is.

    ⚠️ The caller has already been authorized for the *action*
    (``enquiry.list``). This applies the row-level half; neither is sufficient
    alone.
    """
    page_size = max(1, min(limit, MAX_PAGE_SIZE))

    statement: Select[tuple[EnquirySubmission]] = select(EnquirySubmission).where(
        EnquirySubmission.tenant_id == tenant_id
    )
    statement = _visible_to(statement, actor_user_id=actor_user_id, role=actor_role)

    if needs_response_only:
        # 🔒 The exact clause ``ix_enquiry_submissions__needs_response`` is
        # partial on. Written as ``IS NULL`` on the same column the index
        # predicate names, so the planner can prove the index applies — the
        # lesson migration 0014 exists to record.
        statement = statement.where(EnquirySubmission.responded_at.is_(None))

    if source is not None:
        statement = statement.where(EnquirySubmission.source == source)

    total = await _count(session, statement) if include_total else None

    ascending = needs_response_only
    if cursor is not None:
        statement = _apply_cursor(statement, cursor, ascending=ascending)

    ordering = (
        (EnquirySubmission.submitted_at.asc(), EnquirySubmission.id.asc())
        if ascending
        else (EnquirySubmission.submitted_at.desc(), EnquirySubmission.id.desc())
    )
    statement = statement.order_by(*ordering)

    rows = list(await session.scalars(statement.limit(page_size + 1)))

    has_more = len(rows) > page_size
    visible = rows[:page_size]
    next_cursor = (
        EnquiryCursor(submitted_at=visible[-1].submitted_at, submission_id=visible[-1].id).encode()
        if has_more and visible
        else None
    )

    return EnquiryPage(
        items=await _project(session, visible, tenant_id=tenant_id, now=now),
        next_cursor=next_cursor,
        total=total,
    )


def _apply_cursor(
    statement: Select[tuple[EnquirySubmission]], raw: str, *, ascending: bool
) -> Select[tuple[EnquirySubmission]]:
    """Resume strictly past the cursor's row, in the index's own order.

    🔒 A row comparison rather than two chained conditions: ``(submitted_at, id)``
    is one ordering key, which is both what the index can seek on and what makes a
    tie impossible to straddle.
    """
    decoded = EnquiryCursor.decode(raw)
    if decoded is None:
        return statement

    key = tuple_(EnquirySubmission.submitted_at, EnquirySubmission.id)
    target = (decoded.submitted_at, decoded.submission_id)
    return statement.where(key > target if ascending else key < target)


async def _count(session: AsyncSession, statement: Select[tuple[EnquirySubmission]]) -> int:
    """``COUNT(*)`` over the same filters, without ordering or the page.

    ⚠️ Built from the filtered statement rather than rebuilt, so a filter added
    above cannot be forgotten here — which would report a total that disagrees
    with the rows beside it.
    """
    subquery = statement.order_by(None).subquery()
    total = await session.scalar(select(func.count()).select_from(subquery))
    return int(total or 0)


async def _project(
    session: AsyncSession,
    rows: list[EnquirySubmission],
    *,
    tenant_id: uuid.UUID,
    now: datetime,
) -> list[EnquiryListItem]:
    """Turn submission rows into list items, resolving clients in bulk.

    🔒 **Two queries regardless of page size.** The obvious implementation — one
    lookup per row — is the N+1 that makes a list slow at exactly the caseload
    where it starts to matter (API §7.1's own warning). ``find_many`` exists on
    the port for this reason.

    ⚠️ Clients are read through ``ClientDirectory`` (Arch §3.4b), never by
    joining ``clients``: that table belongs to another module (R6). Owner *names*
    come from ``users``, which is a kernel table every module may read.
    """
    if not rows:
        return []

    client_ids = [row.client_id for row in rows if row.client_id is not None]
    identities = await get_client_directory().find_many(
        session, tenant_id=tenant_id, client_ids=client_ids
    )

    owner_ids = {identity.owner_user_id for identity in identities.values()}
    owner_names: dict[uuid.UUID, str] = {}
    if owner_ids:
        owner_rows = (
            await session.execute(select(User.id, User.full_name).where(User.id.in_(owner_ids)))
        ).all()
        owner_names = {row[0]: row[1] for row in owner_rows}

    items: list[EnquiryListItem] = []
    for row in rows:
        identity = identities.get(row.client_id) if row.client_id is not None else None
        owner_id = identity.owner_user_id if identity is not None else None
        items.append(
            EnquiryListItem(
                id=row.id,
                client_id=row.client_id,
                submitted_name=row.submitted_name,
                submitted_mobile=row.submitted_mobile,
                submitted_email=row.submitted_email,
                primary_goal=row.primary_goal,
                source=row.source,
                source_detail=row.source_detail,
                is_duplicate_of_existing=row.is_duplicate_of_existing,
                submitted_at=row.submitted_at,
                responded_at=row.responded_at,
                responded_by_user_id=row.responded_by_user_id,
                age_hours=age_in_hours(row.submitted_at, now=now),
                # ⚠️ `responded_at` is passed rather than checked here: the
                # "an answered enquiry is never ageing" rule has one definition,
                # in the kernel, so a second caller cannot forget it.
                is_ageing=is_ageing(row.submitted_at, now=now, responded_at=row.responded_at),
                client_stage=identity.stage if identity is not None else None,
                client_owner_user_id=owner_id,
                owner_name=owner_names.get(owner_id) if owner_id is not None else None,
            )
        )
    return items


async def load_submission_client(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    submission_id: uuid.UUID,
) -> tuple[bool, uuid.UUID | None]:
    """Resolve one submission to the client it is about, for authorization.

    🔒 The seam that lets a router authorize a *submission* through the access
    model of the *client* behind it — the only correct scoping for an enquiry
    (see :func:`_visible_to`), and something `leads` cannot do itself because R3
    forbids it importing `clients`.

    Returns:
        ``(exists, client_id)``. ``client_id`` is ``None`` when the submission is
        present but its client was erased (FR-M0-027) — a case the caller must
        distinguish from absence, because the two get different treatment:
        owner-only access versus a 404.

    ⚠️ Deliberately **not** a filtered list lookup. Scanning a page to find one
    row is both O(page) and wrong past the first page — a submission at position
    150 in the queue would 404 while being plainly visible in the UI.
    """
    row = (
        await session.execute(
            select(EnquirySubmission.id, EnquirySubmission.client_id).where(
                EnquirySubmission.tenant_id == tenant_id,
                EnquirySubmission.id == submission_id,
            )
        )
    ).first()

    if row is None:
        return (False, None)
    return (True, row[1])


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "EnquiryCursor",
    "EnquiryListItem",
    "EnquiryPage",
    "list_enquiries",
    "load_submission_client",
]
