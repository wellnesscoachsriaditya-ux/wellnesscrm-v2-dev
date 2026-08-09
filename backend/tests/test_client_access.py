"""Every client-bound route must authorize against the client — AC-M1-006.

🔒 The gap this closes is structural, not logical. ``kernel.authz`` decides
correctly and ``pipeline.authorize()`` applies the decision — but only if a route
*calls* it. A route that loads a client with ``get_client`` and returns it is
reachable by any practitioner in the tenant, and every unit test of the policy
still passes.

⚠️ **This is a heuristic, in the same sense as the R8 boundary check.** It reads
source rather than behaviour, so it catches the shape of the mistake rather than
proving its absence. That is worth having: the mistake has a very consistent
shape — a ``{client_id}`` route that never mentions ``authorized_client`` — and
nothing else in the suite would notice it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROUTERS = Path(__file__).resolve().parents[1] / "app" / "platform" / "http" / "routers"

#: Functions that legitimately take no ``{client_id}`` and so have no client to
#: scope by, plus the helper itself. Listed rather than pattern-matched, so
#: adding one is a decision somebody made rather than a name that happened to
#: match. ``authorized_client`` is here because it *is* the authorization — a
#: check that required it to call itself would be circular.
_NOT_CLIENT_BOUND = frozenset(
    {"authorized_client", "tags_list", "tags_create", "tags_archive", "create"}
)


def _router_sources() -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(_ROUTERS.glob("*.py"))
        if path.name != "__init__.py"
    }


def _endpoint_bodies(source: str) -> dict[str, str]:
    """Split a router module into ``{function name: body}``.

    Crude on purpose — a full AST walk would be more precise and would obscure
    what this is really doing, which is looking for one call in one function.
    """
    bodies: dict[str, str] = {}
    parts = re.split(r"\nasync def (\w+)\(", source)
    for index in range(1, len(parts), 2):
        bodies[parts[index]] = parts[index + 1]
    return bodies


@pytest.mark.parametrize("filename", sorted(_router_sources()))
def test_every_client_bound_route_authorizes(filename: str) -> None:
    """🔒 A ``{client_id}`` route that skips ``authorized_client`` is unscoped.

    The failure mode is silent: the route works, returns the right data to the
    right practitioner in every manual test, and hands a colleague's client to
    anyone who knows a UUID.
    """
    source = _router_sources()[filename]
    if "{client_id}" not in source:
        pytest.skip(f"{filename} declares no client-bound routes")

    bodies = _endpoint_bodies(source)
    offenders: list[str] = []

    for name, body in bodies.items():
        if name in _NOT_CLIENT_BOUND:
            continue
        # Only endpoints that actually take a client id are in scope.
        if "client_id: uuid.UUID" not in body:
            continue
        if "authorized_client(" in body:
            continue
        offenders.append(name)

    assert not offenders, (
        f"{filename}: these routes take a client_id but never call "
        f"`authorized_client`, so any practitioner in the tenant can reach any "
        f"client through them (AC-M1-006): {', '.join(sorted(offenders))}"
    )


def test_the_helper_still_authorizes() -> None:
    """⚠️ The test above is only as good as the helper it looks for.

    If ``authorized_client`` stopped calling ``authorize``, every route would
    still "call the helper" and none of them would be scoped — the check would
    pass while enforcing nothing.
    """
    source = (_ROUTERS / "clients.py").read_text(encoding="utf-8")
    body = _endpoint_bodies(source)["authorized_client"]
    assert "load_for_access(" in body, "the helper no longer loads the grants"
    assert "await authorize(" in body, "the helper no longer applies the decision"


#: 🔒 Client actions that legitimately carry no ``owner_or_assigned`` policy,
#: each with the reason it cannot.
#:
#: ⚠️ **Adding a name here removes a security guarantee**, so each entry states
#: what protects the action *instead*. An action whose replacement protection is
#: "the role gate" alone does not belong in this list — that is precisely the
#: hole FR-M0-017 is about.
_POLICY_EXEMPT: dict[str, str] = {
    # Ownership is established *by* this write (`owner_user_id` defaults to the
    # caller), not checked before it. There is no resource to scope by yet.
    "client.create": "no resource exists until the write completes",
    # A list has no single resource either, and the policy takes one. Scoping a
    # *collection* is a different operation — a WHERE clause — and it lives in
    # `discovery._visible_to`. Asserted separately below, because "the policy is
    # absent" and "the query is scoped" are different claims.
    "client.list": "scoped inside the query by discovery._visible_to",
    # Names many clients, and the policy takes one. Owner-only at the role gate,
    # and every id is loaded and checked inside `bulk_reassign_owner` before
    # anything is written.
    "client.bulk_reassign": "owner-only, and every id is verified in the module",
}


def test_the_scoping_policy_is_declared_on_client_actions() -> None:
    """🔒 The other half: a policy nobody attached decides nothing.

    ``owner_or_assigned`` on the action is what makes ``authorize`` do anything —
    without it the call runs, finds no policies and allows.
    """
    actions = (
        Path(__file__).resolve().parents[1] / "app" / "modules" / "clients" / "actions.py"
    ).read_text(encoding="utf-8")

    scoped = re.findall(r"register_action\(\s*\"(client\.[\w_]+)\"(.*?)\n\)", actions, re.DOTALL)
    assert scoped, "no client actions found — has the file moved?"

    unscoped = [
        name for name, block in scoped if name not in _POLICY_EXEMPT and "_SCOPED" not in block
    ]
    assert not unscoped, (
        "these client actions carry no ownership policy, so the coarse role gate "
        f"is all that protects them (FR-M0-017): {', '.join(sorted(unscoped))}"
    )


def test_every_exemption_still_names_a_real_action() -> None:
    """⚠️ An exemption for an action that no longer exists is a hole waiting.

    If ``client.list`` were renamed, its exemption would silently apply to
    nothing — and the *new* name would need one, which is the moment somebody
    adds it without thinking. This fails instead.
    """
    actions = (
        Path(__file__).resolve().parents[1] / "app" / "modules" / "clients" / "actions.py"
    ).read_text(encoding="utf-8")
    declared = set(re.findall(r'register_action\(\s*"(client\.[\w_]+)"', actions))

    stale = sorted(set(_POLICY_EXEMPT) - declared)
    assert not stale, f"these actions are exempt from scoping but no longer exist: {stale}"


def test_the_list_query_scopes_by_the_actor() -> None:
    """🔒 The replacement protection for ``client.list``, asserted rather than trusted.

    The exemption above is only defensible because the query does the scoping.
    This checks the mechanism is still there: a `_visible_to` that stopped
    filtering would hand a practitioner the whole tenant, and the action's
    missing policy would then be a real hole rather than a deliberate one.

    ⚠️ Structural, not behavioural — the row-level proof is in
    ``tests/integration/test_discovery.py``, which needs a database. This is the
    cheap guard that fails on a refactor.
    """
    source = (
        Path(__file__).resolve().parents[1] / "app" / "modules" / "clients" / "discovery.py"
    ).read_text(encoding="utf-8")

    assert "def _visible_to(" in source, "the list's scoping predicate has gone"
    assert "UserRole.OWNER" in source, "the owner's tenant-wide exemption has gone"
    assert "ClientAssignment.revoked_at.is_(None)" in source, (
        "the grant predicate no longer excludes revoked assignments, so a "
        "withdrawn colleague would keep seeing the client in their list"
    )
    assert "_visible_to(statement" in source, "list_clients no longer applies the predicate"
