"""Metering the one resource this module owns — FR-M1-002, M1.5.

🔒 ``active_clients`` is counted live from ``clients`` (DB §14.4), and ``clients``
belongs to this module — so this module does the counting and hands the number to
``kernel.entitlements.EntitlementGuard``. Nothing outside can: Arch R6 forbids
another module reading these tables, and the kernel cannot count a table it is not
allowed to know about.

⚠️ **Its own file so that ``service`` and ``transitions`` can share it.**
``transitions`` already imports ``service`` for the clock; putting the helper in
either would make the other's import circular. Three entry points consume a slot
and every one of them must check:

* creating a client directly at ``active`` (API §7.1 — "🔒 Metered if
  stage=`active`")
* moving an existing client into ``active`` (FR-M1-002)
* restoring a client archived at ``active`` (EC-M1-06)

🔒 The check is **not** on the paths that release a slot — moving out of
``active``, or archiving. Freeing capacity is never refused, including when the
tenant is already over their limit after a downgrade (EC-M1-06): refusing it would
trap them over the limit with no way down.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.entitlements import ResourceCode, get_entitlement_guard
from app.modules.clients.queries import count_active_clients


async def require_active_client_headroom(session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
    """Permit one more active client, or raise — FR-M1-002.

    🔒 Called immediately before the write that would consume the slot
    (FR-M0-045), inside the caller's transaction. The guard locks the tenant's
    subscription row for live-counted resources, which is what stops two
    concurrent activations of *different* clients both reading the same count and
    both passing — they touch different client rows and would otherwise contend
    on nothing.

    ⚠️ The count is taken *after* any row lock the caller holds, so it reflects
    the state the pending write is about to change rather than a snapshot taken
    before the caller had exclusive access.

    Raises:
        EntitlementError: 402 — the plan's ``active_clients`` limit is reached,
            the subscription does not permit new metered actions, or the
            allowance is indeterminate (FR-M0-046 fails safe).
    """
    await get_entitlement_guard().require(
        session,
        tenant_id=tenant_id,
        resource=ResourceCode.ACTIVE_CLIENTS,
        amount=1,
        live_used=await count_active_clients(session, tenant_id=tenant_id),
    )
