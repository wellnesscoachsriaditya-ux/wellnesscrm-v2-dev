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


# ─── Enquiries (S2 Slice F) ──────────────────────────────────────────────
#
# 🔒 An enquiry carries a name, a mobile and a stated health goal — the same
# facts as the client record it created. So AC-M1-006 has to hold on this surface
# too, and the checks above cannot see it: they read `client.*` action names and
# `{client_id}` route paths, and the enquiry routes have neither.
#
# ⚠️ This is the leak that would otherwise be invisible: a practitioner refused a
# client's record reading the same details from the enquiry that produced them.
# Same data, different door.

_LEADS = Path(__file__).resolve().parents[1] / "app" / "modules" / "leads"

#: 🔒 Enquiry actions that legitimately carry no `owner_or_assigned`, each with
#: the reason it cannot and what protects it instead. Same contract as
#: `_POLICY_EXEMPT`: adding a name here removes a guarantee.
_ENQUIRY_POLICY_EXEMPT: dict[str, str] = {
    # A list has no single resource, and the policy takes one. Scoped inside the
    # query by `leads.discovery._visible_to`, asserted below.
    "enquiry.list": "scoped inside the query by leads.discovery._visible_to",
    # The resource is a *submission*, which has no `owner_user_id` for the policy
    # to inspect. The router resolves the enquiry's client and authorizes that
    # instead — asserted below, because the redirection is the whole protection.
    "enquiry.respond": "the router authorizes the submission's client explicitly",
    # A form is a title and some intro prose — no fact about any person, which is
    # why the public endpoint may serve it to a stranger (API §11.1).
    "enquiry_form.read": "TENANT_METADATA — the form contains no client data",
    "enquiry_form.update": "TENANT_METADATA — the form contains no client data",
}


def test_every_enquiry_action_is_scoped_or_explicitly_exempt() -> None:
    """🔒 AC-M1-006 on the enquiry surface — the same rule, a different module.

    ⚠️ Every enquiry action is currently exempt, and that reads alarmingly until
    the reasons are checked one by one. The test still earns its place: it fails
    the moment a *new* action is added without a decision being recorded here.
    """
    actions = (_LEADS / "actions.py").read_text(encoding="utf-8")

    declared = re.findall(
        r'register_action\(\s*"((?:enquiry|enquiry_form)\.[\w_]+)"(.*?)\n\)', actions, re.DOTALL
    )
    assert declared, "no enquiry actions found — has the file moved?"

    unscoped = [
        name
        for name, block in declared
        if name not in _ENQUIRY_POLICY_EXEMPT and "_SCOPED" not in block
    ]
    assert not unscoped, (
        "these enquiry actions carry no ownership policy and no recorded "
        f"exemption (AC-M1-006): {', '.join(sorted(unscoped))}"
    )

    stale = sorted(set(_ENQUIRY_POLICY_EXEMPT) - {name for name, _ in declared})
    assert not stale, f"these enquiry exemptions no longer name a real action: {stale}"


def test_the_enquiry_list_scopes_through_the_client() -> None:
    """🔒 The replacement protection for ``enquiry.list``, asserted not trusted.

    An enquiry is scoped by who may see the *client* it is about, and the rule
    comes from the `clients` module through the kernel port — not reimplemented
    here, which is what keeps the two from drifting.
    """
    source = (_LEADS / "discovery.py").read_text(encoding="utf-8")

    assert "def _visible_to(" in source, "the enquiry list's scoping predicate has gone"
    assert "visible_client_ids(" in source, (
        "the enquiry list no longer scopes through `ClientDirectory."
        "visible_client_ids` — if it now derives visibility itself, that is a "
        "second definition of AC-M1-006 which will drift from the first"
    )
    assert "_visible_to(statement" in source, "list_enquiries no longer applies the predicate"
    assert "UserRole.OWNER" in source, "the owner's tenant-wide exemption has gone"


def test_the_respond_route_authorizes_the_enquiry_s_client() -> None:
    """🔒 The replacement protection for ``enquiry.respond``.

    ``owner_or_assigned`` cannot run on a submission — it reads `owner_user_id`
    and a submission has none. The router therefore resolves the client behind
    the enquiry and authorizes *that*. If it stopped, any practitioner in the
    tenant could clear a colleague's enquiry and the action's missing policy
    would become a real hole rather than a deliberate one.
    """
    source = (_ROUTERS / "enquiries.py").read_text(encoding="utf-8")
    body = _endpoint_bodies(source)["enquiries_mark_responded"]

    assert "load_submission_client(" in body, "the route no longer resolves the enquiry's client"
    assert "load_for_access(" in body, "the route no longer loads the client's grants"
    assert "await authorize(" in body, "the route no longer applies the authorization decision"


def test_the_public_enquiry_endpoints_never_reveal_a_match() -> None:
    """🔒 EC-M2-02 / API §11.2 — the client-enumeration oracle, guarded structurally.

    ``acknowledgement()`` takes no arguments, so it cannot branch on the match
    (pinned in `test_kernel_leads.py`). This checks the *router* does not
    reintroduce the branch by reading `is_duplicate` off the result and shaping a
    response from it.

    ⚠️ Source inspection, like the rest of this file. The behavioural proof is in
    `tests/integration/test_lead_capture.py`, which submits a matching and a
    non-matching enquiry and compares the two responses byte for byte.
    """
    source = (_ROUTERS / "public_forms.py").read_text(encoding="utf-8")
    body = _endpoint_bodies(source)["submit_enquiry"]

    assert "is_duplicate" not in body, (
        "the public submit endpoint reads `is_duplicate`. Whatever it does with "
        "it, the value must not reach the response — that is the "
        "client-enumeration oracle API §11.2 calls the most serious privacy leak "
        "available on the public surface (EC-M2-02)."
    )
    assert "acknowledgement()" in body, (
        "the public response is no longer built by `acknowledgement()`, which is "
        "the function whose signature makes the reply unable to vary on the match"
    )
