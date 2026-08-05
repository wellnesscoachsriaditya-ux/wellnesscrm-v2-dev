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

## ⏳ Not here yet

Deployment configuration — staging and production environments, the web and
worker process definitions, and the pooler settings that the DB §2.3 launch gate
depends on. The S0 Definition of Done item *"all three apps deploy to staging and
render"* is **not met**: the apps build and render, but nothing in this
repository deploys them.

🔒 The pooler must run in **transaction mode**. `verify_pooler_isolation` fails
startup if it is not, but that check only runs where something is deployed.
