"""Mobile-suffix search, made reachable under RLS — the correction to 0013.

Revision ID: 0014_mobile_reversed_leakproof
Revises: 0013_discovery
Created: 2026-08-09

🔒 AC-M1-002, NFR-005. Migration 0013 chose `reverse(mobile) text_pattern_ops`
over `pg_trgm`, and the reasoning there still holds — a suffix match should be a
prefix match on a reversed string, and that needs no extension and no superuser.

⚠️ **What 0013 missed is that `clients` has FORCE ROW LEVEL SECURITY.** Under a
security policy PostgreSQL will only push a qual into an *index condition* if the
qual is leakproof — a non-leakproof function could otherwise observe rows the
policy is about to hide. Neither half of 0013's predicate qualifies:

| Function | `proleakproof` |
|---|---|
| `reverse()` | ❌ false |
| `textlike` (`LIKE`) | ❌ false |

So the index was built, was valid, and could never be *seeked*. The planner's
best available plan was a bitmap scan of every row in the tenant with the suffix
applied afterwards as a Filter — measured on the local cluster at 2,500 rows:

| Plan | Cost |
|---|---|
| 0013, bitmap scan + Filter | 91.05 |
| This revision, Index Cond seek | 0.28 |

⚠️ This is exactly the failure mode 0013's own docstring warned about — "the
symptom is a slow list rather than a wrong one" — arriving through a mechanism it
did not anticipate. The index was never the problem; the *predicate* could not
reach it.

📌 **The fix, and why this shape.**

❌ Mark `reverse()` leakproof — needs superuser, and asserts a security property
about a function to win a planner optimisation. Wrong trade.

❌ `SECURITY INVOKER` view, or disabling RLS — removes the guarantee AC-M0-003
rests on.

✅ **Chosen: a plain STORED generated column + range operators.** The column is
ordinary storage, so no function runs at query time, and `~>=~` / `~<~` are
leakproof (`proleakproof = t`), so they land in the Index Cond.

⚠️ Leakproofness was necessary but **not sufficient** — the first attempt at this
migration still produced the bitmap plan. 0013's partial predicate,
`WHERE mobile IS NOT NULL`, has to be provable from the query, and Postgres
cannot derive a fact about `mobile` from a clause about `mobile_reversed`. Both
halves had to change together; see the `CREATE INDEX` below.

⚠️ The application must write the predicate as the range form — see
`modules.clients.discovery._apply_search`, which is the only caller. A
`mobile_reversed LIKE 'suffix%'` against this column is still `textlike` and
still cannot be pushed down, so it would silently restore the old plan.

🔒 The column is **nullable**, and must be. FR-M1-004 requires a name plus *one*
contact method, so a client captured with only an email has no mobile, and
`reverse(NULL)` is NULL — a NOT NULL constraint here would reject them at INSERT.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014_mobile_reversed_leakproof"
down_revision: str | None = "0013_discovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 🔒 `GENERATED ALWAYS ... STORED`, matching `search_vector` in 0009 and for
    # the same reason: there is no way to write a row that bypasses it, so the
    # column cannot drift from the mobile it reverses.
    #
    # ⚠️ Nullable deliberately — see the module docstring.
    op.execute(
        """
        ALTER TABLE clients
            ADD COLUMN mobile_reversed text
            GENERATED ALWAYS AS (reverse(mobile)) STORED;
        """
    )

    # The 0013 index — valid, and unusable under RLS. Dropped rather than left
    # in place: it costs every write and can serve nothing.
    op.execute("DROP INDEX IF EXISTS ix_clients__tenant_mobile_reversed")

    # 🔒 Same name and same leading `tenant_id` — the change is the indexed
    # expression (function call → stored column) **and the partial predicate**.
    #
    # ⚠️ The predicate is on `mobile_reversed`, not on `mobile`. Postgres's
    # predicate-implication checker is not a theorem prover: from a WHERE clause
    # on `mobile_reversed` it cannot derive `mobile IS NOT NULL`, so an index
    # predicated on the source column is skipped for any query that does not
    # name `mobile` directly. The suffix search never does. Predicating on the
    # column the query actually constrains is what makes the partial index
    # provable — and the clauses imply non-nullness anyway, because the range
    # operators are strict, so the predicate drops no rows.
    #
    # ⚠️ `text_pattern_ops` remains required, and for the reason 0013 gave: a
    # plain btree is ordered by collation, and only a byte-ordered index can
    # serve a prefix range.
    op.execute(
        """
        CREATE INDEX ix_clients__tenant_mobile_reversed
            ON clients (tenant_id, mobile_reversed text_pattern_ops)
            WHERE mobile_reversed IS NOT NULL AND archived_at IS NULL;
        """
    )


def downgrade() -> None:
    """Restore 0013's index, then drop the column it replaced.

    ⚠️ In that order — the column cannot be dropped while an index depends on
    it, and the restored index deliberately does not.
    """
    op.execute("DROP INDEX IF EXISTS ix_clients__tenant_mobile_reversed")
    op.execute(
        """
        CREATE INDEX ix_clients__tenant_mobile_reversed
            ON clients (tenant_id, reverse(mobile) text_pattern_ops)
            WHERE mobile IS NOT NULL AND archived_at IS NULL;
        """
    )
    op.execute("ALTER TABLE clients DROP COLUMN IF EXISTS mobile_reversed")
