"""Shared reads over the client spine's own tables.

🔒 One definition per question. DB §5.2's entitlement predicate — ``stage =
'active' AND archived_at IS NULL`` — has two callers with different reasons to
ask: the :class:`~app.kernel.clients.ClientDirectory` port answers it for *other*
modules (M1.5), and the transition path answers it for itself inside a locked
transaction before consuming a slot (FR-M1-002).

⚠️ It lives here rather than on the directory because a second definition of a
billing predicate is a second thing that can drift, and the drift would be a
practitioner billed for a client the list does not show. The directory delegates;
so does enforcement.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.clients import ClientStage
from app.modules.clients.models import Client


async def count_active_clients(session: AsyncSession, *, tenant_id: uuid.UUID) -> int:
    """Clients consuming an ``active_clients`` entitlement — DB §5.2, M1.5.

    🔒 Counted live rather than from a ``usage_counters`` row (DB §14.4). It is the
    product's most visible limit, and a drifting counter would produce a bill the
    practitioner can disprove by eye.

    Served by ``ix_clients__tenant_stage``, whose partial predicate already
    excludes archived rows.

    ⚠️ Runs under the caller's transaction and tenant scope. Called after a
    ``FOR UPDATE`` lock on the enforcement path, so the number it returns is the
    one the pending write is about to change — a count taken before the lock could
    already be stale.
    """
    total = await session.scalar(
        select(func.count())
        .select_from(Client)
        .where(
            Client.tenant_id == tenant_id,
            Client.stage == ClientStage.ACTIVE,
            Client.archived_at.is_(None),
        )
    )
    return int(total or 0)
