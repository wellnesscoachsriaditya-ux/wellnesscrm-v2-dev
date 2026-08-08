"""Entitlement rules — FR-M0-044/045/046, FR-M10-001..005, DDR-14.

Pure rules, no database. That is the point of the kernel/platform split: every
assertion below is about *what a plan permits*, and none of it needs a row to
exist to be checked.

🔒 The fail-safe is the reason this file is long. FR-M0-046 has three states
(enforced / unlimited / unknown) and collapsing any two produces a defect that a
happy-path test cannot see: an indeterminate allowance and an unlimited one both
have ``limit is None``, so a test that only checks ``limit`` would pass while the
system handed out free quota on every configuration error.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.kernel.entitlements import (
    WARNING_THRESHOLD,
    Allowance,
    ResourceCode,
    check,
    crosses_warning_threshold,
    is_over_limit,
    next_plan_after,
    status_permits_metered_action,
    would_exceed,
)
from app.kernel.errors import EntitlementError


def _allowance(
    *,
    used: int | Decimal = 0,
    limit: int | Decimal | None = 10,
    plan_code: str = "starter",
    is_determinate: bool = True,
    already_warned: bool = False,
    resource: ResourceCode = ResourceCode.AI_GENERATIONS,
) -> Allowance:
    """An allowance with the boring parts filled in.

    Defaults to a *determinate, enforceable* allowance so each test states only
    the property it is about.
    """
    return Allowance(
        resource=resource,
        used=Decimal(used),
        limit=None if limit is None else Decimal(limit),
        plan_code=plan_code,
        is_determinate=is_determinate,
        already_warned=already_warned,
    )


# ─── The three states of a limit (FR-M0-046) ─────────────────────────────


def test_determinate_none_limit_is_unlimited() -> None:
    """A plan that says ``null`` means unlimited, and says so knowingly."""
    allowance = _allowance(limit=None, is_determinate=True)
    assert allowance.is_unlimited
    assert not would_exceed(allowance, amount=Decimal(10**9))


def test_indeterminate_none_limit_is_not_unlimited() -> None:
    """🔒 The fail-open FR-M0-046 exists to prevent.

    Both states carry ``limit is None``. Only one of them means "no ceiling"; the
    other means "we could not tell", and treating the second as the first hands
    out unlimited quota on every configuration error.
    """
    allowance = _allowance(limit=None, is_determinate=False)
    assert not allowance.is_unlimited
    assert would_exceed(allowance, amount=Decimal(1))


def test_indeterminate_state_blocks_new_actions() -> None:
    """FR-M0-046 — unknown entitlement state denies new metered actions."""
    with pytest.raises(EntitlementError):
        check(_allowance(used=0, limit=1000, is_determinate=False), amount=Decimal(1))


def test_indeterminate_error_is_distinguishable_from_a_limit_hit() -> None:
    """The operator has to be able to tell this from an ordinary limit hit.

    An indeterminate denial is a *configuration* failure on our side, not the
    tenant exceeding a plan. Both are ``EntitlementError`` (both are 402), so the
    distinguishing signal is in ``details``. If they were indistinguishable, the
    support conversation would start by telling the practitioner to upgrade —
    which would not fix anything, because the limit was never the problem.
    """
    with pytest.raises(EntitlementError) as raised:
        check(_allowance(is_determinate=False), amount=Decimal(1))

    assert raised.value.details["reason"] == "indeterminate"
    # 🔒 No upgrade path offered: there is nothing for them to buy.
    assert "upgrade_to" not in raised.value.details


def test_zero_limit_is_enforced_not_treated_as_absent() -> None:
    """A plan that includes none of a resource is a real limit, not a missing one.

    ⚠️ ``0`` is falsy in Python, so a truthiness check anywhere on this path turns
    "your plan does not include this" into "unlimited".
    """
    allowance = _allowance(used=0, limit=0)
    assert not allowance.is_unlimited
    assert would_exceed(allowance, amount=Decimal(1))
    with pytest.raises(EntitlementError):
        check(allowance, amount=Decimal(1))


def test_zero_limit_has_no_usage_ratio() -> None:
    """No percentage exists for a resource the plan does not include."""
    assert _allowance(used=0, limit=0).usage_ratio is None


# ─── The boundary (FR-M0-045) ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("used", "amount", "expected"),
    [
        (9, 1, False),  # exactly reaching the limit is permitted
        (10, 1, True),  # the action after that is not
        (0, 10, False),  # a single action consuming the whole quota
        (0, 11, True),  # ...or one over it
        (5, 3, False),
    ],
)
def test_would_exceed_boundary(used: int, amount: int, expected: bool) -> None:
    """🔒 The limit is inclusive: a plan of 10 permits the 10th, denies the 11th.

    Off-by-one here is a billing dispute, not a rounding error — the practitioner
    counts what they got and compares it to what they bought.
    """
    assert would_exceed(_allowance(used=used, limit=10), amount=Decimal(amount)) is expected


def test_check_passes_at_exactly_the_limit() -> None:
    """Reaching the limit is allowed; the next action is what fails."""
    check(_allowance(used=9, limit=10), amount=Decimal(1))
    with pytest.raises(EntitlementError):
        check(_allowance(used=10, limit=10), amount=Decimal(1))


def test_over_limit_is_distinct_from_would_exceed() -> None:
    """🔒 EC-M10-01 — a downgrade puts a tenant over a limit passively.

    They did nothing; the plan changed underneath them. They keep their data and
    lose only the ability to add more, so the two questions must stay separate.
    """
    downgraded = _allowance(used=25, limit=10)
    assert is_over_limit(downgraded)
    assert would_exceed(downgraded, amount=Decimal(1))


def test_remaining_never_goes_negative() -> None:
    """ "-15 remaining" reads as a bug in the UI rather than as the over-limit state."""
    assert _allowance(used=25, limit=10).remaining == Decimal(0)


def test_remaining_is_none_when_unlimited() -> None:
    assert _allowance(limit=None, is_determinate=True).remaining is None


# ─── The error message (FR-M0-045) ───────────────────────────────────────


def test_limit_error_names_limit_usage_and_upgrade_path() -> None:
    """🔒 FR-M0-045 — the error carries what the UI needs, in one response.

    Without the upgrade target in the payload the client has to make a second
    call to render the one button the message is about.
    """
    with pytest.raises(EntitlementError) as raised:
        check(_allowance(used=10, limit=10, plan_code="starter"), amount=Decimal(1))

    error = raised.value
    assert error.status_code == 402
    assert error.details["limit"] == 10
    assert error.details["used"] == 10
    assert error.details["plan_code"] == "starter"
    assert error.details["upgrade_to"] == "growth"


def test_limit_error_offers_a_free_remedy_where_one_exists() -> None:
    """Deactivating a finished client costs nothing; offer it before the upsell.

    ⚠️ The remedy lands in ``action``, not ``details`` — ``for_limit`` composes it
    into the sentence the user reads (NFR-063), because "what can I do next" is
    prose rather than a field the UI branches on.
    """
    with pytest.raises(EntitlementError) as raised:
        check(
            _allowance(used=10, limit=10, resource=ResourceCode.ACTIVE_CLIENTS),
            amount=Decimal(1),
        )

    action = raised.value.action
    assert "inactive" in action
    # Offered before the upsell, because it is free.
    assert action.index("inactive") < action.index("upgrade")


def test_no_remedy_invented_where_none_exists() -> None:
    """An AI draft already generated cannot be freed up, so only the upgrade shows."""
    with pytest.raises(EntitlementError) as raised:
        check(
            _allowance(used=10, limit=10, resource=ResourceCode.AI_GENERATIONS),
            amount=Decimal(1),
        )

    assert raised.value.action == "You can upgrade to Growth."


def test_top_of_ladder_offers_no_upgrade() -> None:
    """🔒 The top tier must not be told to upgrade to a plan that does not exist."""
    assert next_plan_after("clinic") is None
    with pytest.raises(EntitlementError) as raised:
        check(_allowance(used=10, limit=10, plan_code="clinic"), amount=Decimal(1))
    assert raised.value.details["upgrade_to"] is None


def test_unknown_plan_offers_no_upgrade() -> None:
    """A custom or retired plan has no defined successor, so none is guessed."""
    assert next_plan_after("enterprise-bespoke-2029") is None


def test_upgrade_ladder_is_ordered() -> None:
    assert next_plan_after("free") == "starter"
    assert next_plan_after("starter") == "growth"
    assert next_plan_after("growth") == "clinic"


# ─── Subscription status (DB §14.2) ──────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "permitted"),
    [
        ("trialing", True),
        ("active", True),
        ("past_due", True),
        ("suspended", False),
        ("cancelled", False),
        (None, False),
    ],
)
def test_status_permits_metered_action(status: str | None, permitted: bool) -> None:
    """🔒 ``past_due`` still works; ``None`` does not.

    A failed payment is a billing problem — cutting a practitioner off from their
    clients the day a card expires damages the practice before anyone has read the
    dunning email. Suspension is the deliberate lever (FR-M10-008).

    ``None`` — no subscription row — is indeterminate, not free: a provisioning
    failure must not read as unlimited access.
    """
    assert status_permits_metered_action(status) is permitted


# ─── The 80% warning (FR-M10-005) ────────────────────────────────────────


def test_warning_fires_on_the_crossing_increment() -> None:
    """The increment that takes 79% to 80% is the one that warns."""
    assert crosses_warning_threshold(_allowance(used=7, limit=10), amount=Decimal(1))


def test_warning_does_not_fire_below_the_threshold() -> None:
    assert not crosses_warning_threshold(_allowance(used=5, limit=10), amount=Decimal(1))


def test_warning_fires_once_per_period() -> None:
    """🔒 FR-M10-005 — warned once, not on every action for the rest of the month."""
    assert not crosses_warning_threshold(
        _allowance(used=9, limit=10, already_warned=True), amount=Decimal(1)
    )


def test_warning_does_not_fire_when_already_above_before_the_action() -> None:
    """Crossing is a transition. Already-above is not a crossing.

    Without this, every action after 80% would re-warn on a counter whose stamp
    had been lost, which is the behaviour ``already_warned`` exists to prevent —
    but the transition check has to hold on its own too.
    """
    assert not crosses_warning_threshold(_allowance(used=9, limit=10), amount=Decimal(1))


def test_no_warning_when_unlimited_or_indeterminate() -> None:
    """Neither has a percentage to cross."""
    assert not crosses_warning_threshold(
        _allowance(limit=None, is_determinate=True), amount=Decimal(1)
    )
    assert not crosses_warning_threshold(
        _allowance(limit=None, is_determinate=False), amount=Decimal(1)
    )


def test_warning_threshold_is_eighty_percent() -> None:
    """Pinned: the copy and the crossing test must agree on the number."""
    assert Decimal("0.8") == WARNING_THRESHOLD


# ─── Resource metadata (FR-M0-044) ───────────────────────────────────────


def test_every_resource_has_a_limit_key_and_a_human_name() -> None:
    """🔒 A resource with no limit key can never be enforced.

    It would read as a missing key in ``limits``, become indeterminate, and deny
    every action — discovered in production rather than here.
    """
    for resource in ResourceCode:
        assert resource.limit_key
        assert resource.human_name


def test_monthly_resources_carry_the_period_in_their_limit_key() -> None:
    """⚠️ DB §14.1 fixes these key names; the resource code alone is not the key."""
    assert ResourceCode.AI_GENERATIONS.limit_key == "ai_generations_per_month"
    assert ResourceCode.WHATSAPP_MESSAGES.limit_key == "whatsapp_messages_per_month"


def test_only_cumulative_resources_are_counted_live() -> None:
    """🔒 The two totals that do not reset, and nothing else.

    ⚠️ The distinction is *cumulative vs per-period*, not "expensive vs cheap".
    A monthly counter zeroes on the 1st, which is correct for AI drafts and
    WhatsApp messages — both are per-month allowances — and wrong for a total a
    tenant is currently holding. Metering storage through a monthly counter would
    let a tenant on a 500 MB plan hold unlimited data by uploading 400 MB a month
    forever, and would have no way to give bytes back when a file is deleted.

    Adding a resource here without that property means it stops being enforced
    correctly across a month boundary, so the set is asserted exactly.
    """
    live = {resource for resource in ResourceCode if resource.is_counted_live}
    assert live == {ResourceCode.ACTIVE_CLIENTS, ResourceCode.STORAGE_MB}


def test_per_period_resources_are_not_counted_live() -> None:
    """The other side of the same rule — these do reset monthly, by design."""
    for resource in (ResourceCode.AI_GENERATIONS, ResourceCode.WHATSAPP_MESSAGES):
        assert not resource.is_counted_live
        assert resource.limit_key.endswith("_per_month")


def test_limit_keys_are_unique() -> None:
    """Two resources sharing a key would silently meter against one number."""
    keys = [resource.limit_key for resource in ResourceCode]
    assert len(keys) == len(set(keys))
