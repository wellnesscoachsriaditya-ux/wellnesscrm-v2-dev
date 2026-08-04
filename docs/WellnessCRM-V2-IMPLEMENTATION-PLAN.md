# WellnessCRM V2 — Implementation Plan

**Status:** Draft v0.1 — awaiting review
**Owner:** Founder / CTO
**Date:** 2026-08-04
**Phase:** 6 of 11 (Implementation Planning)
**Derives from:** PRD, Architecture, Database Design, API Specification — 🔒 all frozen and approved

---

## Document Control

### Purpose

The sprint-by-sprint build plan. 🔒 Every task traces to an approved requirement; nothing new is introduced.

| Defines | Does not define |
|---|---|
| Sprint boundaries, objectives, sequence | Production code (Phase 9) |
| Migrations, backend and frontend tasks per sprint | Screen designs (Phase 6 of the *product*, i.e. UI/UX — see §2.4) |
| Testing strategy and Definition of Done per sprint | Detailed daily task breakdown |
| Effort estimates and risk posture | Deployment runbooks (Phase 11) |

### Conventions

> 🔒 **BINDING** — traces to an approved requirement.
> 🟡 **PROPOSAL** — a planning judgement requiring your approval.
> ⚠️ **RISK** — known hazard with delivery impact.
> ⏳ **EXTERNAL** — non-engineering dependency on the calendar.

### Effort units

🟡 Estimates are in **focused development days** — days where implementation is the primary activity. For a solo founder also handling curation, validation calls, legal and vendor onboarding, 🔒 **assume 3–4 focused days per calendar week, not 5.**

| Notation | Meaning |
|---|---|
| **d** | Focused development days |
| **Range** | Low = things go well; high = expected friction |
| 🔒 Confidence | High / Medium / Low, stated per sprint |

---

## 1. Planning Principles

| # | Principle | Source | Consequence |
|---|---|---|---|
| 1 | **Every sprint ends deployable and demonstrable** | 🔒 Your requirement | No sprint leaves the app in a broken state |
| 2 | **Structural before functional** | Arch §20.1 | Tenancy, authz, audit, consent, design system come first |
| 3 | **Risk-front-loaded** | Arch §20.1 | The wedge and the riskiest dependency are proven early |
| 4 | **One vertical slice at a time** | Solo-dev constraint | Backend + frontend + tests for one capability, not layer-by-layer |
| 5 | **External dependencies start on day 1** | PRD App. B | Meta, Razorpay, IFCT, legal run in parallel from S0 |
| 6 | **Validation gates before dependent work** | ASM-01/02/05 | Practitioner feedback lands before it becomes expensive |
| 7 | **No sprint exceeds ~3 calendar weeks** | Motivation risk | Long sprints hide slippage |

### 1.1 Why vertical slices, not horizontal layers

📌 **PDR-01 — build vertically (feature-complete slices), never horizontally (all models, then all APIs, then all UI)**

| Option | Verdict |
|---|---|
| All migrations → all backend → all frontend | ❌ Nothing is demonstrable until the end. Integration problems surface last, when they are most expensive. Violates Principle 1 |
| **Vertical slice per capability** | ✅ **Chosen.** Each sprint produces working software. Integration risk is discovered continuously. A solo developer keeps momentum by seeing the product work |

⚠️ **The exception is S1 (kernel).** It is structural and has no user-facing surface. 🔒 That is unavoidable — retrofitting tenancy or authorization means touching every module (Arch §20.2). It is the one sprint that ends with tests passing rather than a screen to look at, and it is deliberately as short as it can be.

---

## 2. Sprint Overview

### 2.1 The plan at a glance

| # | Sprint | Objective | Effort | Cumulative |
|---|---|---|---|---|
| **S0** | Foundations & Design System | Repo, CI, boundaries, tokens, primitives | 8–11d | ~11d |
| **S1** | Platform Kernel | Tenancy, authz, audit, consent, jobs | 12–16d | ~27d |
| **S2** | Client Spine | Auth UI, client records, lifecycle, timeline | 10–13d | ~40d |
| **S3** | Nutrition Catalogue ⭐ | Foods, portions, search, custom foods | 11–15d | ~55d |
| **S4** | Plan Authoring ⭐ | Builder, locking, budget, templates, PDF | 16–21d | ~76d |
| **S5** | Messaging Engine | Scheduler, WhatsApp, delivery log | 10–13d | ~89d |
| **S6** | Client Portal | PWA, today view, adherence, offline | 12–16d | ~105d |
| **S7** | Clinical Framework | Versioned assessments, measurements, docs | 11–14d | ~119d |
| **S8** | AI Plan Drafting | Grounding, validation gate, locks | 10–13d | ~132d |
| **S9** | Appointments | Booking, reminders, statuses | 6–8d | ~140d |
| **S10** | Progress & Retention | At-risk, check-in review, trends | 7–9d | ~149d |
| **S11** | Billing & Entitlements | Plans, metering, invoicing, trial | 8–11d | ~160d |
| **S12** | Operator Console | Tenant support, curation tooling | 7–9d | ~169d |
| **S13** | Launch Readiness | Gates, restore test, legal, hardening | 9–12d | ~181d |

🔒 **Total: 137–181 focused development days.**

### 2.2 What that means in calendar time

⚠️ 🔒 **This is the most important number in the document, and I want to be direct about it.**

At **3–4 focused days per week** (Principle: effort units):

| Pace | Calendar duration |
|---|---|
| 4 focused days/week, optimistic effort | ~34 weeks (~8 months) |
| 3.5 focused days/week, mid effort | ~45 weeks (~10.5 months) |
| 3 focused days/week, pessimistic effort | ~60 weeks (~14 months) |

🔒 **Your stated target was 4–6 months to first paying customer.** This plan does not meet it, and I am not going to present padded estimates that pretend otherwise.

**Three honest options** — §3 sets them out with the trade-offs. My recommendation is **Option B: a Paid Pilot at S6** (~105 days ≈ 6.5 months at 4d/week), where the six-step loop is complete and the product is genuinely sellable, with S7–S13 continuing while real practitioners use it.

### 2.3 Dependency graph

```
S0 Foundations
 └─▶ S1 Kernel
      └─▶ S2 Client Spine
           ├─▶ S3 Nutrition Catalogue ⭐
           │    └─▶ S4 Plan Authoring ⭐
           │         ├─▶ S5 Messaging ──▶ S6 Client Portal
           │         │                     └─▶ S10 Progress
           │         └─▶ S8 AI Drafting  (also needs S7)
           ├─▶ S7 Clinical Framework ──▶ S8
           └─▶ S9 Appointments (needs S5 for reminders)
      └─▶ S11 Billing (enforcement hooks land in S1)
           └─▶ S12 Operator Console
                └─▶ S13 Launch Readiness
```

🔒 **Critical path:** S0 → S1 → S2 → S3 → S4 → S5 → S6. Everything after S6 is genuinely parallelisable against real usage.

### 2.4 Note on UI/UX (product Phase 6)

⚠️ 🔒 **The original 11-phase plan placed UI/UX Design as Phase 6, before Implementation Planning.** We have arrived here without it.

🟡 **PROPOSAL — fold UI/UX design into each sprint rather than doing it as one upfront phase**, for two reasons:

