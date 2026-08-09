"""Discovery — the indexes the client list and search actually need.

Revision ID: 0013_discovery
Revises: 0012_timeline
Created: 2026-08-09

🔒 FR-M1-021/022, AC-M1-002, NFR-005. No new tables: the client spine and the
tag junction already hold everything the list reads. What is missing is the
index support for two access paths migration 0009 could not settle.

📌 **Mobile-suffix search without `pg_trgm`** — the decision 0009 deferred here.

AC-M1-002 asks that "the last 4 digits of a mobile" find a client. `search_vector`
cannot serve it: `to_tsvector` tokenises `+919876543210` as one term, so a suffix
match degrades to a sequential scan on the largest table in the product.

❌ `pg_trgm` GIN on `mobile` — `CREATE EXTENSION` needs superuser, which
`app_migrator` deliberately is not (DB §2.4). It would add an operator
provisioning step to every environment, a customer's included, to serve one
search box.

❌ `LIKE '%4321'` unindexed — a scan per keystroke, and FR-M1-021 fires this on
every character typed.

✅ **Chosen: `reverse(mobile)` + `text_pattern_ops`.** A suffix match becomes a
*prefix* match on the reversed string, which btree serves directly. No
extension, no superuser, no provisioning step.

⚠️ The application must reverse the query the same way — see
`modules.clients.discovery`, which is the only caller and states the same rule.
A query using `LIKE '%…'` against the forward column would silently not use this
index, and the symptom is a slow list rather than a wrong one.

⚠️ **`text_pattern_ops` is required, not decorative.** A plain btree on a text
column is ordered by the database's collation, and `LIKE 'prefix%'` can only use
an index whose ordering is plain byte comparison. Without the operator class this
index exists and is never chosen — the worst outcome, because it costs writes and
returns nothing.

🔒 The second index serves FR-M1-022's sorts. `ix_clients__tenant_stage` already
covers filtering by stage, but every sort this slice offers ends in a keyset
cursor over `(sort_column, id)`, and only `created_at`/`updated_at` orderings can
be served from an index that does not carry them.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_discovery"
down_revision: str | None = "0012_timeline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 🔒 AC-M1-002 — see the module docstring for why this shape rather than
    # `pg_trgm`. Partial on the same predicate as every other list index, so a
    # query that forgets the archived filter still cannot see archived rows
    # through it.
    op.execute(
        """
        CREATE INDEX ix_clients__tenant_mobile_reversed
            ON clients (tenant_id, reverse(mobile) text_pattern_ops)
            WHERE mobile IS NOT NULL AND archived_at IS NULL;
        """
    )

    # FR-M1-022 — "sortable by name, recent activity and creation date".
    #
    # ⚠️ `id` is the last column in each, and it is load-bearing rather than
    # decorative: the cursor is a keyset over `(sort_column, id)` because
    # `updated_at` ties are routine — a bulk reassignment stamps a whole batch in
    # one transaction — and a cursor with no unique tiebreaker skips or repeats
    # rows at exactly that boundary. Same argument as the timeline's index.
    op.execute(
        """
        CREATE INDEX ix_clients__tenant_updated
            ON clients (tenant_id, updated_at DESC, id DESC)
            WHERE archived_at IS NULL;
        """
    )
    op.execute(
        """
        CREATE INDEX ix_clients__tenant_created
            ON clients (tenant_id, created_at DESC, id DESC)
            WHERE archived_at IS NULL;
        """
    )
    # ⚠️ Case-insensitive, matching the ORDER BY. A practitioner scanning an
    # alphabetical list does not expect "asha" to sort after "Zara", and an index
    # on the raw column cannot serve `ORDER BY lower(full_name)`.
    op.execute(
        """
        CREATE INDEX ix_clients__tenant_name
            ON clients (tenant_id, lower(full_name), id)
            WHERE archived_at IS NULL;
        """
    )

    # 🔒 Tag filtering (FR-M1-022) reads the junction the other way round from
    # `ix_client_tags__tag`: "which clients carry this tag" is the list's
    # question, and the existing index leads with `tenant_id, tag_id` — which
    # serves it, but returns `client_id` only as a heap lookup. Including it
    # makes the filter an index-only scan.
    op.execute(
        """
        CREATE INDEX ix_client_tags__tag_client
            ON client_tags (tenant_id, tag_id, client_id);
        """
    )


def downgrade() -> None:
    """Drop the indexes. No data is touched — this revision creates none."""
    op.execute("DROP INDEX IF EXISTS ix_client_tags__tag_client")
    op.execute("DROP INDEX IF EXISTS ix_clients__tenant_name")
    op.execute("DROP INDEX IF EXISTS ix_clients__tenant_created")
    op.execute("DROP INDEX IF EXISTS ix_clients__tenant_updated")
    op.execute("DROP INDEX IF EXISTS ix_clients__tenant_mobile_reversed")
