"""Notification preferences — who may be messaged, how often, and when.

DB §11.8, FR-M8-008/009/027, US-M8-06.

🔒 **Precedence is resolved in one function** (:func:`resolve`). Four rows can
apply to a single send — tenant default, tenant per-type, client default, client
per-type — and a rule applied differently by the dispatch path and the settings
screen is indistinguishable from no rule at all. Every caller goes through here.

🔒 **The two halves of precedence are deliberately different**, and this is the
one design decision in this file worth arguing:

* ``is_enabled`` — **any disable wins.** A tenant-wide "stop sending check-ins"
  (FR-M8-027) must not be overridable by a leftover per-client row, and a client
  unsubscribe (US-M8-06) must not be overridable by the practitioner. Both are
  opt-outs, and an opt-out that a more specific row can undo is not an opt-out.
* quiet hours, weekly cap, transport — **the most specific value wins.** These
  are preferences rather than withdrawals: a practitioner legitimately allows one
  client more contact than the default, and a client legitimately asks for a
  narrower window than their practice's.

⚠️ Nothing here reads a client's stage, consent or the tenant's status. Those are
suppression inputs, assembled at dispatch by ``dispatch.py`` and judged by
``kernel.messaging.evaluate_suppression``. A preference is what the *people*
asked for; suppression is what the *rules* require, and folding them together
would make "we did not send because they withdrew consent" indistinguishable
from "we did not send because someone flipped a toggle".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import time

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.errors import ValidationError
from app.kernel.messaging import (
    DEFAULT_MAX_MESSAGES_PER_WEEK,
    DEFAULT_QUIET_HOURS_END,
    DEFAULT_QUIET_HOURS_START,
)
from app.kernel.models import TransportType
from app.modules.messaging.models import NotificationPreference
from app.modules.messaging.templates import Template


@dataclass(frozen=True, slots=True)
class ResolvedPreference:
    """What the layered rows add up to for one (client, message type).

    Frozen: the resolution is a fact about the rows that existed when it was
    read. Code that mutated it would be changing a decision the dispatch path had
    already made, in a way no audit row would explain.
    """

    is_enabled: bool
    quiet_hours_start: time
    quiet_hours_end: time
    max_messages_per_week: int
    transport: TransportType | None


#: 🔒 The defaults when no row says otherwise — `kernel.messaging`'s constants,
#: not restated here. A second copy of "21:00" is a second thing to update.
DEFAULT_PREFERENCE: ResolvedPreference = ResolvedPreference(
    is_enabled=True,
    quiet_hours_start=DEFAULT_QUIET_HOURS_START,
    quiet_hours_end=DEFAULT_QUIET_HOURS_END,
    max_messages_per_week=DEFAULT_MAX_MESSAGES_PER_WEEK,
    transport=None,
)


def _specificity(row: NotificationPreference, client_id: uuid.UUID | None) -> int:
    """How closely a row targets this send. Higher wins for non-boolean fields.

    ⚠️ ``client_id`` is compared rather than assumed: the query returns tenant
    rows too, and a tenant row must score below a client row even though both
    are "the same" template code.
    """
    score = 0
    if client_id is not None and row.client_id == client_id:
        score += 2
    if row.template_code is not None:
        score += 1
    return score


async def resolve(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID | None,
    template_code: str,
) -> ResolvedPreference:
    """The effective preference for one message about to be sent.

    🔒 One query for all four applicable rows, then resolution in Python. The
    alternative — four queries, or a ``ORDER BY … LIMIT 1`` per field — would
    make the "any disable wins" rule impossible to express, because it needs to
    see every row rather than the most specific one.
    """
    rows = list(
        await session.scalars(
            select(NotificationPreference).where(
                NotificationPreference.tenant_id == tenant_id,
                or_(
                    NotificationPreference.client_id.is_(None),
                    NotificationPreference.client_id == client_id,
                ),
                or_(
                    NotificationPreference.template_code.is_(None),
                    NotificationPreference.template_code == template_code,
                ),
            )
        )
    )
    # ⚠️ A row for *another* client can reach here when `client_id` is None: the
    # `IS NULL OR = NULL` comparison is NULL for those rows, so they are excluded
    # by SQL — but a row whose client is None is not, which is correct. This
    # filter is the belt to that braces, and costs nothing.
    rows = [row for row in rows if row.client_id is None or row.client_id == client_id]

    if not rows:
        return DEFAULT_PREFERENCE

    # 🔒 Any disable wins. See the module docstring.
    is_enabled = all(row.is_enabled for row in rows)

    ordered = sorted(rows, key=lambda row: _specificity(row, client_id))
    quiet_start = DEFAULT_PREFERENCE.quiet_hours_start
    quiet_end = DEFAULT_PREFERENCE.quiet_hours_end
    weekly_cap = DEFAULT_PREFERENCE.max_messages_per_week
    transport = DEFAULT_PREFERENCE.transport

    for row in ordered:  # least specific first, so the last write wins
        if row.quiet_hours_start is not None and row.quiet_hours_end is not None:
            quiet_start, quiet_end = row.quiet_hours_start, row.quiet_hours_end
        if row.max_messages_per_week is not None:
            weekly_cap = row.max_messages_per_week
        if row.transport is not None:
            transport = row.transport

    return ResolvedPreference(
        is_enabled=is_enabled,
        quiet_hours_start=quiet_start,
        quiet_hours_end=quiet_end,
        max_messages_per_week=weekly_cap,
        transport=transport,
    )


def assert_disableable(template: Template) -> None:
    """🔒 Refuse to turn off a message type that must always be sendable.

    FR-M8-027 says "any **non-essential** message type", and DB §11.6 explains
    why the qualifier is there: a client locked out of their portal because their
    practitioner turned off magic links is unacceptable. The database carries the
    same rule as `ck_message_templates__essential_not_disableable`; this raises
    first so the practitioner gets a sentence instead of a constraint violation.

    Raises:
        ValidationError: If the template is essential or otherwise not
            practitioner-disableable.
    """
    if template.is_essential:
        raise ValidationError(
            f"{template.code!r} carries portal access and account security, so it cannot "
            "be turned off.",
            action="Disable a reminder or nudge instead.",
        )
    if not template.is_practitioner_disableable:
        raise ValidationError(
            f"{template.code!r} is part of the core workflow and cannot be turned off.",
            action="Adjust its schedule or quiet hours instead.",
        )


async def upsert(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID | None,
    template_code: str | None,
    is_enabled: bool | None = None,
    quiet_hours_start: time | None = None,
    quiet_hours_end: time | None = None,
    max_messages_per_week: int | None = None,
    transport: TransportType | None = None,
) -> NotificationPreference:
    """Create or update the preference row for one scope.

    🔒 The scope — ``(tenant, client, template_code)`` — is the unique key
    (`uq_notification_preferences__scope`, `NULLS NOT DISTINCT`). Without that
    index a tenant could accumulate several contradictory "default" rows and
    which one won would depend on physical order.

    ⚠️ Fields left as ``None`` are **not** written, so a caller setting quiet
    hours does not silently clear a weekly cap. That makes ``None`` mean "leave
    alone" rather than "unset", which is the right default for a settings screen
    that submits one section at a time. Clearing a value is a separate,
    deliberate operation this module does not yet expose — no requirement asks
    for it, and inventing one would be a second meaning for ``None``.
    """
    if quiet_hours_start is not None and quiet_hours_end is None:
        raise ValidationError(
            "A quiet-hours window needs both a start and an end.",
            action="Set both times, or neither.",
        )
    if max_messages_per_week is not None and max_messages_per_week < 0:
        raise ValidationError(
            "A weekly message limit cannot be negative.",
            action="Use 0 to stop messages entirely, or a positive number.",
        )

    existing = await session.scalar(
        select(NotificationPreference).where(
            NotificationPreference.tenant_id == tenant_id,
            NotificationPreference.client_id.is_(None)
            if client_id is None
            else NotificationPreference.client_id == client_id,
            NotificationPreference.template_code.is_(None)
            if template_code is None
            else NotificationPreference.template_code == template_code,
        )
    )

    row = existing or NotificationPreference(
        tenant_id=tenant_id,
        client_id=client_id,
        template_code=template_code,
    )
    if is_enabled is not None:
        row.is_enabled = is_enabled
    if quiet_hours_start is not None and quiet_hours_end is not None:
        row.quiet_hours_start = quiet_hours_start
        row.quiet_hours_end = quiet_hours_end
    if max_messages_per_week is not None:
        row.max_messages_per_week = max_messages_per_week
    if transport is not None:
        row.transport = transport

    if existing is None:
        session.add(row)
    await session.flush()
    return row


async def list_for_tenant(
    session: AsyncSession, *, tenant_id: uuid.UUID, client_id: uuid.UUID | None = None
) -> list[NotificationPreference]:
    """Every preference row a settings screen needs to render.

    With ``client_id`` given, returns that client's overrides *and* the tenant
    defaults they layer on — a screen showing an override without what it
    overrides cannot explain itself.
    """
    statement = select(NotificationPreference).where(NotificationPreference.tenant_id == tenant_id)
    if client_id is None:
        statement = statement.where(NotificationPreference.client_id.is_(None))
    else:
        statement = statement.where(
            or_(
                NotificationPreference.client_id.is_(None),
                NotificationPreference.client_id == client_id,
            )
        )
    rows = await session.scalars(
        statement.order_by(
            NotificationPreference.client_id.nulls_first(),
            NotificationPreference.template_code.nulls_first(),
        )
    )
    return list(rows)
