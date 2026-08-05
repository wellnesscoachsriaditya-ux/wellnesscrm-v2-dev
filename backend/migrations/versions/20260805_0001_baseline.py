"""Baseline — establishes the migration chain and locks down the version table.

Revision ID: 0001_baseline
Revises:
Created: 2026-08-05

🔒 **This migration creates no application tables, and that is deliberate.**
S0 is scaffolding: the platform-core tables (D0 — tenants, users, sessions,
audit_log …) land in S1 with the kernel that owns them. Building tables before
the code that uses them is exactly the horizontal build order PDR-01 forbids.

What it does do is close a hole that `ops/db/001_roles.sql` cannot close by
itself. That script grants `app_user` CRUD on future tables via ALTER DEFAULT
PRIVILEGES, so that every table a later migration creates is usable without
anyone remembering to grant it. `alembic_version` is created by `app_migrator`
like any other table — so it inherits those same default privileges, and the
application would silently gain write access to its own schema history.

An application that can UPDATE `alembic_version` can convince the next deploy
that a migration already ran. This revision revokes that access. It is the
first migration precisely so the window in which it is true never opens.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ⚠️ Guarded by a role-existence check rather than run unconditionally. A local
# developer may have a single-role database (config.migration_url falls back to
# the application URL), and a REVOKE naming a role that does not exist aborts
# the transaction — which would make the baseline unrunnable on exactly the
# setup most likely to be running it for the first time.
_REVOKE_VERSION_TABLE = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        REVOKE ALL ON TABLE alembic_version FROM app_user;
    END IF;
END
$$;
"""

_RESTORE_VERSION_TABLE = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE alembic_version TO app_user;
    END IF;
END
$$;
"""


def upgrade() -> None:
    op.execute(_REVOKE_VERSION_TABLE)


def downgrade() -> None:
    """Restore the default-privilege grant this revision removed.

    ⚠️ Reverting to a *less* safe state, which is what a downgrade of a
    hardening change means. It exists so the chain is honestly reversible; it is
    not something to run for its own sake.
    """
    op.execute(_RESTORE_VERSION_TABLE)
