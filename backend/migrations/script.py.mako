"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Created: ${create_date}

Checklist before writing this migration — DB §2:

* RLS enabled and a policy declared for every tenant-scoped table (§8).
* Grants named explicitly. 🔒 Append-only tables (`audit_log`,
  `consent_records`, `operator_actions`) get SELECT and INSERT only — never
  UPDATE or DELETE. DDR-15 makes immutability a property of the grant table,
  and `ops/db/001_roles.sql` grants all four verbs by default privilege, so a
  migration creating one of these MUST revoke the two it must not have::

      op.execute("REVOKE UPDATE, DELETE ON TABLE audit_log FROM app_user")

  Then re-run `ops/db/002_verify_grants.sql`, which is what notices if you did
  not.
* `downgrade` either works or raises. ⚠️ A silent `pass` makes the chain
  dishonestly reversible — worse than one that refuses.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: F401  (used by most, not all, revisions)
from alembic import op  # noqa: F401
${imports if imports else ""}
revision: str = "${up_revision}"
down_revision: str | None = ${'"%s"' % down_revision if down_revision else "None"}
branch_labels: str | Sequence[str] | None = ${'"%s"' % branch_labels if branch_labels else "None"}
depends_on: str | Sequence[str] | None = ${'"%s"' % depends_on if depends_on else "None"}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