1. **S0 already delivers the design system** (tokens, primitives, patterns, layout shells) — 🔒 NFR-056's actual requirement. That is the part that must precede feature screens.
2. Designing all ~40 screens upfront, alone, before any of them is built, tends to produce designs that are revised the moment real data appears.

🔒 **What this does NOT mean:** skipping design. Each sprint's frontend tasks include explicit design work, and 🔒 the click budgets (NFR-011…019) and one-primary-purpose-per-screen rule (NFR-058) are Definition-of-Done items, not aspirations.

⚠️ **If you would rather run a dedicated design phase before Sprint 1, say so** — it adds ~10–15 days to the critical path but reduces rework in S2–S6. Your call.

---

## 3. The Timeline Problem

🔒 Raised here rather than buried, because it changes what "launch" means.

### 3.1 Why the estimate is what it is

The scope is not padded. It is what the approved documents specify:

| Driver | Effort |
|---|---|
| 🔒 Structural work that cannot be retrofitted (S0, S1) | ~20–27d |
| 🔒 The nutrition engine — the wedge (S3, S4) | ~27–36d |
| 🔒 Three frontend applications | Embedded across S2–S12 |
| 🔒 DPDP consent, audit, erasure | Embedded in S1 + S13 |
| 🔒 Offline-capable PWA with sync | ~4–6d of S6 |
| 🔒 Testing to the approved ACs | ~20% of every sprint |

⚠️ **The largest single cost is S4 (Plan Authoring, 16–21d)** — the plan builder with locking, the nutrition budget, constrained recalculation, templates, versioning and PDF generation. 🔒 It is also the wedge. Cutting it is not an option; it *is* the product.

### 3.2 Three options

| | **A — Ship the full MVP** | **B — Paid Pilot at S6** ✅ | **C — Cut scope further** |
|---|---|---|---|
| First paying customer | After S13 | 🔒 **After S6** | After a reduced S6 |
| Calendar (at 4d/wk) | ~11 months | 🔒 **~6.5 months** | ~5 months |
| What they get | Everything | 🔒 Full six-step loop: capture → convert → manage → plan → follow up → basic retention | Loop with manual gaps |
| Billing | Automated | 🔒 **Manual (already the plan — M10.3)** | Manual |
| Missing at first sale | Nothing | Full assessment, AI drafting, appointments, at-risk view, operator console | Above, plus WhatsApp or offline |
| Risk | ⚠️ 11 months before market contact | 🔒 Sellable while building | ⚠️ Weak against WhatsApp+Sheets |

### 3.3 Why I recommend Option B

🔒 **At the end of S6 the product does the thing you are selling.** A practitioner can capture an enquiry, convert them, build a diet plan from a template far faster than in Word, deliver it on WhatsApp, and have the client view it and log adherence in a PWA. That is the four-hours-a-week promise, delivered.

**What is genuinely missing is depth, not reach:**
- Full clinical assessment (S7) — 🔒 but the minimal nutrition-calculation set ships in S3/S4, per your approved refinement
- AI drafting (S8) — the differentiator, but templates already deliver most of the time saving
- Appointments, at-risk view, operator console — real, but not why someone switches

🔒 **Billing being manual at first is already the approved plan** (M10.3), which means S11 is not a blocker for revenue — a Razorpay link and a flag suffices for the first ~20 customers.

⚠️ **The strongest argument for B is not speed — it is information.** Ten practitioners using the product for two months between S6 and S13 will tell you things ASM-01, ASM-02 and ASM-05 cannot be validated by interview. Building S7–S13 with that feedback produces a materially better product than building it blind.

🟡 **This is a business decision, not a technical one. I need your call before Sprint 1.**

---

## 4. Cross-Sprint Standards

🔒 Applies to every sprint. Not repeated per sprint.

### 4.1 Universal Definition of Done

A sprint is **not** done until all of these hold:

| # | Criterion | Source |
|---|---|---|
| 1 | 🔒 CI green: lint, types, boundary checker, tests, bundle budget, API-client freshness | Arch §3.5 |
| 2 | 🔒 Deployed to staging and manually exercised | Principle 1 |
| 3 | 🔒 Every new endpoint declares its authz action | ADR-05 |
| 4 | 🔒 Every new tenant-scoped table has RLS enabled with a policy | DB §17.3 |
| 5 | 🔒 No clinical data in logs, job payloads, audit metadata or error reports | NFR-033 |
| 6 | 🔒 Migrations are expand–contract safe against the previous release | Arch §16.4 |
| 7 | 🔒 Automated tests cover the sprint's acceptance criteria | NFR-073 |
| 8 | 🔒 Module README updated: purpose, public interface, dependencies | NFR-074 |
| 9 | 🔒 Generated TypeScript client regenerated and committed | NFR-079 |
| 10 | 🔒 No business logic in UI components | NFR-068 |
| 11 | 🔒 Click budgets met for any workflow the sprint touches | NFR-011…019 |
| 12 | 🔒 Every list/table has a designed empty state | NFR-064 |
| 13 | 🔒 Destructive actions confirm and state what is lost | NFR-065 |
| 14 | 🔒 Errors state what happened and what to do next | NFR-063 |

### 4.2 Testing strategy

🔒 Arch §5.4. Proportions are targets, not rules.

| Level | Scope | Share |
|---|---|---|
| **Unit** | Pure domain logic — portion conversion, totals, budget maths, at-risk rules, authz matrix | ~45% |
| **Module** | Service + real Postgres, one module | ~30% |
| **Contract** | Each integration adapter against recorded fixtures | ~5% |
| **API** | Full pipeline including authz, tenancy, entitlements | ~15% |
| **E2E** | Playwright, Journeys J1–J3 only | ~5% |

🔒 **Two test categories are mandatory and non-negotiable** (Arch §5.4):

1. 🔒 **Tenant isolation** — with the application's tenant filter deliberately removed, cross-tenant reads must return nothing (AC-M0-003). Added in S1, run in every sprint thereafter.
2. 🔒 **Safety corpus** — assessments with allergies and dietary exclusions; no violation may appear in any generated draft or recalculation (AC-M5-003). Added in S3 for deterministic filtering, extended in S8 for AI.

⚠️ **Testing is ~20% of each sprint's estimate and is included in the figures.** It is not an add-on to be cut when a sprint runs long — the ACs are the contract.

### 4.3 External dependency tracker

⏳ 🔒 **All start in S0**, independent of code.

| Dependency | Lead time | Blocks | Risk |
|---|---|---|---|
| **Meta Business Verification** | 2–6 weeks, can be rejected | 🔒 S5 | 🔴 High |
| **WhatsApp template approval** | Days–weeks each | S5 message types | 🟠 Medium |
| **IFCT 2017 licensing** | Unknown | 🔒 S3 seed data | 🔴 High |
| **Food data curation** | Continuous from S0 | 🔒 S3 | 🔴 High |
| **Razorpay KYC** | Days–weeks | S11 | 🟠 Medium |
| **Practitioner design partners** | Ongoing | ASM-01/02/05, S7 | 🔴 High |
| **§9 assessment review** | Weeks | S7 definition v1 | 🟠 Medium |
| **Privacy lawyer** (OD-05, ASM-10) | Weeks to schedule | 🔒 S13 / launch | 🔴 High |
| **Accountant — GST** (ASM-09) | Days | S11 | 🟡 Low |
| **Supabase pooler mode verification** | Hours | 🔒 S1 | 🔴 High |
| **Storage backup verification** | Hours | S13 | 🔴 High |

