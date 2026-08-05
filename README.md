# WellnessCRM V2

Practice management for Indian clinical dietitians, nutritionists and health coaches.

**Status:** Sprint S0 — Foundations & Design System

---

## What this is

Software that gives an independent nutrition practitioner back four hours a week.

The practitioner we serve manages 20–150 clients using WhatsApp, Google Sheets and diet charts
typed in Word. Their business works, but it consumes hours every week in manual, repetitive
labour. We replace that labour, not their WhatsApp.

**We are not competing on features with Practice Better. We are replacing manual work.**

## Documents — the source of truth

🔒 All five are **frozen and approved**. Implementation follows them; it does not reinterpret them.

| Document | Defines |
|---|---|
| [`docs/WellnessCRM-V2-PRD.md`](docs/WellnessCRM-V2-PRD.md) | What we build and why — 12 modules, 273 requirements |
| [`docs/WellnessCRM-V2-ARCHITECTURE.md`](docs/WellnessCRM-V2-ARCHITECTURE.md) | How it is built — 15 ADRs, module boundaries |
| [`docs/WellnessCRM-V2-DATABASE.md`](docs/WellnessCRM-V2-DATABASE.md) | ~70 tables, 18 DDRs, RLS strategy |
| [`docs/WellnessCRM-V2-API.md`](docs/WellnessCRM-V2-API.md) | The frontend/backend contract — 12 ADR-As |
| [`docs/WellnessCRM-V2-IMPLEMENTATION-PLAN.md`](docs/WellnessCRM-V2-IMPLEMENTATION-PLAN.md) | 14 sprints, S0–S13 |

When code and documents disagree, **the documents win** — or the document is changed
deliberately, with a recorded decision.

---

## Architecture in one page

**A modular monolith.** One codebase, two processes, one database.

```
practitioner SPA   client PWA   operator console     (React + Vite, one design system)
        │              │              │
        └──────────────┴──────────────┘
                       │  HTTPS / JSON — one API
                       ▼
        ┌──────────────────────────────────┐
        │  FastAPI                          │
        │  ┌────────────────────────────┐   │
        │  │ HTTP layer                 │   │
        │  ├────────────────────────────┤   │
        │  │ kernel/  (cross-cutting)   │   │  identity · authz · tenancy
        │  ├────────────────────────────┤   │  consent · audit · entitlements
        │  │ modules/ (domain)          │   │  notifications · storage · events
        │  ├────────────────────────────┤   │
        │  │ integrations/ (adapters)   │   │
        │  └────────────────────────────┘   │
        │                                   │
        │  web process      worker process  │  same image, different entry point
        └──────────────┬───────────────────┘
                       ▼
              PostgreSQL + Storage (Supabase, Mumbai)
```

### The rules that matter most

These exist because V1 failed on each of them specifically. They are enforced by tooling,
not by discipline — see `backend/tools/check_boundaries.py`.

| # | Rule | Why |
|---|---|---|
| 1 | **The browser never queries the database.** FastAPI is the only data path | V1's auth became unmaintainable because Supabase *and* FastAPI were both the backend. Two data paths meant two authorization systems, which drifted |
| 2 | **Business logic never lives in a UI component** | V1's worst failure: business logic duplicated across screens |
| 3 | **Modules never import each other.** Only via kernel events or query ports | A *permitted* import is an import that happens everywhere |
| 4 | **One authorization decision point.** Deny by default | An endpoint without a declared action fails at startup |
| 5 | **Tenant isolation below the application layer** (RLS) | A forgotten `WHERE tenant_id` must not leak client data |
| 6 | **Design system before feature screens** | V1 had no design system, and navigation drifted |
| 7 | **One implementation each** of: authorization, portion conversion, nutritional totalling, message scheduling, entitlement checks, audit writing, tenant resolution | Any second implementation is a defect |

### Deliberately not used

No microservices. No event bus. No Kubernetes. No Redis. No caching tier. No search service.

The job queue is Postgres (`SKIP LOCKED`). Rate limiting is Postgres. Search is Postgres
full-text. This is a one-developer product targeting 200 practitioners — every piece of
infrastructure omitted is one that cannot break at 2am.

