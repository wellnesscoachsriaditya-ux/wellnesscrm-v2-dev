#!/usr/bin/env bash
# WellnessCRM V2 — provision a local test database for the isolation gate.
#
# ═══════════════════════════════════════════════════════════════════════════
# 🔒 Turns an empty PostgreSQL into one the AC-M0-003 suite can prove things on.
# ═══════════════════════════════════════════════════════════════════════════
#
# Automates the steps ops/db/README.md > 'Verifying tenant isolation' lists by
# hand. It exists because that sequence has an ordering constraint that is easy
# to get wrong and fails confusingly: the roles must exist *before* the
# migrations run, because `alembic upgrade head` connects as `app_migrator` and
# revision 0003 issues `REVOKE ... FROM app_user`.
#
# ─── Usage ────────────────────────────────────────────────────────────────
#
#   docker compose -f ops/db/docker-compose.yml up -d
#   ops/db/provision-test-db.sh
#   eval "$(ops/db/provision-test-db.sh --export)"   # print the two URLs only
#
# Environment overrides (defaults target ops/db/docker-compose.yml):
#   TEST_DB_HOST (localhost) · TEST_DB_PORT (5433) · TEST_DB_NAME
#   (wellnesscrm_test) · TEST_DB_SUPERUSER (postgres) · TEST_DB_SUPERPASS
#   (localdev) · TEST_DB_APPPASS (localdev)
#
# ⚠️ Idempotent, and destructive only of its own schema: re-running drops
# nothing, but `alembic upgrade head` on an already-migrated database is a
# no-op rather than an error. Never point this at a database with real data —
# the roles it creates carry throwaway passwords.

set -euo pipefail

HOST="${TEST_DB_HOST:-localhost}"
PORT="${TEST_DB_PORT:-5433}"
NAME="${TEST_DB_NAME:-wellnesscrm_test}"
SUPERUSER="${TEST_DB_SUPERUSER:-postgres}"
SUPERPASS="${TEST_DB_SUPERPASS:-localdev}"
APPPASS="${TEST_DB_APPPASS:-localdev}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OPS_DB="$REPO_ROOT/ops/db"

SUPER_URL="postgresql://${SUPERUSER}:${SUPERPASS}@${HOST}:${PORT}/${NAME}"
# 🔒 `+psycopg` — SQLAlchemy's async driver. The bare `postgresql://` form the
# psql invocations use would give the tests a *sync* engine and every `await`
# would fail on a driver that cannot provide one.
APP_URL="postgresql+psycopg://app_user:${APPPASS}@${HOST}:${PORT}/${NAME}"
MIGRATION_URL="postgresql+psycopg://app_migrator:${APPPASS}@${HOST}:${PORT}/${NAME}"

# `--export` prints the two variables and does nothing else, so a shell can
# `eval` it after provisioning has already happened.
if [[ "${1:-}" == '--export' ]]; then
    echo "export TEST_DATABASE_URL='${APP_URL}'"
    echo "export TEST_DATABASE_MIGRATION_URL='${MIGRATION_URL}'"
    exit 0
fi

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# 🔒 Wait for readiness rather than assuming it. `docker compose up -d` returns
# when the container starts, not when PostgreSQL accepts connections, so the
# first psql would race initdb — an intermittent failure that reads like a
# broken script.
say "Waiting for PostgreSQL at ${HOST}:${PORT}"
for attempt in $(seq 1 30); do
    if PGPASSWORD="$SUPERPASS" psql "$SUPER_URL" -c 'SELECT 1' >/dev/null 2>&1; then
        echo "Ready after ${attempt} attempt(s)."
        break
    fi
    if (( attempt == 30 )); then
        echo "ERROR: PostgreSQL did not become ready after 30 attempts." >&2
        echo "Is the container up?  docker compose -f ops/db/docker-compose.yml ps" >&2
        exit 1
    fi
    sleep 1
done

say "1/4 Roles — ops/db/001_roles.sql"
PGPASSWORD="$SUPERPASS" psql "$SUPER_URL" -v ON_ERROR_STOP=1 -q -f "$OPS_DB/001_roles.sql"

# 🔒 Separate from role creation by design: 001_roles.sql sets no passwords
# (NFR-034), so a role exists but cannot authenticate until this runs. Local
# throwaway values only — these must never be a real secret.
say "2/4 Passwords — local throwaway values"
PGPASSWORD="$SUPERPASS" psql "$SUPER_URL" -v ON_ERROR_STOP=1 -q \
    -c "ALTER ROLE app_migrator WITH PASSWORD '${APPPASS}'" \
    -c "ALTER ROLE app_user     WITH PASSWORD '${APPPASS}'"

# Alembic reads the URL from `get_settings().migration_url`, so the environment
# variable the application uses is what steers it — not a flag.
say "3/4 Migrations — alembic upgrade head, as app_migrator"
(
    cd "$REPO_ROOT/backend"
    export DATABASE_MIGRATION_URL="$MIGRATION_URL"
    export DATABASE_URL="$APP_URL"
    if [[ -x .venv/Scripts/alembic.exe ]]; then
        .venv/Scripts/alembic.exe upgrade head       # Windows
    elif [[ -x .venv/bin/alembic ]]; then
        .venv/bin/alembic upgrade head               # POSIX
    else
        alembic upgrade head                         # already on PATH (CI)
    fi
)

# 🔒 The counterweight to 001_roles.sql's blanket ALTER DEFAULT PRIVILEGES,
# which grants all four verbs on future tables. Immutability is enforced by the
# *absence* of an UPDATE grant, and nothing fails loudly when one is added back.
say "4/4 Grant verification — ops/db/002_verify_grants.sql"
PGPASSWORD="$SUPERPASS" psql "$SUPER_URL" -v ON_ERROR_STOP=1 -q -f "$OPS_DB/002_verify_grants.sql"

say "Provisioned. Run the gate with:"
cat <<EOF

  export TEST_DATABASE_URL='${APP_URL}'
  export TEST_DATABASE_MIGRATION_URL='${MIGRATION_URL}'
  cd backend && pytest tests/integration -q

Or in one step:  eval "\$(ops/db/provision-test-db.sh --export)"
EOF
