# WellnessCRM V2 — Architecture Document

**Status:** Draft v0.1 — awaiting review
**Owner:** Founder / CTO
**Date:** 2026-08-04
**Phase:** 3 of 11 (Architecture)
**Derives from:** `docs/WellnessCRM-V2-PRD.md` (approved)

---

## Document Control

### Purpose

This document is the **technical blueprint** implementation will follow. It defines *how* the system is built. Every decision here traces to a PRD requirement, cited by ID.

| This document DOES define | This document does NOT define |
|---|---|
| Process topology and runtime shape | Database tables and columns (Phase 4) |
| Module boundaries and dependency rules | API endpoints and payloads (Phase 5) |
| Where each responsibility lives, exactly once | Screen layouts and components (Phase 6) |
| Mechanisms for tenancy, authorization, jobs, AI grounding | Sprint sequencing (Phase 8) |
| Deployment topology and cost model | Any implementation code (Phase 9) |
| Build order and its dependency reasoning | |

### Conventions

> 🔒 **BINDING** — derives from a PRD requirement or a V1 failure. Changing it requires an explicit decision.
> 🟡 **PROPOSAL** — new to this phase, not in the PRD. Flagged per instruction; requires your approval.
> ⚠️ **RISK** — known hazard with cost, delivery or security impact.
> 📌 **ADR-{nn}** — Architecture Decision Record. Summarised in §21.

### Traceability rule

🔒 No architectural element exists without a PRD justification. Where this document introduces something the PRD does not require, it is marked 🟡 **PROPOSAL** and listed in §22.

---

## 1. Architectural Principles

These are the decision filters. When a choice is unclear, apply them in order.

| # | Principle | Source | Practical test |
|---|---|---|---|
| 1 | **One developer must be able to debug it at 2am** | Solo-dev constraint | If it needs a distributed-systems mental model, reject it |
| 2 | **Every responsibility has exactly one home** | NFR-072, V1 duplication failure | If a rule could live in two places, the design is wrong |
| 3 | **Boundaries are enforced by tooling, not discipline** | V1 coupling failure | If it relies on remembering, it will decay |
| 4 | **Correctness below the application layer where possible** | NFR-030, FR-M0-011 | If forgetting a filter leaks data, push the guarantee down |
| 5 | **Boring technology, minimal moving parts** | NFR-077, NFR-093 | Prefer the thing already in the stack over the better thing |
| 6 | **Cheap now, changeable later** | NFR-087, NFR-094 | Optimise for the 200-tenant target; document the trigger to revisit |
| 7 | **External dependencies are replaceable** | ASM-03, ASM-08 | Every third party sits behind an interface we own |
| 8 | **Architecture precedes features** | User's stated V1 lesson | Scaffolding, boundaries and CI exist before module #1 |

---

## 2. Overall System Architecture

### 2.1 Shape: modular monolith, two deployables

🔒 Per NFR-077 and the solo-developer constraint, the system is **one application codebase** deployed as **two processes** sharing one database.

```
┌─────────────────────────────────────────────────────────────────┐
│                          CLIENTS                                 │
│                                                                  │
│  Practitioner SPA        Client PWA           Operator Console   │
│  (React, desktop-first)  (React, mobile-first) (React, minimal)  │
│         │                     │                      │           │
└─────────┼─────────────────────┼──────────────────────┼───────────┘
          │                     │                      │
          │      HTTPS / JSON — one API, one auth model │
          ▼                     ▼                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                    APPLICATION (FastAPI)                         │
│                                                                  │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  HTTP Layer — routing, serialisation, request context      │  │
│  ├───────────────────────────────────────────────────────────┤  │
│  │  Cross-Cutting Kernel                                      │  │
│  │  identity · authz · tenancy · consent · audit · entitle-   │  │
│  │  ments · notifications · storage · events                  │  │
│  ├───────────────────────────────────────────────────────────┤  │
│  │  Domain Modules                                            │  │
│  │  clients · leads · clinical · nutrition · ai_drafting ·    │  │
│  │  appointments · messaging · progress · billing · admin     │  │
│  ├───────────────────────────────────────────────────────────┤  │
│  │  Integration Layer (Ports & Adapters)                      │  │
│  │  whatsapp · sms · email · llm · payments · storage         │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
│   PROCESS 1: Web (serves HTTP)    PROCESS 2: Worker (jobs)      │
│   Same codebase. Same modules. Different entry point.           │
└──────────────────────────┬──────────────────────────────────────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
   ┌────────────┐   ┌────────────┐   ┌────────────┐
   │ PostgreSQL │   │  Object    │   │  External  │
   │ (Supabase, │   │  Storage   │   │  Services  │
   │  Mumbai)   │   │ (Supabase) │   │            │
   │            │   │            │   │ WhatsApp   │
   │ • data     │   │ • labs     │   │ Razorpay   │
   │ • RLS      │   │ • PDFs     │   │ LLM        │
   │ • job queue│   │ • uploads  │   │ SMS/Email  │
   └────────────┘   └────────────┘   └────────────┘
```

### 2.2 Why two processes and not one

📌 **ADR-01 — Separate worker process from day one**

NFR-095 requires background work to be separable "without re-architecture." The cheapest way to guarantee that is to separate it immediately.

| Option | Verdict |
|---|---|
| Background work in the web process (threads/tasks) | ❌ A long AI call or PDF render blocks request capacity. A web deploy kills in-flight jobs. Guarantees a painful migration later |
| **Separate worker process, same codebase** | ✅ **Chosen.** No added complexity — same code, different entry point. Deploy and scale independently. NFR-095 satisfied by construction |
| Separate service with its own codebase | ❌ Violates NFR-077. Two deployables to keep in sync, for no benefit at this scale |

**Cost:** one extra small process (~₹400–600/month). Buys crash isolation, independent scaling, and safe deploys.

### 2.3 The single data path

🔒 **NFR-031 — the browser MUST NOT query the database directly.** This is the most important structural rule in the system, and the direct countermeasure to V1's *"authentication, routing and permissions became difficult to maintain."*

📌 **ADR-02 — FastAPI is the sole data path; Supabase is infrastructure, not a backend**

V1 failed because Supabase and FastAPI both legitimately acted as the backend. Two data paths meant two authorization systems, which drifted. The fix is not "be more careful" — it is to remove the second path entirely.

| Component | Role | Explicitly NOT |
|---|---|---|
| **FastAPI** | The only path for reads and writes. All domain logic. All authorization decisions | — |
| **Postgres (Supabase)** | Storage. RLS as defence-in-depth | The primary permission system |
| **Supabase Auth** | Identity and token issuance (GoTrue) | Authorization |
| **Supabase Storage** | File bytes | Direct client access |
| **`supabase-js` in browser** | **Nothing.** Not included in the bundle | Any data access |

⚠️ **Enforcement, not convention.** `@supabase/supabase-js` is **not a frontend dependency.** It cannot be imported because it is not installed. Discipline decays; a missing package does not (Principle 3).

**Auth token handling:** the frontend calls our own `/auth/*` endpoints. FastAPI brokers with GoTrue server-side and returns our own session token. The browser never holds a Supabase key of any kind. This also keeps the door open to replacing Supabase Auth later without touching the frontend (Principle 7).

### 2.4 Technology stack

🔒 Carried from Phase 1. Each entry states why it survives the "boring technology" filter.

| Layer | Choice | Justification |
|---|---|---|
| **Frontend** | React + Vite + TypeScript | Known. PWA-capable (FR-M7-010). One codebase, three apps (§4) |
| **Backend** | Python 3.12 + FastAPI | Known. Strong LLM ecosystem (M5). Native OpenAPI → typed client (NFR-079) |
| **Database** | PostgreSQL 15+ via Supabase (Mumbai) | Managed backups (NFR-023). RLS for NFR-030. Region matches DPDP posture |
| **Auth** | Supabase Auth (GoTrue), brokered server-side | Avoids hand-rolling credential storage (NFR-029) |
| **Storage** | Supabase Storage, private buckets + signed URLs | NFR-035 |
| **Jobs** | Postgres-backed queue (§11) | 🔒 NFR-077 — no Redis, no Celery, no broker |
| **Validation** | Pydantic v2 | One definition serves validation, serialisation and OpenAPI |
| **ORM** | SQLAlchemy 2.0 | Explicit, mature, transparent SQL |
| **Migrations** | Alembic | NFR-076 |
| **Type sharing** | OpenAPI → generated TS client | NFR-079 |
| **Testing** | pytest + Vitest + Playwright | AC verification (NFR-073) |
| **Errors** | Sentry, with PII scrubbing | NFR-080 + NFR-033 |

**Python 3.12 minimum** — required for the async and typing behaviour assumed throughout.

### 2.5 Repository layout

Single repository. Boundaries visible in the directory structure.

```
wellnesscrm/
├── backend/
│   ├── app/
│   │   ├── main.py                 # Web entry point
│   │   ├── worker.py               # Worker entry point
│   │   ├── kernel/                 # Cross-cutting — depended on by all
│   │   │   ├── identity/  authz/  tenancy/  consent/
│   │   │   ├── audit/  entitlements/  notifications/
│   │   │   ├── storage/  events/  errors/  context/
│   │   ├── modules/                # Domain modules — peers, no direct imports
│   │   │   ├── clients/  leads/  clinical/  nutrition/
│   │   │   ├── ai_drafting/  appointments/  messaging/
│   │   │   ├── progress/  billing/  admin/
│   │   ├── integrations/           # Ports & adapters
│   │   │   ├── whatsapp/  sms/  email/  llm/  payments/  files/
│   │   └── platform/               # Framework wiring, no domain logic
│   │       ├── db/  http/  jobs/  config/  observability/
│   ├── migrations/
│   ├── tests/
│   └── tools/
│       └── check_boundaries.py     # CI boundary enforcement (§3.5)
├── frontend/
│   ├── packages/
│   │   ├── design-system/          # 🔒 Built FIRST (NFR-056)
│   │   ├── api-client/             # Generated from OpenAPI — never hand-edited
│   │   └── shared/
│   └── apps/
│       ├── practitioner/  client-pwa/  operator/
├── docs/
└── ops/
```

**Each module directory contains** — `__init__.py` (public interface, the *only* legal import surface), `service.py` (domain logic), `models.py` (ORM), `schemas.py` (Pydantic), `router.py` (HTTP), `events.py`, `README.md` (NFR-074), `tests/`.

---

## 3. Module Boundaries and Responsibilities

### 3.1 The three layers and their dependency rule

