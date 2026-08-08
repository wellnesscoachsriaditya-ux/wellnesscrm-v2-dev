"""Client creation, reading and editing — the write side of the spine.

🔒 DB §5, FR-M1-004..010. This module owns every write to ``clients`` and
``client_stage_history``. Nothing else may write them (DB §5: "Writers:
`clients` only"), and nothing else may read the tables directly — other modules
come through ``kernel.clients.ClientDirectory``.

⚠️ **Stage transitions are not here.** Creating a client sets an initial stage
and writes the opening history row, but *moving* between stages is a named
action with entitlement checks, ``activated_at`` handling and message
cancellation (ADR-A06). That is Slice B, in ``transitions.py``. A generic
``update_client`` that accepted ``stage=`` would be the PATCH-on-status-field
ADR-A06 exists to forbid, so :class:`ClientUpdate` has no stage field at all.

⚠️ Functions take an ``AsyncSession`` rather than opening one. The transaction
belongs to the request pipeline (ADR-04), and a repository that committed
independently would create a client whose accompanying consent record rolled
back.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.clients import (
    ClientStage,
    ContactDetails,
    DietaryClass,
    SexType,
    normalise_mobile,
    validate_full_name,
)
from app.kernel.errors import ConflictError, NotFoundError
from app.modules.clients.models import Client, ClientStageHistory


def now() -> datetime:
    """Timezone-aware current time. Centralised so every row is UTC (NFR-099)."""
    return datetime.now(UTC)


class Unset(enum.Enum):
    """The "field not supplied" marker for a PATCH.

    🔒 ``None`` cannot serve as this marker, because ``None`` is a *meaningful*
    value for every nullable field on :class:`ClientUpdate` — clearing a client's
    email is a real edit a practitioner performs. A sentinel that could not
    express the difference would make every nullable field write-once, and the
    symptom would be "the clear button does nothing".

    An enum rather than ``object()`` so the type narrows: ``x is not UNSET``
    leaves mypy with the real type on the other branch.
    """

    TOKEN = "unset"


#: The singleton marker. See :class:`Unset`.
UNSET = Unset.TOKEN


@dataclass(frozen=True, slots=True)
class ClientCreate:
    """What is needed to create a client — FR-M1-004, FR-M1-005.

    🔒 Name plus **one** contact method is the whole requirement (FR-M1-004).
    Everything else is optional, because a practitioner capturing a lead on a
    phone call has a name and a number and nothing more, and a form that demands
    more than that is a form they abandon.
    """

    full_name: str
    owner_user_id: uuid.UUID
    mobile: str | None = None
    email: str | None = None
    stage: ClientStage = ClientStage.LEAD
    date_of_birth: date | None = None
    sex: SexType | None = None
    city: str | None = None
    preferred_language: str = "en"
    source: str | None = None
    source_detail: str | None = None
    dietary_class: DietaryClass | None = None


@dataclass(frozen=True, slots=True)
class ClientUpdate:
    """A partial edit — PATCH semantics.

    ⚠️ **No ``stage`` field, deliberately** (ADR-A06). Every field here is
    inert data; a stage change has consequences and gets its own named action.

    ⚠️ :data:`UNSET` rather than ``None`` as the "not supplied" marker — see
    :class:`Unset`.
    """

    full_name: str | None = None
    preferred_language: str | None = None
    mobile: str | None | Unset = UNSET
    email: str | None | Unset = UNSET
    date_of_birth: date | None | Unset = UNSET
    sex: SexType | None | Unset = UNSET
    city: str | None | Unset = UNSET
    dietary_class: DietaryClass | None | Unset = UNSET


async def create_client(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    payload: ClientCreate,
    actor_user_id: uuid.UUID | None,
) -> Client:
    """Create a client and its opening stage-history row.

    🔒 Both writes, or neither. FR-M1-015 requires every transition recorded, and
    creation is the first one — a client with no opening history row has a
    lifecycle that appears to start from nothing, and the conversion metrics in
    FR-M9-006 would silently undercount.

    ⚠️ The mobile is normalised before it is stored (NFR-100). The database
    CHECK asserts E.164 *shape*; this is what turns ``98765 43210`` into that
    shape, and refusing here names the field rather than surfacing an integrity
    error three frames up.
    """
    contact = ContactDetails(
        mobile=normalise_mobile(payload.mobile) if payload.mobile else None,
        email=payload.email.strip() if payload.email else None,
    )

    moment = now()
    client = Client(
        tenant_id=tenant_id,
        stage=payload.stage,
        full_name=validate_full_name(payload.full_name),
        mobile=contact.mobile,
        email=contact.email,
        date_of_birth=payload.date_of_birth,
        sex=payload.sex,
        city=payload.city.strip() if payload.city else None,
        preferred_language=payload.preferred_language,
        source=payload.source,
        source_detail=payload.source_detail,
        owner_user_id=payload.owner_user_id,
        dietary_class=payload.dietary_class,
        # 🔒 Set only if the client is born active — the check-in anchor
        # (FR-M8-023). A lead has not activated and must not carry a date.
        activated_at=moment if payload.stage is ClientStage.ACTIVE else None,
    )
    session.add(client)
    await session.flush()

    session.add(
        ClientStageHistory(
            tenant_id=tenant_id,
            client_id=client.id,
            from_stage=None,
            to_stage=payload.stage,
            changed_by_user_id=actor_user_id,
            changed_at=moment,
        )
    )
    await session.flush()
    return client


async def get_client(
    session: AsyncSession, *, tenant_id: uuid.UUID, client_id: uuid.UUID
) -> Client:
    """One client by id.

    ⚠️ Returns archived clients too. FR-M1-010 makes archiving a soft delete and
    AC-M1-007 requires the data intact — hiding archived rows *here* would make
    restoring one impossible. Exclusion from default views is the list query's
    job, and the partial indexes make it structural there.

    Raises:
        NotFoundError: 🔒 Also raised when the row belongs to another tenant —
            RLS makes that indistinguishable from absent, which API §5.4 requires
            it to be.
    """
    client = await session.scalar(
        select(Client).where(Client.tenant_id == tenant_id, Client.id == client_id)
    )
    if client is None:
        raise NotFoundError(
            "That client could not be found.",
            action="Check the link, or search for them by name.",
        )
    return client


async def update_client(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    payload: ClientUpdate,
    expected_updated_at: datetime | None = None,
) -> Client:
    """Apply a partial edit.

    🔒 Optimistic concurrency via ``expected_updated_at`` (ADR-14, API §4.4).
    Two practitioners editing one client is routine in a clinic, and a
    last-write-wins update silently discards the first one's work — EC-M4-07 is
    the same failure in a different module.

    Raises:
        ConflictError: The record changed since it was read.
        NotFoundError: No such client in this tenant.
    """
    client = await get_client(session, tenant_id=tenant_id, client_id=client_id)

    if expected_updated_at is not None and client.updated_at != expected_updated_at:
        raise ConflictError(
            "Someone else changed this client while you were editing.",
            action="Reload the client and reapply your changes.",
            details={"current_state": client.updated_at.isoformat()},
        )

    if payload.full_name is not None:
        client.full_name = validate_full_name(payload.full_name)
    if payload.preferred_language is not None:
        client.preferred_language = payload.preferred_language

    # 🔒 Contact changes are validated as a pair, not field by field. Clearing a
    # mobile is legitimate; clearing the *last* contact method is not
    # (FR-M1-004), and only the combination can tell the difference.
    mobile = client.mobile
    email = client.email
    if not isinstance(payload.mobile, Unset):
        mobile = normalise_mobile(payload.mobile) if payload.mobile else None
    if not isinstance(payload.email, Unset):
        email = payload.email.strip() if payload.email else None

    contact = ContactDetails(mobile=mobile, email=email)
    client.mobile = contact.mobile
    client.email = contact.email

    if not isinstance(payload.date_of_birth, Unset):
        client.date_of_birth = payload.date_of_birth
    if not isinstance(payload.sex, Unset):
        client.sex = payload.sex
    if not isinstance(payload.city, Unset):
        client.city = payload.city.strip() if payload.city else None
    if not isinstance(payload.dietary_class, Unset):
        client.dietary_class = payload.dietary_class

    client.updated_at = now()
    await session.flush()
    return client
