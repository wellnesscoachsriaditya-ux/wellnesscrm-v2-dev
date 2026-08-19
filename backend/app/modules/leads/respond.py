"""Marking an enquiry answered — the half of FR-M2-011 the list depends on.

🔒 A **status on the submission, not on the client** — the decision that makes
"has this enquiry been answered" a per-enquiry question rather than a per-client
one. A returning prospect (EC-M2-02) creates a second submission against a
client who was responded to months ago; a flag on the client would show the new
enquiry as already handled, which is how enquiries get lost.

⚠️ **"Responded" means the practitioner did something**, not that the prospect
replied. FR-M2-011 asks for "leads awaiting response"; the queue clears when the
practitioner acts. Tracking whether the *prospect* answered is a follow-up
sequence concern (FR-M2-014), which is Phase 2.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.errors import NotFoundError
from app.modules.leads.forms import now
from app.modules.leads.models import EnquirySubmission


async def mark_responded(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    submission_id: uuid.UUID,
    responded_by_user_id: uuid.UUID,
) -> None:
    """Clear an enquiry from the needs-response queue — FR-M2-011.

    🔒 Idempotent: marking an already-answered enquiry again is a no-op rather
    than an error. The queue refreshes itself after the action, and a practitioner
    who double-taps the same row should not see an error for doing the same thing
    twice — the same shape as `client.change_stage`'s no-op refusal, but softer
    because nothing here can be *wrong* to repeat.

    🔒 **The actor is recorded** (``responded_by_user_id``). "Who cleared this
    from the queue" is a question the audit log answers, and the row carries the
    answer beside it. The constraint
    ``ck_enquiry_submissions__response_complete`` pairs it with ``responded_at``
    so neither can exist alone.

    ⚠️ **The row-level scoping is the caller's job, and it is not optional.**
    This function applies the tenant scope only. The route must have resolved the
    submission's client and passed through ``access.load_for_access`` — or a
    practitioner could clear a colleague's enquiry, which AC-M1-006 forbids. The
    reason the check lives in the router rather than here is structural: `leads`
    may not import `clients` (R3), and `clients` owns the access model.

    Raises:
        NotFoundError: The submission is not this tenant's. RLS makes a foreign
            tenant's row indistinguishable from absent, which API §5.4 requires
            it to be.
    """
    # ⚠️ Two statements rather than one, and the split is what makes the
    # idempotency real. A single UPDATE carrying `responded_at IS NULL` cannot
    # distinguish "not your row" from "already answered" — both report zero rows
    # — so it would have to treat them alike. They are not alike: one is a 404
    # and the other is success.
    # ⚠️ Two statements rather than one, and the split is what makes the
    # idempotency real. A single UPDATE carrying ``responded_at IS NULL`` cannot
    # distinguish "not your row" from "already answered" — both report zero rows
    # — so it would have to treat them alike. They are not alike: one is a 404,
    # the other is success.
    #
    # ⚠️ The row is selected, not the column. ``scalar()`` returns ``None`` both
    # for a missing row and for a present row whose ``responded_at`` is NULL, so
    # a column read cannot tell apart the exact two cases this function exists
    # to distinguish.
    #
    # 🔒 ``FOR UPDATE`` because this is a read-then-write. Two practitioners
    # clearing the same enquiry concurrently would otherwise both observe NULL
    # and both write, and the second would overwrite the first's attribution.
    row = (
        await session.execute(
            select(EnquirySubmission)
            .where(
                EnquirySubmission.tenant_id == tenant_id,
                EnquirySubmission.id == submission_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()

    if row is None:
        raise NotFoundError(
            "That enquiry could not be found.",
            action="Refresh the list and try again.",
        )

    if row.responded_at is not None:
        # Already answered. The original responder and timestamp stand: the
        # question this row answers is "when was this first handled, and by
        # whom", and a second tap must not overwrite that.
        return

    row.responded_at = now()
    row.responded_by_user_id = responded_by_user_id
    await session.flush()


__all__ = ["mark_responded"]
