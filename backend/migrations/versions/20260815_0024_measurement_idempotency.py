"""Measurement idempotency — the key `/portal/sync` replays against.

Revision ID: 0024_measurement_idempotency
Revises: 2eb56b8913d5
Created: 2026-08-15

🔒 **S6 Slice 2.** The client portal syncs offline work in batches, and a batch
that is retried — a flaky connection, a service worker replaying its queue — must
not produce a second weight for the same morning. The client supplies a key; the
database refuses the duplicate.

🔒 **Enforced by a unique index, not by application logic.** A ``SELECT`` then
``INSERT`` can lose a race between two replays arriving together; a unique index
cannot. This is the same argument DB §11.4 makes for ``scheduled_messages``, and
the same shape: uniqueness scoped to the tenant *and* the client.

⚠️ **Scoped to ``(tenant_id, client_id, idempotency_key)``, not to the key alone.**
Two clients — in one practice or in different ones — can legitimately send the
same key, because the key is generated on their own device with no knowledge of
anyone else's. A global unique index would make one client's sync silently
suppress another's measurement, which is a data-loss bug wearing a
deduplication feature's clothes.

⚠️ **Partial, on ``WHERE idempotency_key IS NOT NULL``.** Practitioner-entered
measurements (DB §7.5, FR-M3-012) carry no key and must stay unconstrained —
a practitioner may record two measurements for one client on one day, and that
is a clinical decision, not a duplicate. PostgreSQL does treat NULLs as
distinct in a plain unique index, so a non-partial index would *happen* to work;
the partial predicate says so on purpose rather than resting on a subtlety of
NULL comparison that a future reader would have to know.

⚠️ **Not a data migration.** The column is nullable and every existing row keeps
``NULL``, so no backfill runs and no measurement is touched. The index can
therefore be created on a populated table without rewriting it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_measurement_idempotency"
down_revision: str | None = "2eb56b8913d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "uq_measurements__client_idempotency"


def upgrade() -> None:
    op.add_column(
        "measurements",
        sa.Column(
            "idempotency_key",
            sa.Text(),
            nullable=True,
            comment=(
                "🔒 Client-supplied; deduplicates a replayed /portal/sync batch. "
                "NULL for practitioner-entered measurements."
            ),
        ),
    )

    # 🔒 The enforcement. Scoped to the client so two clients cannot collide,
    # partial so practitioner entries are unaffected.
    op.create_index(
        _INDEX,
        "measurements",
        ["tenant_id", "client_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="measurements")
    op.drop_column("measurements", "idempotency_key")