🔒 **NFR-070** — modules communicate only through published interfaces.

```
        ┌──────────────────────────────┐
        │   KERNEL (cross-cutting)     │  ← may not import modules
        └──────────────┬───────────────┘
                       │ imported by
        ┌──────────────▼───────────────┐
        │   MODULES (domain)           │  ← may not import each other
        └──────────────┬───────────────┘
                       │ imported by
        ┌──────────────▼───────────────┐
        │   INTEGRATIONS (adapters)    │  ← imported only via kernel ports
        └──────────────────────────────┘
```

| Rule | Statement |
|---|---|
| **R1** | Kernel MUST NOT import any module |
| **R2** | A module MUST NOT import another module's internals — only its `__init__.py` |
| **R3** | 🔒 Modules MUST NOT import each other **at all** at MVP. Cross-module needs go via kernel events or an explicit orchestration seam (§3.4) |
| **R4** | Integrations MUST NOT import modules |
| **R5** | Nothing imports `platform/` except entry points |
| **R6** | A module MUST NOT read another module's tables |

**R3 is deliberately stricter than "published interfaces only."** With one developer and no code review, a permitted import is an import that will happen everywhere. Zero is enforceable; "only through the interface" is not.

### 3.2 Kernel components

| Component | Single responsibility | PRD |
|---|---|---|
| `identity` | Who is the actor. Three realms; token issue/verify | FR-M0-001…009 |
| `authz` | 🔒 **The only place a permission decision is made** | FR-M0-015, NFR-032 |
| `tenancy` | Resolve tenant from request; set the DB session variable | FR-M0-010…014 |
| `consent` | Append-only consent ledger; purpose checks | FR-M0-021…030 |
| `audit` | Append-only audit writer | FR-M0-031…036 |
| `entitlements` | Limits, metering, enforcement | FR-M0-044…046 |
| `notifications` | Transport-agnostic send port | FR-M0-041…043 |
| `storage` | File put/get with authorization | FR-M0-037…040 |
| `events` | In-process event bus (§3.4) | Enables R3 |
| `context` | Request-scoped actor, tenant, request-id | — |
| `errors` | Exception taxonomy (§17) | NFR-063 |

🔒 **NFR-072 mapping** — each named "exactly one implementation" concern has an owner: authorization → `kernel.authz` · portion conversion → `modules.nutrition` · nutritional totalling → `modules.nutrition` · message scheduling and dispatch → `modules.messaging` · entitlement checks → `kernel.entitlements` · audit writing → `kernel.audit` · tenant resolution → `kernel.tenancy`.

### 3.3 Domain modules

| Module | Owns | Must never |
|---|---|---|
| `clients` | Client record, lifecycle stage, notes, tags, timeline, search | Decide entitlements; send messages |
| `leads` | Public enquiry form, submission, source attribution | Own a separate lead entity 🔒 (M1.3) |
| `clinical` | Assessment definitions/responses, measurements, notes, documents | Interpret clinical values (FR-M5-007) |
| `nutrition` | Foods, portions, recipes, meals, templates, plans, versioning, totals, document generation | Call the LLM |
| `ai_drafting` | Prompt assembly, grounding, LLM orchestration, draft validation | Persist plans directly — hands validated structure to `nutrition` |
| `appointments` | Appointments, statuses, reschedule history | Send reminders (raises events) |
| `messaging` | 🔒 The single scheduled-message engine | Contain domain rules about *why* a message is sent |
| `progress` | At-risk detection, check-in aggregation, progress views | Own measurements (reads via events/queries) |
| `billing` | Plans as config, subscription state, invoices | Enforce limits — that is `kernel.entitlements` |
| `admin` | Operator console, food curation, tenant support views | Bypass authz or audit |

**`leads` as a separate module with no separate entity** — it owns the *public, unauthenticated* surface, which has a different security posture from everything else. The record it creates belongs to `clients`. This satisfies M1.3 while isolating the only endpoint reachable without a session.

**`ai_drafting` separate from `nutrition`** — nutrition is deterministic domain logic; AI drafting is a non-deterministic external call with its own failure and cost model (FR-M5-010, NFR-089). Merging them would make nutrition untestable without an LLM.

### 3.4 Cross-module communication

Three mechanisms, in order of preference.

**(a) Domain events — the default.** In-process, synchronous within the transaction, or deferred to the worker.

```
appointments  ──emits──▶  AppointmentScheduled  ──▶  messaging (schedules reminders)
nutrition     ──emits──▶  PlanApproved          ──▶  messaging (delivery notification)
                                                └──▶  clients   (timeline entry)
clinical      ──emits──▶  MeasurementRecorded   ──▶  progress  (recompute at-risk)
clients       ──emits──▶  StageChanged          ──▶  messaging (cancel scheduled)
                                                └──▶  entitlements (recount)
```

Publisher knows nothing about subscribers. Adding a message type requires **no change to `appointments`** — satisfying AC-M8-008.

