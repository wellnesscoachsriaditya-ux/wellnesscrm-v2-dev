# Database operations

Everything in this directory is run by a **human operator with superuser
credentials**, not by the application and not by CI. That is the point: these
are the steps that create the privilege boundary the application then lives
inside, so they cannot be performed by anything on the application's side of it.

## Order

| # | File | When | As |
|---|---|---|---|
| 1 | `001_roles.sql` | Once per database, **before the first migration** | superuser |
| 2 | *(set passwords — see below)* | Immediately after step 1 | superuser |
| 3 | `alembic upgrade head` (from `backend/`) | Every deploy | `app_migrator` |
| 4 | `002_verify_grants.sql` | After any migration touching an append-only table | superuser |

```bash
psql "$SUPERUSER_URL" -v ON_ERROR_STOP=1 -f ops/db/001_roles.sql
# set passwords (step 2) — see below
cd backend && alembic upgrade head
psql "$SUPERUSER_URL" -v ON_ERROR_STOP=1 -f ops/db/002_verify_grants.sql
```

On Supabase, `001_roles.sql` and `002_verify_grants.sql` paste directly into the
SQL editor, which runs as `postgres`.

## Passwords

🔒 `001_roles.sql` creates both roles **without passwords** — NFR-034 keeps
secrets out of source control, and a `LOGIN` role with no password cannot
authenticate under `scram`/`md5`, so the default state is closed rather than
open. Set them from your secret store immediately after step 1:

```sql
ALTER ROLE app_migrator WITH PASSWORD '<from secret store>';
ALTER ROLE app_user     WITH PASSWORD '<from secret store>';
```

Then populate `DATABASE_MIGRATION_URL` (`app_migrator`) and `DATABASE_URL`
(`app_user`) — see `.env.example`.

## The two roles — DB §2.4

| Role | Used by | Holds | Never holds |
|---|---|---|---|
| `app_migrator` | Alembic only | DDL: `CREATE` on `public` | Runtime use |
| `app_user` | Web + worker | CRUD, **RLS enforced** | 🔒 `BYPASSRLS`, DDL |

🔒 **`app_user` must never have `BYPASSRLS`.** With it, every tenant-isolation
policy in DB §8 is inert while still appearing present in `pg_policies` — the V1
failure mode ADR-02 exists to prevent. This is asserted in three places, on
purpose: `001_roles.sql` refuses to finish without it,
`app.platform.db.verify_no_rls_bypass` re-checks it at every non-local startup,
and `tests/test_migrations.py` checks the script still makes the claim.

⚠️ Using the Supabase **service-role key** for data access has the same effect
as `BYPASSRLS`. It is for Auth and Storage administration only.

## Why roles are not an Alembic migration

Alembic authenticates as `app_migrator`, so it cannot create that role; `CREATE
ROLE` needs privileges we deliberately withhold from the migration credential;
and roles are cluster-scoped, so they do not belong to one database's linear
history. The full argument is in the header of `001_roles.sql`.

## Verifying tenant isolation (AC-M0-003)

> ⏳ **Status: runs in CI; not yet run on the development machine.** The tests
> below are written, committed and executable, and the `integration` job in
> `.github/workflows/ci.yml` runs them against a real PostgreSQL 16.4 service on
> every push — with `REQUIRE_LIVE_DATABASE=1`, so a missing database fails the
> build instead of skipping. They have **not** been run locally: this machine has
> neither Docker nor `psql` installed. `ops/db/docker-compose.yml` plus
> `ops/db/provision-test-db.sh` (below) are the one-command path once it does.
>
> 🔒 The suite is *not* skipped silently. With no `TEST_DATABASE_URL` set,
> `pytest` reports the tests as skipped with the reason pointing here, so the gap
> stays visible in every test summary rather than disappearing into a green run.
>
> ⚠️ 🔒 **A green CI run does not close DB §2.3.** There is no PgBouncer in front
> of the service container, so `test_scope_does_not_survive_its_transaction`
> proves `SET LOCAL` behaves on a *direct* connection — which was never the doubt.
> The launch gate is whether the **Supabase pooler** runs in transaction mode, and
> only a deployed environment can confirm that. That item stays open.

AC-M0-003 is the acceptance criterion the whole tenancy model rests on:

> With the application's tenant filter deliberately removed, a cross-tenant read
> returns **zero rows**.

Nothing that runs without a database can prove this. `backend/tests/test_kernel_schema.py`
checks that policies are *declared* — enabled, forced, carrying `WITH CHECK` —
by reading the migration source. Only PostgreSQL can demonstrate that it
*enforces* them, and that is what `backend/tests/integration/` does.

### What runs

`backend/tests/integration/test_tenant_isolation.py`, nine tests:

