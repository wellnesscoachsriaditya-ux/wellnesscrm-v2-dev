# WellnessCRM V2 — Coach Portal Premium Redesign

## Problem statement
Recompose and visually redesign the **Coach/Practitioner** frontend of the existing
WellnessCRM monorepo (Vite React + FastAPI + PostgreSQL + RLS) into a premium,
mobile-first SaaS experience — WITHOUT changing backend architecture, API contracts,
auth, RLS, tenant isolation, or business rules. Reference screenshots (Desktop
Dashboard, Desktop Client/360, Mobile Dashboard) are the **visual quality bar**; the
PRD/architecture are the **functional source of truth**. Real data only — no fabricated
metrics; honest empty/dependency states where an endpoint is missing.

## Architecture (unchanged)
- Monorepo `frontend/` (apps: practitioner, client-pwa, operator; packages: design-system, ia, api-client).
- SCREEN → HOOK → api-client → FastAPI → authz → RLS → PostgreSQL. Preserved.
- Backend `backend/` FastAPI, Alembic, SQLAlchemy, `/api/v1/{public,app,portal,admin}`.

## Preview environment (this container)
- PostgreSQL 15 provisioned locally; all migrations applied; grants + append-only immutability verified.
- Backend served via `server.py` shim (re-exports `app.main:app`) on the API path; practitioner Vite app on port 3000.
- Reproducible DB provisioning: `/app/scripts/provision_local_db.sh`. Real-data seed: `/app/scripts/dev_seed.py`.
- One ops-only correction: identity SECURITY DEFINER functions reassigned to a superuser owner so they bypass RLS for login exactly as on Supabase (migrations there run as superuser). No app/security change.

## Implemented (2026-06)
- **Premium visual layer** (`apps/practitioner/src/premium/`): design tokens (`theme.css`, extends DS tokens, Manrope), shared components (`ui.tsx`: Icon set, Avatar, StatusPill, KpiCard, Donut, SectionCard, QuickAction, Skeleton, PremiumPlaceholder), and `AppShell` (navy Coach-Portal sidebar + command bar; mobile bottom nav + drawer). Reuses design-system as foundation; DS primitives/contracts untouched.
- **Coach shell** wired via IA (`ia/manifest.ts` extended to full nav: Dashboard, Clients, Leads, Appointments, Nutrition Plans, Progress, Messages, Reports, Resources, Settings). Nav semantics (`<nav aria-label="Main">`, `<a aria-current>`) preserved.
- **Dashboard** (desktop + mobile): greeting, real KPIs (active/leads/total via `include_total`, waiting enquiries), Needs-attention queue, Recently-active, real Caseload-by-stage donut, quick actions. No fabricated trends/sparklines.
- **Clients** (desktop table + mobile cards): stage chips, search, sort, avatars, status pills, load-more — all via existing `useClientList`.
- **Extension screens** (Appointments, Progress, Reports, Resources, Settings): on-brand premium screens that name their backend dependency instead of faking data.
- Verified: typecheck ✓, ESLint ✓, 188/188 vitest ✓, production build ✓. (Fixed a pre-existing broken test dep: installed `@testing-library/dom`.)

## Backend dependencies (features with no endpoint yet — reported, not faked)
- Appointments (M6): scheduling endpoints.
- Progress & Retention: tenant-level progress/adherence aggregate.
- Reports; Resources library; Settings/usage (M10) reads.
- Nutrition **food catalogue** seed (foods/portions/nutrients) — Plan Builder meal *items* need it; structure/targets already work.
- Dashboard trend metrics (time-series), appointments, adherence, revenue.

## Backlog / Next
- P0: Client 360 premium recomposition; Nutrition Plan Builder premium workspace (plan hero, day nav, meal slots, sticky nutrition rail) using existing plan APIs.
- P1: Leads + Messages premium screens (endpoints exist); mobile Client-360 tabbed layout.
- P1: Re-add client bulk reassign to the new Clients list (deferred; old component retained).
- P2: Command palette (⌘K) behaviour; real Appointments/Reports once backend lands.
