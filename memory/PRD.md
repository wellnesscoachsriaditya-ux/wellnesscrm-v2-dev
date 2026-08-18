# WellnessCRM V2 — Coach Workspace (agent working notes)

> The authoritative product/architecture docs are in `/app/docs/` (PRD, ARCHITECTURE,
> DATABASE, API, IMPLEMENTATION-PLAN). This file is the agent's running log.

## Repo state
- Branch: `worktree-s1-identity-auth` (per user instruction; do NOT merge/rename/push).
- Stack (authoritative, do not migrate): FastAPI modular monolith + PostgreSQL/RLS +
  SQLAlchemy/Alembic; npm-workspaces Vite React monorepo (practitioner SPA, client PWA,
  operator) + shared design-system + generated OpenAPI client.
- Commit `bc17b57` is NOT present; the equivalent Plan Authoring slice IS present under
  other SHAs (20cac06 etc). Treat repo state as authoritative.

## Phase 1 — Coach/Practitioner Workspace (DONE — 2026-06)
Scope agreed with user: shell/nav + dashboard + client mgmt/360 polish + Plan Authoring UI.
Workouts OUT (PRD non-goal). PDF renderer OUT (Phase 2). Leads/WhatsApp not rebuilt.
Client PWA untouched.

Implemented:
- **Auth (real, ADR-A02)**: login screen, in-memory access token + bearer injection in the
  api-client, silent 401 refresh, refresh token persisted for reload, route guard, logout.
  Files: `packages/api-client/src/index.ts` (token store + `headers` support),
  `apps/practitioner/src/features/auth/*`, `screens/Login.tsx`, `App.tsx`.
- **Backend lock/unlock (Task 4)**: `is_locked` added to `ItemPatch`/`SlotPatch` and the
  `update_item`/`update_slot` module fns — smallest compatible change, reuses authz +
  tenant/client isolation + `If-Match` + audit + draft-only. Tests: `test_plan_locking.py`.
- **Backend food portions (missing API)**: `GET /nutrition/foods/{id}/portions` +
  `list_food_portions()` — the builder needs measure_unit_ids to add a food.
  Tests: `test_food_portions.py`.
- **Plan Builder UI (Task 5 — flagship)**: `features/nutrition/{plansApi,usePlanBuilder,
  useClientPlans}`, `components/nutrition/{NutritionBudgetPanel,FoodPicker,PlanSlotCard,
  PlanVersionHistory,ClientPlansPanel}`, `screens/{PlanBuilder,Plans}.tsx`. Budget,
  household-measure food add, lock/unlock, day nav, add meal/day, discard, issue,
  version history, ETag/409 conflict surfacing, loading/empty/error states.
- **Client 360 Plans entry point**: `ClientPlansPanel` wired into `ClientDetail.tsx`
  (list + create + open → `/plans/:planId`). IA manifest `plans`/`plan-detail` now render
  the real screens (were S4 placeholders).

Quality gates (all GREEN, run against a locally-provisioned Postgres):
- backend `pytest` (all), `ruff`, `mypy`, `check_boundaries` (be+fe)
- frontend all-workspace `vitest`, `tsc` typecheck, `eslint`, production `build`, bundle budget
- OpenAPI freshness + generated client freshness

## Known limitation (local sandbox)
The app boots and the full test suites pass against a locally-installed Postgres. Ad-hoc
*manual* practitioner login does not complete locally because `identity_lookup_by_subject`
(SECURITY DEFINER over `users` with FORCE RLS) returns no row without the login bootstrap
context, and the credential store is the in-memory `LocalCredentialStore` (GoTrue/Supabase
adapter is intentionally unimplemented — see `credentials.py`). This is pre-existing S1
identity infra, not Phase 1 code; the integration tests exercise these paths via their own
session/actor fixtures. Not modified per the user's Phase 0 constraint.

## Deferred to Phase 2 (do NOT build yet)
PDF renderer (architecture is PDF-ready), plan alternatives/supplements UI, templates UI,
revise-issued-plan flow, Leads/WhatsApp automation, appointments, billing, operator/SaaS
admin, Client PWA expansion.
