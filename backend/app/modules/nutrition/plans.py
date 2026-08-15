"""Plan authoring — the writes behind API §8.2, §8.3 and §8.6.

🔒 **Two invariants hold across everything here**, and both are enforced before
any row is touched rather than checked afterwards:

* :func:`~app.kernel.nutrition.assert_draft` — a version that is not a ``draft``
  is a clinical record (DDR-11, EC-M4-03). There is no edit path to one; the
  practitioner revises instead.
* ``row_version`` — every mutation states the version it believed it was editing
  and is refused if that has moved (ADR-14, EC-M4-07). 🔒 A child edit bumps the
  **parent version's** counter, because the aggregate is the unit of concurrency:
  two practitioners editing different slots of one plan are still editing one
  plan, and a builder holding a stale view must find out.

⚠️ **Nothing here commits** (ADR-04). The router owns the transaction, which is
what lets a create write a plan, a version, seven days and forty-nine slots
atomically without any function knowing about the others.

⚠️ **Every query carries an explicit ``tenant_id`` predicate** even though RLS
already filters. RLS is the guarantee; the predicate is the part a reviewer can
see, and it is what makes a stray query through the migrator role in a test fail
loudly instead of quietly returning another tenant's row.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.errors import ConflictError, NotFoundError, ValidationError
from app.kernel.events import publish
from app.kernel.nutrition import (
    DEFAULT_SLOT_SEQUENCE,
    MealSlotType,
    PlanOrigin,
    PlanState,
    RenderStatus,
    assert_draft,
    snapshot_content_hash,
)
from app.modules.nutrition.composition import (
    ITEM_TYPE_FOOD,
    ITEM_TYPE_MEAL,
    ITEM_TYPE_RECIPE,
)
from app.modules.nutrition.events import PlanVersionIssued
from app.modules.nutrition.models import (
    DietPlan,
    DietPlanVersion,
    PlanDay,
    PlanItem,
    PlanSlot,
    PlanSnapshot,
)

#: 🔒 API §8.2 — where a new plan's content comes from (FR-M4-032).
#:
#: ⏳ ``template`` and ``ai_draft`` are named by the contract and refused here:
#: the template tables exist but nothing reads them yet, and AI drafting is M5.
#: Accepting either and quietly producing a blank plan would be worse than a
#: clear refusal.
PLAN_SOURCE_BLANK = "blank"
PLAN_SOURCE_PREVIOUS_PLAN = "previous_plan"
PLAN_SOURCE_TEMPLATE = "template"
PLAN_SOURCE_AI_DRAFT = "ai_draft"

_SUPPORTED_SOURCES = frozenset({PLAN_SOURCE_BLANK, PLAN_SOURCE_PREVIOUS_PLAN})

#: The item references a plan item may carry. ``ck_plan_items__one_reference``
#: enforces exactly one at the table; this maps the caller's word onto it.
_ITEM_TYPES = frozenset({ITEM_TYPE_FOOD, ITEM_TYPE_RECIPE, ITEM_TYPE_MEAL})

#: A sentinel for "this field was not supplied", so a PATCH can tell *clear this*
#: from *leave it alone*. Mirrors ``kernel.clients``' ``UNSET``.
_UNSET: object = object()


# ─── Loading and guarding ────────────────────────────────────────────────


async def load_plan(session: AsyncSession, *, tenant_id: uuid.UUID, plan_id: uuid.UUID) -> DietPlan:
    """One plan, or 404. 🔒 Another tenant's plan is indistinguishable from absent."""
    plan = (
        await session.execute(
            select(DietPlan).where(DietPlan.id == plan_id, DietPlan.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if plan is None:
        raise NotFoundError(
            message="That plan doesn't exist.",
            action="Go back to the client and try again.",
        )
    return plan


async def load_plan_version(
    session: AsyncSession, *, tenant_id: uuid.UUID, version_id: uuid.UUID
) -> DietPlanVersion:
    """One version, or 404."""
    version = (
        await session.execute(
            select(DietPlanVersion).where(
                DietPlanVersion.id == version_id, DietPlanVersion.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise NotFoundError(
            message="That plan version doesn't exist.",
            action="Reload the plan and try again.",
        )
    return version


async def list_plan_versions(
    session: AsyncSession, *, tenant_id: uuid.UUID, plan_id: uuid.UUID
) -> list[DietPlanVersion]:
    """Every version of one plan, newest first — 🔒 AC-M4-008.

    Superseded and discarded versions are included: the point of DDR-11's
    copy-on-revise is that the history survives, and a list that hid the
    superseded ones would make it unreachable through the API that owns it.
    """
    return list(
        (
            await session.execute(
                select(DietPlanVersion)
                .where(
                    DietPlanVersion.tenant_id == tenant_id,
                    DietPlanVersion.plan_id == plan_id,
                )
                .order_by(DietPlanVersion.version_number.desc())
            )
        )
        .scalars()
        .all()
    )


async def list_plans_for_client(
    session: AsyncSession, *, tenant_id: uuid.UUID, client_id: uuid.UUID
) -> list[tuple[DietPlan, DietPlanVersion | None]]:
    """A client's plans, each with the version to show for it.

    The version shown is ``current_version_id`` when the plan has been issued at
    least once, and otherwise its open draft — which is what a practitioner
    means by "the plan" in both states.
    """
    plans = list(
        (
            await session.execute(
                select(DietPlan)
                .where(DietPlan.tenant_id == tenant_id, DietPlan.client_id == client_id)
                .order_by(DietPlan.id)
            )
        )
        .scalars()
        .all()
    )
    if not plans:
        return []

    versions = list(
        (
            await session.execute(
                select(DietPlanVersion).where(
                    DietPlanVersion.tenant_id == tenant_id,
                    DietPlanVersion.plan_id.in_([plan.id for plan in plans]),
                )
            )
        )
        .scalars()
        .all()
    )
    by_id = {version.id: version for version in versions}
    drafts = {version.plan_id: version for version in versions if version.state is PlanState.draft}

    return [
        (
            plan,
            by_id.get(plan.current_version_id) if plan.current_version_id else drafts.get(plan.id),
        )
        for plan in plans
    ]


@dataclass(frozen=True, slots=True)
class IssuedPlan:
    """The plan a client is currently following, and the hash of its content.

    🔒 ``content_hash`` comes from ``plan_snapshots``, not from re-hashing what
    a read computed. DDR-12 makes the hash the portal's cache key (FR-M7-011,
    EC-M7-03): if it were derived on read, a corrected curated food would change
    it and every client's service worker would discard a plan that had not
    actually been revised.

    ⚠️ ``content_hash`` is nullable because ``issue_plan_version`` writes the
    snapshot in the same transaction as the state change, but an issued version
    from before that write existed would have none. A missing hash means "do not
    cache", which is the safe reading.
    """

    plan: DietPlan
    version: DietPlanVersion
    content_hash: str | None


async def current_issued_plan_for_client(
    session: AsyncSession, *, tenant_id: uuid.UUID, client_id: uuid.UUID
) -> IssuedPlan | None:
    """The one plan a client should see — API §12.2's input.

    🔒 **Issued only.** A draft is the practitioner's working copy; showing one
    to a client would put unreviewed content in front of them, which is the
    inverse of FR-M4-001's "practitioner always approves".

    🔒 **Archived plans are excluded**, and a client with no issued plan returns
    ``None`` rather than raising — EC-M7-02 requires a meaningful empty state,
    not a 404 on the portal's landing route.

    ⚠️ Newest issue wins when a client somehow holds two live plans. DDR-11
    supersedes the previous version *within* a plan, but nothing stops a
    practitioner creating a second plan, and "the most recently issued one" is
    the only answer a client would recognise.
    """
    row = (
        await session.execute(
            select(DietPlan, DietPlanVersion)
            .join(DietPlanVersion, DietPlanVersion.id == DietPlan.current_version_id)
            .where(
                DietPlan.tenant_id == tenant_id,
                DietPlan.client_id == client_id,
                DietPlan.archived_at.is_(None),
                DietPlanVersion.tenant_id == tenant_id,
                DietPlanVersion.state == PlanState.issued,
            )
            .order_by(DietPlanVersion.issued_at.desc().nullslast())
            .limit(1)
        )
    ).first()

    if row is None:
        return None

    plan, version = row
    content_hash = (
        await session.execute(
            select(PlanSnapshot.content_hash).where(
                PlanSnapshot.tenant_id == tenant_id,
                PlanSnapshot.plan_version_id == version.id,
            )
        )
    ).scalar_one_or_none()

    return IssuedPlan(plan=plan, version=version, content_hash=content_hash)


async def _claim_draft(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    version_id: uuid.UUID,
    expected_row_version: int,
) -> DietPlanVersion:
    """🔒 The gate every mutation passes through — ADR-14, EC-M4-07, DDR-11.

    Three refusals, in an order chosen so each answer is the honest one:

    1. **404** when the version is absent or another tenant's. Confirming
       existence is itself the leak (API §5.4).
    2. **409** when it is not a draft. That is a state error, not a staleness
       error, and telling the practitioner "reload" when the real answer is
       "this plan was already sent" would send them round a loop.
    3. **409** when ``row_version`` has moved. Somebody else edited this plan
       since the caller read it.

    Bumping the counter here rather than at the end is deliberate: the ``UPDATE``
    takes a row lock on the version, so two concurrent edits to different slots
    of the same plan serialise on it instead of interleaving.
    """
    version = await load_plan_version(session, tenant_id=tenant_id, version_id=version_id)
    assert_draft(version.state)

    result = await session.execute(
        update(DietPlanVersion)
        .where(
            DietPlanVersion.id == version_id,
            DietPlanVersion.tenant_id == tenant_id,
            DietPlanVersion.row_version == expected_row_version,
        )
        .values(row_version=DietPlanVersion.row_version + 1)
    )
    if result.rowcount == 0:
        raise ConflictError(
            message="Someone else changed this plan while you were editing it.",
            action="Reload the plan to see their changes, then make yours again.",
            details={"current_row_version": version.row_version},
        )

    await session.refresh(version)
    return version


async def _version_of_day(
    session: AsyncSession, *, tenant_id: uuid.UUID, day_id: uuid.UUID
) -> tuple[PlanDay, uuid.UUID]:
    day = (
        await session.execute(
            select(PlanDay).where(PlanDay.id == day_id, PlanDay.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if day is None:
        raise NotFoundError(
            message="That day doesn't exist.", action="Reload the plan and try again."
        )
    return day, day.plan_version_id


async def _version_of_slot(
    session: AsyncSession, *, tenant_id: uuid.UUID, slot_id: uuid.UUID
) -> tuple[PlanSlot, uuid.UUID]:
    row = (
        await session.execute(
            select(PlanSlot, PlanDay.plan_version_id)
            .join(PlanDay, PlanDay.id == PlanSlot.plan_day_id)
            .where(PlanSlot.id == slot_id, PlanSlot.tenant_id == tenant_id)
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError(
            message="That meal slot doesn't exist.", action="Reload the plan and try again."
        )
    return row[0], row[1]


async def _version_of_item(
    session: AsyncSession, *, tenant_id: uuid.UUID, item_id: uuid.UUID
) -> tuple[PlanItem, uuid.UUID]:
    row = (
        await session.execute(
            select(PlanItem, PlanDay.plan_version_id)
            .join(PlanSlot, PlanSlot.id == PlanItem.plan_slot_id)
            .join(PlanDay, PlanDay.id == PlanSlot.plan_day_id)
            .where(PlanItem.id == item_id, PlanItem.tenant_id == tenant_id)
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError(
            message="That item doesn't exist.", action="Reload the plan and try again."
        )
    return row[0], row[1]


async def _next_sort_order(
    session: AsyncSession,
    model: type[PlanSlot] | type[PlanItem],
    parent_column: object,
    parent_id: uuid.UUID,
) -> int:
    highest = (
        await session.execute(
            select(func.max(model.sort_order)).where(parent_column == parent_id)  # type: ignore[arg-type]
        )
    ).scalar()
    return int(highest or 0) + 1


# ─── Creation ────────────────────────────────────────────────────────────


async def create_plan(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    created_by_user_id: uuid.UUID,
    title: str,
    source: str = PLAN_SOURCE_BLANK,
    source_plan_version_id: uuid.UUID | None = None,
    goal_type: str | None = None,
    day_count: int = 1,
    slot_types: Sequence[MealSlotType] | None = None,
    valid_from: date | None = None,
    valid_to: date | None = None,
    target_energy_kcal: Decimal | None = None,
    target_protein_g: Decimal | None = None,
    target_carbs_g: Decimal | None = None,
    target_fat_g: Decimal | None = None,
) -> tuple[DietPlan, DietPlanVersion]:
    """Create a plan **and** its first draft version — API §8.2, FR-M4-032.

    🔒 The two are inseparable. A plan with no version is not a state a
    practitioner can do anything with, so the API never produces one.

    ``slot_types`` defaults to :data:`~app.kernel.nutrition.DEFAULT_SLOT_SEQUENCE`
    — FR-M4-025's seven — applied to every day. 🟡 Those seven are a PRD
    *proposal*, so the sequence is a parameter rather than a hard-coded loop:
    pass an empty sequence for a bare plan, or a different one once G1 has said
    what practitioners actually use.

    Raises:
        ValidationError: On an unsupported ``source``, a missing source version,
            an empty title, or a non-positive ``day_count``.
        ConflictError: If the plan somehow already has an open draft. 🔒 Raised by
            ``uq_diet_plan_versions__one_draft`` at the table, not by a check
            here — a unique index cannot be raced, and a check can.
    """
    clean_title = title.strip()
    if not clean_title:
        raise ValidationError(
            "A plan needs a title.", action="Give the plan a name the client will recognise."
        )
    if day_count < 1:
        raise ValidationError(
            "A plan needs at least one day.", action="Choose a single-day or 7-day plan."
        )
    if source not in _SUPPORTED_SOURCES:
        raise ValidationError(
            f"Plans cannot be created from “{source}” yet.",
            action="Create the plan from blank or from a previous plan.",
            details={"supported": sorted(_SUPPORTED_SOURCES)},
        )

    source_version: DietPlanVersion | None = None
    if source == PLAN_SOURCE_PREVIOUS_PLAN:
        if source_plan_version_id is None:
            raise ValidationError(
                "Choose the plan to copy from.",
                action="Pick a previous plan version, or start from blank.",
            )
        source_version = await load_plan_version(
            session, tenant_id=tenant_id, version_id=source_plan_version_id
        )

    plan = DietPlan(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        client_id=client_id,
        title=clean_title,
        goal_type=goal_type,
        created_by_user_id=created_by_user_id,
    )
    session.add(plan)

    version = DietPlanVersion(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        plan_id=plan.id,
        version_number=1,
        state=PlanState.draft,
        origin=PlanOrigin.revision if source_version else PlanOrigin.manual,
        source_version_id=source_version.id if source_version else None,
        valid_from=_as_datetime(valid_from),
        valid_to=_as_datetime(valid_to),
        # A copy inherits the source's targets unless the caller overrode them,
        # because "same plan, next week" is the J2 follow-up loop and retyping
        # four numbers is how that loop leaks time.
        target_energy_kcal=_first_set(target_energy_kcal, source_version, "target_energy_kcal"),
        target_protein_g=_first_set(target_protein_g, source_version, "target_protein_g"),
        target_carbs_g=_first_set(target_carbs_g, source_version, "target_carbs_g"),
        target_fat_g=_first_set(target_fat_g, source_version, "target_fat_g"),
    )
    session.add(version)
    await session.flush()

    if source_version is not None:
        await _copy_structure(
            session, tenant_id=tenant_id, source_version_id=source_version.id, target=version
        )
    else:
        await _build_blank_structure(
            session,
            tenant_id=tenant_id,
            version_id=version.id,
            day_count=day_count,
            slot_types=DEFAULT_SLOT_SEQUENCE if slot_types is None else tuple(slot_types),
        )

    await session.flush()
    return plan, version


def _as_datetime(value: date | None) -> datetime | None:
    """``valid_from``/``valid_to`` are dates in the API and timestamps in the table."""
    if value is None:
        return None
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def _first_set(
    override: Decimal | None, source: DietPlanVersion | None, attribute: str
) -> Decimal | None:
    if override is not None:
        return override
    if source is None:
        return None
    value = getattr(source, attribute)
    return value if isinstance(value, Decimal) else None


async def _build_blank_structure(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    version_id: uuid.UUID,
    day_count: int,
    slot_types: tuple[MealSlotType, ...],
) -> None:
    for day_number in range(1, day_count + 1):
        day = PlanDay(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            plan_version_id=version_id,
            day_number=day_number,
            label=f"Day {day_number}",
        )
        session.add(day)
        for position, slot_type in enumerate(slot_types, start=1):
            session.add(
                PlanSlot(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    plan_day_id=day.id,
                    slot_type=slot_type.value,
                    sort_order=position,
                )
            )


async def _copy_structure(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    source_version_id: uuid.UUID,
    target: DietPlanVersion,
) -> None:
    """Deep-copy a version's days, slots and items into a fresh draft — DDR-11.

    🔒 **Copied, never shared.** The point of the plan/version split is that
    editing the new draft cannot reach back into what a client was already given
    (EC-M4-03), and that is only true if every row is a new row.

    🔒 ``is_locked`` is carried across, on both slots and items. A practitioner
    who fixed a client's breakfast last week has not un-fixed it by starting this
    week's plan from it — and the locks stay editable, which is why unlock exists
    and is always available.

    ⏳ Alternatives and supplements are not copied because nothing writes them
    yet. The slice that adds them extends this function; leaving a silent gap
    here would mean a copy that loses data nobody noticed.
    """
    days = list(
        (
            await session.execute(
                select(PlanDay)
                .where(PlanDay.tenant_id == tenant_id, PlanDay.plan_version_id == source_version_id)
                .order_by(PlanDay.day_number)
            )
        )
        .scalars()
        .all()
    )
    if not days:
        return

    slots = list(
        (
            await session.execute(
                select(PlanSlot)
                .where(
                    PlanSlot.tenant_id == tenant_id,
                    PlanSlot.plan_day_id.in_([day.id for day in days]),
                )
                .order_by(PlanSlot.sort_order)
            )
        )
        .scalars()
        .all()
    )
    items = (
        list(
            (
                await session.execute(
                    select(PlanItem)
                    .where(
                        PlanItem.tenant_id == tenant_id,
                        PlanItem.plan_slot_id.in_([slot.id for slot in slots]),
                    )
                    .order_by(PlanItem.sort_order)
                )
            )
            .scalars()
            .all()
        )
        if slots
        else []
    )

    items_by_slot: dict[uuid.UUID, list[PlanItem]] = {}
    for item in items:
        items_by_slot.setdefault(item.plan_slot_id, []).append(item)
    slots_by_day: dict[uuid.UUID, list[PlanSlot]] = {}
    for slot in slots:
        slots_by_day.setdefault(slot.plan_day_id, []).append(slot)

    # ⚠️ Flushed level by level — days, then slots, then items. SQLAlchemy orders
    # INSERTs across tables by foreign key, but these rows are built from source
    # rows that are *already* in this session's identity map, and letting one
    # flush sort a mixed batch of old and new rows is how a child arrives before
    # its parent. Three small flushes cost nothing and make the order explicit.
    new_days: list[tuple[PlanDay, uuid.UUID]] = []
    for day in days:
        new_day = PlanDay(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            plan_version_id=target.id,
            day_number=day.day_number,
            label=day.label,
        )
        session.add(new_day)
        new_days.append((new_day, day.id))
    await session.flush()

    new_slots: list[tuple[PlanSlot, uuid.UUID]] = []
    for new_day, source_day_id in new_days:
        for slot in slots_by_day.get(source_day_id, []):
            new_slot = PlanSlot(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                plan_day_id=new_day.id,
                slot_type=slot.slot_type,
                custom_label=slot.custom_label,
                target_time=slot.target_time,
                sort_order=slot.sort_order,
                is_locked=slot.is_locked,
            )
            session.add(new_slot)
            new_slots.append((new_slot, slot.id))
    await session.flush()

    for new_slot, source_slot_id in new_slots:
        for item in items_by_slot.get(source_slot_id, []):
            session.add(
                PlanItem(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    plan_slot_id=new_slot.id,
                    item_type=item.item_type,
                    food_id=item.food_id,
                    recipe_id=item.recipe_id,
                    meal_id=item.meal_id,
                    quantity=item.quantity,
                    measure_unit_id=item.measure_unit_id,
                    notes=item.notes,
                    client_note=item.client_note,
                    is_locked=item.is_locked,
                    # 🔒 Never copied. `resolved_grams` is frozen *at issue*
                    # (DB §8.12); carrying it into a draft would present a stale
                    # weight as a live one.
                    sort_order=item.sort_order,
                )
            )
    await session.flush()


# ─── Version-level edits ─────────────────────────────────────────────────


async def update_plan_version(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    version_id: uuid.UUID,
    expected_row_version: int,
    title: str | object = _UNSET,
    goal_type: str | None | object = _UNSET,
    practitioner_notes: str | None | object = _UNSET,
    valid_from: date | None | object = _UNSET,
    valid_to: date | None | object = _UNSET,
    target_energy_kcal: Decimal | None | object = _UNSET,
    target_protein_g: Decimal | None | object = _UNSET,
    target_carbs_g: Decimal | None | object = _UNSET,
    target_fat_g: Decimal | None | object = _UNSET,
) -> DietPlanVersion:
    """Edit a draft's metadata and targets — API §8.3.

    ``title`` and ``goal_type`` live on the *plan*, not the version, so setting
    them here updates the parent. Everything else is per-version, because a
    revision may legitimately carry different targets from the one before it.
    """
    version = await _claim_draft(
        session,
        tenant_id=tenant_id,
        version_id=version_id,
        expected_row_version=expected_row_version,
    )

    if title is not _UNSET or goal_type is not _UNSET:
        plan = await load_plan(session, tenant_id=tenant_id, plan_id=version.plan_id)
        if title is not _UNSET:
            # ⚠️ `None` is refused rather than coerced. A PATCH may legitimately
            # clear `goal_type` or `practitioner_notes`, but a plan with no title
            # is not a thing a client can be handed — and `str(None).strip()`
            # would have written the literal "None" into it.
            clean = title.strip() if isinstance(title, str) else ""
            if not clean:
                raise ValidationError(
                    "A plan needs a title.", action="Give the plan a name and try again."
                )
            plan.title = clean
        if goal_type is not _UNSET:
            plan.goal_type = goal_type  # type: ignore[assignment]

    if practitioner_notes is not _UNSET:
        version.practitioner_notes = practitioner_notes  # type: ignore[assignment]
    if valid_from is not _UNSET:
        version.valid_from = _as_datetime(valid_from)  # type: ignore[arg-type]
    if valid_to is not _UNSET:
        version.valid_to = _as_datetime(valid_to)  # type: ignore[arg-type]
    for attribute, supplied in (
        ("target_energy_kcal", target_energy_kcal),
        ("target_protein_g", target_protein_g),
        ("target_carbs_g", target_carbs_g),
        ("target_fat_g", target_fat_g),
    ):
        if supplied is not _UNSET:
            setattr(version, attribute, supplied)

    await session.flush()
    return version


async def discard_plan_version(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    version_id: uuid.UUID,
    expected_row_version: int,
) -> DietPlanVersion:
    """Abandon a draft — API §8.1.

    🔒 The row stays. ``discarded`` is a state, not a delete: the practitioner
    started this plan for a reason, and DDR-11 keeps the history of what was
    tried. It also frees ``uq_diet_plan_versions__one_draft`` so a new draft can
    be started for the same plan.
    """
    version = await _claim_draft(
        session,
        tenant_id=tenant_id,
        version_id=version_id,
        expected_row_version=expected_row_version,
    )
    version.state = PlanState.discarded
    await session.flush()
    return version


# ─── Days ────────────────────────────────────────────────────────────────


async def add_day(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    version_id: uuid.UUID,
    expected_row_version: int,
    label: str | None = None,
    slot_types: Sequence[MealSlotType] | None = None,
) -> PlanDay:
    """Append a day to a draft — FR-M4-026."""
    await _claim_draft(
        session,
        tenant_id=tenant_id,
        version_id=version_id,
        expected_row_version=expected_row_version,
    )

    highest = (
        await session.execute(
            select(func.max(PlanDay.day_number)).where(
                PlanDay.tenant_id == tenant_id, PlanDay.plan_version_id == version_id
            )
        )
    ).scalar()
    day_number = int(highest or 0) + 1

    day = PlanDay(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        plan_version_id=version_id,
        day_number=day_number,
        label=(label or f"Day {day_number}").strip(),
    )
    session.add(day)

    for position, slot_type in enumerate(
        DEFAULT_SLOT_SEQUENCE if slot_types is None else tuple(slot_types), start=1
    ):
        session.add(
            PlanSlot(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                plan_day_id=day.id,
                slot_type=slot_type.value,
                sort_order=position,
            )
        )

    await session.flush()
    return day


async def update_day(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    day_id: uuid.UUID,
    expected_row_version: int,
    label: str,
) -> PlanDay:
    """Rename a day."""
    day, version_id = await _version_of_day(session, tenant_id=tenant_id, day_id=day_id)
    await _claim_draft(
        session,
        tenant_id=tenant_id,
        version_id=version_id,
        expected_row_version=expected_row_version,
    )

    clean = label.strip()
    if not clean:
        raise ValidationError("A day needs a label.", action="Give the day a name.")
    day.label = clean
    await session.flush()
    return day


# ─── Slots ───────────────────────────────────────────────────────────────


async def add_slot(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    day_id: uuid.UUID,
    expected_row_version: int,
    slot_type: MealSlotType,
    custom_label: str | None = None,
    target_time: str | None = None,
) -> PlanSlot:
    """Add a meal slot to a day — FR-M4-025."""
    _, version_id = await _version_of_day(session, tenant_id=tenant_id, day_id=day_id)
    await _claim_draft(
        session,
        tenant_id=tenant_id,
        version_id=version_id,
        expected_row_version=expected_row_version,
    )

    slot = PlanSlot(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        plan_day_id=day_id,
        slot_type=slot_type.value,
        custom_label=custom_label,
        target_time=target_time,
        sort_order=await _next_sort_order(session, PlanSlot, PlanSlot.plan_day_id, day_id),
    )
    session.add(slot)
    await session.flush()
    return slot


async def update_slot(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    slot_id: uuid.UUID,
    expected_row_version: int,
    slot_type: MealSlotType | object = _UNSET,
    custom_label: str | None | object = _UNSET,
    target_time: str | None | object = _UNSET,
    sort_order: int | object = _UNSET,
) -> PlanSlot:
    """Rename, retime or reorder a slot — FR-M4-025.

    🔒 Renaming sets ``custom_label``, not ``slot_type``. The type is structural
    (it is what the food log and the PDF group by); the label is what the
    practitioner calls it.
    """
    slot, version_id = await _version_of_slot(session, tenant_id=tenant_id, slot_id=slot_id)
    await _claim_draft(
        session,
        tenant_id=tenant_id,
        version_id=version_id,
        expected_row_version=expected_row_version,
    )

    # ⚠️ `isinstance` rather than `is not _UNSET`: it narrows the union *and*
    # refuses a raw string, so the stored value can only ever be a member of the
    # vocabulary. The column is `text` until the PostgreSQL enum lands, so this
    # is currently the only thing standing between it and free-form input.
    if isinstance(slot_type, MealSlotType):
        slot.slot_type = slot_type.value
    if custom_label is not _UNSET:
        slot.custom_label = custom_label  # type: ignore[assignment]
    if target_time is not _UNSET:
        slot.target_time = target_time  # type: ignore[assignment]
    # ⚠️ `isinstance` again: a PATCH body carrying an explicit null for a field
    # that has no "cleared" meaning must be ignored, not coerced. `int(None)` is
    # a TypeError, which would surface as a 500 for a caller mistake.
    if isinstance(sort_order, int):
        slot.sort_order = sort_order

    await session.flush()
    return slot


async def remove_slot(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    slot_id: uuid.UUID,
    expected_row_version: int,
) -> None:
    """Remove a slot and everything in it — FR-M4-025.

    🔒 A hard delete, and safe because of *what* is being deleted: an unissued
    draft's structure. Once issued, the content lives in ``plan_snapshots`` and
    the version is frozen, which is why migration 0020's
    ``plan_slots__delete_draft_only`` policy refuses this at the database for any
    non-draft — independently of :func:`_claim_draft` above.

    ⚠️ Items are removed first. There is no ``ON DELETE CASCADE`` on
    ``plan_items.plan_slot_id``, deliberately: a cascade would make an
    accidentally broad delete silent.
    """
    _, version_id = await _version_of_slot(session, tenant_id=tenant_id, slot_id=slot_id)
    await _claim_draft(
        session,
        tenant_id=tenant_id,
        version_id=version_id,
        expected_row_version=expected_row_version,
    )

    await session.execute(
        delete(PlanItem).where(PlanItem.tenant_id == tenant_id, PlanItem.plan_slot_id == slot_id)
    )
    await session.execute(
        delete(PlanSlot).where(PlanSlot.tenant_id == tenant_id, PlanSlot.id == slot_id)
    )
    await session.flush()


# ─── Items ───────────────────────────────────────────────────────────────


async def add_item(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    slot_id: uuid.UUID,
    expected_row_version: int,
    item_type: str,
    quantity: Decimal,
    measure_unit_id: uuid.UUID,
    food_id: uuid.UUID | None = None,
    recipe_id: uuid.UUID | None = None,
    meal_id: uuid.UUID | None = None,
    notes: str | None = None,
    client_note: str | None = None,
) -> PlanItem:
    """Put a food, recipe or meal into a slot — FR-M4-024.

    Raises:
        ValidationError: If the reference does not match ``item_type``, or more
            than one is supplied. ``ck_plan_items__one_reference`` would refuse it
            at the table too; refusing here produces a message a practitioner can
            act on instead of an integrity error.
    """
    if item_type not in _ITEM_TYPES:
        raise ValidationError(
            f"“{item_type}” is not something a plan can contain.",
            action="Add a food, a recipe or a saved meal.",
            details={"supported": sorted(_ITEM_TYPES)},
        )
    references = {
        ITEM_TYPE_FOOD: food_id,
        ITEM_TYPE_RECIPE: recipe_id,
        ITEM_TYPE_MEAL: meal_id,
    }
    if references[item_type] is None or any(
        value is not None for key, value in references.items() if key != item_type
    ):
        raise ValidationError(
            "An item points at exactly one food, recipe or meal.",
            action="Choose one item and try again.",
        )
    if quantity <= 0:
        raise ValidationError(
            "A quantity has to be more than zero.",
            action="Enter how much of this the client should have.",
        )

    _, version_id = await _version_of_slot(session, tenant_id=tenant_id, slot_id=slot_id)
    await _claim_draft(
        session,
        tenant_id=tenant_id,
        version_id=version_id,
        expected_row_version=expected_row_version,
    )

    item = PlanItem(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        plan_slot_id=slot_id,
        item_type=item_type,
        food_id=food_id,
        recipe_id=recipe_id,
        meal_id=meal_id,
        quantity=quantity,
        measure_unit_id=measure_unit_id,
        notes=notes,
        client_note=client_note,
        sort_order=await _next_sort_order(session, PlanItem, PlanItem.plan_slot_id, slot_id),
    )
    session.add(item)
    await session.flush()
    return item


async def update_item(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    item_id: uuid.UUID,
    expected_row_version: int,
    quantity: Decimal | object = _UNSET,
    measure_unit_id: uuid.UUID | object = _UNSET,
    notes: str | None | object = _UNSET,
    client_note: str | None | object = _UNSET,
    sort_order: int | object = _UNSET,
) -> PlanItem:
    """Change how much, in what measure, or where in the slot.

    ⚠️ **The food itself cannot be changed.** Swapping ``food_id`` under a fixed
    id would be a substitution disguised as an edit — the practitioner removes
    the item and adds the one they meant, which is visible in the audit log as
    two decisions rather than one silent one.
    """
    item, version_id = await _version_of_item(session, tenant_id=tenant_id, item_id=item_id)
    await _claim_draft(
        session,
        tenant_id=tenant_id,
        version_id=version_id,
        expected_row_version=expected_row_version,
    )

    if quantity is not _UNSET:
        if not isinstance(quantity, Decimal) or quantity <= 0:
            raise ValidationError(
                "A quantity has to be more than zero.",
                action="Enter how much of this the client should have.",
            )
        item.quantity = quantity
    if isinstance(measure_unit_id, uuid.UUID):
        item.measure_unit_id = measure_unit_id
    if notes is not _UNSET:
        item.notes = notes  # type: ignore[assignment]
    if client_note is not _UNSET:
        item.client_note = client_note  # type: ignore[assignment]
    if isinstance(sort_order, int):
        item.sort_order = sort_order

    await session.flush()
    return item


async def remove_item(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    item_id: uuid.UUID,
    expected_row_version: int,
) -> None:
    """Take an item out of a slot — API §8.1.

    🔒 Refused at the database for any non-draft version by
    ``plan_items__delete_draft_only`` (migration 0020), independently of the
    :func:`_claim_draft` check above.
    """
    _, version_id = await _version_of_item(session, tenant_id=tenant_id, item_id=item_id)
    await _claim_draft(
        session,
        tenant_id=tenant_id,
        version_id=version_id,
        expected_row_version=expected_row_version,
    )

    await session.execute(
        delete(PlanItem).where(PlanItem.tenant_id == tenant_id, PlanItem.id == item_id)
    )
    await session.flush()


# ─── Issue (Slice 1.3; the snapshot it writes is still a stub) ───────────


async def announce_issue(
    session: AsyncSession,
    tenant_id: UUID,
    plan_version_id: UUID,
    client_id: UUID,
    issued_at: datetime,
) -> None:
    """Publish that a version was issued — PDF rendering and delivery follow.

    🔒 One event, two subscribers, and neither is named here (Arch §3.4a):
    `nutrition` enqueues the PDF render, and from S5 `messaging` schedules the
    delivery message (FR-M8-013). Adding the second consumer required no change
    to this call, which is the property AC-M8-008 asserts.

    ⚠️ Renamed from ``enqueue_pdf_generation``. The old name described one
    subscriber's reaction rather than the fact being announced, and a publisher
    that names its subscribers is the coupling the event bus exists to remove.
    """
    await publish(
        PlanVersionIssued(
            tenant_id=tenant_id,
            plan_version_id=plan_version_id,
            client_id=client_id,
            issued_at=issued_at,
        ),
        session,
    )


# -- State Machine Transitions --


async def issue_plan_version(
    session: AsyncSession,
    tenant_id: UUID,
    plan_id: UUID,
    version_id: UUID,
    issued_by_user_id: UUID,
) -> None:
    """Issue a draft plan version."""
    stmt = select(DietPlanVersion).where(
        DietPlanVersion.id == version_id,
        DietPlanVersion.tenant_id == tenant_id,
        DietPlanVersion.plan_id == plan_id,
    )
    result = await session.execute(stmt)
    version = result.scalar_one_or_none()

    if not version:
        raise NotFoundError(
            message="The requested diet plan version could not be found.",
            action="Check the version ID and try again.",
        )

    if version.state != PlanState.draft:
        raise ConflictError(
            message="This plan version has already been issued or discarded.",
            action="Reload the plan to see the latest version.",
        )

    # In a full implementation, we would query the resolved portions here
    # and compute real totals using `calculate_composition`.
    computed_totals = {"energy_kcal": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0, "fibre_g": 0}

    now = datetime.now(UTC)

    # Generate snapshot (denormalized JSON representing the plan in full)
    snapshot_doc = {"title": "Diet Plan", "version_number": version.version_number, "days": []}

    # 🔒 DDR-12 — hashed here, from the document that is about to be persisted,
    # so the stored hash can never describe a different document than the one
    # stored beside it. The portal's cache validity rests on that being true.
    snapshot = PlanSnapshot(
        tenant_id=tenant_id,
        plan_version_id=version_id,
        document=snapshot_doc,
        document_schema_version=1,
        content_hash=snapshot_content_hash(snapshot_doc),
        pdf_status=RenderStatus.pending,
    )
    session.add(snapshot)

    # Update version
    version.state = PlanState.issued
    version.issued_at = now
    version.issued_by_user_id = issued_by_user_id
    version.computed_totals = computed_totals

    # Supersede previous issued version
    stmt_supersede = (
        update(DietPlanVersion)
        .where(
            DietPlanVersion.plan_id == plan_id,
            DietPlanVersion.tenant_id == tenant_id,
            DietPlanVersion.state == PlanState.issued,
            DietPlanVersion.id != version_id,
        )
        .values(state=PlanState.superseded)
    )
    await session.execute(stmt_supersede)

    # Update current_version_id on DietPlan
    stmt_plan = (
        update(DietPlan)
        .where(DietPlan.id == plan_id, DietPlan.tenant_id == tenant_id)
        .values(current_version_id=version_id)
    )
    await session.execute(stmt_plan)

    # 🔒 Announce the issue. The PDF render and the client's delivery message
    # both hang off this one event; neither is called by name from here.
    #
    # ⚠️ The client is read from the plan rather than passed in: the event must
    # carry it (a `messaging` subscriber cannot read `diet_plans` — R6), and
    # taking it as a parameter would let a caller announce a plan against the
    # wrong client.
    plan_client_id = await session.scalar(
        select(DietPlan.client_id).where(DietPlan.id == plan_id, DietPlan.tenant_id == tenant_id)
    )
    if plan_client_id is None:  # pragma: no cover — the update above proved it exists
        raise NotFoundError(
            message="The plan this version belongs to could not be found.",
            action="Reload the plan and try again.",
        )
    await announce_issue(session, tenant_id, version_id, plan_client_id, now)
