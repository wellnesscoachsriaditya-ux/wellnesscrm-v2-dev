# Test credentials & local bring-up (WellnessCRM V2)

> This repo is NOT the default Emergent stack. It is FastAPI + PostgreSQL/RLS + an
> npm-workspaces Vite monorepo. It does not run on the default supervisor/preview URL.

## Local bring-up (used to run the test suites)
1. Postgres 15 (installed via apt in this sandbox), cluster started, superuser password
   set to `localdev`, database `wellnesscrm_test` created.
2. Provisioned with the repo's own script:
   `TEST_DB_HOST=localhost TEST_DB_PORT=5432 TEST_DB_NAME=wellnesscrm_test \
    TEST_DB_SUPERUSER=postgres TEST_DB_SUPERPASS=localdev TEST_DB_APPPASS=localdev \
    bash ops/db/provision-test-db.sh`  (roles + 27 migrations + grant verification)
3. Python 3.12 venv at `/app/backend/.venv` (`uv venv --python 3.12`; `uv pip install -e ".[dev]"`).
4. Frontend: `@testing-library/dom` peer installed via `npm install` (node_modules is gitignored).

## Running the gates
- Backend tests:
  `TEST_DATABASE_URL=postgresql+psycopg://app_user:localdev@localhost:5432/wellnesscrm_test \
   TEST_DATABASE_MIGRATION_URL=postgresql+psycopg://app_migrator:localdev@localhost:5432/wellnesscrm_test \
   REQUIRE_LIVE_DATABASE=1 /app/backend/.venv/bin/pytest`
- Frontend: `cd frontend && npm run test && npm run typecheck && npm run lint && npm run build`

## Practitioner account (local test DB only)
- Email: `drrao@raonutrition.com`  Password: `<local-throwaway-password>`  Role: owner (tenant seeded)
- The password is a throwaway local value chosen at seed time; it has no meaning outside the
  ephemeral local test DB (and interactive login does not complete anyway — see below).
- ⚠️ NOTE: interactive login does not complete in local because the identity provider is the
  in-memory `LocalCredentialStore` and `identity_lookup_by_subject` runs under FORCE RLS
  without the login bootstrap context (pre-existing S1 infra; GoTrue/Supabase adapter is
  intentionally unimplemented). Auth/session flows are verified by the integration + vitest
  suites, which use their own session/actor fixtures rather than a manual login.