**(b) Kernel query ports.** Where a module needs data, not a notification (e.g. `messaging` needs a client's mobile and stage), the kernel exposes a narrow read port. Not a general-purpose cross-module query API — each port is explicitly declared.

**(c) Orchestration in the HTTP layer.** For genuinely multi-module user actions (approve plan → deliver), the router calls modules in sequence. 🟡 **PROPOSAL** — permitted only in routers, never inside a module service, and only where the sequence is user-initiated.

⚠️ **Event ordering.** Events are in-process, not a distributed bus. Handlers that must be transactional run inside the caller's transaction; handlers that may fail independently (message dispatch) are enqueued as jobs. Never both.

### 3.5 Enforcement in CI

🔒 Principle 3. `tools/check_boundaries.py` runs in CI and fails the build on:

| Violation | Rule |
|---|---|
| Kernel importing a module | R1 |
| Module importing another module | R3 |
| Import of a module's internals (`modules.x.service`) | R2 |
| Integration importing a module | R4 |
| ORM model referencing another module's table | R6 |
| `supabase-js` present in frontend dependencies | ADR-02 |
| Business logic in a React component (heuristic: domain imports in `components/`) | NFR-068 |

**Why a custom script rather than a library:** ~150 lines of AST inspection with rules matching *our* module names, no dependency to maintain (NFR-078), and failure messages that name the exact rule.

---

## 4. Frontend Architecture

### 4.1 Three applications, one codebase

🔒 Three distinct audiences (P1/P2, P3, P5) with different devices, security postures and design constraints.

| App | Audience | Priority | Auth |
|---|---|---|---|
| `practitioner` | P1, P2 | Desktop-first, tablet-capable (NFR-055) | Email + password |
| `client-pwa` | P3 | 🔒 Mobile-first, offline, installable | Magic link (FR-M0-005) |
| `operator` | P5 | Desktop, minimal, deliberately austere | Email + password + 2FA |

**Why separate builds:** the client PWA must be small and fast on 4G (NFR-002). Shipping practitioner code to clients would blow the budget. Separate builds also mean **operator console code never reaches a client's browser** — a security property, not just an optimisation.

**Why one repo:** shared design system (NFR-056), shared generated API client, one dependency tree, one CI pipeline.

### 4.2 The design system is built first

🔒 **NFR-056** — direct countermeasure to V1's *"no unified design system from the beginning."*

📌 **ADR-03 — `packages/design-system` is the first frontend code written, before any feature screen**

Layers: **tokens** (colour, type scale, spacing, radii, elevation — the only place raw values exist) → **primitives** (Button, Input, Select, Modal, Toast, Table…) → **patterns** (PageHeader, EmptyState, ConfirmDialog, FormField, DataList) → **layout shells** (AppShell, DetailShell, MobileShell).

**CI rule:** feature apps MUST NOT declare raw colour or spacing values. A lint rule enforces token usage.

**Why patterns matter:** NFR-064 (every list has a designed empty state) and NFR-065 (destructive actions confirm) are only realistic if `EmptyState` and `ConfirmDialog` exist as components. Otherwise they become 40 individual acts of discipline that will not happen.

### 4.3 Information architecture as data

🔒 **NFR-057** — one declared IA drives navigation, breadcrumbs and routes, so they cannot drift.

A single declarative route manifest per app defines path, label, icon, parent, required permission and breadcrumb. Navigation, breadcrumbs, the router and permission-gated menu visibility all derive from it. **Adding a screen means adding one manifest entry** — it cannot appear in the router but be missing from navigation, which is exactly how V1's navigation became inconsistent.

### 4.4 Business logic never lives in components

🔒 **NFR-068** — V1's most damaging failure.

| Layer | May contain | Must not contain |
|---|---|---|
| **Component** | Rendering, local UI state, event handlers that call hooks | Domain calculations, business rules, direct API calls |
| **Feature hook** | Data fetching, cache keys, mutations, optimistic updates | Domain calculations |
| **API client** | Generated HTTP calls, types | Anything hand-written |
| **Backend service** | 🔒 **All domain logic** | — |

**The rule stated plainly:** if a function computes a *domain* outcome — nutritional totals, entitlement state, whether a client is at risk, whether a plan is valid — it belongs on the server. The frontend displays what the server computed.

⚠️ **Deliberate consequence:** some interactions require a round trip that a client-side calculation could avoid — notably running plan totals (FR-M4-027). **Accepted.** Duplicated portion-conversion and totalling logic across client and server is precisely NFR-072's prohibition, and a nutrition mismatch between what the practitioner sees and what the plan contains is a clinical defect. Mitigated by a debounced call and optimistic display of the last known total.

### 4.5 Typed client generation

🔒 **NFR-079** — types generated from the backend's OpenAPI schema. `packages/api-client` is **build output, never hand-edited**, and CI fails if it is stale. A backend contract change breaks the frontend build rather than production.

### 4.6 Client PWA specifics

| Requirement | Approach |
|---|---|
| Installable (FR-M7-010) | Web app manifest, service worker, maskable icons |
| Offline plan viewing (FR-M7-011) | Service worker caches the active plan document + shell |
| Offline queue (FR-M7-012) | Writes to IndexedDB with pending status; replayed on reconnect |
| ≤2.5s on 4G (NFR-002) | Aggressive code splitting; strict bundle budget in CI |
| 60-second rule (M7.3) | Route-level: today's plan is the default landing view |
| No password (FR-M0-005) | Magic-link token exchanged for a session |
| Deep links (FR-M7-013) | WhatsApp link → token → target route in one hop |

**Offline conflict resolution (EC-M7-05):** server is authoritative for plan *content*; client logs are append-only and never discarded. A queued adherence log carries its original client timestamp, so a stale queue produces correctly-dated history rather than corrupted state.

⚠️ **iOS PWA limits.** Home-screen install required before push; storage may be evicted after ~7 days of inactivity. This is why web push is Phase 2 (FR-M7-016) and WhatsApp is the notification channel. **Offline is an enhancement; the product must never depend on it.**

---

## 5. Backend Architecture

### 5.1 Request lifecycle

Every authenticated request passes through the same pipeline. 🔒 There is no bypass.

```
Request
  │
  ├─ 1. Request context      → request-id, timing
  ├─ 2. Authentication       → kernel.identity: verify token, resolve actor + realm
  ├─ 3. Tenant resolution    → kernel.tenancy: derive tenant, SET LOCAL app.tenant_id
  ├─ 4. Transaction begin    → session bound to request
  ├─ 5. Authorization        → kernel.authz: can(actor, action, resource) — DENY BY DEFAULT
  ├─ 6. Entitlement check    → kernel.entitlements: only for metered actions
  ├─ 7. Validation           → Pydantic
  ├─ 8. Module service       → 🔒 all domain logic here
  ├─ 9. Audit                → kernel.audit: automatic for mutations
  ├─ 10. Events              → transactional handlers inline; others enqueued
  ├─ 11. Commit
  └─ 12. Response
```

**Steps 2, 3, 5, 9 are framework-level, not per-endpoint.** A developer cannot forget them, because they are not written per endpoint. This is the structural answer to V1's *"permissions became difficult to maintain."*

### 5.2 Layer responsibilities

| Layer | Responsibility | Forbidden |
|---|---|---|
| **Router** | HTTP concerns, declare the authz action, call one service, orchestrate multi-module sequences (§3.4c) | Domain logic, direct DB access |
| **Service** | 🔒 All domain logic. Transaction-agnostic | HTTP awareness, direct external calls |
| **Repository** | Data access for its own module | Cross-module queries (R6) |
| **Model** | ORM mapping | Behaviour beyond trivial derivation |
| **Schema** | Validation and serialisation | Domain rules |

### 5.3 Transaction model

📌 **ADR-04 — one transaction per request, owned by the HTTP layer**

Services never commit. The pipeline commits on success, rolls back on any unhandled error. This makes multi-step operations atomic without services knowing about each other.

**Consequences:**
- Job enqueue is **transactional** — a job is only queued if its transaction commits. No orphan work (a genuine advantage of a Postgres-backed queue, §11).
- Audit writes participate in the transaction — a rolled-back mutation leaves no audit entry, because it did not happen.
- Long external calls (LLM, WhatsApp) **MUST NOT** occur inside a request transaction. They run in jobs (§11).

### 5.4 Testability

🔒 NFR-073 — every module tested against its acceptance criteria.

| Level | Scope | Speed |
|---|---|---|
| Unit | Service logic, pure functions (portion conversion, totals, at-risk rules) | ms |
| Module | Service + real Postgres, one module | fast |
| Contract | Every integration adapter against a recorded fixture | fast |
| API | Full pipeline including authz and tenancy | moderate |
| E2E | Playwright, Journeys J1–J3 | slow, CI only |

🔒 **Two mandatory test categories**, given the failure modes that matter most:

1. **Tenant isolation tests** (AC-M0-003) — deliberately remove the application filter and assert RLS still blocks the read. This is the only way to verify defence-in-depth is real.
2. **Safety tests** (AC-M5-003) — a corpus of assessments with allergies and dietary exclusions, asserting no violation appears in any generated draft.

---

## 6. Authentication & Authorization

### 6.1 Three identity realms

🔒 FR-M0-004 — a credential in one realm MUST NOT authenticate into another.

| Realm | Who | Method | Session | 2FA |
|---|---|---|---|---|
| `practitioner` | P1, P2, P4 | Email + password | Longer-lived, refreshable | Phase 2 |
| `client` | P3 | Magic link (FR-M0-005) | Short-lived | N/A |
| `operator` | P5 | Email + password | **Short-lived** | 🔒 Mandatory (FR-M0-009) |

**Mechanism:** realm is a claim in the token *and* enforced by separate signing keys per realm. A practitioner token presented to an operator endpoint fails signature verification, not merely a claim check — AC-M11-005 satisfied cryptographically rather than by an `if` statement.

🟡 **PROPOSAL — separate signing keys per realm.** The PRD requires realm separation; per-realm keys are my mechanism choice. Cost: three keys in configuration. Benefit: realm confusion becomes structurally impossible.

### 6.2 Magic links (client realm)

Requirements: single-use (EC-M0-01), time-limited (AC-M0-007), self-re-requestable (EC-M7-01), audited on consumption.

Flow: `messaging` sends a WhatsApp deep link containing an opaque token → client opens it → token verified, marked consumed, exchanged for a short-lived session → subsequent requests use the session.

🟡 **PROPOSAL — validity periods.** Not specified in the PRD. Recommended: link valid **7 days or until used**; resulting session **30 days**. Rationale: clients receive a link, open it days later, and must not be locked out; but a forwarded link must not grant indefinite access. Needs your ratification.

### 6.3 The single authorization decision point

🔒 **FR-M0-015 / NFR-032** — the most load-bearing rule in the system.

📌 **ADR-05 — policy-based authorization, one function, deny by default**

```
kernel.authz.can(actor, action, resource) → Allow | Deny(reason)
```

- **One function.** No module implements permission logic (R3 + CI check §3.5).
- **Deny by default** (FR-M0-019) — an unmapped action is denied, not permitted.
- **Declarative** — actions are declared on routers, so the permission surface is enumerable by reading the manifest, not by auditing code paths.
- **Testable in isolation** — the full matrix (4 roles × N actions × ownership conditions) is a table test, not an integration test.

**Why not decorators alone:** a decorator can be forgotten. The pipeline (§5.1 step 5) *requires* a declared action; an endpoint without one fails at startup, not at runtime.

**Role model at MVP** (FR-M0-016): `Owner` (all clients in tenant), `Practitioner` (assigned clients only, FR-M0-017), `Client` (own records only, FR-M0-018), `PlatformOperator` (cross-tenant, read-only by default, fully audited).

**Ownership is part of the decision, not a separate filter.** "Practitioner may read a client" is insufficient — it must be "may read *this* client." Conflating the two is how row-level leaks appear in role-based systems.

### 6.4 Defence in depth

| Layer | Protects against | Mechanism |
|---|---|---|
| 1. Authorization | Wrong actor, wrong action | `kernel.authz`, deny by default |
| 2. Tenant session variable | Cross-tenant access | `SET LOCAL app.tenant_id` per request |
| 3. **RLS policies** | 🔒 **Forgotten application filter** | Postgres policies (§7) |
| 4. Audit | Undetected access | Append-only log |

⚠️ Layer 3 exists precisely because a solo developer will eventually forget a `WHERE tenant_id`. NFR-030 requires the guarantee to live below the application layer.

---

## 7. Multi-Tenancy Model

### 7.1 Shared database, shared schema, RLS isolation

📌 **ADR-06 — shared schema with row-level security**

| Model | Verdict |
|---|---|
| Database per tenant | ❌ 200 databases, 200 migration runs, unmanageable solo. Violates NFR-087 |
| Schema per tenant | ❌ Same migration problem at smaller scale |
| **Shared schema + RLS** | ✅ **Chosen.** One migration path, one connection pool, isolation enforced by Postgres (NFR-030) |
| Shared schema, app-filtered only | ❌ One forgotten filter = cross-tenant breach. Rejected by NFR-030 |

**Trade-off accepted:** weaker physical isolation than database-per-tenant. Appropriate at 200 tenants (NFR-092) and a documented revisit trigger (§18).

### 7.2 How isolation works

1. Request arrives → `kernel.tenancy` resolves the tenant from the actor's token.
2. Pipeline issues `SET LOCAL app.tenant_id = <uuid>` on the request's connection.
3. Every tenant-scoped table carries a `tenant_id` and an RLS policy comparing it to that setting.
4. `SET LOCAL` is transaction-scoped, so it cannot leak across pooled connections.

**The application still filters by tenant** — RLS is the seatbelt, not the steering. Belt-and-braces is the point.

⚠️ **Connection pooling constraint.** `SET LOCAL` requires the transaction and the query to share a connection. Supabase's transaction-mode pooler is compatible; **session-mode pooling with connection reuse across requests would break isolation.** This must be verified in Phase 11 as a launch gate, not assumed.

### 7.3 Data classification

| Class | Tenant-scoped | RLS | Examples |
|---|---|---|---|
| **Tenant data** | ✅ | ✅ | Clients, plans, assessments, appointments, messages |
| **Curated platform data** | ❌ | Read-all, operator-write | Curated foods, portions, seed recipes, assessment definitions |
| **Tenant-custom data** | ✅ | ✅ | Custom foods, meals, templates |
| **Platform operational** | ❌ | Operator-only | Tenants, plans-as-config, audit, job queue |

🔒 **Curated and custom foods share one structure with an ownership discriminator** (M4.3) — one table, `tenant_id` nullable, null meaning curated/global. RLS policy: readable if `tenant_id IS NULL OR tenant_id = current_setting(...)`. This satisfies "no duplicate tables" while keeping custom foods tenant-private (FR-M4-013).

### 7.4 Clinic readiness

🔒 P4 must be supported by the data model without rearchitecting, though the interface is Phase 3.

Structure: **tenant** (billing and isolation boundary) → **users** with roles (`Owner`, `Practitioner`) → **clients** with an owning practitioner (FR-M1-009) plus optional explicit grants (EC-M0-04).

A solo practitioner is simply a tenant with one user who is both Owner and Practitioner. **No special case, no migration when they hire someone** — this is what "clinic-ready architecture" means concretely.

---

## 8. Nutrition Engine Architecture

🔒 M4 is the wedge and the moat. This is the most domain-dense module in the system.

### 8.1 Internal structure

`nutrition` is one module with four internal concerns, each with a single owner:

| Concern | Responsibility | NFR-072 |
|---|---|---|
| **Food catalogue** | Curated + custom foods, search, categories, dietary classification | — |
| **Portion conversion** | 🔒 Household measure ↔ grams | **The only implementation** |
| **Composition** | 🔒 Nutritional totalling across food → meal → day → plan | **The only implementation** |
| **Plan authoring** | Slots, versioning, templates, alternatives, document generation | — |

### 8.2 The composition hierarchy

Per M4.3, six levels. Architecturally the critical property is **how nutrition values resolve**:

```
Food ──has many──▶ Portion (measure → grams, per food)
  │
  ├─ referenced by ─▶ Recipe ─┐
  ├─ referenced by ─▶ Meal ───┼──▶ Plan Slot ──▶ Plan Day ──▶ Plan
  └───────────────────────────┘
```

📌 **ADR-07 — nutrition values are computed on read, snapshotted on issue**

The tension: FR-M4-027 wants live totals as the practitioner edits; EC-M4-03 requires an issued plan to retain the values in force when it was issued, even after a curated food is corrected.

| Approach | Verdict |
|---|---|
| Always compute from current food values | ❌ Correcting a curated food silently rewrites history. Violates EC-M4-03 |
| Always store computed totals | ❌ Stale during editing; duplicates the source of truth |
| **Compute while `Draft`; snapshot on `Issued`** | ✅ **Chosen.** Live during authoring; immutable once delivered |

**Mechanism:** an issued plan version stores the resolved food references *and the composition values used*. The client's plan document renders from the snapshot. New plans use current values. A curated correction (EC-M11-03) affects future plans only, and the correction is audited.

### 8.3 Portion conversion — the single implementation

🔒 FR-M4-004 / NFR-072.

Every quantity in the system is `(food_id, measure, count)`. Conversion to grams is **per food**, never global (FR-M4-003) — 1 katori of cooked dal and 1 katori of dry poha are different weights.

**Architectural rule:** every path that needs grams calls `nutrition.portions.to_grams(...)`. There is no second implementation — not in the frontend (§4.4), not in `ai_drafting`, not in document generation.

⚠️ **Foods without a portion mapping.** A custom food (FR-M4-012) may be created with grams only. The system must degrade gracefully: quantity expressible in grams, household measures unavailable for that food, and no error. 🟡 **PROPOSAL** — prompt the practitioner to optionally supply a household equivalent at creation, since they know it and we do not.

### 8.4 Food search performance

🔒 NFR-004 — ≤300ms, the most repeated interaction in the product.

**Requirements:** as-you-type (FR-M4-008), vernacular/regional name matching (FR-M4-009), dietary filtering applied (FR-M4-035), curated + tenant-custom in one result set.

📌 **ADR-08 — Postgres full-text search with a synonym-bearing search vector; no external search service**

| Option | Verdict |
|---|---|
| `ILIKE '%term%'` | ❌ No ranking, no synonyms, poor at scale |
| **Postgres FTS + trigram, precomputed vector** | ✅ **Chosen.** No new infrastructure (NFR-077), meets budget at expected size |
| Elasticsearch / Typesense | ❌ Violates NFR-077 and NFR-087 for a few thousand rows |

**Vernacular matching** is a data problem, not a search-engine problem: each food carries alternate names (`chapati`/`roti`/`phulka`) folded into its search vector. This is curation work (M11 FR-M11-010), and it is *why* the moat is defensible — it cannot be scraped.

**Dietary filtering must be applied in the query, not after** — filtering post-retrieval breaks ranking and result counts.

### 8.5 Plan versioning

🔒 FR-M4-033 — revision must not destroy the issued version; this is the core of the weekly follow-up loop (J2).

Model: a **Plan** (client + intent) has ordered **Plan Versions**. Only one is `Active`; prior versions are immutable and retrievable (AC-M4-008). "Revise" copies the current version into a new `Draft`, so the practitioner edits from where they were rather than from blank.

**EC-M7-03** (client viewing a plan when it is revised): the client's view is pinned to the version they loaded; the PWA is told a newer version exists and offers refresh. **Content is never silently swapped** — a client following a plan must not have it change under them mid-meal.

### 8.6 Document generation

🔒 FR-M4-038 — output quality is a switching blocker (AC-M4-010).

📌 **ADR-09 — server-side HTML→PDF via headless browser, executed in the worker**

| Option | Verdict |
|---|---|
| Client-side (jsPDF etc.) | ❌ Inconsistent across devices; duplicates layout logic |
| Python PDF libraries | ❌ Poor Indic script and typography support; slow to iterate on design |
| **Headless-browser HTML→PDF in worker** | ✅ **Chosen.** Same design system as the web view; excellent Unicode/Indic rendering (NFR-101); iterate in HTML |
| Third-party PDF API | ❌ Recurring cost, and clinical content leaves our boundary |

**Runs in the worker** (§11): browser rendering is memory-heavy and slow (NFR-007 allows 5s). It must never occupy a web process.

⚠️ **RISK — memory.** A headless browser is the single largest memory consumer in the system and can exhaust a small worker. Mitigations: one render at a time per worker, hard timeout, restart the browser process periodically. If it proves unstable on the target instance size, the fallback is a dedicated micro-worker — a cost decision, not an architectural change.

---

## 9. AI Integration Architecture

🔒 M5 is the differentiator, and the highest-risk subsystem for both safety (§M5.3) and cost (NFR-089).

### 9.1 Pipeline

📌 **ADR-10 — deterministic grounding and deterministic validation; the model only fills a constrained middle**

```
1. CONTEXT ASSEMBLY        (deterministic)
   assessment + measurements + dietary rules + allergies
   + practitioner's templates & saved meals + candidate food set
        ↓
2. CANDIDATE FOOD SET      (deterministic)  🔒 grounding boundary
   query nutrition module → only foods this tenant may use,
   already filtered by dietary class, allergies, exclusions
        ↓
3. PROMPT ASSEMBLY         (versioned, deterministic)
   system prompt + constraints + candidate set + practitioner style
        ↓
4. MODEL CALL              (external, in worker, timeout-bounded)
        ↓
5. STRUCTURAL VALIDATION   (deterministic)  🔒 the safety gate
   • every food id ∈ candidate set        → else reject (EC-M5-04)
   • no allergen present                  → else reject (FR-M5-006)
   • dietary class respected              → else reject
   • no advisory/diagnostic language      → else filter (EC-M5-05)
   • schema valid                         → else fail (EC-M5-03)
        ↓
6. PERSIST AS DRAFT        (nutrition module owns the write)
   🔒 state = Draft. No delivery path exists from here (FR-M5-003)
        ↓
7. PRACTITIONER REVIEW     (standard plan builder, FR-M5-004)
```

**The critical property: steps 1, 2 and 5 are ordinary code, not prompt instructions.** FR-M5-006 (absolute allergy enforcement) is a safety requirement, and *"the model was told not to"* is not enforcement. The model can only select from a pre-filtered set, and anything outside it is rejected before persistence.

### 9.2 Grounding

🔒 FR-M5-002 — grounded in the food database and the practitioner's own templates.

The candidate set is assembled by `nutrition`, not by the LLM. It contains only: foods the tenant may use, already excluding allergens and dietary violations, prioritising foods appearing in that practitioner's own templates and saved meals.

**This is what makes "AI that writes plans in your style" true rather than marketing.** A practitioner whose templates use bajra roti and moong dal receives drafts using those, because they dominate the candidate set.

⚠️ **Cold start.** A new practitioner has no templates. Their candidate set falls back to curated foods matched to the client's regional cuisine and dietary class (§9.9 of the PRD). Drafts will be more generic until their library accumulates — which is honest, and also creates the accumulating-moat effect (M4.2).

### 9.3 Provider abstraction

🔒 Principle 7. `integrations/llm` exposes one port; the provider is configuration.

**Recorded per generation** (FR-M5-005): provider, model identifier, prompt version, token counts, latency, outcome. This is what makes cost attributable per tenant (NFR-088) and quality regressions diagnosable when a prompt or model changes.

🟡 **PROPOSAL — provider choice deferred to Phase 7.** Model capability and pricing move fast; committing now risks being wrong by implementation. The architecture requires only that the provider supports structured output and is reachable from our region.

### 9.4 Cost control

🔒 NFR-089 / NFR-090 — no tenant may cost more than they pay.

| Control | Mechanism |
|---|---|
| Per-tenant quota | `kernel.entitlements` checked **before** the call (FR-M5-009) |
| Failed generation is free | Quota consumed only on successful validation (FR-M5-010, EC-M10-04) |
| Caching (NFR-090) | 🟡 **PROPOSAL** — cache on a hash of the grounding inputs; identical inputs reuse the prior draft |
| Token ceiling | Hard max per request; oversized context truncates the candidate set, never the constraints |
| Anomaly alerting | Per-tenant cost monitored (NFR-085) |

⚠️ **Caching caveat.** Two clients with identical assessments are rare, so hit rates will be low. Its real value is protecting against repeated regeneration (EC-M5-06). I would not count on caching for material cost reduction — quotas are the primary control.

### 9.5 Safety controls, and where each lives

🔒 §M5.3 is non-negotiable. Mapping each rule to its enforcement point:

| Rule | Enforced by | Not by |
|---|---|---|
| Human approval (FR-M5-003) | Plan state machine — no transition `Draft → Delivered` exists | Prompt |
| Grounding (FR-M5-002) | Candidate set + validation step 5 | Prompt |
| Allergy exclusion (FR-M5-006) | Pre-filter + post-validate | Prompt |
| No diagnosis (FR-M5-007) | Output filter + prompt constraints | Prompt alone |
| CKD / eating disorder (OD-06) | ⚠️ **Blocked pending clinical decision** | — |

⚠️ **OD-06 is an open decision that affects this module's control flow.** The architecture accommodates either outcome via a pre-generation eligibility check in step 1 — but the rule itself must come from a clinician. Flagged for Phase 4/5.

---

## 10. Scheduled Message Engine Architecture

🔒 M8.3 — one engine, not three. This is the most important anti-duplication decision in the system.

### 10.1 Structure

```
     DOMAIN EVENTS                    SCHEDULING                  DISPATCH
  ┌──────────────────┐          ┌─────────────────┐        ┌──────────────┐
  │ AppointmentSched │─────────▶│                 │        │              │
  │ PlanApproved     │─────────▶│  Message        │───────▶│  Transport   │
  │ ClientActivated  │─────────▶│  Scheduler      │        │  Port        │
  │ LeadSubmitted    │─────────▶│                 │        │              │
  │ StageChanged     │──cancel─▶│  • due time     │        │ whatsapp │   │
  └──────────────────┘          │  • template+ver │        │ sms      │   │
                                │  • recipient    │        │ email    │   │
  ┌──────────────────┐          │  • idempotency  │        │ push(P2) │   │
  │ Recurring rules  │─────────▶│    key          │        └──────┬───────┘
  │ (check-ins)      │          └────────┬────────┘               │
  └──────────────────┘                   │                        ▼
                                         ▼                 ┌──────────────┐
                              ┌────────────────────┐       │ Delivery Log │
                              │   SUPPRESSION      │       │ (immutable)  │
                              │ ✗ stage not Active │       └──────────────┘
                              │ ✗ consent withdrawn│
                              │ ✗ tenant suspended │
                              │ ✗ quota exceeded   │
                              │ ✗ quiet hours      │
                              │ ✗ frequency cap    │
                              └────────────────────┘
```

**Every message in the product is a row in this engine.** Adding a message type requires a template and a schedule rule — no new infrastructure (AC-M8-008).

### 10.2 Suppression is centralised

🔒 The six suppression rules (FR-M8-005…009) are evaluated in **one place**, immediately before dispatch — not at scheduling time.

**Why at dispatch, not scheduling:** a client's stage, consent or tenant status can change between scheduling and send. A check-in scheduled on Monday for Friday must be suppressed if the client is paused on Wednesday. Checking only at scheduling time would send it anyway — a direct violation of FR-M8-005 and exactly the kind of bug that erodes practitioner trust.

Suppressed messages are **recorded as suppressed with a reason** (AC-M8-004), never silently dropped.

### 10.3 Idempotency

🔒 EC-M8-06 — a retry must never deliver twice.

Every scheduled message carries an idempotency key derived from `(tenant, recipient, template, logical occasion)`. Dispatch is conditional on the key not already being marked sent, enforced by a unique constraint in the database — not by application logic, which can race.

### 10.4 Staleness

🔒 EC-M8-05 — a check-in more than 24h late is dropped and logged, not sent.

Each message type declares a **staleness tolerance**: appointment reminders are worthless after the appointment; check-ins are noise if a day late; magic links and transactional messages are never stale. On worker recovery, overdue messages beyond tolerance are logged as `expired`, not delivered.

**Why this matters:** without it, a 6-hour outage produces a burst of confusing, contextually wrong messages to every client — a reputational failure for practitioners (US-M8-04) worse than the outage itself.

### 10.5 Quiet hours and frequency

- **Quiet hours** (FR-M8-009, 08:00–21:00): a message due outside the window is **deferred to the next permitted window**, not dropped (AC-M8-006).
- **Frequency cap** (FR-M8-008, EC-M8-09): applied per recipient across *all* message types. Where two are due, the lower-priority one defers. Priority is declared per template.

⚠️ Transactional messages (magic links, EC-M10-04) are **exempt from quota and frequency limits** — a client locked out of their portal because their practitioner hit a marketing quota is unacceptable. Exemption is a property of the template, declared once.

---

## 11. Background Jobs and Workers

### 11.1 Postgres-backed queue

📌 **ADR-11 — job queue in Postgres; no Redis, no Celery, no broker**

🔒 NFR-077 forbids infrastructure without demonstrated need.

| Option | Verdict |
|---|---|
| Celery + Redis | ❌ Two more moving parts, another failure mode, ~₹800+/mo. Unjustified below thousands of jobs/minute |
| External queue service | ❌ Cost, and another vendor |
| **Postgres `SKIP LOCKED` queue** | ✅ **Chosen.** Zero new infrastructure. Transactional enqueue (§5.3). Trivially inspectable with SQL |
| In-process background tasks | ❌ Lost on deploy; no retry; no visibility |

**Volume check:** at 200 tenants — roughly a few thousand messages, a few hundred documents and a few hundred AI generations *per day*. Postgres handles this comfortably. Revisit trigger documented in §18.

**Advantage that matters most here:** transactional enqueue. A job is queued only if its transaction commits, so an approved plan cannot fail to schedule its delivery, and a rolled-back approval cannot leave a ghost job. With an external broker this requires an outbox pattern — extra machinery we get for free.

### 11.2 Job classes

| Class | Examples | Timeout | Retry | Priority |
|---|---|---|---|---|
| **Dispatch** | Send WhatsApp/SMS/email | 30s | Yes, backoff | High |
| **Generation** | AI drafts | 60s | ❌ **No** — cost | Normal |
| **Rendering** | Plan PDFs | 60s | Yes, ×2 | Normal |
| **Recurring** | Check-in scheduling, at-risk recompute | 300s | Yes | Low |
| **Maintenance** | Retention purge, quota reset | 600s | Yes | Lowest |

⚠️ **AI generation is not auto-retried.** Each attempt costs money, and a failure is usually deterministic (malformed output, provider rejection). The practitioner retries explicitly, and FR-M5-010 guarantees they can proceed manually meanwhile.

### 11.3 Scheduler

A single ticker in the worker process claims due work. 🟡 **PROPOSAL — 60-second tick.** NFR-009 allows 60s for dispatch; a 60s tick with immediate-claim on enqueue satisfies it without polling pressure.

⚠️ **Single-worker assumption.** At MVP one worker process runs. `SKIP LOCKED` makes multiple workers safe if added later, and recurring-job claims use a lock to prevent duplicate scheduling. **This must be correct from the start** — retrofitting concurrency safety is far harder than building it in.

### 11.4 Failure handling

Retries use exponential backoff with a bounded attempt count (FR-M8-004). Terminal failures move to a **dead-letter state**, visible in the operator console (FR-M11-004) — never silently discarded (AC-M8-007).

🔒 **Jobs are not lost on deploy** (NFR-025): a job claim has a lease; a worker killed mid-job releases its lease on timeout and the job is re-claimed. Combined with idempotency (§10.3), re-execution is safe.

---

## 12. Integration Layer

### 12.1 Ports and adapters

🔒 Principle 7 — every third party sits behind an interface we own.

| Port | Adapters | Why the port exists |
|---|---|---|
| `notifications` | WhatsApp, SMS, email, push (P2) | ASM-03 could fail; transports must be swappable |
| `llm` | Provider TBD (§9.3) | Model landscape moves fast |
| `payments` | Razorpay | Two domains (§12.4); regional expansion later |
| `files` | Supabase Storage | Storage is commodity |

🔒 **FR-M0-041 — modules MUST NOT integrate a transport directly.** `appointments` does not know WhatsApp exists; it emits an event, and `messaging` chooses a transport.

**Every adapter has a contract test** against a recorded fixture (§5.4), so an adapter can be replaced with confidence, and CI does not depend on third-party availability.

### 12.2 WhatsApp — the delivery layer

🔒 M8.4 — in India, WhatsApp is the delivery layer, not an integration.

| Constraint | Architectural response |
|---|---|
| Templates need pre-approval | Template registry stores the provider's approved template id + version; content is not free-form |
| Approval can be revoked (EC-M8-03) | Per-template health state; one suspended template does not affect others |
| Cost is the dominant variable (NFR-088) | Metered per tenant; quota enforced pre-send |
| Policy violations risk the number | Only registered templates may be sent; no ad-hoc messaging path exists |
| Not all numbers are on WhatsApp (EC-M8-01) | Transport result recorded; SMS fallback is Phase 2 (FR-M8-043) |
| Inbound replies (EC-M8-07) | 🔒 MVP: no inbound handling. Replies reach the practitioner's own WhatsApp. Webhook endpoint exists but only records delivery status |

⚠️ **ASM-03 is a launch-blocking dependency.** Meta Business Verification takes 2–6 weeks and can be rejected. The port abstraction means engineering is not blocked — SMS/email adapters can carry MVP messages if verification slips — but the *product* depends on WhatsApp, and no architecture fixes that.

### 12.3 Two payment domains

🔒 The most important boundary in `billing` — conflating these would be an architectural and regulatory error.

| Domain | Who is merchant | Module | Rule |
|---|---|---|---|
| **SaaS billing** | 🔒 We are | `billing` | Our Razorpay account. Subscriptions, invoices (FR-M10-011) |
| **Practitioner→client** | 🔒 Practitioner is | `billing` (separate ledger) | Method-agnostic recording. **We never custody funds** |

**MVP posture:** SaaS collection is manual (M10.3) — the operator records a payment and activates the plan (FR-M10-008). Practitioner→client payments are **recorded, not processed** — UPI, PhonePe, Google Pay, bank transfer, cash all reduce to a payment record with a method label.

**Architecturally this means the ledger is method-agnostic from day one**, and gateway integration later becomes an *additional source of payment records*, not a redesign.

### 12.4 Email and SMS

Both exist at MVP as fallback transports (FR-M0-041) and for practitioner-facing notifications where WhatsApp is unsuitable.

⚠️ **SMS requires TRAI DLT registration in India** — sender ID and template registration with a telecom operator. Another calendar dependency, not an engineering one. 🟡 **PROPOSAL — treat SMS as optional at MVP** and rely on WhatsApp + email, adding SMS once DLT registration completes. This avoids a second long-lead external dependency on the critical path.

### 12.5 Failure isolation

🔒 Every external call happens in a job (§5.3), never in a request. Consequences: an outage never blocks a practitioner's work; retries are automatic and bounded; failures are visible in the delivery log; and a slow provider cannot exhaust web capacity.

**Circuit-breaking** 🟡 **PROPOSAL** — after N consecutive failures for a provider, pause dispatch and alert (NFR-085), rather than burning retries and quota against a dead endpoint (EC-M8-10).

---

## 13. File Storage Strategy

### 13.1 Model

🔒 FR-M0-037…040, NFR-035.

| Property | Approach |
|---|---|
| Buckets | **Private only.** No public bucket exists |
| Access | 🔒 Always via our API. Authorization checked per request (NFR-035) |
| Delivery | Short-lived signed URL issued *after* authorization |
| Path | Tenant-scoped, opaque identifiers — never guessable or enumerable |
| Upload | Constrained by type and size (FR-M0-039, NFR-036); never executable |
| Quota | Enforced per tenant by `kernel.entitlements` (FR-M0-040) |

🔒 **"Unguessable URL" is explicitly not sufficient** (FR-M0-038). Every retrieval is authorized; the signed URL is a delivery mechanism with a short life, not the access-control mechanism.

### 13.2 Content classes

| Class | Contains clinical data | Retention |
|---|---|---|
| Client documents (labs, reports) | ✅ Yes | Tenant lifetime + retention policy |
| Generated plan documents | ✅ Yes | Plan version lifetime |
| Practitioner branding | ❌ No | Tenant lifetime |
| Assessment attachments | ✅ Yes | With the assessment |

⚠️ **Clinical files are subject to DPDP erasure (FR-M0-027).** Deleting a database row while its file persists in object storage is a compliance failure and a common oversight. **Erasure must traverse storage**, and this is a launch-gate test, not an assumption.

### 13.3 Uploads

📌 **ADR-12 — direct-to-storage upload with a server-issued, scoped, short-lived credential**

Routing file bytes through FastAPI wastes memory and request capacity on a small instance. Instead: client requests permission → server authorizes, checks quota and type, issues a scoped upload credential → client uploads directly → server records the file on confirmation.

⚠️ **The record is created on confirmation, not on request** — so an abandoned upload leaves an orphan object, not a phantom database row. 🟡 **PROPOSAL** — a maintenance job reaps unconfirmed objects after 24h.

---

## 14. Caching Strategy

### 14.1 Position: almost none, deliberately

📌 **ADR-13 — no cache server; four narrowly-scoped caches only**

🔒 NFR-093 forbids a caching tier at this scale. Caching is the most common source of stale-data bugs, and a solo developer debugging a cache-coherence problem at 2am is exactly what Principle 1 exists to prevent.

| Cache | Where | Invalidation | Justification |
|---|---|---|---|
| **Static assets** | CDN/browser | Content hash | Free, no coherence risk |
| **Client PWA shell + active plan** | Service worker | Version-aware (§4.6) | Required by FR-M7-011 |
| **Curated food search vector** | Postgres materialised column | On curation write | Precomputation, not a cache tier |
| **AI grounding results** | Database | Input hash (§9.4) | Cost control (NFR-090) |

**Not cached:** client lists, plans, entitlement state, authorization decisions, timelines. All are cheap Postgres queries at this scale, and all are correctness-critical.

**Revisit trigger:** if NFR-004 (food search ≤300ms) fails at real data volume, the first response is index tuning, not a cache. See §18.

---

## 15. Security Architecture

### 15.1 Layers

| # | Layer | Controls |
|---|---|---|
| 1 | Transport | TLS everywhere (NFR-027); HSTS |
| 2 | Identity | Three realms, separate keys (§6.1); hashed passwords (NFR-029); operator 2FA (NFR-041) |
| 3 | Authorization | Single decision point, deny by default (NFR-032) |
| 4 | Tenancy | Session variable + RLS (NFR-030) |
| 5 | Input | Server-side validation always (NFR-037); parameterised queries only (NFR-038) |
| 6 | Data | Encrypted at rest (NFR-028); no clinical data in logs (NFR-033) |
| 7 | Files | Authorized retrieval; type/size limits; non-executable (NFR-035/036) |
| 8 | Rate limiting | Auth and public endpoints (NFR-039) |
| 9 | Audit | Immutable, append-only (NFR-047, FR-M0-034) |
| 10 | Supply chain | Pinned dependencies, vulnerability monitoring (NFR-040) |

### 15.2 PII and clinical data in observability

🔒 **NFR-033 — the most easily violated rule in the document.**

⚠️ Clinical data in a log file is a breach that database encryption does not prevent. Log aggregation is frequently the least-protected component in a stack.

**Structural controls:**
- Log **identifiers, never values.** `client_id=<uuid>`, never a name, phone, weight, condition or plan content.
- Sentry configured with `send_default_pii=False` and a `before_send` scrubber.
- 🟡 **PROPOSAL** — a CI check flagging log statements that interpolate known-sensitive field names. Heuristic, imperfect, and still worth having.
- Exception messages must not echo request bodies.

### 15.3 The public attack surface

The only unauthenticated endpoints: the public enquiry form (`leads`), magic-link redemption, WhatsApp delivery webhook, and health checks.

🔒 This is why `leads` is a separate module (§3.3) — the unauthenticated surface is small, enumerable, and reviewable in one place. Each endpoint is rate-limited (NFR-039), spam-protected (FR-M2-008), and unable to read existing data.

⚠️ **The enquiry form is the highest-risk endpoint** — unauthenticated, writes to the database, and reveals tenant existence by design. **It must never disclose whether a submitted mobile matches an existing client** (EC-M2-02 appends server-side, silently).

### 15.4 The operator console

⚠️ The highest-privilege, cross-tenant surface (M11.3).

| Control | Mechanism |
|---|---|
| Separate realm + key | §6.1 — cryptographic, not conditional |
| Mandatory 2FA | FR-M0-009; no bypass path exists |
| Read-only default | Mutations are a distinct, separately-authorised action set |
| Every read audited | FR-M0-032 — the only place reads are audited |
| Minimum clinical exposure | Aggregate views serve most support cases (FR-M11-003) |
| No cross-tenant export | AC-M11-008 — no such endpoint exists |
| Impersonation | 🔒 **Phase 2** (FR-M11-012), not MVP — deliberately deferred until audit is proven in production |

### 15.5 Secrets

🔒 NFR-034. Secrets live in the platform's environment configuration, never in source. Distinct credentials per environment. Rotation must not require a code change. 🟡 **PROPOSAL** — a CI secret-scanner on every commit; recovering from a leaked key in git history is far more expensive than preventing it.

---

## 16. Deployment Architecture

### 16.1 Topology

🔒 NFR-087 — fixed infrastructure under ₹5,000/month at up to 200 tenants.

```
   Users (India)
        │
        ▼
   ┌─────────────────────┐
   │  CDN / Static Host  │  practitioner · client-pwa · operator
   │  (free tier)        │  three static bundles
   └──────────┬──────────┘
              │ /api/*
              ▼
   ┌─────────────────────┐      ┌─────────────────────┐
   │  WEB PROCESS        │      │  WORKER PROCESS     │
   │  FastAPI (uvicorn)  │      │  same image         │
   │  Mumbai region      │      │  jobs + scheduler   │
   └──────────┬──────────┘      └──────────┬──────────┘
              │                             │
              └──────────────┬──────────────┘
                             ▼
              ┌──────────────────────────────┐
              │  Supabase (Mumbai)           │
              │  Postgres · Auth · Storage   │
              │  daily backups (NFR-023)     │
              └──────────────────────────────┘
                             │
                             ▼
              External: WhatsApp · LLM · Razorpay · Email
```

### 16.2 Cost model

🟡 **PROPOSAL — figures are estimates requiring verification (ASM-08).**

| Item | ₹/month | Notes |
|---|---|---|
| Supabase Pro (Mumbai) | ~2,100 | Postgres + Auth + Storage + daily backups |
| Web process | ~600 | Small always-on instance |
| Worker process | ~600 | Small always-on instance |
| Static hosting + CDN | 0 | Free tier is sufficient for three SPAs |
| Error tracking | 0 | Free tier |
| Domain + TLS | ~100 | TLS included |
| **Fixed total** | **~3,400** | ✅ Within the ₹5,000 ceiling |

**Variable (outside the ceiling, scales with revenue):** WhatsApp ₹60–90/tenant · LLM ~₹15–40/tenant · payment gateway ~2% of collections. At 200 tenants: ~₹15,000–26,000/month variable against ~₹3.6 lakh revenue.

⚠️ **Headroom is ~₹1,600/month.** The most likely pressure points are worker memory (PDF rendering, §8.6) and Supabase compute. If the worker needs a larger instance the ceiling is breached — a cost decision, not an architectural failure. Flagging it now so it is not a surprise.

### 16.3 Environments

| Environment | Purpose | Data |
|---|---|---|
| Local | Development | Seeded synthetic |
| Staging | Pre-release verification | 🔒 Synthetic only — **never production clinical data** |
| Production | Live | Real |

🔒 NFR-075 — identical configuration shape across environments; only values differ.

⚠️ **Staging costs money.** 🟡 **PROPOSAL** — run staging on free/hobby tiers and accept that it is not performance-representative. Its purpose is correctness verification (migrations, integrations, auth flows), not load testing. A shared-nothing staging database is non-negotiable regardless.

### 16.4 Deployment process

Push to `main` → CI (lint · boundary check §3.5 · tests · bundle budget · API-client freshness) → build one backend image → run migrations → deploy web → deploy worker → smoke test.

🔒 **Migrations run before deploy, and must be backward-compatible with the currently-running version.** With one instance per process and no blue-green, a breaking migration is a hard outage. Expand-contract is mandatory: add nullable → deploy code → backfill → make required → remove old in a later release.

**Rollback:** redeploy the previous image. 🔒 **Migrations are not automatically rolled back** — reversibility (NFR-076) means a reviewed, deliberate down-migration, never an automated one. Automated down-migrations on a production database destroy data.

### 16.5 Backup and recovery

🔒 NFR-021 (RPO ≤24h), NFR-022 (RTO ≤8h), NFR-023 (daily, ≥30 days), **NFR-024 (restore tested before launch)**.

| Asset | Backup | Recovery |
|---|---|---|
| Database | Supabase daily automated | Restore to a new instance, repoint |
| Object storage | 🟡 **PROPOSAL — needs verification.** Supabase Storage backup guarantees must be confirmed; if absent, a scheduled export job is required | Restore from export |
| Code | Git | Redeploy |
| Configuration | 🟡 **PROPOSAL** — documented in an encrypted store, not only in the platform UI | Reapply |

⚠️ **NFR-024 is the requirement most likely to be skipped and most likely to be fatal.** An untested restore is not a backup. **Launch gate: perform a full restore into a scratch environment and verify the application runs against it.** Add to the Phase 11 checklist.

⚠️ **Storage backup is an open item.** Database backups are managed; object storage may not be. If clinical documents are not covered, they are the least-recoverable asset in the system. Verify before launch.

---

## 17. Error Handling Strategy

### 17.1 Exception taxonomy

🔒 One taxonomy in `kernel.errors`; every error maps to a category with defined HTTP status, log level, user message policy and audit behaviour.

| Category | Meaning | Status | User sees | Logged |
|---|---|---|---|---|
| `ValidationError` | Input failed validation | 422 | Field-level, actionable | Debug |
| `AuthenticationError` | Not authenticated | 401 | Generic — 🔒 never reveals account existence (NFR-043) | Info |
| `AuthorizationError` | Not permitted | 403 | Generic refusal | **Warn + audit** |
| `EntitlementError` | Plan limit reached | 402 | 🔒 Limit + upgrade path (FR-M0-045) | Info |
| `ConsentError` | Processing not permitted | 403 | Explains the consent basis | Info + audit |
| `NotFoundError` | Absent, or not visible to actor | 404 | Generic | Info |
| `ConflictError` | Concurrent modification | 409 | Explains and offers reload | Info |
| `IntegrationError` | External service failed | 502/503 | "Try again shortly"; work never lost | **Error + alert** |
| `DomainRuleError` | Business rule violated | 422 | The rule, in plain language | Info |
| `InternalError` | Unexpected | 500 | Generic + reference id | **Error + alert** |

🔒 **`NotFoundError` for unauthorized resources.** Returning 403 for a record in another tenant confirms it exists. Cross-tenant access returns 404 — while still auditing the attempt (AC-M0-002).

### 17.2 User-facing messages

🔒 NFR-063 — state what went wrong and what to do next, in plain language, never a code alone.

| Rule | Example |
|---|---|
| Name the constraint | "You've reached 30 active clients on Starter." |
| Give the next action | "Move a client to Paused, or upgrade to Growth." |
| Never blame the user | Not "invalid input" — say which field and why |
| Never expose internals | No stack traces, table names or provider errors |
| Always recoverable | Every error state offers a path forward |

**Client-facing errors (P3) are gentler and never mention the practitioner's account state** — EC-M7-08: a suspended tenant shows a neutral message, never "your dietitian hasn't paid."

### 17.3 Failure philosophy

| Situation | Behaviour |
|---|---|
| External service down | 🔒 Degrade, never block. Practitioner keeps working; work is queued |
| Entitlement service unavailable | 🔒 **Fail safe** (FR-M0-046) — reads allowed, metered writes blocked |
| AI generation fails | 🔒 Manual path unaffected, no quota consumed (FR-M5-010) |
| Document generation fails | Retry; plan data never lost (EC-M4-08) |
| Message dispatch fails | Retry with backoff; terminal failure visible (AC-M8-007) |
| Offline client action | Queued locally, never discarded (FR-M7-012) |

🔒 **The governing rule: the practitioner's work is never lost, and never blocked by something outside their control.** Every failure mode above resolves to "the user continues; the system reconciles."

### 17.4 Concurrency

⚠️ EC-M4-07 — two practitioners editing the same plan must not silently discard each other's work.

📌 **ADR-14 — optimistic concurrency via version check on mutable aggregates**

Each mutable aggregate (plan version, client record, assessment response) carries a version. A write carries the version it read; a mismatch returns `ConflictError` with a reload path. **No last-write-wins anywhere that matters.**

Rare at MVP (mostly solo practitioners), but cheap now and expensive to retrofit once clinics arrive (P4).

---

## 18. Logging, Monitoring and Auditing

### 18.1 Three distinct streams

🔒 Frequently conflated; they have different audiences, retention and privacy rules.

| Stream | Purpose | Contains PII/clinical | Retention | Mutable |
|---|---|---|---|---|
| **Application logs** | Debugging | 🔒 **Never** (NFR-033) | Short | Yes |
| **Audit log** | Compliance, forensics | Identifiers only, 🔒 never values (FR-M0-035) | Long | 🔒 **Never** |
| **Delivery log** | Message operations | Recipient identifier + status | Medium | Append-only |

**The audit log is domain data, not telemetry.** It lives in Postgres under our control, participates in transactions, and is never shipped to a third party.

### 18.2 Audit

🔒 FR-M0-031…036.

| Event | Audited |
|---|---|
| Create/update/delete of client or clinical data | ✅ Always (FR-M0-031) |
| **Operator read of tenant data** | ✅ 🔒 The only reads audited (FR-M0-032) |
| Authorization failures | ✅ (§17.1) |
| Consent grant/change/withdrawal | ✅ Separate consent ledger (FR-M0-023) |
| Stage transitions | ✅ (FR-M1-015) |
| Entitlement/plan changes | ✅ |

**Immutability** (FR-M0-034): append-only by database permission — no update or delete grant exists on the audit table for the application role, so no application pathway can alter it regardless of code.

**Written by the framework**, not per endpoint (§5.1 step 9) — the same reason authorization is framework-level. A developer cannot forget it.

### 18.3 Monitoring

🔒 NFR-085 — a solo operator cannot watch dashboards. Alert on what costs money or breaks silently.

| Alert | Threshold | Why |
|---|---|---|
| Message failure rate | 🟡 >10% over 15 min | WhatsApp policy issue or outage |
| AI failure rate | 🟡 >20% over 15 min | Prompt/model regression |
| Job queue depth | 🟡 >500 or oldest >30 min | Worker stalled |
| Dead-letter jobs | Any | Something is silently failing |
| **Backup failure** | Any | 🔒 Most dangerous silent failure |
| Per-tenant cost anomaly | 🟡 >3× median | NFR-089 |
| Error rate | 🟡 >2% of requests | Regression |
| Health check failure | 2 consecutive | Outage |

Alerts go to a channel the founder actually reads. 🟡 **PROPOSAL — WhatsApp or Telegram, not email.** An email alert at 2am is not an alert.

### 18.4 Health checks

🔒 NFR-086 — liveness, readiness (database + storage reachable), and dependency status (external providers, surfaced in the operator console rather than failing the check — a WhatsApp outage must not take the application down).

---

## 19. Future Scalability

🔒 NFR-092/093/094 — designed for 200 tenants, with documented triggers for revisiting.

### 19.1 Current headroom

| Dimension | MVP design | Comfortable to | First constraint |
|---|---|---|---|
| Tenants | Shared schema + RLS | ~2,000 | Postgres connections, backup duration |
| Clients | Single Postgres | ~500,000 rows | Index tuning before anything structural |
| Food search | Postgres FTS | ~50,000 foods | Ranking quality before latency |
| Jobs | Postgres queue | ~50k/day | Table churn; partitioning before a broker |
| Documents | Worker rendering | ~500/day | Worker memory |
| AI | Sync in worker | Quota-bound | Cost before capacity |
| Web | 1 instance | ~50 concurrent | Add instances (stateless) |

**The design is comfortable at roughly 10× the 18-month target.** That is the correct margin: enough that growth is not a crisis, not so much that we paid for capacity we will not use.

### 19.2 Revisit triggers

🔒 NFR-094 — growth must not be a surprise.

| Trigger | Response | Not |
|---|---|---|
| Food search >300ms sustained | Index tuning, then materialised ranking | ❌ Search service |
| Job queue backlog persists | Second worker (safe by design, §11.3) | ❌ Broker |
| Web CPU >70% sustained | Additional web instance | ❌ Microservices |
| Postgres CPU >70% | Query tuning → read replica for reporting | ❌ Sharding |
| Backup window exceeds RPO | Larger instance, then archival strategy | ❌ Per-tenant databases |
| One tenant dominates load | Per-tenant rate limiting | ❌ Isolation rearchitecture |
| Storage costs exceed plan | Lifecycle policy on old documents | ❌ Self-hosting |

**The pattern: every first response is tuning or a bigger instance, never new architecture.** Architectural change is a last resort with a documented trigger, per Principle 5.

### 19.3 What is deliberately deferred

| Deferred | Enabled by | When |
|---|---|---|
| Multi-region (GCC, UK, US) | Region attribute (FR-M0-013), config-driven compliance (NFR-054) | Market entry |
| Public API | Clean API layer; only the client changes | Phase 3 (PRD §12) |
| Native mobile apps | 🔒 API has no web-only assumptions | Post-PMF |
| Read replicas | Repository layer isolates query routing | Postgres pressure |
| Service extraction | Module boundaries + events (§3) | Team growth, if ever |

🔒 **Module boundaries are what make future extraction possible.** A module that talks only through its public interface and events can become a service without touching its callers. **That option is bought by §3's discipline, not by building services now.**

### 19.4 What would genuinely require rearchitecting

Honest limits:

| Change | Impact |
|---|---|
| Per-tenant data residency (enterprise/EU) | Shared schema cannot satisfy it — needs per-region deployments |
| Real-time collaborative plan editing | Request/response model is wrong for it |
| Millions of end clients | Single Postgres becomes the constraint |
| Practitioner-facing offline desktop | Server-authoritative domain logic assumes connectivity |

None is on the roadmap. Listed so the boundary of this design is explicit rather than discovered later.

---

## 20. Implementation Roadmap

🔒 Build order derived from dependencies, risk-front-loading, and the user's stated V1 lesson: *"build the architecture first, then implement features."*

### 20.1 Ordering principles

1. **Structural before functional** — anything expensive to retrofit comes first (tenancy, authz, audit, consent, design system).
2. **Risk-first** — the wedge (nutrition) and the riskiest dependency (WhatsApp) are proven early, not discovered late.
3. **Every stage ends deployable** — 🔒 the user's requirement that each phase be independently usable.
4. **The six-step loop closes as early as possible** — the first end-to-end traversal is the real validation event.

### 20.2 Stage sequence

```
S0 Foundations ─▶ S1 Kernel ─▶ S2 Clients ─▶ S3 Nutrition ─▶ S4 Messaging
                                                  │              │
                                                  ▼              ▼
                                            S5 AI Draft    S6 Client PWA
                                                  │              │
                                                  └──────┬───────┘
                                                         ▼
                                    S7 Clinical ─▶ S8 Appointments ─▶
                                    S9 Progress ─▶ S10 Billing ─▶ S11 Admin ─▶ S12 Launch
```

---

#### **S0 — Project foundations** *(no features)*

Repo structure · CI pipeline · **boundary checker (§3.5)** · migrations · error taxonomy · request context · config · local/staging environments · **design system tokens and primitives** · OpenAPI→TS generation.

**Why first:** 🔒 the user's own V1 lesson. The boundary checker must exist before the first module, or it enforces nothing — and the design system must exist before the first screen (NFR-056, ADR-03), or V1's inconsistency recurs.
**Deployable:** an empty application that builds, tests, deploys and enforces its own rules.

#### **S1 — Kernel** *(no features)*

`identity` (3 realms) · `tenancy` (+ RLS) · `authz` · `audit` · `consent` · `entitlements` · `notifications` port · `storage` · `events` · job queue + worker.

**Why second:** 🔒 every module depends on all of it. Every one is structurally impossible to retrofit — this is the direct answer to V1's *"auth, routing and permissions became difficult to maintain."*
**Gate:** tenant-isolation tests pass **with the application filter deliberately removed** (AC-M0-003).
**Deployable:** authentication works; nothing else exists.

#### **S2 — Client record** — *M1, M2*

Client record with lifecycle stages · notes · tags · search · timeline · public enquiry form · manual lead entry.

**Why third:** the spine — every subsequent module attaches to a client. Includes `leads` because it creates client records and needs no other module.
**Deployable:** ✅ **First genuinely usable release.** A practitioner can replace their spreadsheet.

#### **S3 — Nutrition engine** ⭐ — *M4*

Food catalogue + seed data · portion conversion · search · custom foods · meals · plan builder · versioning · templates · document generation.

**Why fourth, before everything else feature-wise:** 🔒 **the wedge.** It is the largest, most domain-dense module and the one most likely to reveal that an assumption was wrong (ASM-02, ASM-13). Building it early means discovering that in month 2, not month 5.
⚠️ **Seed data is a parallel, non-engineering track** — start curation during S0.
**Deployable:** ✅ **The first release that saves real time.** Plans can be built and exported as documents even before automated delivery exists.

#### **S4 — Messaging engine** — *M8*

Scheduler · template registry · WhatsApp adapter · suppression · idempotency · staleness · quiet hours · delivery log.

**Why fifth:** 🔒 plans exist but cannot be delivered; and ASM-03 (WhatsApp approval) is a launch-blocking external risk that must be proven early.
⚠️ **Meta verification starts in S0**, months before this code is written.
**Deployable:** ✅ **The core loop closes.** Enquiry → client → plan → delivered on WhatsApp.

#### **S5 — AI plan drafting** — *M5*

Grounding · candidate set · prompt assembly · validation gate · draft state · metering.

**Why here:** requires the nutrition engine (grounding) and a practitioner template library to draft *in their style*. Building it before S3 would produce generic output and prove nothing.
**Gate:** 🔒 safety corpus passes — no allergen or dietary violation in any generated draft (AC-M5-003).
**Deployable:** ✅ **The differentiator is live.**

#### **S6 — Client PWA** — *M7*

Magic-link access · today's plan · adherence logging · weight logging · trends · offline · install · deep links.

**Why after S4:** WhatsApp deep links are how clients reach the portal (M7.3). Without messaging, the PWA has no entry point.
**Deployable:** ✅ **Both sides of the relationship are live.** Validates ASM-07.

#### **S7 — Clinical workspace** — *M3*

Versioned assessment engine · measurements · consultation notes · documents.

**Why this late — deliberate:** ⚠️ 🔒 **§9 of the PRD is unvalidated (ASM-05) and has four open clinical decisions (OD-06, 07, 08, 13).** Building it early risks building the wrong assessment. Placing it here maximises the time available for practitioner review while remaining before launch.
🟡 **PROPOSAL — a minimal intake (goal, dietary preference, allergies) ships in S3**, since AI grounding and dietary filtering need it. The *full* assessment lands here.
**Deployable:** ✅ Structured intake replaces retyping from WhatsApp.

#### **S8 — Appointments** — *M6*

Booking · views · statuses · reschedule history · reminders via S4.

**Why here:** genuinely thin, and depends on messaging. Nothing else depends on it.
**Deployable:** ✅ Scheduling with automated reminders.

#### **S9 — Progress & retention** — *M9*

At-risk view · check-in aggregation · progress display · practice counts.

**Why here:** requires accumulated data from S3, S6 and S7. Building it earlier means building against empty tables.
**Deployable:** ✅ Retention loop closes.

#### **S10 — Billing** — *M10*

Plans as config · metering surfaces · usage display · trial · manual activation · suspension · GST invoicing.

**Why last among features:** 🔒 M10.3 — enforcement lives in the kernel from S1; only *collection* is deferred, and manual collection is minutes per month below 20 customers.
⚠️ **Razorpay KYC starts in S0.**
**Deployable:** ✅ **Revenue can be collected.**

#### **S11 — Operator console** — *M11*

Tenant search · account state · aggregate views · delivery log · plan management · **food curation** · platform health.

**Why last:** support is needed only once customers exist. 🔒 Impersonation stays Phase 2 (FR-M11-012).
⚠️ **Exception:** basic food-curation tooling may be needed during S3 seeding. 🟡 **PROPOSAL** — a minimal internal curation path in S3, with the full console here.
**Deployable:** ✅ Support without database access.

#### **S12 — Launch readiness**

🔒 Launch gates, all blocking:

| Gate | Requirement |
|---|---|
| **Restore tested** | 🔒 NFR-024 — full restore verified, application runs against it |
| **Storage backup verified** | §16.5 open item resolved |
| Tenant isolation | AC-M0-003 with filters removed |
| AI safety corpus | AC-M5-003 clean |
| Erasure traverses storage | §13.2 |
| Connection pooling | §7.2 — `SET LOCAL` isolation verified under the production pooler |
| Performance budgets | NFR-001…010 on a mid-range Android over 4G |
| Click budgets | NFR-011…019 |
| Privacy legal review | ASM-10, OD-05 |
| GST treatment | ASM-09 |
| Alerting | NFR-085 verified by firing each alert |

### 20.3 Sequencing rationale

| Decision | Reason |
|---|---|
| Kernel entirely before any feature | Retrofitting tenancy, authz, consent or audit means touching every module |
| Nutrition before AI | AI grounds in nutrition data and practitioner templates |
| Messaging before PWA | WhatsApp deep links are the portal's entry point |
| Clinical late | §9 is unvalidated; buys maximum review time |
| Billing late | Enforcement is structural (S1); collection is operational |
| Admin last | Needed only once customers exist |
| Design system in S0 | 🔒 NFR-056 — after the first screen is too late |
| Boundary checker in S0 | Enforces nothing if written after the modules |

### 20.4 Parallel non-engineering tracks

⏳ 🔒 These run from S0 and are **not** gated on code. Each can silently consume a month (PRD Appendix B).

| Track | Start | Blocks |
|---|---|---|
| **Meta Business Verification** | S0 | S4 |
| **Razorpay KYC** | S0 | S10 |
| **Food data curation** | S0 | S3 |
| **IFCT 2017 licensing** | S0 | S3 |
| **Practitioner validation** (ASM-01/02/05) | S0 | S7, and pricing |
| **§9 assessment review** | S0 | S7 |
| **Privacy lawyer** | Mid-build | Launch |
| **Accountant — GST** | Before S10 | S10 |

⚠️ **The four highest-risk items are all non-engineering.** WhatsApp verification, food data licensing, practitioner validation and the assessment review can each delay launch regardless of how well the code goes.

---

## 21. Architecture Decision Records

| ID | Decision | Rationale | Reversibility |
|---|---|---|---|
| **ADR-01** | Separate worker process from day one | NFR-095 by construction; crash isolation; safe deploys | Easy |
| **ADR-02** | 🔒 FastAPI is the sole data path; `supabase-js` absent from frontend | Direct fix for V1's dual-backend auth failure (NFR-031) | **Hard — foundational** |
| **ADR-03** | Design system before any feature screen | NFR-056; V1 had none | Hard |
| **ADR-04** | One transaction per request, owned by HTTP layer | Atomicity without cross-module knowledge; transactional enqueue | Moderate |
| **ADR-05** | Single policy function, deny by default | FR-M0-015, NFR-032 | **Hard — foundational** |
| **ADR-06** | Shared schema + RLS | One migration path; isolation below the app (NFR-030) | **Hard** |
| **ADR-07** | Compute nutrition on read; snapshot on issue | FR-M4-027 live totals + EC-M4-03 historical integrity | Moderate |
| **ADR-08** | Postgres FTS, no search service | NFR-004 within NFR-077/087 | Easy |
| **ADR-09** | Headless-browser HTML→PDF in worker | Design reuse; Indic rendering (NFR-101) | Moderate |
| **ADR-10** | Deterministic grounding + validation around the model | Safety cannot rest on prompt compliance (FR-M5-006) | **Hard — safety-critical** |
| **ADR-11** | Postgres job queue | NFR-077; transactional enqueue | Moderate |
| **ADR-12** | Direct-to-storage upload with scoped credential | Protects small instances from file bytes | Easy |
| **ADR-13** | No cache tier | NFR-093; caching is the top source of stale-data bugs | Easy |
| **ADR-14** | Optimistic concurrency on mutable aggregates | EC-M4-07; cheap now, expensive later | Moderate |

---

## 22. Proposals Requiring Approval

🟡 Introduced in this phase and **not** derived from the PRD. Flagged per your instruction.

| # | Proposal | § | Impact if rejected |
|---|---|---|---|
| 1 | Separate signing keys per identity realm | 6.1 | Realm separation by claim check instead — weaker but adequate |
| 2 | Magic link 7 days / session 30 days | 6.2 | Need your values; blocks S1 |
| 3 | Prompt practitioner for a household measure when creating a custom food | 8.3 | Custom foods remain grams-only |
| 4 | AI grounding cache keyed on input hash | 9.4 | Slightly higher LLM cost; quotas still protect |
| 5 | LLM provider choice deferred to Phase 7 | 9.3 | Decide now against current pricing |
| 6 | 60-second scheduler tick | 11.3 | Choose another interval |
| 7 | SMS optional at MVP (defer TRAI DLT) | 12.4 | DLT registration joins the critical path |
| 8 | Circuit breaker on repeated provider failure | 12.5 | Wasted retries and quota during outages |
| 9 | Reap unconfirmed uploads after 24h | 13.3 | Orphaned storage objects accumulate |
| 10 | CI check for sensitive field names in logs | 15.2 | NFR-033 relies on discipline alone |
| 11 | CI secret scanner | 15.5 | Leaked-key risk in git history |
| 12 | Staging on free tiers, not performance-representative | 16.3 | Higher fixed cost |
| 13 | Configuration documented in an encrypted store | 16.5 | Config recoverable only from the platform UI |
| 14 | Alert thresholds (§18.3) | 18.3 | Set your own values |
| 15 | Alerts to WhatsApp/Telegram, not email | 18.3 | Email alerts get missed |
| 16 | Minimal intake ships in S3; full assessment in S7 | 20.2 | AI grounding blocked until S7 |
| 17 | Minimal food curation tooling in S3 | 20.2 | Seed data managed by direct DB access |
| 18 | Orchestration permitted in routers only | 3.4c | Multi-module actions need another mechanism |

---

## 23. Open Items Carried Forward

| Item | Blocks | Owner |
|---|---|---|
| ⚠️ Supabase Storage backup guarantees | Launch (NFR-023) | Verify in S0 |
| ⚠️ Connection pooler mode vs `SET LOCAL` | S1 | Verify in S1 |
| ⚠️ OD-06 (CKD / eating disorder AI policy) | S5 | Clinical decision |
| ⚠️ Worker memory for PDF rendering | S3 | Measure in S3 |
| ⚠️ IFCT 2017 licensing | S3 | Non-engineering, S0 |
| ⚠️ ASM-08 (WhatsApp/LLM current rates) | Pricing | Non-engineering, S0 |
| PRD OD-01…14 | Phases 4–6 | Founder + practitioners |

---

**END OF DOCUMENT**

*Phase 3 of 11 complete. Awaiting review before Phase 4 — Database Design.*