---

## Layout

```
backend/
  app/
    kernel/         cross-cutting; may NOT import modules
    modules/        domain; may NOT import each other
    integrations/   third-party adapters behind ports we own
    platform/       framework wiring; no domain logic
  migrations/       Alembic — versions/ holds the linear chain
  tests/
  tools/            check_boundaries.py — CI boundary enforcement
                    export_openapi.py   — OpenAPI export (NFR-079)
frontend/
  packages/
    design-system/  tokens, primitives, patterns, shells  (built first)
    ia/             route/nav/breadcrumb manifests — one declared IA per app
    api-client/     GENERATED from OpenAPI — never hand-edited
  apps/
    practitioner/   desktop-first
    client-pwa/     mobile-first, offline-capable, installable
    operator/       minimal, austere, 2FA
  types/            ambient declarations shared by every project
docs/               the five frozen documents
ops/
  db/               role provisioning and grant verification — run by an operator
```

---

## Getting started

**Prerequisites:** Python ≥3.12, Node ≥20, PostgreSQL 15+ (or a Supabase project).

```bash
# Database roles — 🔒 ONCE per database, as a superuser, BEFORE the first migration.
# Alembic connects as app_migrator and cannot create the role it authenticates as.
psql "$SUPERUSER_URL" -v ON_ERROR_STOP=1 -f ops/db/001_roles.sql
# then set both passwords — see ops/db/README.md

# Backend
cd backend
python -m venv .venv
source .venv/Scripts/activate      # Windows (Git Bash);  .venv/bin/activate on macOS/Linux
pip install -e ".[dev]"
cp ../.env.example ../.env         # then fill in real values
alembic upgrade head               # applies migrations as app_migrator
uvicorn app.main:app --reload      # http://localhost:8000/docs

# Worker (separate terminal — ADR-01: background work never runs in the web process)
python -m app.worker

# Frontend
cd frontend
npm install
npm run gallery                    # design system component gallery
npm run dev:practitioner           # http://localhost:5173
npm run dev:portal                 # http://localhost:5174  (client PWA)
npm run dev:operator               # http://localhost:5175
```

## Checks

```bash
# Backend
cd backend
ruff check . && ruff format --check .
mypy app tools
pytest
python tools/check_boundaries.py          # architectural boundaries — R1..R8
python tools/export_openapi.py --check    # committed schema matches the code — NFR-079 ①

# Frontend
cd frontend
npm run lint && npm run typecheck && npm test && npm run build
npm run budget                            # gzipped bundle budgets — NFR-002
npm run check:client                      # committed types match the schema — NFR-079 ②
```

🔒 **All of the above run in CI. A boundary violation fails the build.**

### Changing the API

The generated client is build output, so a contract change is two commands and
both artefacts go in the same commit as the change that caused them:

```bash
cd backend  && python tools/export_openapi.py     # rewrite openapi.json
cd frontend && npm run generate:client            # rewrite generated/schema.ts
```

⚠️ Skip them and CI fails — which is the point. See
`frontend/packages/api-client/README.md`.

---

## Conventions

- **Python** — 4-space indent, ruff-formatted, `mypy --strict`, line length 100.
- **TypeScript** — strict, `noUncheckedIndexedAccess`, no `any`, type-only imports explicit.
- **Naming** — `snake_case` in the database and on the API wire; `camelCase` in TypeScript
  internals only. The API uses snake_case so no translation layer is needed.
- **Decimals** — transported as **strings**. JSON numbers are IEEE-754 doubles, and silent
  float rounding in a clinical nutrition value is a defect (ADR-A04).
- **Timestamps** — `timestamptz` everywhere, UTC on the wire, never a naive timestamp.
- **Money** — `{ amount: "1799.00", currency_code: "INR" }`. Never a bare float.

## Two things that must never happen

🔒 **No clinical data in logs, job payloads, audit metadata, traces or error reports.**
A clinical value in a log file is a data breach that database encryption does not prevent.

🔒 **`app_user` must never have `BYPASSRLS`, and the Supabase service-role key must never be
used for data access.** Either would silently disable every isolation policy in the system.

---

## Licence

Proprietary. All rights reserved.