⚠️ 🔒 **Four of the highest-risk items are not engineering.** They can delay launch regardless of how the code goes, which is why they start in S0 rather than when their sprint arrives.

### 4.4 Validation gates

🔒 Principle 6 — practitioner feedback before it becomes expensive to act on.

| Gate | After | Validates | If it fails |
|---|---|---|---|
| **G1** | S3 | 🔒 ASM-02 (plan creation is the biggest time sink), ASM-13 (seed food coverage) | Re-prioritise before S4's 16–21d |
| **G2** | S4 | 🔒 AC-M4-010 (output beats their Word template), AC-M4-001 (<5 min from template) | Fix before building on it |
| **G3** | S6 | 🔒 ASM-07 (clients will use a PWA), ASM-01 (willingness to pay) | Reconsider the engagement model |
| **G4** | S7 | 🔒 ASM-05 (assessment reflects real practice) | Revise definition v1 — 🔒 no migration needed |

🔒 **G1 and G2 are the important ones.** They sit either side of the largest sprint, on the wedge. Getting a "no" at G1 costs 3 days of conversations; getting it after S13 costs the project.

---

## 5. Sprints

---

## S0 — Foundations & Design System

> 🔒 **No features. This sprint exists because of your own stated V1 lesson: "build the architecture first, then implement features."**

**Objective** — a repository that builds, tests, deploys and **enforces its own architectural rules**, plus the design system that every screen will be built from.

**Modules** — none (scaffolding only)
**Dependencies** — none
**Effort** — **8–11d** · 🔒 Confidence: **High**

### Database migrations
- Alembic initialised; empty baseline migration
- `app_user`, `app_migrator` roles created — 🔒 **`app_user` without `BYPASSRLS`** (DB §2.4)

### Backend tasks

| Task | Detail | Source |
|---|---|---|
| Repo structure | `kernel/`, `modules/`, `integrations/`, `platform/` | Arch §2.5 |
| 🔒 **Boundary checker** | `tools/check_boundaries.py` — R1–R6 + `supabase-js` absence + logic-in-components heuristic | Arch §3.5 |
| Config | Typed settings, env-per-environment | NFR-075 |
| Error taxonomy | `kernel/errors` — 13 categories, envelope shape | API §5 |
| Request context | request-id, actor, tenant placeholders | Arch §5.1 |
| Health endpoints | `/public/health` — liveness, readiness | NFR-086 |
| OpenAPI → TS generation | Script + CI freshness check | NFR-079 |
| CI pipeline | lint · types · boundaries · tests · bundle budget · client freshness | DoD §4.1 |
| Sentry | 🔒 `send_default_pii=False` + `before_send` scrubber | NFR-033 |

### Frontend tasks

| Task | Detail | Source |
|---|---|---|
| Monorepo | `packages/design-system`, `packages/api-client`, `apps/{practitioner,client-pwa,operator}` | Arch §4.1 |
| 🔒 **Design tokens** | Colour, type scale, spacing, radii, elevation — 🔒 the only place raw values exist | ADR-03 |
| 🔒 **Primitives** | Button, Input, Select, Checkbox, Radio, Textarea, Modal, Toast, Table, Card, Badge, Spinner, Tabs | ADR-03 |
| 🔒 **Patterns** | PageHeader, **EmptyState**, **ConfirmDialog**, FormField, DataList, ErrorState | NFR-064/065 |
| Layout shells | AppShell (practitioner), MobileShell (PWA), AdminShell | Arch §4.2 |
| 🔒 **IA manifest** | Declarative route/nav/breadcrumb/permission structure | NFR-057 |
| Lint rule | 🔒 No raw colour/spacing values in feature apps | ADR-03 |
| Three app shells | Each builds, deploys, renders a placeholder | Principle 1 |

⚠️ 🔒 **`EmptyState` and `ConfirmDialog` are built here deliberately.** NFR-064 and NFR-065 apply to ~40 screens. As components they are free; as per-screen discipline they will not happen.

### Testing strategy
- Boundary checker tested against deliberate violations (must fail)
- CI verified on a scratch branch
- Design system rendered in a component gallery
- 🔒 Sentry scrubber tested with a synthetic PII payload

### Definition of Done
🔒 Universal DoD §4.1, plus:
- Boundary checker **fails** CI on an intentional cross-module import
- All three apps deploy to staging and render
- Component gallery shows every primitive and pattern
- 🔒 `@supabase/supabase-js` is **absent** from frontend `package.json` (ADR-02)

### Deliverables
Working CI/CD · enforced boundaries · complete design system · three deployable shells · empty database with correct roles

### ⏳ Started this sprint (non-engineering)
🔒 Meta Business Verification · Razorpay KYC · IFCT licensing enquiry · food curation · practitioner outreach · privacy lawyer scheduling · **Supabase pooler mode verification**

---

## S1 — Platform Kernel

> ⚠️ 🔒 The one sprint with no user-facing surface. Unavoidable — retrofitting any of this means touching every module.

**Objective** — identity, tenancy, authorization, consent, audit, entitlements, jobs and storage, proven correct before any domain code exists.

**Modules** — `kernel` (all)
**Dependencies** — S0
**Effort** — **12–16d** · 🔒 Confidence: **Medium** *(RLS + pooler behaviour is the unknown)*

### Database migrations
`tenants` · `users` · `operators` · `client_access_grants` · `magic_links` · `sessions` · `audit_log` · `consent_purposes` · `consent_notices` · `consent_records` · `data_requests` · `plan_definitions` · `subscriptions` · `subscription_events` · `usage_counters` · `usage_events` · `files` · `jobs` · `job_runs` · `idempotency_records`
🔒 RLS enabled on every tenant-scoped table · 🔒 **INSERT/SELECT-only grants on `audit_log` and `consent_records`** (DDR-15)

### Backend tasks

| Task | Detail | Source |
|---|---|---|
| 🔒 `identity` | Three realms, **separate signing keys**, GoTrue brokering | Arch §6.1 |
| Session management | 🔒 Rotating refresh + **reuse detection → family revocation** | DDR-05 |
| Magic links | 🔒 Hashed tokens, 15–30 min expiry, single-use by conditional update | DDR-04 |
| 🔒 `tenancy` | Resolve tenant, `SET LOCAL app.tenant_id` per transaction | ADR-06 |
| 🔒 `authz` | **One** `can(actor, action, resource)`; deny by default; **startup failure if an endpoint lacks a declared action** | ADR-05 |
| `consent` | Append-only ledger; purpose checks; derived current state | DB §16 |
| `audit` | 🔒 Framework-level writer — not per endpoint | Arch §5.1 |
| `entitlements` | Counters + events; 🔒 fail-safe when indeterminate | FR-M0-046 |
| `notifications` port | Interface only; no adapters yet | FR-M0-041 |
| `storage` | Authorize → scoped upload credential → confirm | ADR-12 |
| `events` | In-process bus; transactional vs deferred handlers | Arch §3.4 |
| Job queue + worker | 🔒 `SKIP LOCKED`, leases, retries, dead-letter | ADR-11 |
| Request pipeline | 🔒 12 steps, no bypass | Arch §5.1 |
| Auth endpoints | register, verify, login, refresh, logout, reset | API §2.2 |

### Frontend tasks
Auth flows for practitioner (register, verify, login, reset) · session handling with silent refresh · 🔒 access token in memory only (ADR-A02) · protected-route wrapper driven by the IA manifest · error boundary rendering the API error envelope

