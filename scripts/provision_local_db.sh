#!/usr/bin/env bash
# Local preview provisioning for WellnessCRM — idempotent, destructive.
#
# Recreates the `wellnesscrm` database from scratch: roles-in-DB, migrations,
# grant verification, and the one ops-level correction the local (non-Supabase)
# setup needs.
#
# 🔒 Why the SECURITY DEFINER owner change:
#   The identity_* SECURITY DEFINER functions (migration 0022 etc.) must bypass
#   RLS to resolve a login before any tenant scope exists. On Supabase, migrations
#   run as the `postgres` superuser, so those functions are superuser-owned and
#   bypass RLS by design. Here Alembic runs as `app_migrator` (NOBYPASSRLS), so
#   under `users` FORCE RLS they would return nothing and every login would fail.
#   Reassigning ONLY these already-minimal, REVOKE-from-public functions to a
#   superuser restores the intended production behaviour. It does not weaken RLS,
#   tenant isolation or authorization for the application role (`app_user`).
set -euo pipefail

su - postgres -c "psql -v ON_ERROR_STOP=1 -c 'DROP DATABASE IF EXISTS wellnesscrm;' -c 'CREATE DATABASE wellnesscrm OWNER app_migrator;'"
su - postgres -c "psql -d wellnesscrm -v ON_ERROR_STOP=1 -f /app/ops/db/001_roles.sql"
(cd /app/backend && /root/.venv/bin/alembic upgrade head)
su - postgres -c "psql -d wellnesscrm -v ON_ERROR_STOP=1 -f /app/ops/db/002_verify_grants.sql"
su - postgres -c "psql -d wellnesscrm -v ON_ERROR_STOP=1 -c \"DO \\\$\\\$ DECLARE f record; BEGIN FOR f IN SELECT p.oid::regprocedure AS sig FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE p.prosecdef AND n.nspname='public' LOOP EXECUTE format('ALTER FUNCTION %s OWNER TO postgres', f.sig); END LOOP; END \\\$\\\$;\""
echo "Provisioned. Restart backend, then run: /root/.venv/bin/python /app/scripts/dev_seed.py"
