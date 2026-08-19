#!/usr/bin/env bash
# Local preview provisioning for WellnessCRM — idempotent, destructive, self-contained.
#
# Recreates everything the preview needs from a bare Postgres install: cluster
# roles, the `wellnesscrm` database, migrations, grant verification, the ops-only
# SECURITY DEFINER owner fix, and the curated food catalogue.
#
# 🔒 SECURITY DEFINER owner change: the identity_* functions must bypass RLS to
#   resolve a login before any tenant scope exists. On Supabase, migrations run as
#   the `postgres` superuser, so those functions are superuser-owned and bypass RLS
#   by design. Alembic here runs as `app_migrator` (NOBYPASSRLS), so under `users`
#   FORCE RLS they would return nothing and every login would fail. Reassigning
#   ONLY these already-minimal, REVOKE-from-public functions to a superuser restores
#   the intended production behaviour. It does not weaken RLS/tenant isolation for
#   the application role (`app_user`).
set -euo pipefail

PW=localdev

service postgresql start
# wait for the socket
for _ in $(seq 1 20); do su - postgres -c "psql -tAc 'SELECT 1'" >/dev/null 2>&1 && break; sleep 0.5; done

# 1. Roles (cluster-level) + passwords + database.
su - postgres -c "psql -v ON_ERROR_STOP=1 -f /app/ops/db/001_roles.sql"
su - postgres -c "psql -v ON_ERROR_STOP=1 \
  -c \"ALTER ROLE app_migrator WITH PASSWORD '$PW';\" \
  -c \"ALTER ROLE app_user WITH PASSWORD '$PW';\" \
  -c 'DROP DATABASE IF EXISTS wellnesscrm;' \
  -c 'CREATE DATABASE wellnesscrm OWNER app_migrator;'"

# 2. Grants inside the database (must precede migration so tables inherit them).
su - postgres -c "psql -d wellnesscrm -v ON_ERROR_STOP=1 -f /app/ops/db/001_roles.sql"

# 3. Schema.
(cd /app/backend && /root/.venv/bin/alembic upgrade head)

# 4. Verify grants + append-only immutability.
su - postgres -c "psql -d wellnesscrm -v ON_ERROR_STOP=1 -f /app/ops/db/002_verify_grants.sql"

# 5. Ops-only SECURITY DEFINER owner fix (see header).
su - postgres -c "psql -d wellnesscrm -v ON_ERROR_STOP=1 -c \"DO \\\$\\\$ DECLARE f record; BEGIN FOR f IN SELECT p.oid::regprocedure AS sig FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE p.prosecdef AND n.nspname='public' LOOP EXECUTE format('ALTER FUNCTION %s OWNER TO postgres', f.sig); END LOOP; END \\\$\\\$;\""

# 6. Curated food catalogue (real reference nutrition; global tenant_id IS NULL).
su - postgres -c "psql -d wellnesscrm -v ON_ERROR_STOP=1 -f /app/ops/db/seed_food_catalogue.sql"

echo "Provisioned. Restart backend, then run: /root/.venv/bin/python /app/scripts/dev_seed.py"