### Testing strategy
🔒 **This sprint is test-heavy by design.**
- 🔒 **Tenant isolation with the application filter removed** — AC-M0-003. **The gate for this sprint.**
- Authz matrix: 4 roles × all actions × ownership — table-driven
- Realm crossing: practitioner token → `/admin/*` and `/portal/*` must fail
- Magic link: expiry, single-use, replay
- 🔒 Refresh reuse detection: replayed token revokes the family
- Consent: grant, withdraw, re-grant; current state derived correctly
- Audit immutability: 🔒 UPDATE/DELETE attempts fail at the database
- Jobs: claim, lease expiry, retry, dead-letter, idempotency
- Entitlements: enforcement, fail-safe, 80% warning

### Definition of Done
🔒 Universal DoD, plus:
- 🔒 **AC-M0-003 passes** — filter removed, no cross-tenant data
- 🔒 **Supabase pooler confirmed in transaction mode** (DB §2.3) — ⚠️ **if it is session-mode with reuse, stop and resolve before S2**
- An endpoint without a declared authz action fails at startup
- A practitioner can register, verify, log in, refresh and log out
- Worker processes a job end to end and survives a restart mid-job

### Deliverables
Working auth for three realms · enforced multi-tenancy · single authorization point · consent ledger · immutable audit · job queue + worker · entitlement enforcement

⚠️ 🔒 **Highest-severity item in the whole plan:** if the pooler runs in session mode with connection reuse, `SET LOCAL` tenant isolation silently fails and every RLS policy becomes decorative. **Verify in S0, confirm in S1.**

---

## S2 — Client Spine

**Objective** — a practitioner can sign up and manage their whole client base. 🔒 **First genuinely usable release** — it replaces the spreadsheet.

**Modules** — `clients`, `leads`
**Dependencies** — S1
**Effort** — **10–13d** · 🔒 Confidence: **High**

### Database migrations
`clients` · `client_stage_history` · `client_notes` · `tags` · `client_tags` · `client_assignments` · `timeline_events` · `enquiry_forms` · `enquiry_submissions`
🔒 Search index: GIN tsvector on clients (NFR-005) · 🔒 partial indexes excluding archived rows

### Backend tasks
Client CRUD with 🔒 `ck_clients__contact_present` · 🔒 stage transitions as **named actions** with entitlement check on `→ active` (ADR-A06) · stage history · notes, tags, assignments · 🔒 **materialised timeline via event subscribers** (DDR-06) · search ≤300ms · public enquiry form endpoints · 🔒 **silent server-side duplicate matching** (EC-M2-02) · captcha + rate limiting · lead acknowledgement and practitioner notification *(logged only — no transport until S5)*

### Frontend tasks
Client list: filter by stage/tag/owner, sort, search-as-you-type · client detail with timeline · 🔒 create client in **≤3 interactions** (NFR-011) · 🔒 stage change in **≤2** (NFR-012) · notes and tags inline · needs-response lead view · **public enquiry form** (standalone, mobile-first, consent capture) · empty states for every list

### Testing strategy
Client CRUD + validation · stage transitions incl. 402 at limit · 🔒 entitlement counts only `active` (M1.5) · duplicate mobile permitted (EC-M1-01) · reactivation reuses the original record (EC-M1-02) · timeline ordering across event types · 🔒 **enquiry submission returns an identical response for known and unknown mobiles** · practitioner-scope isolation (AC-M1-006) · E2E: enquiry → lead → active client

### Definition of Done
🔒 Universal DoD, plus: AC-M1-001…007 pass · NFR-005 and NFR-006 met with seeded volume · 🔒 click budgets NFR-011/012 verified by counting · public form completes in <60s on a mid-range Android (AC-M2-001)

### Deliverables
Full client management · lead capture with consent · unified timeline · public enquiry form · 🔒 **a practitioner could genuinely stop using their spreadsheet**

---

## S3 — Nutrition Catalogue ⭐

> 🔒 **The wedge begins here.** Also where ASM-02 and ASM-13 get tested for real.

**Objective** — a curated Indian food database with household measures, searchable in under 300ms, extensible by practitioners inline.

**Modules** — `nutrition` (catalogue half)
**Dependencies** — S2 · ⏳ **IFCT licensing + curated seed data**
**Effort** — **11–15d** · 🔒 Confidence: **Medium** *(seed data quality is the unknown, not the code)*

### Database migrations
`food_categories` · `foods` · `food_aliases` · `nutrients` · `food_nutrients` · `measure_units` · `food_portions` · `recipes` · `recipe_items` · `meals` · `meal_items` · `supplements` · `dietary_rules` · `nutrition_targets` · `food_search_misses`
🔒 Nullable `tenant_id` + Pattern B RLS (DDR-03) · 🔒 trigger-maintained `search_vector` · GIN + trigram indexes

### Backend tasks

| Task | Detail | Source |
|---|---|---|
| 🔒 **Portion conversion service** | **The single implementation** — every gram figure resolves here | FR-M4-004, NFR-072 |
| 🔒 **Composition service** | **The single implementation** of nutritional totalling | NFR-072 |
| Food search | 🔒 FTS + trigram, aliases folded in, ≤300ms | NFR-004 |
| 🔒 Dietary filtering **in-query** | Allergens, dietary class, onion/garlic, root veg — never post-filter | FR-M4-035 |
| Custom foods | 🔒 Inline creation, tenant-private | FR-M4-012/013 |
| Meals | Reusable sets; nutrition derived | FR-M4-018/019 |
| Supplements | 🔒 **Vendor-neutral — no brand/price/vendor columns** | Approved |
| 🔒 `dietary_rules` engine | hard vs soft; `applies_to_ai` vs `applies_to_manual` | DB §8.15 |
| 🔒 Search-miss logging | Server-side on zero results | FR-M4-014 |
| Seed loader job | 🔒 Idempotent upsert from versioned data files | DDR-17 |
| Body composition | BMI, WHR derived — 🔒 **no threshold bands until OD-08** | FR-M3-012 |

### Frontend tasks
Food search-as-you-type with household-measure display · food detail (portions, nutrients, provenance) · 🔒 **inline custom-food creation that does not lose surrounding state** (FR-M4-012) · meal builder · meal library · supplement picker

### Testing strategy
🔒 **Portion conversion: exhaustive.** Every measure type, edge quantities, foods without portions, gram-only fallback
🔒 **Composition: verified against manual calculation** (AC-M4-006)
Search: ranking, aliases, typos, dietary filtering, ≤300ms at full seed size (AC-M4-003)
🔒 **Safety corpus v1** — allergen and dietary exclusions never appear in filtered results (AC-M4-009)
Custom food tenant isolation · retire vs delete (EC-M4-04) · seed loader idempotency

### Definition of Done
🔒 Universal DoD, plus: NFR-004 met at full seed volume · AC-M4-003/004/005/006/009 pass · 🔒 seed set covers the foods in **10 real diet plans** (ASM-13) · portion conversion has **one** implementation, verified by the boundary checker

### Deliverables
Curated Indian food database · household measures · vernacular search · custom foods · reusable meals · vendor-neutral supplements · dietary rules engine · search-miss telemetry

### 🔒 Validation Gate G1
⚠️ **Before S4 begins — 3 practitioner sessions:**
1. Search for 10 foods they use daily. **Hit rate?** *(ASM-13)*
2. Time them building a plan in their current tool. **Is it the biggest time sink?** *(ASM-02)*
3. Are the katori/roti reference values right? *(OD-03)*

