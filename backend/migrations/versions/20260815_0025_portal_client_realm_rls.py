"""Pattern C — the client realm's row-level boundary.

Revision ID: 0025_portal_client_realm_rls
Revises: 0024_measurement_idempotency
Created: 2026-08-15

🔒 **S6 Slice 2.** ``/portal/*`` is the first surface where the caller is a
*client*, not a practitioner. Pattern A answers "which tenant?"; it does not
answer "which client?", and inside one practice every client's plan, weight and
assessment sits in the same tenant. Without a second predicate, the only thing
standing between two clients of the same dietitian is a ``WHERE client_id``
clause in application code — which is precisely the class of guarantee DB §17.1
refuses to leave to application code.

DB §17.1 Pattern C:

    USING (tenant_id = current_setting('app.tenant_id')::uuid
           AND client_id = current_setting('app.actor_id')::uuid)

🔒 **Expressed as RESTRICTIVE policies alongside the existing Pattern A ones,
not as replacements.** Restrictive policies are ANDed with the permissive ones,
so a client-realm session must satisfy *both* the tenant predicate and the
client predicate. A permissive policy would be ORed and would therefore *widen*
access, which is the opposite of what is wanted.

🔒 **The client predicate is keyed on the realm, through
:func:`portal_client_id`.** ``app.actor_id`` holds the practitioner's user id on
a ``/app`` request and the client's own id on a ``/portal`` one — the two are
different kinds of identifier in the same variable, so a policy comparing
``client_id = app.actor_id`` unconditionally would hide every row from every
practitioner. The helper returns the client id only when ``app.actor_role`` is
``client``, and NULL otherwise; a NULL short-circuits the policy to true and
practitioner, operator and worker sessions are untouched.

⚠️ **A client-realm session always carries a role.** ``kernel.authz.can()``
denies an actor with no role before the pipeline opens a transaction
(``no_role:<action>``), so a session that could reach these tables with
``app.actor_role`` unset does not exist. That is what makes the "NULL means not
a client" reading safe rather than an open door.

🔒 **``TO app_user``, matching every existing policy in the schema.** The
migrator owns these tables and seeds test fixtures across clients; a policy
without a role list would apply to it too and break seeding in a way that looks
like a fixture bug rather than a policy decision.

⚠️ **The plan tree is reached by EXISTS, not by a ``client_id`` column.**
``diet_plan_versions``, ``plan_days``, ``plan_slots``, ``plan_items`` and
``plan_snapshots`` carry no client, so each walks up to ``diet_plans``. The
subqueries only ever run for a client-realm session: for everyone else
``portal_client_id()`` is NULL and the OR short-circuits before the EXISTS is
evaluated.

🔒 **``adherence_logs`` is repaired here.** Revision ``2eb56b8913d5`` created it
with a single RESTRICTIVE policy reading ``app.current_tenant_id`` — a session
variable this application never sets (it sets ``app.tenant_id``; see
``platform.db.TENANT_SETTING``). Two defects in one statement: the predicate
compared against NULL and matched nothing, and with no PERMISSIVE policy on the
table RLS denied everything anyway. The table was therefore unreadable and
unwritable by ``app_user``. It is given the Pattern A policy every other
tenant-scoped table has, plus FORCE, plus its Pattern C policy.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: F401
from alembic import op

revision: str = "0025_portal_client_realm_rls"
down_revision: str | None = "0024_measurement_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: 🔒 The realm gate, in one place. Returns the acting client's id on a
#: client-realm request and NULL on every other kind, so each policy below reads
#: as "not a client, or this client's row" rather than repeating the role test.
#:
#: ``STABLE`` rather than ``IMMUTABLE``: it reads a session variable, which is
#: constant within a statement but not across them. Marking it IMMUTABLE would
#: let the planner fold it into a cached plan.
_HELPER = """
CREATE OR REPLACE FUNCTION portal_client_id() RETURNS uuid
LANGUAGE sql STABLE
AS $$
    SELECT CASE
        WHEN NULLIF(current_setting('app.actor_role', true), '') = 'client'
        THEN NULLIF(current_setting('app.actor_id', true), '')::uuid
    END
