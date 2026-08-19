"""Widen ``adherence_logs``' tenant policy to every role — the Pattern A shape.

Revision ID: 0027_adherence_logs_policy_roles
Revises: 0026_adherence_logs_to_spec
Created: 2026-08-15

🔒 **A defect in revision ``0025``, found by the first row ever written.**

``0025`` repaired ``adherence_logs``' broken tenant policy and, at the same time,
enabled ``FORCE ROW LEVEL SECURITY`` and scoped the replacement policy ``TO
app_user``. Each half is defensible; together they lock out ``app_migrator``.

``FORCE`` makes the table's **owner** subject to RLS, and ``app_migrator`` owns
these tables. A role with no *permissive* policy applying to it sees nothing —
PostgreSQL denies by default — so the owner lost every row on a table it is
responsible for maintaining, migrating and purging. Nothing failed loudly: reads
returned zero rows, which is indistinguishable from an empty table, and the table
*was* empty until Step 3 wrote to it.

Every other Pattern A policy in this schema gets this right by having no ``TO``
clause at all — ``clients`` (0009), ``measurements`` (0016), ``users`` (0002) all
apply to ``public``, so the owner is bound by the same tenant predicate as the
application rather than being exempt from it or excluded by it. The predicate is
what matters; the role list was never the control:

    tablename       policyname                        roles
    clients         clients__tenant_isolation         {public}
    measurements    measurements__tenant_isolation    {public}
    adherence_logs  adherence_logs__tenant_isolation  {app_user}   ← the outlier

🔒 **The Pattern C policy keeps its ``TO app_user``, deliberately.** That
asymmetry is the correct one: a *restrictive* policy that applied to the owner
would confine ``app_migrator`` to a single client, and the migrator's job — data
migrations, DPDP erasure, fixture teardown — spans every client in a tenant. A
restrictive policy that does not apply to a role simply does not narrow it, so
the client boundary stays exactly as strong for ``app_user``, which is the only
role the client realm ever connects as (DB §2.4).

⚠️ ``ALTER POLICY`` cannot change a policy's roles from a role list back to
``public``, so the policy is dropped and recreated with the same predicate.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: F401
from alembic import op

revision: str = "0027_adherence_logs_policy_roles"
down_revision: str | None = "0026_adherence_logs_to_spec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY = "adherence_logs__tenant_isolation"


def upgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON adherence_logs")
    op.execute(
        f"""
        CREATE POLICY {_POLICY} ON adherence_logs
            USING (tenant_id = current_tenant_id())
            WITH CHECK (tenant_id = current_tenant_id())
        """
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON adherence_logs")
    op.execute(
        f"""
        CREATE POLICY {_POLICY} ON adherence_logs
            AS PERMISSIVE FOR ALL
            TO app_user
            USING (tenant_id = current_tenant_id())
            WITH CHECK (tenant_id = current_tenant_id())
        """
    )