🔒 **If the hit rate is poor, curate before S4 — do not build a plan builder on a food database practitioners do not trust.**

---

## S4 — Plan Authoring ⭐

> 🔒 **The largest and most valuable sprint. This is the product.**

**Objective** — build a diet plan from a template in under 5 minutes, with locking as fixed nutritional constraints, and deliver a document that beats their Word template.

**Modules** — `nutrition` (authoring half)
**Dependencies** — S3 · G1 passed
**Effort** — **16–21d** · 🔒 Confidence: **Medium** *(largest scope; PDF rendering is the main unknown)*

### Database migrations
`diet_templates` · `template_days` · `template_slots` · `template_items` · `diet_plans` · `diet_plan_versions` · `plan_days` · `plan_slots` · `plan_items` · `plan_item_alternatives` · `plan_supplements` · `plan_snapshots`
🔒 `uq_diet_plan_versions__one_draft` partial unique · `is_locked` on template_items, plan_items, plan_slots, plan_supplements · `row_version` for optimistic concurrency

### Backend tasks

| Task | Detail | Source |
|---|---|---|
| Plan/version lifecycle | 🔒 Copy-on-revise; one draft at a time | DDR-11 |
| 🔒 **State machine** | **No `draft → delivered` transition exists** | FR-M5-003 |
| Slot structure | Add, rename, remove, reorder | FR-M4-025 |
| Item management | Foods, recipes, meals; alternatives | FR-M4-030 |
| 🔒 **Nutrition budget** | target / locked_consumed / unlocked_current / remaining_available | ADR-A07 |
| 🔒 **Locking** | Item, slot, supplement; 🔒 practitioner can always unlock | Approved |
| 🔒 **Constrained recalculation** | Subtract locked nutrition; redistribute across unlocked; 🔒 **quantities only — never add, remove or replace foods**; 🔒 `respect_locks:false` → 422 | Approved |
| Templates | Save-as, instantiate, 🔒 **locks preserved but editable before approval** | Approved |
| Issue | 🔒 Atomic: resolve → compute → snapshot → freeze → supersede | DB §8.12 |
| 🔒 PDF generation | Headless browser **in the worker**; one render at a time; hard timeout | ADR-09 |
| Snapshot | 🔒 One document serves PDF + portal + offline; `content_hash` | DDR-12 |
| Optimistic concurrency | `If-Match` / ETag; 409 on stale | ADR-14 |

### Frontend tasks
🔒 **Plan builder — the most important screen in the product.** Slot/day structure, inline food search, quantities in household measures, running totals, 🔒 **nutrition budget always visible**, lock/unlock toggles, alternatives, drag-reorder · template library and save-as · plan version history · issue flow with 🔒 **≤2 interactions** (NFR-016) · PDF preview · pending-render state

### Testing strategy
🔒 **The locking guarantees are the critical tests:**
- Recalculation never alters a locked item — byte-identical, asserted
- 🔒 `respect_locks:false` → 422, always
- 🔒 **Recalculation never adds, removes or substitutes a food** (approved)
- Budget maths correct incl. negative `remaining_available`
- Template instantiation preserves locks and they remain editable

Plus: issue atomicity and idempotency · issued version immutable after curated food correction (EC-M4-03) · concurrent edit → 409 (EC-M4-07) · PDF renders correctly with Indic scripts · 🔒 E2E: template → plan → issue → PDF

### Definition of Done
🔒 Universal DoD, plus:
- 🔒 **AC-M4-001: plan from template in <5 minutes** — timed with a real practitioner
- 🔒 **AC-M4-002: from blank in <15 minutes**
- AC-M4-007/008 pass · NFR-007 (PDF ≤5s) met
- 🔒 **No code path modifies a locked item**, verified by test and review

### Deliverables
Complete plan builder · locking as nutritional constraints · constrained recalculation · templates · versioning · professional PDF · portal-ready snapshots

### 🔒 Validation Gate G2
⚠️ **Before S5 — 3 practitioners:**
1. 🔒 **Is our PDF better than your Word template?** (AC-M4-010) — *if no, fix before building anything else*
2. Build a real plan. **Under 5 minutes?**
3. Does locking behave as you would expect?

---

## S5 — Messaging Engine

**Objective** — every message in the product, sent by one engine, with WhatsApp as the primary transport. 🔒 **The core loop closes here.**

**Modules** — `messaging`, `integrations/whatsapp|email|sms`
**Dependencies** — S4 · ⏳ 🔒 **Meta Business Verification approved**
**Effort** — **10–13d** · 🔒 Confidence: **Medium** *(external dependency, not code)*

### Database migrations
`message_templates` · `scheduled_messages` · `message_dispatches` · `checkin_schedules` · `notification_preferences`
🔒 `uq_scheduled_messages__idempotency` · partial index on due pending messages