$$;
"""

#: Tables that carry the client on the row itself. ``clients`` is keyed by ``id``
#: rather than ``client_id`` — a client's "own row" is the client.
_DIRECT: dict[str, str] = {
    "clients": "id",
    "diet_plans": "client_id",
    "measurements": "client_id",
    "assessment_responses": "client_id",
    "adherence_logs": "client_id",
}

#: Tables reached through the plan tree. Each predicate resolves to the owning
#: ``diet_plans`` row and compares its client.
#:
#: ⚠️ Written as nested EXISTS rather than one flat join so each policy names
#: only its immediate parent. A four-table join repeated in four policies would
#: have to be edited in four places the day the tree changes shape.
_VIA_PLAN: dict[str, str] = {
    "diet_plan_versions": """
        EXISTS (
            SELECT 1 FROM diet_plans p
            WHERE p.id = diet_plan_versions.plan_id
              AND p.client_id = portal_client_id()
        )
    """,
    "plan_snapshots": """
        EXISTS (
            SELECT 1 FROM diet_plan_versions v
            JOIN diet_plans p ON p.id = v.plan_id
            WHERE v.id = plan_snapshots.plan_version_id
              AND p.client_id = portal_client_id()
        )
    """,
    "plan_days": """
        EXISTS (
            SELECT 1 FROM diet_plan_versions v
            JOIN diet_plans p ON p.id = v.plan_id
            WHERE v.id = plan_days.plan_version_id
              AND p.client_id = portal_client_id()
        )
    """,
    "plan_slots": """
        EXISTS (
            SELECT 1 FROM plan_days d
            JOIN diet_plan_versions v ON v.id = d.plan_version_id
            JOIN diet_plans p ON p.id = v.plan_id
            WHERE d.id = plan_slots.plan_day_id
              AND p.client_id = portal_client_id()
        )
    """,
    "plan_items": """
        EXISTS (
            SELECT 1 FROM plan_slots s
            JOIN plan_days d ON d.id = s.plan_day_id
            JOIN diet_plan_versions v ON v.id = d.plan_version_id
            JOIN diet_plans p ON p.id = v.plan_id
            WHERE s.id = plan_items.plan_slot_id
              AND p.client_id = portal_client_id()
        )
    """,
    "plan_item_alternatives": """
        EXISTS (
            SELECT 1 FROM plan_items i
            JOIN plan_slots s ON s.id = i.plan_slot_id
            JOIN plan_days d ON d.id = s.plan_day_id
            JOIN diet_plan_versions v ON v.id = d.plan_version_id
            JOIN diet_plans p ON p.id = v.plan_id
            WHERE i.id = plan_item_alternatives.plan_item_id
              AND p.client_id = portal_client_id()
        )
    """,
}

#: 🔒 Every policy has this shape: pass when the session is not a client, and
#: otherwise only for the client's own rows. ``WITH CHECK`` mirrors ``USING``, so
#: a client cannot write a row attributed to somebody else either — the portal's
#: writes land in S6's later steps and inherit the boundary rather than
#: re-stating it.
_POLICY = """
CREATE POLICY {table}__client_realm ON {table}
    AS RESTRICTIVE FOR ALL
    TO app_user
    USING (portal_client_id() IS NULL OR ({predicate}))
    WITH CHECK (portal_client_id() IS NULL OR ({predicate}))
"""

_ALL_TABLES: tuple[str, ...] = (*_DIRECT, *_VIA_PLAN)


def _predicates() -> dict[str, str]:
    return {
        **{table: f"{column} = portal_client_id()" for table, column in _DIRECT.items()},
        **_VIA_PLAN,
    }


def upgrade() -> None:
    # ─── 1. Repair `adherence_logs` (see the module docstring) ───────────
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON adherence_logs")
    op.execute("ALTER TABLE adherence_logs FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY adherence_logs__tenant_isolation ON adherence_logs
            AS PERMISSIVE FOR ALL
            TO app_user
            USING (tenant_id = current_tenant_id())
            WITH CHECK (tenant_id = current_tenant_id())
        """
    )

    # ─── 2. The realm gate ───────────────────────────────────────────────
    op.execute(_HELPER)
    op.execute("REVOKE ALL ON FUNCTION portal_client_id() FROM public")
    op.execute("GRANT EXECUTE ON FUNCTION portal_client_id() TO app_user")

    # ─── 3. Pattern C, on every table the portal can reach ───────────────
    for table, predicate in _predicates().items():
        op.execute(_POLICY.format(table=table, predicate=predicate.strip()))


def downgrade() -> None:
    for table in _ALL_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}__client_realm ON {table}")

    op.execute("DROP FUNCTION IF EXISTS portal_client_id()")

    # Restore `adherence_logs` to the state revision 2eb56b8913d5 left it in.
    # ⚠️ That state is broken by design of this revision's repair — the policy
    # names a session variable nothing sets — but a downgrade that invented a
    # working policy would not be a downgrade.
    op.execute("DROP POLICY IF EXISTS adherence_logs__tenant_isolation ON adherence_logs")
    op.execute("ALTER TABLE adherence_logs NO FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON adherence_logs
            AS RESTRICTIVE
            USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
        """
    )