| Test | Proves |
|---|---|
| `test_cross_tenant_read_returns_nothing_with_the_filter_removed` | 🔒 **AC-M0-003 itself** |
| `test_unscoped_connection_sees_no_rows_at_all` | A missing scope fails closed, not open |
| `test_write_carrying_another_tenants_id_is_rejected` | The `WITH CHECK` half — `USING` alone would let a tenant *write into* another |
| `test_update_cannot_move_a_row_to_another_tenant` | `WITH CHECK` constrains UPDATE too |
| `test_delete_cannot_reach_another_tenants_row` | An unfiltered `DELETE` is scoped |
| `test_tenant_b_row_survives_tenant_a_deleting_everything` | …verified as the owner, since `app_user` cannot see the surviving rows |
| `test_scope_does_not_survive_its_transaction` | 🔒 DB §2.3 — the pooler is in **transaction** mode |
| `test_app_user_cannot_perform_ddl` | DB §2.4 — `app_user` cannot `DISABLE ROW LEVEL SECURITY` |
| `test_app_user_cannot_write_the_version_table` | The baseline's `alembic_version` revocation holds |

⚠️ **Every query in that file deliberately omits `WHERE tenant_id`.** That is the
experiment, not an oversight. A query carrying the filter passes against a
database with RLS entirely disabled — the false pass the suite exists to prevent.
`test_ac_m0_003_is_covered_by_an_executable_test` in `tests/test_kernel_schema.py`
fails if a future refactor "tidies" the filter back in.

### Steps — automated

`ops/db/provision-test-db.sh` does the whole sequence below. Prefer it: the
ordering is load-bearing and easy to get wrong, because `alembic upgrade head`
connects as `app_migrator` and revision 0003 `REVOKE`s from `app_user`, so both
roles must exist *before* the migrations run.

```bash
docker compose -f ops/db/docker-compose.yml up -d   # PostgreSQL 16.4 on :5433
ops/db/provision-test-db.sh                         # roles → passwords → migrate → verify
eval "$(ops/db/provision-test-db.sh --export)"      # export the two URLs
cd backend && pytest tests/integration -q
docker compose -f ops/db/docker-compose.yml down -v # `-v` matters: drop the volume
```

Requires `docker` and `psql` on `PATH`. Neither is present on this machine today,
which is why the section above still reports the local run as not done.

### Steps — by hand

Steps 1–2 are the ones already above; they are repeated here so this section
stands alone. ⚠️ Use a **throwaway database**. `seeded_tenants` deletes the rows
it creates, but a failure mid-test can leave them behind.

```powershell
# 0. A database to work in (any PostgreSQL 15+; Supabase local, Docker, or native)
psql "$env:SUPERUSER_URL" -c "CREATE DATABASE wellnesscrm_test"

# 1. Roles — superuser, once per cluster
psql "$env:SUPERUSER_URL" -v ON_ERROR_STOP=1 -f ops/db/001_roles.sql

# 2. Passwords — local throwaway values only, never a real secret
psql "$env:SUPERUSER_URL" -c "ALTER ROLE app_migrator WITH PASSWORD 'localdev'"
psql "$env:SUPERUSER_URL" -c "ALTER ROLE app_user     WITH PASSWORD 'localdev'"

# 3. Migrate, as app_migrator
cd backend
$env:DATABASE_MIGRATION_URL = "postgresql+psycopg://app_migrator:localdev@localhost:5432/wellnesscrm_test"
.venv/Scripts/alembic upgrade head

# 4. Run the gate
$env:TEST_DATABASE_URL           = "postgresql+psycopg://app_user:localdev@localhost:5432/wellnesscrm_test"
$env:TEST_DATABASE_MIGRATION_URL = $env:DATABASE_MIGRATION_URL
.venv/Scripts/python -m pytest tests/integration -v

# 5. Prove the migration reverses cleanly, then re-apply
.venv/Scripts/alembic downgrade base
.venv/Scripts/alembic upgrade head
```

**Both** URLs are required. With only `TEST_DATABASE_URL` the fixtures skip
rather than run: seeding as `app_user` cannot cross a tenant boundary, so the
"other tenant's row" would never exist and the cross-tenant read would return
zero rows for the wrong reason — a pass that proves nothing.

### Preconditions the fixtures enforce

`tests/integration/conftest.py` refuses to run against a database that could
produce a false pass. Each check names its own fix:

1. **`users` exists** — otherwise every query errors and could be misread as
   "no rows returned".
2. **The connecting role has neither `rolbypassrls` nor `rolsuper`** — with
   either, the policies are inert and the gate is meaningless. Point
   `TEST_DATABASE_URL` at `app_user`, not `postgres`.
3. **`pg_class.relforcerowsecurity` is true on `users`** — `ENABLE` alone does
   not set it, and `app_migrator` *owns* these tables, so without `FORCE` the
   owner bypasses every policy.

### Recording the result

The `integration` job in CI is the standing record: it runs the nine tests
against PostgreSQL 16.4 on every push, and `REQUIRE_LIVE_DATABASE=1` means it
cannot pass by skipping. **A green run of that job is what closes AC-M0-003 as an
enforcement claim** — the first one is the date to cite.

Two things a green job does *not* close, both tracked above:

* **DB §2.3** — the pooler's transaction mode. No PgBouncer in CI.
* The local run, if you want the sequence verified on a workstation.

## ⏳ Not here yet

Deployment configuration — staging and production environments, the web and
worker process definitions, and the pooler settings that the DB §2.3 launch gate
depends on. The S0 Definition of Done item *"all three apps deploy to staging and
render"* is **not met**: the apps build and render, but nothing in this
repository deploys them.

🔒 The pooler must run in **transaction mode**. `verify_pooler_isolation` fails
startup if it is not, but that check only runs where something is deployed.