### Backend tasks
🔒 **One scheduler** — no module schedules its own messages (FR-M8-001) · versioned template registry with 🔒 per-template provider approval state (EC-M8-03) · WhatsApp adapter · email adapter · 🔒 SMS deferred *(approved proposal #7 — TRAI DLT off the critical path)* · 🔒 **suppression evaluated at dispatch, not scheduling** (six rules) · 🔒 idempotency by unique constraint · 🔒 staleness tolerance per template (EC-M8-05) · 🔒 quiet hours **deferring, not dropping** (AC-M8-006) · frequency capping across all types · 🔒 essential messages exempt from quota/frequency · delivery log · webhook receiver *(status only — 🔒 replies not processed, EC-M8-07)* · check-in schedules · 8 MVP message types · per-tenant message metering

### Frontend tasks
Message history per client · 🔒 **template preview as the client will see it** (FR-M8-026) · tenant-wide message-type toggles (FR-M8-027) · check-in configuration per client · pending scheduled messages view · delivery failure surfacing

### Testing strategy
🔒 Suppression: **each of the six rules independently**, evaluated at dispatch after a state change
🔒 Idempotency: retry never double-sends (EC-M8-06)
🔒 Staleness: stale check-in expires rather than sending late
🔒 Quiet hours: 02:00 message defers, does not drop
Frequency capping across types (EC-M8-09) · essential messages bypass quota (EC-M10-04) · WhatsApp adapter contract tests against fixtures · webhook signature verification and idempotency · 🔒 **AC-M8-008: a new message type requires only a template + schedule**

### Definition of Done
🔒 Universal DoD, plus: AC-M8-001…009 pass · real WhatsApp message delivered to a real number on staging · 🔒 approved plan triggers delivery within 60s (NFR-009) · all 8 message types previewed

### Deliverables
Single scheduled-message engine · WhatsApp delivery · delivery log · check-in scheduling · practitioner controls · 🔒 **enquiry → client → plan → WhatsApp delivery works end to end**

⚠️ 🔒 **If Meta verification has not landed, this sprint still ships** — email and logged-only transports work, and the WhatsApp adapter is swapped in when approval arrives. 🔒 **But the product is not sellable in India without it**, so treat approval as a launch gate regardless of code readiness.

---

## S6 — Client Portal (PWA)

> 🔒 **Both sides of the relationship become real. This is the recommended Paid Pilot point.**

**Objective** — a client receives a plan on WhatsApp, opens it on their phone, and logs adherence in under 60 seconds — offline-capable.

**Modules** — `progress` (adherence), `apps/client-pwa`
**Dependencies** — S5
**Effort** — **12–16d** · 🔒 Confidence: **Medium** *(offline sync is genuinely tricky)*

### Database migrations
`adherence_logs` (🔒 with `idempotency_key` unique per client) · `client_nutrition_visibility` flag *(🔒 per-client, default off — approved)*

### Backend tasks
🔒 **`/portal/today` aggregate** (ADR-A09) · magic-link redemption with deep-link targets · 🔒 **self-service link re-request — one tap, no practitioner** (EC-M7-01) · adherence logging with 🔒 client + server timestamps · measurement logging from portal · progress read · 🔒 **`/portal/sync` — per-operation results, `duplicate` is success, nothing discarded** (EC-M7-05) · consent view/update · 🔒 `capabilities` object on every response · 🔒 **nutrition fields omitted entirely unless the per-client flag is set** (approved) · 🔒 degradation states never expose practitioner account state (EC-M7-08)

### Frontend tasks
🔒 **Mobile-first PWA.** Today view as the landing route · one-tap adherence (🔒 ≤6 interactions/day, NFR-018) · weight logging (🔒 ≤3, NFR-019) · full plan view · progress charts · appointments *(placeholder until S9)* · document upload · consent management · 🔒 **service worker: shell + active plan cached** · 🔒 **offline queue in IndexedDB with visible pending state** · install prompt · 🔒 expired-link screen with one-tap re-request

Practitioner side: per-client nutrition-visibility toggle · portal access status and resend

### Testing strategy
🔒 **Offline is the hard part:**
- Plan viewable with network disabled after first load (AC-M7-004)
- Adherence logged offline shows pending, syncs correctly (AC-M7-005)
- 🔒 Sync batch with one bad operation — others still apply
- 🔒 Replayed queue → `duplicate`, not error
- 🔒 Plan revised while viewing → informed, **not silently swapped** (EC-M7-03)

Plus: magic link expiry → re-request → access, without practitioner involvement · 🔒 **nutrition values absent from payload when the flag is off** · paused client read-only (EC-M7-06) · suspended tenant neutral message (EC-M7-08) · 🔒 NFR-002 ≤2.5s on throttled 4G · install on Android and iOS · E2E Journey J3

### Definition of Done
🔒 Universal DoD, plus: AC-M7-001…008 pass · 🔒 **NFR-002 verified on a real mid-range Android over real 4G**, not a simulator · click budgets NFR-018/019 verified · installs and launches without browser chrome

### Deliverables
Installable client PWA · today view · one-tap adherence · offline plan + queue · progress · consent management · 🔒 **the six-step loop is complete end to end**

### 🔒 Validation Gate G3 — the big one
⚠️ **Run a real pilot: 3–5 practitioners, 10+ real clients, 2+ weeks.**
1. 🔒 **Do clients actually open the PWA from WhatsApp?** *(ASM-07)*
2. 🔒 **Do they log adherence more than once?**
3. 🔒 **Will practitioners pay ₹799–1,799/month for this?** *(ASM-01)*
4. What is the single most-requested missing thing?

🔒 **Answer 4 should re-order S7–S12.** The sequence below is my best guess absent data; the pilot replaces the guess.

---

## S7 — Clinical Framework

> 🔒 Per your approved refinement: the **real versioned framework** ships, seeded with a v1 definition holding only nutrition-calculation fields.

**Objective** — structured assessment, measurements, notes and documents — expandable later without redesign.

**Modules** — `clinical`
**Dependencies** — S2 (S6 recommended, for pilot feedback)
**Effort** — **11–14d** · 🔒 Confidence: **Medium** *(content, not code, is the risk)*

### Database migrations
`assessment_definitions` · `assessment_responses` · 🔒 **`client_nutrition_profile`** · `measurements` · `consultation_notes` · `client_documents` · 🟡 `clinical_reference_ranges`
🔒 Seed: `nutrition_core` v1 — date of birth, sex, height, weight, activity level, goal, dietary class, allergies, exclusions, fasting, staple grain, region

### Backend tasks
🔒 Versioned definition engine — 🔒 **responses pin their definition version** (FR-M3-003) · 🔒 **`calculation_bindings` projection into `client_nutrition_profile`** (DDR-08) · partial save and resume (FR-M3-005) · section-level skipping (FR-M3-006) · repeat administration · measurements with 🔒 derived BMI/WHR — 🔒 **no threshold bands until OD-08 is cited** · consultation notes 🔒 (never client-visible) · document upload via the S1 storage flow · 🔒 **allergens captured as food IDs, never free text** (DB §7.4)

### Frontend tasks
🔒 **Schema-driven form renderer** — 🔒 adding assessment fields requires no frontend release · assessment completion (practitioner and client) with section navigation and resume · measurement entry and trend charts · consultation notes · document upload and in-browser viewing · 🔒 **allergy capture as a food picker, not a text field**

### Testing strategy
🔒 Definition versioning: publish v2, v1 responses still render correctly (AC-M3-003)
🔒 Projection: bindings produce correct typed values; nutrition module never parses `jsonb`
Resume across sessions without loss (AC-M3-001) · coach skips all clinical sections, result still usable (AC-M3-002) · implausible values warn but accept (EC-M3-02) · 🔒 consultation notes invisible in every client-facing surface (AC-M3-006) · 🔒 derived BMI matches manual calculation

### Definition of Done
🔒 Universal DoD, plus: AC-M3-001…007 pass · 🔒 **adding a field to the definition requires no code change and no migration** — demonstrated · 🔒 no clinical threshold displayed without a citation

### Deliverables
Versioned assessment framework · minimal v1 definition · nutrition profile projection · measurements with trends · consultation notes · document management

### 🔒 Validation Gate G4
Practitioners review the §9 proposal against definition v1 (ASM-05). 🔒 **Revisions are new definition versions — no migration, no release.**

---

## S8 — AI Plan Drafting

**Objective** — a draft plan grounded in the practitioner's own templates and permitted foods, under a deterministic safety gate.

**Modules** — `ai_drafting`, `integrations/llm`
**Dependencies** — S4, S7
**Effort** — **10–13d** · 🔒 Confidence: **Medium**

### Database migrations
`ai_prompt_versions` · `ai_generations`

### Backend tasks

| Task | Detail | Source |
|---|---|---|
| 🔒 **Candidate set assembly** | Deterministic; allergens and dietary violations removed **before** the model sees anything | ADR-10 |
| Grounding | 🔒 Practitioner templates and saved meals bias the set | FR-M5-002 |
| Prompt assembly | Versioned; structured output | FR-M5-005 |
| Async generation | 🔒 202 + poll, executed in the worker | ADR-A08 |
| 🔒 **Validation gate** | Every food ∈ candidate set · no allergen · dietary class respected · no advisory language · schema valid | ADR-10 |
| 🔒 **Lock preservation** | Locked items carried through unchanged; nutrition subtracted from target; 🔒 **post-validated byte-identical or the generation fails** | Approved |
| 🔒 Draft persistence | 🔒 `ai_drafting` **hands validated structure to `nutrition`** — it has no write path to plan tables | DB §9 |
| Eligibility endpoint | 🔒 The OD-06 seam | API §9.5 |
| Metering | 🔒 Quota consumed **only on success** | FR-M5-010 |
| Rationale | Grounding counts + brief explanation | FR-M5-011 |

### Frontend tasks
Generate-draft entry point with 🔒 eligibility pre-check · progress with 🔒 "keep waiting / build manually" escape · 🔒 draft opens in the **standard plan builder** — no AI-specific editor (FR-M5-004) · grounding transparency ("312 of your foods, 4 of your templates") · 🔒 failure states stating **no quota consumed** · quota display

### Testing strategy
🔒 **Safety corpus is the gate:**
- 🔒 Every food in every generated draft exists in the candidate set (AC-M5-002)
- 🔒 **Zero allergen or dietary violations across the full corpus** (AC-M5-003)
- 🔒 **No path from `Draft` to delivered without explicit approval** (AC-M5-004)
- 🔒 Locked items never altered by generation
- Failure consumes no quota (AC-M5-006) · quota exhaustion still allows manual building (AC-M5-007) · malformed model output handled as failure, no partial draft (EC-M5-03) · provider outage does not block manual work (EC-M5-07)

### Definition of Done
🔒 Universal DoD, plus: AC-M5-001…008 pass · 🔒 **safety corpus 100% clean — this is a blocking gate, not a metric** · NFR-008 (≤30s with progress) met · 🔒 practitioners rate drafts "a useful starting point" or better in a majority of cases

### Deliverables
Grounded AI drafting · deterministic safety gate · lock preservation · async generation with quota protection · grounding transparency

---

## S9 — Appointments

**Objective** — booking with automated reminders. Deliberately thin.

**Modules** — `appointments`
**Dependencies** — S5
**Effort** — **6–8d** · 🔒 Confidence: **High**

### Database migrations
`appointments` · `appointment_history`

### Backend tasks
CRUD with 🔒 unambiguous timezone storage (NFR-099) · statuses · 🔒 reschedule creates a new appointment, original retained (FR-M6-007) · reminders 🔒 **via the S5 engine — no separate scheduling** · practitioner-supplied meeting links · timeline integration · 🔒 blocked for paused clients (EC-M6-04)

### Frontend tasks
Day, week and list views · 🔒 booking in **≤4 interactions** (NFR-017) · status changes with notes in the same flow · overlap warning 🔒 (warn, never block — EC-M6-01) · reminder configuration · client portal appointment view *(completes the S6 placeholder)*

### Testing strategy
Booking, reschedule chain, cancellation · reminders fire at configured intervals via the S5 engine · paused client blocked (EC-M6-04) · back-dated booking permitted (EC-M6-02) · overlap warns but allows · AC-M6-001…006

### Definition of Done
🔒 Universal DoD, plus: AC-M6-001…006 pass · 🔒 reminders use the S5 engine — **verified no second scheduler exists** · NFR-017 met

### Deliverables
Appointment management · automated reminders · reschedule history · client-visible appointments

---

## S10 — Progress & Retention

**Objective** — surface disengaging clients before they churn.

**Modules** — `progress` (analysis half)
**Dependencies** — S6, S7
**Effort** — **7–9d** · 🔒 Confidence: **High**

### Database migrations
`progress_snapshots` (🔒 with `dismissed_until` — EC-M9-02)

### Backend tasks
🔒 **Nightly at-risk rollup** (DDR-13) · consolidated check-in review · per-client progress: trend + adherence rate · 🔒 **adherence as a rate, never a judgemental score** (FR-M9-007) · practice-level counts · 🔒 dismissal for clients engaging outside the platform (EC-M9-02) · 🔒 new clients excluded until an onboarding window elapses (EC-M9-01)

### Frontend tasks
At-risk list with dismissal · 🔒 **week's check-ins for 40 clients on one screen** (AC-M9-002) · per-client progress on the client record · practice dashboard · 🔒 client-facing progress framed neutrally (EC-M9-03)

### Testing strategy
At-risk threshold correctness across four activity sources · new clients excluded · dismissal respected · adherence rate matches manual calculation · practice counts reconcile with client records (AC-M9-004) · 🔒 no negative framing in client-facing progress

### Definition of Done
🔒 Universal DoD, plus: AC-M9-001…005 pass · rollup completes within its window at full volume · 40-client review on one screen

### Deliverables
At-risk detection · check-in review · progress tracking · practice metrics

---

## S11 — Billing & Entitlements

> 🔒 Enforcement shipped in S1. This sprint adds plans, surfaces and invoicing. **Collection stays manual** (M10.3).

**Objective** — plans, usage visibility, trial, manual activation, GST-compliant invoicing.

**Modules** — `billing`
**Dependencies** — S1 · ⏳ Razorpay KYC · ⏳ accountant (ASM-09)
**Effort** — **8–11d** · 🔒 Confidence: **Medium** *(GST is the unknown)*

### Database migrations
`invoices` · 🔒 `invoice_sequences` · `payments` · `practitioner_payment_records`
🔒 Seed `plan_definitions`: free, starter, growth, clinic

### Backend tasks
🔒 Plans as configuration (FR-M10-001) · usage display against limits · 80% warnings · trial with full access · 🔒 operator manual activation (FR-M10-008) · suspension with 🔒 **90-day read-only grace and export** (FR-M10-010) · 🔒 **GST invoicing with gapless per-financial-year numbering via a locked sequence table — not a Postgres SEQUENCE** · 🔒 **practitioner→client payment recording, method-agnostic, we never custody funds** (approved)

### Frontend tasks
Plan and usage display · upgrade prompts at limits with the upgrade path · trial status · invoice list and download · practitioner→client payment recording · billing settings

### Testing strategy
Limit enforcement at the point of action · 🔒 over-limit tenants retain **full read and edit** access (AC-M10-004) · downgrade over limit (EC-M10-01) · 🔒 **invoice numbering gapless under concurrent issuance and after rollback** · double payment reconciled (EC-M10-08) · suspension preserves data and export (AC-M10-006) · metered resources enforce independently (AC-M10-008)

### Definition of Done
🔒 Universal DoD, plus: AC-M10-001…008 pass · 🔒 **GST invoice format confirmed by an accountant** (ASM-09) · gapless numbering verified under concurrency · plan limits changeable without a release

### Deliverables
Plan management · usage visibility · trial · manual activation · GST invoicing · practitioner payment ledger

---

## S12 — Operator Console

**Objective** — support a practitioner without their password, and curate the food database properly.

**Modules** — `admin`
**Dependencies** — S11
**Effort** — **7–9d** · 🔒 Confidence: **High**

### Database migrations
`operator_actions`

### Backend tasks
Tenant search and account state · 🔒 **aggregates without client-identifying content** (FR-M11-003) · message delivery log · plan assignment, suspension, reactivation · 🔒 password reset trigger — never sets or sees a password · platform health · 🔒 **food curation with `affected_future_plans_only` in the response** (EC-M11-03) · search-miss review · dietary rules management 🔒 **(the OD-06 seam)** · 🔒 every operator read audited (FR-M0-032) · 🔒 **no cross-tenant export endpoint** (AC-M11-008)

### Frontend tasks
🔒 Operator console — deliberately austere · tenant search and detail · message log with failure reasons · plan management · food curation (create, correct, retire, aliases, portions) · search-miss queue ranked by demand · dietary rules editor · platform health · job queue and dead letters

### Testing strategy
🔒 Operator cannot authenticate without 2FA (AC-M11-004) · 🔒 realm crossing fails both directions (AC-M11-005) · 🔒 every operator action and read is audited (AC-M11-003) · 🔒 **no interface exposes cross-tenant export** (AC-M11-008) · curated correction does not alter issued plans (EC-M11-03) · aggregates contain no client-identifying content

### Definition of Done
🔒 Universal DoD, plus: AC-M11-001…008 pass · 🔒 **a real support scenario resolved without reading clinical content** (AC-M11-002) · 🔒 impersonation **not built** (deferred, FR-M11-012)

### Deliverables
Operator support console · food curation tooling · search-miss prioritisation · dietary rules management · platform health

---

## S13 — Launch Readiness

> 🔒 Every item here is a **blocking gate**. None is optional.

**Objective** — verified, compliant, recoverable, and safe to put real clinical data into.

**Modules** — all (hardening)
**Dependencies** — 🔒 **ALL sprints S0–S12** *(not just S12 — gates 4 and 14 require S8 and S5 respectively)* · ⏳ 🔒 privacy lawyer
**Effort** — **9–12d** · 🔒 Confidence: **Low** *(legal and external verification timing is outside our control)*

### Database migrations
🔒 **None.** No new tables — this sprint verifies and hardens what S0–S12 built. 🔒 Any migration discovered as necessary here is a defect in an earlier sprint, to be fixed as an expand–contract change (Arch §16.4).

### Backend tasks
🔒 DPDP erasure — 🔒 **verified to traverse object storage** (Arch §13.2) · data export for access requests · retention purge job · alerting for all 8 conditions (NFR-085) · rate limits verified on every surface · 🔒 **restore procedure executed and verified** (NFR-024) · storage backup verified or an export job built · post-restore file reconciliation

### Frontend tasks
Error state audit across all three apps · empty state audit · 🔒 click budget verification (NFR-011…019) · 🔒 performance verification on a **real mid-range Android over real 4G** · accessibility pass (NFR-060…062) · consent notice presentation · onboarding and first-run experience

### 🔒 Launch gates — all blocking

| # | Gate | Source |
|---|---|---|
| 1 | 🔒 **Full restore executed; application verified running against it** | NFR-024 |
| 2 | 🔒 **Object storage backup verified** | Arch §23 |
| 3 | 🔒 **Tenant isolation passes with application filters removed** | AC-M0-003 |
| 4 | 🔒 **AI safety corpus 100% clean** | AC-M5-003 |
| 5 | 🔒 **Erasure traverses storage — no orphaned clinical files** | §13.2 |
| 6 | 🔒 **Pooler confirmed in transaction mode** | DB §2.3 |
| 7 | 🔒 **`app_user` lacks `BYPASSRLS`; service key never used for data access** | DB §2.4 |
| 8 | 🔒 Performance budgets met on real devices | NFR-001…010 |
| 9 | 🔒 Click budgets met | NFR-011…019 |
| 10 | 🔒 **Privacy lawyer sign-off, incl. OD-05 minor consent** | ASM-10 |
| 11 | 🔒 GST treatment confirmed | ASM-09 |
| 12 | 🔒 Every alert fired and received | NFR-085 |
| 13 | 🔒 No clinical data in logs — audited | NFR-033 |
| 14 | 🔒 WhatsApp templates approved and sending | ASM-03 |

### Testing strategy
🔒 **This sprint is entirely verification — the Launch Gates table above IS the test plan.** No new feature tests; instead every gate is executed once, in a production-equivalent environment, with evidence recorded. 🔒 Gates 1, 2, 3, 5 and 6 are destructive or infrastructural and MUST be run against a scratch environment restored from a real backup, not against staging as it stands.

### Definition of Done
🔒 **All 14 gates pass.** No exceptions, no "we'll fix it after launch."

### Deliverables
🔒 Production-ready, compliance-verified, recoverable platform

---

## 6. Risk Register

| Risk | Impact | Likelihood | Mitigation | Sprint |
|---|---|---|---|---|
| 🔒 **Pooler in session mode** | 🔴 Total isolation failure | Low | Verify in S0; confirm S1 | S0/S1 |
| 🔒 **Meta verification rejected** | 🔴 No delivery channel | Medium | Start S0; email fallback keeps S5 shipping | S0 |
| 🔒 **Food data insufficient** | 🔴 Practitioners reject the wedge | Medium | G1 gate; search-miss telemetry from S3 | S3 |
| 🔒 **IFCT licensing unavailable** | 🔴 Moat has no base | Medium | Resolve in S0; fallback = manual curation | S0 |
| 🔒 **PDF worse than their Word template** | 🔴 No one switches | Medium | G2 gate; iterate in HTML | S4 |
| **PDF rendering exhausts worker memory** | 🟠 Cost ceiling breached | Medium | One render at a time; measure in S4 | S4 |
| 🔒 **Clients don't use the PWA** | 🔴 Engagement model fails | Medium | G3 pilot; WhatsApp remains primary | S6 |
| **Offline sync bugs corrupt data** | 🟠 Trust damage | Medium | Idempotency; append-only logs; heavy testing | S6 |
| 🔒 **Timeline slips past runway** | 🔴 Project fails | **High** | 🔒 **Option B paid pilot at S6** | §3 |
| **OD-05 unresolved at launch** | 🔴 Launch blocked | Medium | Lawyer engaged in S0 | S0 |
| **Solo-developer burnout** | 🔴 Project fails | Medium | ≤3-week sprints; every sprint demonstrable | All |
| **Scope creep from pilot feedback** | 🟠 Timeline extends | High | 🔒 Frozen documents; changes are explicit decisions | S6+ |

⚠️ 🔒 **The two highest-likelihood-times-impact risks are timeline slip and scope creep after the pilot.** Both are managed by the same discipline: the approved documents are frozen, and any change is an explicit decision with a stated cost — not an absorbed one.

---

## 7. Proposals Requiring Approval

| # | Proposal | § | If rejected |
|---|---|---|---|
| 1 | 🔒 **Option B — Paid Pilot at S6** | 3.2 | Choose A (full MVP, ~11 months) or C (further cuts) |
| 2 | 🔒 **UI/UX folded into sprints rather than a separate upfront phase** | 2.4 | Add a dedicated design phase (+10–15d) |
| 3 | Effort in focused days at 3–4/week | Header | Provide your actual availability |
| 4 | Sprint ordering S7–S12 | 2.1 | 🔒 The G3 pilot should re-order these anyway |
| 5 | SMS deferred; email as the S5 fallback | S5 | Add TRAI DLT to the critical path |
| 6 | Validation gates G1–G4 as hard stops | 4.4 | Make them advisory |
| 7 | `clinical_reference_ranges` built in S7 even though OD-08 is unresolved | S7 | Defer the table |
| 8 | Impersonation not built at all in S12 | S12 | Build it (⚠️ I would argue against) |

---

## 8. What I Need From You Before Sprint 1

| # | Decision | Why it blocks |
|---|---|---|
| 1 | 🔒 **Option A, B or C** | Changes what "done" means and when you sell |
| 2 | 🔒 **UI/UX: folded in, or a separate phase first?** | Changes the S2 start date |
| 3 | **Your realistic focused days per week** | Every calendar estimate depends on it |
| 4 | ⏳ **Confirm the S0 external track has started** | Meta, IFCT, lawyer, practitioners — these are the real long poles |

---

**END OF DOCUMENT**

*Phase 6 of 11 complete. Awaiting review and approval before Sprint 1.*
