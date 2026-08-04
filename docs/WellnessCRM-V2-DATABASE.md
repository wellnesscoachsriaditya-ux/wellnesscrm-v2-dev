# WellnessCRM V2 — Database Design

**Status:** Draft v0.1 — awaiting review
**Owner:** Founder / CTO
**Date:** 2026-08-04
**Phase:** 4 of 11 (Database Design)
**Derives from:** `docs/WellnessCRM-V2-PRD.md`, `docs/WellnessCRM-V2-ARCHITECTURE.md` (both approved)

---

## Document Control

### Purpose

The definitive data blueprint. Every table, column, relationship and constraint implementation will follow.

| Defines | Does not define |
|---|---|
| Tables, columns, types, constraints | SQL migration files (Phase 9) |
| Keys, relationships, cardinality | ORM model code (Phase 9) |
| Indexes and their justification | API endpoints (Phase 5) |
| RLS policies per table | Screen designs (Phase 6) |
| Module ownership and access rights | |
| Versioning, soft-delete, archival mechanics | |

### Conventions

> 🔒 **BINDING** — derives from an approved PRD/Architecture requirement.
> 🟡 **PROPOSAL** — new in this phase; requires approval.
> ⚠️ **RISK** — known hazard.
> 📌 **DDR-{nn}** — Database Decision Record (§28).

### Naming rules

🔒 Applied without exception; consistency is a maintainability property (NFR-071).

| Element | Rule | Example |
|---|---|---|
| Table | `snake_case`, **plural** | `diet_plan_versions` |
| Column | `snake_case`, singular | `issued_at` |
| Primary key | `id` | `id` |
| Foreign key | `<singular_referenced_table>_id` | `client_id` |
| Boolean | `is_` / `has_` prefix | `is_archived` |
| Timestamp | `_at` suffix, always `timestamptz` | `created_at` |
| Date (no time) | `_on` suffix | `scheduled_on` |
| Enum type | singular, `_type` or domain noun | `client_stage` |
| Index | `ix_<table>__<cols>` | `ix_clients__tenant_stage` |
| Unique index | `uq_<table>__<cols>` | `uq_foods__tenant_slug` |
| Check constraint | `ck_<table>__<rule>` | `ck_measurements__weight_range` |
| Foreign key | `fk_<table>__<ref>` | `fk_clients__owner_user` |

### Module ownership legend

Every table declares: **Owner** (the one module that may write it) · **Readers** (modules permitted read access via their own repository) · **Writers** (normally the owner alone).

🔒 Per Architecture R6: *a module MUST NOT read another module's tables.* Where a table shows readers beyond its owner, access is via a **kernel query port**, not a direct join. Declared explicitly per table.

---

## 1. Database Design Principles

| # | Principle | Source | Test |
|---|---|---|---|
| 1 | **Every table has exactly one owning module** | Arch §3 | If two modules would write it, the boundary is wrong |
| 2 | **Tenant isolation is enforced by the database** | NFR-030, ADR-06 | A forgotten `WHERE tenant_id` must not leak |
| 3 | **Immutable history for anything clinical or financial** | EC-M4-03, FR-M0-034 | Issued plans, audit and consent are append-only |
| 4 | **Normalise to 3NF, denormalise only with a measured reason** | User instruction | Every denormalisation names the query it serves |
| 5 | **Constraints in the database, not only the application** | NFR-037 | Invalid state must be unrepresentable |
| 6 | **Soft delete for user data; hard delete only via erasure** | FR-M1-010/011 | Nothing user-facing is destructively deleted |
| 7 | **No table designed for scale we will not reach** | NFR-093 | 200 tenants, 20k clients — no partitioning, no sharding |
| 8 | **Deterministic domain data is separate from AI artefacts** | User instruction | 🔒 AI output is never the source of truth |

### 1.1 Universal column conventions

🔒 Every **tenant-scoped** table carries:

| Column | Type | Purpose |
|---|---|---|
| `id` | `uuid` PK, default `gen_random_uuid()` | Surrogate key |
| `tenant_id` | `uuid` NOT NULL FK → `tenants.id` | 🔒 RLS discriminator (ADR-06) |
| `created_at` | `timestamptz` NOT NULL default `now()` | Audit baseline |
| `updated_at` | `timestamptz` NOT NULL default `now()` | Change tracking |

Where user attribution matters: `created_by_user_id`, `updated_by_user_id` (`uuid` FK → `users.id`).
Where soft delete applies (§22): `archived_at timestamptz NULL`, `archived_by_user_id uuid NULL`.

📌 **DDR-01 — UUID primary keys everywhere**

| Option | Verdict |
|---|---|
| `bigserial` | ❌ Sequential IDs leak tenant volume and enable enumeration in URLs |
| `uuid v4` | ✅ **Chosen.** Non-enumerable; safe in URLs and WhatsApp deep links; generatable client-side for offline queue (FR-M7-012) |
| `uuid v7` | 🟡 Better index locality, but ordering reveals creation time. Revisit if index bloat is measured |

**Cost:** 16 bytes vs 8, and random insert order causes some B-tree fragmentation. At 20k clients and ~500k total rows (NFR-092) this is immaterial. Enumeration safety on a clinical product is not.

### 1.2 Type choices

🔒 Postgres-specific, chosen deliberately:

| Concern | Type | Why |
|---|---|---|
| Timestamps | `timestamptz` **always** | NFR-099. Never `timestamp` — a naive timestamp is a bug waiting for GCC/UK expansion |
| Money | `numeric(12,2)` + `currency_code char(3)` | NFR-098. Never float |
| Nutrition values | `numeric(10,3)` | Exact decimal; float rounding in clinical calculations is unacceptable |
| Quantities/weights | `numeric(10,3)` | Same |
| Enumerations | Postgres `ENUM` for stable sets, lookup table for extensible ones | §1.3 |
| Free-form structure | `jsonb` | Only where genuinely schemaless (§1.4) |
| Phone | `text` with format check | NFR-100, E.164 |
| Text | `text`, never `varchar(n)` | No performance difference in Postgres; length limits belong in validation |

### 1.3 Enum vs lookup table

📌 **DDR-02 — Postgres ENUM for closed sets, lookup tables for open sets**

| Use ENUM when | Use lookup table when |
|---|---|
| Values are fixed by design | Users or operators may add values |
| Application logic branches on them | Values carry additional attributes |
| e.g. `client_stage`, `plan_state`, `job_status` | e.g. food categories, tags, message templates |

⚠️ **ENUM values can be added but not easily removed or reordered.** Accepted for the sets above because they are genuinely design-fixed — `client_stage` changing would be a product decision requiring a migration anyway (OD-01).

### 1.4 Where `jsonb` is permitted

🔒 Restricted deliberately. `jsonb` defeats constraints, foreign keys and type safety.

| Permitted use | Justification |
|---|---|
| `assessment_definitions.schema` | 🔒 FR-M3-002 — the definition *is* a document, versioned as a unit |
| `assessment_responses.answers` | Shape is defined by the definition version, not the database |
| `audit_log.metadata` | Heterogeneous by nature; never queried by structure |
| `ai_generations.request_snapshot` / `response_snapshot` | Diagnostic record of an external call |
| `message_dispatches.template_variables` | Varies per template |
| `plan_version_snapshot.document` | 🔒 Immutable issued-plan snapshot (§12.9) |

**Forbidden elsewhere.** Client data, nutrition values, appointments and billing use real columns. A `jsonb` blob for client attributes would make FR-M1-021 search and every constraint impossible.

---

## 2. Multi-Tenancy Strategy

🔒 ADR-06 — shared database, shared schema, RLS isolation.

### 2.1 The tenant boundary

```
tenants (the isolation and billing boundary)
   │
   ├── users            (practitioners, owners — the `practitioner` realm)
   ├── clients          (the practitioner's clients)
   ├── ...every tenant-scoped table
   │
   └── subscriptions    (billing state)
```

A solo practitioner is a tenant with one user holding both `owner` and `practitioner` roles. 🔒 No special case — this is what makes clinic-readiness (P4) free rather than a migration.

### 2.2 Three data classes

| Class | `tenant_id` | RLS | Examples |
|---|---|---|---|
| **Tenant data** | NOT NULL | Isolate by `app.tenant_id` | clients, plans, appointments, messages |
| **Shared catalogue** | NULLable | Readable if `NULL` or matching tenant | foods, portions, recipes |
| **Platform data** | absent | Operator-only or no RLS (not client-reachable) | tenants, plans-as-config, jobs, audit |

📌 **DDR-03 — nullable `tenant_id` for the shared catalogue**

🔒 M4.3 requires curated and custom foods to share one structure with an ownership discriminator, not parallel tables.

```
foods.tenant_id IS NULL  → curated, platform-owned, visible to all tenants
foods.tenant_id = <uuid> → custom, private to that tenant (FR-M4-013)
```

| Alternative | Why rejected |
|---|---|
| Two tables (`curated_foods`, `custom_foods`) | 🔒 Violates "no duplicate tables". Every query becomes a UNION; every FK becomes ambiguous |
| Single table + `is_curated` boolean + `tenant_id` | Redundant — nullability already encodes it, and a boolean permits the invalid state `is_curated=true, tenant_id=<uuid>` |
| **Nullable `tenant_id`** | ✅ **Chosen.** One table, one FK target, RLS expresses it naturally |

⚠️ **Consequence:** a `NOT NULL` tenant check cannot be blanket-applied. Catalogue tables are explicitly enumerated in §3.3 so this is deliberate, not an oversight.

### 2.3 Session variable mechanism

🔒 Arch §7.2. Per request transaction:

```
SET LOCAL app.tenant_id = '<uuid>';
SET LOCAL app.actor_role = '<role>';
SET LOCAL app.actor_id   = '<uuid>';
```

`SET LOCAL` is transaction-scoped, so it cannot leak across pooled connections.

⚠️ **Launch gate (Arch §23):** verify Supabase's pooler runs in transaction mode. **Session-mode pooling with connection reuse would break tenant isolation entirely.** This is the single highest-severity infrastructure assumption in the design.

### 2.4 Database roles

| Role | Used by | Grants |
|---|---|---|
| `app_user` | 🔒 Web + worker processes | CRUD on application tables. **RLS enforced.** No `BYPASSRLS`. No DDL |
| `app_migrator` | Migrations only | DDL. Not used at runtime |
| `app_readonly` | 🟡 Future analytics | SELECT only, RLS enforced |

🔒 **The application role MUST NOT have `BYPASSRLS`.** Using Supabase's service-role key for application queries would silently disable every policy in §8 — the exact V1 failure mode ADR-02 exists to prevent. Supabase's service key is used only for Auth and Storage administration, never for data access.

---

## 3. Schema Map

### 3.1 Ownership overview

| Schema group | Owning module | Tables |
|---|---|---|
| **Platform** | `kernel` | tenants, users, user_identities, roles, sessions, magic_links |
| **Governance** | `kernel` | audit_log, consent_purposes, consent_notices, consent_records, data_requests |
| **Entitlements** | `kernel` | plan_definitions, subscriptions, usage_counters, usage_events |
| **Storage** | `kernel` | files |
| **Jobs** | `kernel` | jobs, job_runs |
| **Clients** | `clients` | clients, client_notes, client_tags, tags, client_stage_history, client_assignments, timeline_events |
| **Leads** | `leads` | enquiry_forms, enquiry_submissions |
| **Clinical** | `clinical` | assessment_definitions, assessment_responses, measurements, consultation_notes, client_documents |
| **Nutrition** | `nutrition` | food_categories, foods, food_aliases, food_nutrients, measure_units, food_portions, recipes, recipe_items, meals, meal_items, diet_templates, template_days, template_slots, template_items, diet_plans, diet_plan_versions, plan_days, plan_slots, plan_items, plan_item_alternatives, plan_snapshots, supplements, plan_supplements, nutrition_targets, dietary_rules |
| **AI** | `ai_drafting` | ai_generations, ai_prompt_versions |
| **Appointments** | `appointments` | appointments, appointment_history |
| **Messaging** | `messaging` | message_templates, scheduled_messages, message_dispatches, checkin_schedules, notification_preferences |
| **Progress** | `progress` | adherence_logs, progress_snapshots |
| **Billing** | `billing` | invoices, payments, practitioner_payment_records |
| **Admin** | `admin` | operator_actions, food_search_misses |

**Total: ~70 tables.** Large, but each has a stated purpose and owner. The nutrition module accounts for ~27 of them — proportionate to it being the wedge.

### 3.2 High-level ERD

```
                    ┌──────────┐
                    │ tenants  │
                    └────┬─────┘
                         │ 1:N
        ┌────────────────┼────────────────┬──────────────┐
        ▼                ▼                ▼              ▼
   ┌─────────┐     ┌──────────┐   ┌──────────────┐ ┌──────────┐
   │  users  │     │ clients  │   │subscriptions │ │  files   │
   └────┬────┘     └────┬─────┘   └──────────────┘ └──────────┘
        │               │
        │      ┌────────┼────────┬──────────┬───────────┐
        │      ▼        ▼        ▼          ▼           ▼
        │ ┌─────────┐┌────────┐┌────────┐┌────────┐┌─────────┐
        │ │assess-  ││measure-││appoint-││ diet_  ││adherence│
        │ │ments    ││ments   ││ments   ││ plans  ││ _logs   │
        │ └─────────┘└────────┘└────────┘└───┬────┘└─────────┘
        │                                     │ 1:N
        │                            ┌────────▼──────────┐
        │                            │ diet_plan_versions│
        │                            └────────┬──────────┘
        │                                     │ 1:N
        │                            ┌────────▼──────────┐
        │                            │    plan_days      │
        │                            └────────┬──────────┘
        │                                     │ 1:N
        │                            ┌────────▼──────────┐
        │                            │    plan_slots     │
        │                            └────────┬──────────┘
        │                                     │ 1:N
        │                            ┌────────▼──────────┐
        │                            │    plan_items     │───┐
        │                            └───────────────────┘   │
        │                                                     │ refs
        │            SHARED CATALOGUE (tenant_id NULLable)    │
        │       ┌──────────┐  ┌──────────────┐  ┌──────────┐ │
        └──────▶│  foods   │◀─│food_portions │  │ recipes  │◀┘
                └────┬─────┘  └──────┬───────┘  └──────────┘
                     │               │
                ┌────▼─────┐  ┌──────▼───────┐
                │food_     │  │ measure_units│
                │nutrients │  └──────────────┘
                └──────────┘
```

### 3.3 Catalogue tables (nullable `tenant_id`)

🔒 Explicitly enumerated — every other tenant-scoped table is `NOT NULL`:

`food_categories` · `foods` · `food_aliases` · `food_nutrients` · `food_portions` · `recipes` · `recipe_items` · `supplements` · `measure_units` (platform-only, no tenant column) · `assessment_definitions` (platform-authored at MVP; tenant-authored in Phase 3, FR-M3-010).

---

## 4. Platform & Identity Schema

**Owner:** `kernel` · **Readers:** all modules via `kernel.tenancy` and `kernel.identity` ports · **Writers:** `kernel` only

### 4.1 `tenants`

**Why it exists:** the isolation and billing boundary. Every tenant-scoped row descends from here.

| Column | Type | Constraints | Purpose |
|---|---|---|---|
| `id` | uuid | PK | |
| `name` | text | NOT NULL | Practice or clinic name |
| `slug` | text | NOT NULL, UNIQUE | 🔒 Public enquiry form URL (FR-M2-001) |
| `region_code` | text | NOT NULL, default `'IN'` | 🔒 FR-M0-013 — region-aware without multi-region infra |
| `timezone` | text | NOT NULL, default `'Asia/Kolkata'` | NFR-099 |
| `currency_code` | char(3) | NOT NULL, default `'INR'` | NFR-098 |
| `status` | `tenant_status` | NOT NULL, default `'trial'` | `trial` \| `active` \| `suspended` \| `closed` |
| `suspended_at` | timestamptz | NULL | FR-M10-009 |
| `data_retention_days` | int | NOT NULL, default 🟡 `2555` | NFR-049; ~7 years |
| `created_at` / `updated_at` | timestamptz | NOT NULL | |

**Indexes:** `uq_tenants__slug`, `ix_tenants__status`
🔒 **No RLS** — platform table, never client-reachable. Access only through `kernel.tenancy`.

⚠️ **`slug` is publicly enumerable by design** (it is in the enquiry URL). It must never be derivable from a client name or contain a personal identifier.

### 4.2 `users`

**Why it exists:** practitioners and clinic owners. 🔒 The `practitioner` realm only — clients are **not** users (§4.4).

| Column | Type | Constraints | Purpose |
|---|---|---|---|
| `id` | uuid | PK | |
| `tenant_id` | uuid | NOT NULL FK | |
| `auth_subject_id` | text | NOT NULL, UNIQUE | 🔒 GoTrue identifier. **No password column** (Arch §2.3) |
| `email` | text | NOT NULL | |
| `full_name` | text | NOT NULL | |
| `mobile` | text | NULL, CHECK E.164 | |
| `role` | `user_role` | NOT NULL | `owner` \| `practitioner` (FR-M0-016) |
| `status` | `user_status` | NOT NULL, default `'invited'` | `invited` \| `active` \| `disabled` |
| `is_two_factor_enabled` | boolean | NOT NULL, default false | Phase 2 (FR-M0-008) |
| `last_active_at` | timestamptz | NULL | |
| `archived_at` | timestamptz | NULL | Soft delete (EC-M1-04) |

**Constraints:** `uq_users__tenant_email` (partial, where `archived_at IS NULL`) · `uq_users__auth_subject`
**Indexes:** `ix_users__tenant_status`
**RLS:** tenant-isolated.

🔒 **No password, hash or salt column exists.** Credentials live in GoTrue (NFR-029). This is a deliberate structural guarantee — we cannot leak what we do not store.

### 4.3 `operators`

**Why it exists:** platform staff (P5). 🔒 A **separate realm** — FR-M0-004 and AC-M11-005.

| Column | Type | Constraints |
|---|---|---|
| `id` | uuid | PK |
| `auth_subject_id` | text | NOT NULL, UNIQUE |
| `email` | text | NOT NULL, UNIQUE |
| `full_name` | text | NOT NULL |
| `is_two_factor_enabled` | boolean | NOT NULL, default **true** 🔒 |
| `status` | `user_status` | NOT NULL |

🔒 **No `tenant_id`.** An operator belongs to no tenant — that is precisely what makes them cross-tenant, and why every read they perform is audited (FR-M0-032).
🔒 **Separate table, not a role on `users`.** A role column would make realm separation an application check; a separate table plus separate signing keys (Arch §6.1) makes cross-realm authentication structurally impossible.

### 4.4 `client_access_grants`

**Why it exists:** clients access the portal without being `users` (FR-M0-005). This table is the `client` realm's identity anchor.

| Column | Type | Constraints | Purpose |
|---|---|---|---|
| `id` | uuid | PK | |
| `tenant_id` | uuid | NOT NULL FK | |
| `client_id` | uuid | NOT NULL FK → clients | |
| `status` | `access_status` | NOT NULL, default `'active'` | `active` \| `revoked` |
| `last_accessed_at` | timestamptz | NULL | Feeds at-risk detection (FR-M9-001) |

**Constraint:** `uq_client_access_grants__client` — one grant per client.

### 4.5 `magic_links`

**Why it exists:** 🔒 passwordless client access. Single-use (EC-M0-01), short-lived, audited.

| Column | Type | Constraints | Purpose |
|---|---|---|---|
| `id` | uuid | PK | |
| `tenant_id` | uuid | NOT NULL FK | |
| `client_id` | uuid | NOT NULL FK | |
| `token_hash` | text | NOT NULL, UNIQUE | 🔒 **Hash only — never the token** |
| `purpose` | `link_purpose` | NOT NULL | `portal` \| `assessment` \| `plan_view` — enables deep links (FR-M7-013) |
| `target_ref` | text | NULL | Deep-link destination |
| `expires_at` | timestamptz | NOT NULL | 🔒 **15–30 minutes** (approved refinement) |
| `consumed_at` | timestamptz | NULL | Single-use enforcement |
| `issued_via` | `transport_type` | NOT NULL | whatsapp \| sms \| email |

**Indexes:** `uq_magic_links__token_hash` · `ix_magic_links__client_expires`

📌 **DDR-04 — store a hash of the magic-link token, never the token**

A database read must not yield working credentials. Verification hashes the presented token and looks up the hash. Consumption sets `consumed_at`; **enforcement is the unique constraint plus a conditional update**, not an application check — that closes the double-redemption race.

⚠️ 🔒 **The approved 15–30 minute expiry makes re-request a routine path, not an error path.** A client opening a WhatsApp message an hour later will find the link expired. **EC-M7-01 self-service re-request must be one tap and must not require the practitioner** — otherwise this becomes the product's most common support burden. Flagged for Phase 6.

### 4.6 `sessions`

**Why it exists:** 🔒 ~30-day sessions with secure renewal (approved refinement). Rotating refresh tokens with reuse detection.

| Column | Type | Constraints | Purpose |
|---|---|---|---|
| `id` | uuid | PK | |
| `realm` | `auth_realm` | NOT NULL | `practitioner` \| `client` \| `operator` |
| `tenant_id` | uuid | NULL | NULL for operators |
| `subject_id` | uuid | NOT NULL | user / client / operator id |
| `refresh_token_hash` | text | NOT NULL, UNIQUE | 🔒 Hash only |
| `previous_token_hash` | text | NULL | 🔒 Reuse detection |
| `issued_at` | timestamptz | NOT NULL | |
| `expires_at` | timestamptz | NOT NULL | ~30 days for clients/practitioners; short for operators |
| `rotated_at` | timestamptz | NULL | |
| `revoked_at` | timestamptz | NULL | Logout, password change (NFR-042) |
| `revocation_reason` | text | NULL | |
| `user_agent_hash` | text | NULL | 🔒 Hashed — not raw (NFR-033) |

📌 **DDR-05 — rotating refresh tokens with reuse detection**

🔒 "Secure session renewal" (approved refinement) means specifically: each refresh issues a new token and invalidates the old. **If a previously-rotated token is presented, the entire session family is revoked** — that is the signature of a stolen token being replayed, and the correct response is to log everyone out of that session rather than allow the attacker in.

Without reuse detection, a 30-day session with renewal is effectively a 30-day bearer credential that never expires for an attacker who captures it once.

---

## 5. Clients Schema

**Owner:** `clients` · **Readers:** via kernel ports — `clinical`, `nutrition`, `appointments`, `messaging`, `progress` all need client identity/stage · **Writers:** `clients` only

### 5.1 `clients`

**Why it exists:** 🔒 **The spine.** M1.3 — leads and clients are **one entity** distinguished by lifecycle stage. This is the single most important modelling decision in the schema.

| Column | Type | Constraints | Purpose |
|---|---|---|---|
| `id` | uuid | PK | |
| `tenant_id` | uuid | NOT NULL FK | |
| `stage` | `client_stage` | NOT NULL, default `'lead'` | 🔒 §5.2 |
| `full_name` | text | NOT NULL | FR-M1-005 |
| `mobile` | text | NULL, CHECK E.164 | NFR-100 |
| `email` | text | NULL | |
| `date_of_birth` | date | NULL | Age for calculations; minor consent (FR-M0-028) |
| `sex` | `sex_type` | NULL | Requirement estimation |
| `city` | text | NULL | |
| `preferred_language` | text | NOT NULL, default `'en'` | NFR-096 |
| `source` | text | NULL | FR-M2-009 |
| `source_detail` | text | NULL | Link parameter |
| `owner_user_id` | uuid | NOT NULL FK → users | FR-M1-009 |
| `dietary_class` | `dietary_class` | NULL | 🔒 Drives plan filtering (FR-M4-035) |
| `is_minor` | boolean | GENERATED from `date_of_birth` | FR-M0-028 |
| `activated_at` | timestamptz | NULL | First entry to `active` — check-in day (FR-M8-023) |
| `archived_at` | timestamptz | NULL | Soft delete |

**Constraints**
- `ck_clients__contact_present` — 🔒 `mobile IS NOT NULL OR email IS NOT NULL` (FR-M1-004, EC-M1-08)
- `ck_clients__stage_owner` — an `active` client must have an owner

**Indexes**
- `ix_clients__tenant_stage` — the client list (FR-M1-022)
- `ix_clients__tenant_owner_stage` — practitioner-scoped list (FR-M0-017)
- `ix_clients__tenant_mobile` — duplicate detection (EC-M2-02, FR-M1-024)
- `ix_clients__search` GIN on a generated `tsvector(full_name, mobile, email)` — 🔒 NFR-005 ≤300ms (FR-M1-021)

⚠️ **No unique constraint on `mobile`.** EC-M1-01 explicitly permits family members sharing a number. Duplicate detection is a *warning* (FR-M1-024), not a constraint. Enforcing uniqueness here would break a real Indian usage pattern.

🔒 **`dietary_class` lives on the client, not the plan** — it is a property of the person, and it must filter food search at authoring time (FR-M4-035), before a plan exists.

### 5.2 `client_stage` enum

🔒 M1.4. 🟡 Values remain PROPOSED pending OD-01.

```
lead · contacted · consultation_scheduled · active · paused · churned · archived
```

🔒 **Only `active` counts toward the plan limit** (M1.5). Entitlement counting is `WHERE stage = 'active' AND archived_at IS NULL` — one predicate, explainable in a sentence, fully under practitioner control.

### 5.3 `client_stage_history`

**Why it exists:** FR-M1-015 — every transition recorded with timestamp and actor. Append-only.

| Column | Type | Notes |
|---|---|---|
| `id` / `tenant_id` / `client_id` | uuid | |
| `from_stage` / `to_stage` | `client_stage` | `from_stage` NULL on creation |
| `changed_by_user_id` | uuid NULL | NULL when system-driven |
| `reason` | text NULL | |
| `changed_at` | timestamptz | |

🔒 **Separate from `audit_log`.** This is queryable domain history feeding the timeline and conversion metrics (FR-M9-006); the audit log is compliance evidence with different retention and immutability rules. Conflating them would force compliance-grade retention on operational data.

### 5.4 `client_notes`, `tags`, `client_tags`

**`client_notes`** (FR-M1-007): `id`, `tenant_id`, `client_id`, `body text NOT NULL`, `author_user_id`, `created_at`, `updated_at`, `archived_at`.
🔒 Author-editable with edits audited (FR-M3-020). Never client-visible (FR-M3-021) — enforced by authorization, and by these tables having no client-realm RLS policy.

**`tags`** (FR-M1-008): tenant-scoped, `uq_tags__tenant_name`, `color` for UI.
**`client_tags`**: junction, PK `(client_id, tag_id)`.

### 5.5 `client_assignments`

**Why it exists:** EC-M0-04 — a second practitioner granted access to a client in a clinic.

| Column | Type | Notes |
|---|---|---|
| `client_id` / `user_id` | uuid | PK together |
| `granted_by_user_id` | uuid | |
| `granted_at` | timestamptz | |
| `revoked_at` | timestamptz NULL | |

🔒 The **owning** practitioner is `clients.owner_user_id`; this table holds *additional* grants only. One owner, N grants — this prevents "which practitioner owns this client" ambiguity while supporting shared care.

### 5.6 `timeline_events`

**Why it exists:** 🔒 FR-M1-018 — a unified reverse-chronological timeline aggregating events from **six different modules**.

📌 **DDR-06 — materialised timeline table, written by event subscribers**

| Option | Verdict |
|---|---|
| UNION across 8+ module tables at read time | ❌ Cross-module reads violate R6. Query cost grows with every new event type. NFR-006 (≤800ms) at risk |
| Read-time aggregation in the application | ❌ Same R6 violation, plus N queries per timeline load |
| **Materialised table via domain events** | ✅ **Chosen.** Each module emits an event; a `clients` subscriber writes one row. One indexed query. Adding an event type touches no existing module |

| Column | Type | Notes |
|---|---|---|
| `id` / `tenant_id` / `client_id` | uuid | |
| `event_type` | `timeline_event_type` | Filtering (FR-M1-019) |
| `occurred_at` | timestamptz | |
| `source_module` | text | Provenance |
| `source_record_id` | uuid NULL | Deep link to the underlying record |
| `summary` | text | 🔒 **Non-clinical label only** — e.g. "Plan issued", never plan contents |
| `actor_type` / `actor_id` | | practitioner \| client \| system |

**Index:** `ix_timeline_events__client_occurred` (client_id, occurred_at DESC) — serves NFR-006 directly.

⚠️ **This is a deliberate denormalisation** (Principle 4). Justification: it is the only way to satisfy FR-M1-018 without violating module boundaries. **It is a derived projection, never a source of truth** — a rebuild job can regenerate it from module tables if it drifts.

🔒 **`summary` must not contain clinical values.** The timeline is the most-viewed screen and the most likely place for clinical data to leak into a log or an error report (NFR-033).

---

## 6. Leads Schema

**Owner:** `leads` · **Readers:** none · **Writers:** `leads`

### 6.1 `enquiry_forms`

**Why it exists:** FR-M2-001 — a public form per tenant. A table rather than a tenant column because Phase 2 allows multiple forms per tenant (FR-M2-012).

`id`, `tenant_id`, `slug` (unique per tenant), `title`, `intro_text`, `is_active`, `fields jsonb` (🟡 custom questions, Phase 2), `consent_notice_id` FK, `created_at`.

### 6.2 `enquiry_submissions`

**Why it exists:** 🔒 The raw record of what a prospect submitted, retained **separately from the client record it creates**.

| Column | Type | Notes |
|---|---|---|
| `id` / `tenant_id` | uuid | |
| `form_id` | uuid FK | |
| `client_id` | uuid FK NULL | The record created or matched |
| `submitted_name` / `submitted_mobile` / `submitted_email` | text | 🔒 As submitted, never mutated |
| `answers` | jsonb | Custom form answers |
| `consent_record_id` | uuid FK | FR-M2-004 |
| `is_duplicate_of_existing` | boolean | EC-M2-02 |
| `spam_score` | numeric NULL | FR-M2-008 |
| `submitted_at` | timestamptz | |

🔒 **Why keep the submission separate from the client record:** the client record is edited over time; the submission is evidence of what was consented to, at a specific notice version. DPDP requires demonstrating the consent basis (NFR-051), which means retaining the original submission — not the current state of an edited record.

⚠️ **Written by an unauthenticated endpoint** (Arch §15.3). Rate-limited, spam-scored, and 🔒 **must never reveal whether a mobile matches an existing client** — EC-M2-02 matching happens server-side, silently.

---

## 7. Clinical Schema

**Owner:** `clinical` · **Readers:** `nutrition` and `ai_drafting` need assessment + measurement data for calculations and grounding (via kernel port) · **Writers:** `clinical`

### 7.1 Design intent — the approved refinement

🔒 Per the approved refinement: *"The Nutrition Assessment framework should begin early with the minimum data required for nutrition calculations. Advanced clinical assessment sections can be introduced later, but the underlying assessment architecture should support future expansion without redesign."*

**This means the real versioned framework ships at S3**, seeded with a v1 definition containing only what nutrition calculations need. Clinical depth arrives as **new definition versions**, not new tables and not a migration.

📌 **DDR-07 — assessments are versioned document definitions, not columns**

| Option | Verdict |
|---|---|
| Columns per assessment field | ❌ Every §9 change is a migration. Directly contradicts FR-M3-002 and the approved refinement |
| EAV (entity-attribute-value) | ❌ No type safety, unreadable queries, notoriously painful |
| **Versioned `jsonb` definition + `jsonb` responses** | ✅ **Chosen.** Structure changes without migration (FR-M3-002); old responses stay readable under their original version (FR-M3-003) |

⚠️ **The trade-off, stated plainly:** answers inside `jsonb` cannot be constrained by the database or joined efficiently. **This is why §7.4 exists** — the handful of values that drive deterministic calculation are *projected into real typed columns*, and only the rest stay in `jsonb`. That split is the whole design.

### 7.2 `assessment_definitions`

**Why it exists:** the versioned form structure. Platform-authored at MVP; tenant-authored in Phase 3 (FR-M3-010) — hence nullable `tenant_id`.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `tenant_id` | uuid NULL | NULL = platform-authored |
| `code` | text NOT NULL | e.g. `nutrition_core` |
| `version` | int NOT NULL | 🔒 Monotonic per `code` |
| `title` | text NOT NULL | |
| `schema` | jsonb NOT NULL | Sections, fields, types, validation, conditional visibility |
| `calculation_bindings` | jsonb NOT NULL | 🔒 §7.4 — which fields project to typed columns |
| `status` | `definition_status` | `draft` \| `published` \| `retired` |
| `published_at` | timestamptz NULL | |

**Constraint:** `uq_assessment_definitions__code_version_tenant`
🔒 **A published definition is immutable.** Changes create a new version. This is what makes FR-M3-003 true rather than aspirational.

**Seeded v1 (`nutrition_core`)** — 🔒 minimum for nutrition calculation only, per the approved refinement: date of birth, sex, height, current weight, activity level, primary goal, dietary class, allergies, food exclusions, religious fasting, staple grain, region.
**Later versions add** medical history, biochemical, FFQ, household context, behavioural readiness (PRD §9 D–L, pending OD-06…14).

### 7.3 `assessment_responses`

| Column | Type | Notes |
|---|---|---|
| `id` / `tenant_id` / `client_id` | uuid | |
| `definition_id` | uuid FK | 🔒 Pins the version (FR-M3-003) |
| `answers` | jsonb NOT NULL | Keyed by field id |
| `status` | `response_status` | `in_progress` \| `completed` |
| `completed_sections` | text[] | Resumability (FR-M3-005) |
| `completed_by` | `actor_type` | Client or practitioner (FR-M3-004) |
| `started_at` / `completed_at` / `updated_at` | timestamptz | |

🔒 **Multiple responses per client are expected** (FR-M3-007) — repeat administration for comparison. No unique constraint on `client_id`.
**Index:** `ix_assessment_responses__client_completed` (client_id, completed_at DESC)

⚠️ **EC-M3-03** — a client mid-completion when a new version publishes finishes under the version they started. Guaranteed by `definition_id` being fixed at creation.

### 7.4 `client_nutrition_profile` — the projection

**Why it exists:** 🔒 **The bridge between flexible assessment and deterministic calculation.** This is the table that makes DDR-07's trade-off acceptable.

Nutrition and AI need typed, constrained, queryable values. They must not parse `jsonb` — that would put assessment-schema knowledge inside the nutrition module, coupling them permanently.

| Column | Type | Notes |
|---|---|---|
| `id` / `tenant_id` | uuid | |
| `client_id` | uuid UNIQUE | One current profile per client |
| `source_response_id` | uuid FK | Provenance |
| `date_of_birth` | date NULL | |
| `sex` | `sex_type` NULL | |
| `height_cm` | numeric(5,1) NULL | |
| `activity_level` | `activity_level` NULL | |
| `primary_goal` | `goal_type` NULL | |
| `dietary_class` | `dietary_class` NULL | 🔒 Mirrors `clients.dietary_class` |
| `excludes_onion_garlic` | boolean NOT NULL default false | 🔒 Jain/observant (PRD §9.9) |
| `excludes_root_vegetables` | boolean NOT NULL default false | Jain |
| `allergen_food_ids` | uuid[] NOT NULL default '{}' | 🔒 **Safety-critical** (FR-M5-006) |
| `excluded_food_ids` | uuid[] NOT NULL default '{}' | Dislikes |
| `fasting_patterns` | text[] | PRD §9.9 |
| `staple_grain` / `region_cuisine` | text NULL | AI grounding (Arch §9.2) |
| `updated_at` | timestamptz | |

🔒 **Written only by `clinical`**, on assessment completion, driven by `calculation_bindings`. **Read by `nutrition` and `ai_drafting` via a kernel port.**

📌 **DDR-08 — project calculation-critical fields into typed columns**

This is a deliberate, justified denormalisation (Principle 4). It buys:
1. **Type safety and constraints** where correctness is clinical.
2. **Module decoupling** — nutrition never parses an assessment document.
3. **Indexable filters** — allergen exclusion applies inside the food-search query (Arch §8.4), not after.
4. **Stable contract** — the assessment schema can change freely; the projection contract does not.

⚠️ **`allergen_food_ids` is safety-critical.** It is the input to the AI candidate-set filter (ADR-10). It must be populated by explicit food selection, **never by free-text parsing** — "nuts" typed into a text box cannot be matched to food IDs reliably, and a missed allergen is a clinical incident. Flagged for Phase 6: allergy capture must be a food-picker, not a text field.

### 7.5 `measurements`

**Why it exists:** FR-M3-011…015. Dated, longitudinal.

| Column | Type | Notes |
|---|---|---|
| `id` / `tenant_id` / `client_id` | uuid | |
| `measured_on` | date NOT NULL | |
| `weight_kg` | numeric(5,2) NULL | CHECK 2–500 |
| `height_cm` | numeric(5,1) NULL | CHECK 30–275 |
| `waist_cm` / `hip_cm` | numeric(5,1) NULL | |
| `body_fat_pct` | numeric(4,1) NULL | Practitioner-entered |
| `source` | `measurement_source` | `practitioner` \| `client` \| `device` |
| `recorded_by_user_id` | uuid NULL | |
| `is_flagged_implausible` | boolean default false | EC-M3-02 |
| `notes` | text NULL | |

🔒 **BMI and waist-hip ratio are NOT stored** (FR-M3-012) — they are derived. Storing them would create two sources of truth and permit drift.

⚠️ **BMI *thresholds* are a separate matter (OD-08).** Indian cut-offs differ from WHO. Thresholds belong in a **configuration table, not application code**, so they can be corrected without a release once sourced. 🟡 **PROPOSAL — `clinical_reference_ranges`**: `metric`, `population`, `sex`, `age_range`, `band_label`, `min_value`, `max_value`, `source_citation`, `effective_from`. 🔒 Nothing displays a threshold until OD-08 is resolved and cited.

**Indexes:** `ix_measurements__client_date` (client_id, measured_on DESC) — trends (FR-M3-014)
**Constraint:** 🟡 no uniqueness on `(client_id, measured_on)` — EC-M3-05 permits practitioner and client values on the same date, both retained with source attribution.

### 7.6 `consultation_notes`, `client_documents`

**`consultation_notes`** (FR-M3-018…021): `client_id`, `appointment_id NULL`, `note_date`, `body text`, `structured jsonb NULL` (🟡 PROPOSED mode), `author_user_id`, `archived_at`.
🔒 Never client-visible — no client-realm RLS policy grants access.

**`client_documents`** (FR-M3-024…027): `client_id`, `file_id` FK → `files`, `document_type`, `document_date`, `uploaded_by` (`practitioner` \| `client`), `description`.
🔒 Metadata only — bytes live in object storage (§19).

---

## 8. Nutrition Engine Schema ⭐

**Owner:** `nutrition` · **Readers:** `ai_drafting` (candidate sets), `progress` (adherence targets) via kernel ports · **Writers:** `nutrition`; `admin` writes catalogue rows via a dedicated curation port

🔒 The wedge and the moat (M4.2). Designed for the extensibility you specified: nutrition calculations, body composition, food database, household measures, recipes, meals, diet templates, meal locking, supplement catalogue, medical/dietary rule enforcement, AI-assisted drafting, PDF snapshot versioning, client portal sync.

🔒 **Governing principle: AI is an assistant layered on deterministic nutrition logic, never the source of truth.** Structurally: `ai_generations` (§14) records what the model produced; **plan tables hold only validated, practitioner-owned data.** No plan table has an "AI-generated" content column — provenance is recorded, content is not delegated.

### 8.1 Catalogue ERD

```
food_categories ──┐
                  │ 1:N
                  ▼
              ┌───────┐  1:N   ┌──────────────┐
              │ foods │───────▶│ food_aliases │  vernacular names (FR-M4-009)
              └───┬───┘        └──────────────┘
                  │ 1:N
        ┌─────────┼──────────┬─────────────────┐
        ▼         ▼          ▼                 ▼
 ┌──────────┐ ┌────────────┐ ┌─────────┐ ┌──────────┐
 │food_     │ │food_       │ │food_tags│ │food_     │
 │nutrients │ │portions    │ └────┬────┘ │dietary_  │
 └──────────┘ └─────┬──────┘      │      │flags     │
                    │ N:1         ▼      └──────────┘
              ┌─────▼──────┐  ┌──────┐
              │measure_    │  │ tags │
              │units       │  └──────┘
              └────────────┘

 recipes ──1:N──▶ recipe_items ──▶ foods
 meals   ──1:N──▶ meal_items   ──▶ foods | recipes
```

### 8.2 `food_categories`

`id`, `tenant_id NULL`, `code`, `name`, `parent_id NULL` (self-FK, one level), `sort_order`, `is_active`.
Hierarchical for the PRD M4.4 grouping (cereals, pulses, dairy, prepared dishes…). 🔒 One level of nesting only — deeper hierarchies are unnecessary and complicate every query.

### 8.3 `foods` — the moat

**Why it exists:** 🔒 The core of the differentiator. Curated Indian food data foreign competitors do not have (M4.2).

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `tenant_id` | uuid **NULL** | 🔒 DDR-03 — NULL = curated, set = tenant-custom (FR-M4-013) |
| `category_id` | uuid FK | |
| `name` | text NOT NULL | Display name |
| `slug` | text NOT NULL | Stable reference |
| `description` | text NULL | |
| `food_type` | `food_type` | `ingredient` \| `prepared_dish` \| `packaged` \| `beverage` |
| `dietary_class` | `dietary_class` NOT NULL | 🔒 veg/eggetarian/non-veg/vegan/jain — drives FR-M4-035 |
| `contains_onion_garlic` | boolean NOT NULL default false | 🔒 Jain/observant filtering |
| `is_root_vegetable` | boolean NOT NULL default false | Jain |
| `base_quantity_g` | numeric(8,2) NOT NULL default 100 | Nutrient reference quantity |
| `default_portion_id` | uuid FK NULL | UX default |
| `meal_suitability` | `meal_slot_type[]` | FR-M4-011 |
| `source` | text NULL | 🔒 e.g. `IFCT2017` — provenance for clinical defensibility |
| `source_reference` | text NULL | Row/code in the source |
| `verification_status` | `verification_status` | `verified` \| `unverified` \| `user_submitted` |
| `search_vector` | tsvector GENERATED | 🔒 §8.5 |
| `is_active` | boolean NOT NULL default true | Retire without deleting (EC-M4-04) |
| `created_by_user_id` | uuid NULL | For custom foods |

**Constraints:** `uq_foods__tenant_slug` (nulls not distinct, so curated slugs are globally unique)
**Indexes:** `ix_foods__search` GIN(search_vector) · `ix_foods__tenant_active_category` · `ix_foods__name_trgm` GIN trigram (fuzzy/typo tolerance)

🔒 **RLS:** `tenant_id IS NULL OR tenant_id = current_setting('app.tenant_id')` — one policy expressing both curated visibility and custom privacy.

🔒 **`source` and `verification_status` exist for clinical defensibility.** A practitioner must be able to see that a value came from IFCT 2017 rather than another practitioner's guess. This distinction is also what lets us later promote a well-used custom food into the curated set (FR-M4-017).

⚠️ **EC-M4-04 — a custom food used in issued plans must not be deletable.** `is_active = false` retires it from search while historical plans still resolve. Combined with §12.9 snapshots, issued plans remain correct regardless.

### 8.4 `food_nutrients`

📌 **DDR-09 — nutrients as rows, not columns**

| Option | Verdict |
|---|---|
| Columns (`energy_kcal`, `protein_g`, …) | ❌ Adding micronutrients (FR-M4-015, Phase 2) is a migration each time. Sparse nulls |
| **Row per (food, nutrient)** | ✅ **Chosen.** Phase 2 micronutrients need zero schema change — exactly the extensibility you asked for |

| Column | Type | Notes |
|---|---|---|
| `food_id` | uuid FK | PK part |
| `nutrient_code` | text | PK part — `energy_kcal`, `protein_g`, `carbohydrate_g`, `fat_g`, `fibre_g` at MVP |
| `amount` | numeric(10,3) NOT NULL | Per `foods.base_quantity_g` |
| `source` | text NULL | |

**Companion `nutrients` lookup:** `code`, `display_name`, `unit`, `category` (macro/micro), `sort_order`, `is_mvp`.

⚠️ **Trade-off:** totalling requires aggregation rather than a column sum. At realistic plan sizes (~40 items × 5 nutrients) this is trivial, and §8.11 defines where the aggregation lives.

### 8.5 `food_aliases` — vernacular search

**Why it exists:** 🔒 FR-M4-009. `chapati` / `roti` / `phulka` are the same food. **This is curation, and it is why the moat cannot be scraped.**

`food_id`, `alias`, `language_code`, `region_code NULL`, `is_primary`.
Folded into `foods.search_vector` (§8.3) so aliases match without a join.

### 8.6 `measure_units` and `food_portions` — household measures

🔒 **Where foreign products fail** (M4.5).

**`measure_units`** — platform-only, no tenant column: `code` (`katori`, `cup`, `glass`, `tbsp`, `tsp`, `piece`, `slice`, `number`, `g`, `ml`), `display_name`, `display_name_hi`, `unit_type` (`volume`/`count`/`weight`), `reference_ml NULL`, `sort_order`.

**`food_portions`** — 🔒 **the conversion table** (FR-M4-003: per food, never global):

| Column | Type | Notes |
|---|---|---|
| `id` / `food_id` / `measure_unit_id` | uuid | |
| `tenant_id` | uuid NULL | Custom measures Phase 2 (FR-M4-007) |
| `quantity` | numeric(6,2) NOT NULL default 1 | e.g. 1 katori, 2 pieces |
| `grams` | numeric(8,2) NOT NULL | 🔒 **The conversion** |
| `size_label` | text NULL | 🟡 `small`/`medium`/`large` |
| `is_default` | boolean | |
| `source` | text NULL | Provenance |

**Constraint:** `uq_food_portions__food_unit_qty_size`
🔒 **This table is the single source of truth for portion conversion** (FR-M4-004, NFR-072). Every gram figure in the system resolves through it.

⚠️ **1 katori is not a fixed weight** — 1 katori of cooked dal ≠ 1 katori of dry poha. That is exactly why conversion is per food. 🟡 **The reference values themselves remain PROPOSED pending OD-03** — the schema is right; the data needs a practitioner.

⚠️ **Foods without a portion row** (Arch §8.3): quantity expressible in grams only, no error. The UI should prompt for a household equivalent at custom-food creation (approved proposal #3).

### 8.7 `recipes`, `recipe_items`

**Why:** M4.3 level 3. Seeded at MVP (FR-M4-021); practitioner-authored in Phase 2 (FR-M4-022) — hence nullable `tenant_id`.

**`recipes`**: `tenant_id NULL`, `name`, `description`, `cuisine_region`, `yield_quantity`, `yield_unit_id`, `servings`, `dietary_class`, `preparation_notes`, `is_active`.
**`recipe_items`**: `recipe_id`, `food_id`, `quantity`, `measure_unit_id`, `sort_order`, `is_optional`.

🔒 Recipe nutrition is **derived from its items** (FR-M4-019), never stored. `servings` divides the total for per-serving values.

### 8.8 `meals`, `meal_items`

**Why:** M4.3 level 4 — reusable named sets, practitioner-owned (FR-M4-018/020).

**`meals`**: `tenant_id NOT NULL` 🔒 (always tenant-owned), `name`, `slot_type`, `notes`, `is_favourite`, `usage_count` 🟡 (surfaces frequently-used meals and feeds AI grounding), `archived_at`.
**`meal_items`**: `meal_id`, `item_type` (`food`\|`recipe`), `food_id NULL`, `recipe_id NULL`, `quantity`, `measure_unit_id`, `sort_order`, `notes`.

**Constraint:** `ck_meal_items__one_reference` — exactly one of `food_id`/`recipe_id` is set.

📌 **DDR-10 — polymorphic item references via mutually-exclusive nullable FKs**

Plan and meal items may reference a food *or* a recipe. Options: separate tables per type (duplication), a generic `item_id` with no FK (loses referential integrity), or two nullable FKs with a check constraint. **Chosen: nullable FKs + check.** Real foreign keys are preserved — losing referential integrity on the nutrition engine would be a serious defect.

### 8.9 Diet templates

🔒 M4.3 level 5 — the practitioner's accumulating library, and a primary switching cost (M4.2).

```
diet_templates ──1:N──▶ template_days ──1:N──▶ template_slots ──1:N──▶ template_items
```

**`diet_templates`**: `tenant_id NOT NULL`, `name`, `description`, `goal_type NULL`, `condition_tags text[]` 🟡 (e.g. PCOS, diabetes — for template discovery), `dietary_class NULL`, `target_energy_kcal NULL`, `day_count` (1 or 7), `usage_count`, `created_by_user_id`, `archived_at`.
**`template_days`**: `template_id`, `day_number`, `label`.
**`template_slots`**: `template_day_id`, `slot_type`, `custom_label NULL`, `target_time NULL`, `sort_order`. 🔒 Slots are reorderable/renamable (FR-M4-025).
**`template_items`**: `template_slot_id`, `item_type` (food\|recipe\|meal), three nullable FKs + check, `quantity`, `measure_unit_id`, `notes`, `is_locked` 🔒 (§8.10), `sort_order`.

### 8.10 Meal locking

🔒 You specified this explicitly. Not in the PRD — 🟡 **flagged as a new capability requiring approval.**

**`is_locked boolean NOT NULL default false`** on `template_items`, `plan_items`, and `plan_slots`.

**Semantics (🟡 PROPOSED, needs your confirmation):**

| Locked entity | Meaning |
|---|---|
| `plan_item.is_locked` | 🔒 **AI drafting and auto-substitution must not alter this item.** Practitioner has deliberately fixed it |
| `plan_slot.is_locked` | The entire slot is fixed — no items added, removed or changed by automation |
| `template_item.is_locked` | Instantiating the template carries the lock forward |

🔒 **Enforcement is deterministic, in `nutrition`, not a prompt instruction.** Locked items are excluded from the AI's mutable set before generation and re-verified after (consistent with ADR-10). This is the same pattern as allergy enforcement: the model is never trusted to respect a constraint.

**Why it matters:** a practitioner who has hand-tuned a client's breakfast must be able to regenerate the rest of the plan without losing it. Without locking, AI regeneration is destructive and practitioners will stop using it.

### 8.11 Diet plans and versions

🔒 FR-M4-033 — the core of the weekly follow-up loop (J2).

```
diet_plans (the ongoing intent for a client)
   └─1:N─▶ diet_plan_versions (immutable once issued)
              └─1:N─▶ plan_days ─1:N─▶ plan_slots ─1:N─▶ plan_items
                                                            └─1:N─▶ plan_item_alternatives
```

📌 **DDR-11 — plan / version split with copy-on-revise**

| Option | Verdict |
|---|---|
| One plan row, mutate in place | ❌ Destroys history. Violates FR-M4-033 and EC-M4-03 |
| Full snapshot per edit | ❌ Enormous churn during authoring |
| **Plan + versions; edit the draft, freeze on issue** | ✅ **Chosen.** Live editing while `draft`; immutable once `issued` |

**`diet_plans`**: `tenant_id`, `client_id`, `title`, `goal_type NULL`, `created_by_user_id`, `current_version_id NULL`, `archived_at`.

**`diet_plan_versions`**:

| Column | Type | Notes |
|---|---|---|
| `id` / `tenant_id` / `plan_id` | uuid | |
| `version_number` | int NOT NULL | Monotonic per plan |
| `state` | `plan_state` NOT NULL | 🔒 `draft` \| `issued` \| `superseded` \| `discarded` |
| `origin` | `plan_origin` NOT NULL | 🔒 `manual` \| `template` \| `ai_draft` \| `revision` (FR-M5-005) |
| `source_template_id` / `source_version_id` / `ai_generation_id` | uuid NULL | Provenance |
| `valid_from` / `valid_to` | date NULL | FR-M4-034 |
| `target_energy_kcal` etc. | numeric NULL | Targets at issue (FR-M4-028) |
| `computed_totals` | jsonb NULL | 🔒 Populated **only on issue** (§8.12) |
| `issued_at` / `issued_by_user_id` | | |
| `practitioner_notes` | text NULL | FR-M4-029 |
| `row_version` | int NOT NULL default 1 | 🔒 Optimistic concurrency (ADR-14, EC-M4-07) |

**Constraints**
- `uq_diet_plan_versions__plan_version`
- 🔒 `uq_diet_plan_versions__one_draft` — partial unique on `plan_id WHERE state='draft'`. **One draft at a time, enforced by the database**
- 🔒 `ck_diet_plan_versions__issued_has_totals` — `state='issued'` requires `issued_at` and `computed_totals`

🔒 **State machine — there is no transition from `draft` to delivered.** Delivery requires `issued`, and `issued` requires an explicit practitioner action. **FR-M5-003 (no AI auto-send) is enforced by the schema, not by application logic.**

**`plan_days`** / **`plan_slots`** / **`plan_items`** mirror the template hierarchy, plus on `plan_items`: `is_locked` (§8.10), `client_note` (free text shown to the client), `resolved_grams` 🔒 (denormalised at issue — §8.12).
**`plan_item_alternatives`** (FR-M4-030): `plan_item_id`, item reference, `quantity`, `measure_unit_id`, `sort_order`.

### 8.12 Computed vs snapshotted nutrition

🔒 **ADR-07** — the tension between FR-M4-027 (live totals while editing) and EC-M4-03 (issued plans retain values in force at issue).

| Plan state | Nutrition values | Where from |
|---|---|---|
| `draft` | 🔒 **Computed on read** | Live join to `food_nutrients` and `food_portions` |
| `issued` | 🔒 **Snapshotted** | `computed_totals` + `plan_items.resolved_grams` + `plan_snapshots` |

**On issue, atomically:** resolve every item's grams via `food_portions`, compute per-slot/day/plan totals, write `computed_totals`, freeze `resolved_grams`, generate the immutable `plan_snapshots` document, set `state='issued'`.

🔒 **After issue, a curated food correction (EC-M11-03) cannot alter the issued plan.** New plans use corrected values; history is preserved. This is a clinical-record integrity requirement, not an optimisation.

### 8.13 `plan_snapshots` — the PDF/portal snapshot

**Why it exists:** you specified *PDF snapshot versioning* and *client portal synchronization*. This one table serves both.

| Column | Type | Notes |
|---|---|---|
| `id` / `tenant_id` | uuid | |
| `plan_version_id` | uuid FK UNIQUE | One snapshot per issued version |
| `document` | jsonb NOT NULL | 🔒 **Fully denormalised, self-contained rendering payload** |
| `document_schema_version` | int NOT NULL | Renderer compatibility |
| `pdf_file_id` | uuid FK NULL | Generated asynchronously (ADR-09) |
| `pdf_generated_at` | timestamptz NULL | |
| `pdf_status` | `render_status` | `pending` \| `ready` \| `failed` (EC-M4-08) |
| `content_hash` | text NOT NULL | 🔒 Client-portal cache validation |
| `created_at` | timestamptz | |

📌 **DDR-12 — one immutable snapshot serves the PDF, the client portal and offline sync**

| Option | Verdict |
|---|---|
| Render PDF and portal view separately from live tables | ❌ Two rendering paths, two chances to diverge. Offline sync needs a stable payload anyway |
| **One denormalised snapshot document** | ✅ **Chosen.** PDF renders from it; portal reads it; service worker caches it; `content_hash` drives cache invalidation (FR-M7-011, EC-M7-03) |

🔒 The snapshot contains **resolved food names, quantities, household measures, gram equivalents and nutrition totals** — no live joins. A client viewing their plan offline sees exactly what was issued, and EC-M7-03 (plan revised while viewing) is detectable by hash comparison rather than by silently swapping content.

### 8.14 `supplements`, `plan_supplements`

**Why:** you specified a **vendor-neutral** supplement catalogue.

**`supplements`**: `tenant_id NULL` (curated + custom), `name`, `generic_name`, `form` (tablet/capsule/powder/liquid), `default_unit`, `category`, `notes`, `is_active`.
🔒 **No brand, price, vendor or purchase link.** Vendor neutrality is a schema property here, not a policy — the columns to violate it do not exist.

**`plan_supplements`**: `plan_version_id`, `supplement_id`, `dosage`, `unit`, `frequency`, `timing`, `duration_days NULL`, `notes`, `sort_order`.

⚠️ 🔒 **The system MUST NOT interpret, recommend or interaction-check supplements** (FR-M3-017, FR-M5-007). This is a record of the practitioner's own prescription — capture only. **AI drafting must never populate it.** Enforced by `ai_drafting` having no write path to this table.

### 8.15 `dietary_rules` — medical & dietary rule enforcement

**Why it exists:** you specified *medical and dietary rule enforcement*. 🔒 Making rules **data** rather than code is what allows clinical policy (OD-06) to change without a release.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `tenant_id` | uuid NULL | Platform rules + 🟡 tenant overrides (Phase 2) |
| `code` | text NOT NULL | e.g. `exclude_non_veg_for_vegetarian` |
| `rule_type` | `rule_type` | `exclusion` \| `warning` \| `target_adjustment` \| `generation_block` |
| `trigger_condition` | jsonb NOT NULL | Profile predicate |
| `effect` | jsonb NOT NULL | What it excludes, warns or blocks |
| `severity` | `rule_severity` | 🔒 `hard` (cannot be overridden) \| `soft` (warns, practitioner may proceed) |
| `applies_to_ai` / `applies_to_manual` | boolean | 🔒 Different enforcement per path |
| `message` | text | Plain-language explanation (NFR-063) |
| `source_citation` | text NULL | Clinical provenance |
| `is_active` | boolean | |

🔒 **Hard rules (allergies, dietary class) are enforced deterministically in `nutrition`, before AI generation and after** — ADR-10. Soft rules warn but never block practitioner judgement (EC-M4-05).

🔒 **`applies_to_ai` vs `applies_to_manual` is deliberate.** A practitioner may knowingly include a food a rule discourages; the AI must not. OD-06 (CKD, eating disorders) becomes a `generation_block` rule row once the clinical decision is made — **no code change, no migration.**

### 8.16 `nutrition_targets`

`client_id`, `energy_kcal`, `protein_g`, `carbohydrate_g`, `fat_g`, `fibre_g`, `calculation_method` 🟡 (OD-13 — the equation used, once chosen), `calculated_value`, `override_value NULL`, `set_by_user_id`, `effective_from`, `superseded_at NULL`.

🔒 **Both calculated and override are stored.** OD-13 requires a named equation with practitioner override; keeping both makes the practitioner's clinical judgement visible and auditable rather than silently replacing the computed figure.

### 8.17 `food_search_misses`

**Why:** 🔒 FR-M4-014 / FR-M11-011 — *the highest-value product signal we will have.*

`tenant_id`, `query_text`, `result_count`, `searched_at`, `led_to_custom_food_id NULL`.
🔒 **Owned by `admin`**, written by `nutrition` via a kernel port. Directly drives curation priority (M4.2).
🟡 Aggregated weekly and pruned; raw rows are not retained long-term.

### 8.18 Body composition

🔒 You specified body composition calculations. **Derived, never stored** (FR-M3-012, consistent with BMI):

| Metric | Inputs | Where |
|---|---|---|
| BMI | weight, height | Derived on read |
| Waist-hip ratio | waist, hip | Derived on read |
| Ideal body weight | height, sex | 🟡 Equation pending OD-13 |
| Energy requirement | age, sex, weight, height, activity | 🟡 Pending OD-13 |

🔒 **All live in one calculation service in `nutrition`** (NFR-072). 🟡 **PROPOSAL — `calculation_method` recorded on every derived clinical value that is persisted**, so a later equation change does not silently reinterpret historical records.

---

## 9. AI Generation Schema

**Owner:** `ai_drafting` · **Readers:** `admin` (diagnostics), `billing` (cost attribution) via ports · **Writers:** `ai_drafting`

🔒 **The separation that makes "AI is never the source of truth" structural:** these tables record *what the model did*. Plan tables (§8.11) hold *what the practitioner owns*. `ai_drafting` has **no write path to any plan table** — it hands a validated structure to `nutrition`, which performs the write.

### 9.1 `ai_prompt_versions`

**Why:** FR-M5-005 requires recording the prompt version. Quality regressions must be attributable.

`id`, `code`, `version`, `system_prompt text`, `output_schema jsonb`, `model_identifier`, `parameters jsonb`, `status` (`draft`\|`active`\|`retired`), `activated_at`, `notes`.
🔒 Platform-owned, no `tenant_id`. Immutable once active.

### 9.2 `ai_generations`

**Why:** the complete diagnostic and cost record of every generation attempt.

| Column | Type | Notes |
|---|---|---|
| `id` / `tenant_id` | uuid | |
| `client_id` | uuid FK | |
| `requested_by_user_id` | uuid FK | |
| `prompt_version_id` | uuid FK | |
| `generation_type` | `ai_generation_type` | `diet_plan_draft` at MVP |
| `status` | `ai_status` | 🔒 `pending`\|`succeeded`\|`validation_failed`\|`provider_failed`\|`blocked` |
| `candidate_food_ids` | uuid[] | 🔒 **The grounding boundary** (ADR-10) |
| `request_snapshot` | jsonb | Inputs, excluding raw clinical free text |
| `response_snapshot` | jsonb NULL | Raw model output — 🔒 evidence for validation failures |
| `validation_errors` | jsonb NULL | 🔒 Which rule rejected it (EC-M5-04/05) |
| `resulting_plan_version_id` | uuid FK NULL | Set only on success |
| `input_tokens` / `output_tokens` | int NULL | Cost attribution (NFR-088) |
| `estimated_cost` | numeric(10,4) NULL | |
| `latency_ms` | int NULL | NFR-082 |
| `quota_consumed` | boolean NOT NULL default false | 🔒 §9.3 |
| `blocked_by_rule_id` | uuid FK NULL | 🔒 OD-06 outcome (§8.15) |
| `created_at` / `completed_at` | timestamptz | |

**Indexes:** `ix_ai_generations__tenant_created` · `ix_ai_generations__status_created` (alerting, NFR-085)

### 9.3 Quota semantics

🔒 FR-M5-010 and EC-M10-04 — a failed generation must cost the practitioner nothing.

| Outcome | `quota_consumed` |
|---|---|
| `succeeded` | ✅ true |
| `validation_failed` | ❌ false — our defect, not theirs |
| `provider_failed` | ❌ false |
| `blocked` | ❌ false |

🔒 Recorded as a column rather than inferred from status, so the metering rule is explicit and auditable rather than reconstructed by a query that could drift from intent.

⚠️ **`response_snapshot` may contain model text resembling clinical advice** (EC-M5-05). It is retained as diagnostic evidence, **never rendered to a practitioner or client**, and subject to the same PHI-free-logging rule (NFR-033) — it must not be forwarded to error tracking.

---

## 10. Appointments Schema

**Owner:** `appointments` · **Readers:** `clinical` (notes link to appointments), `messaging` (reminders) via ports · **Writers:** `appointments`

### 10.1 `appointments`

| Column | Type | Notes |
|---|---|---|
| `id` / `tenant_id` / `client_id` | uuid | |
| `practitioner_user_id` | uuid FK | |
| `starts_at` / `ends_at` | timestamptz NOT NULL | 🔒 NFR-099 — never naive |
| `appointment_type` | `appointment_type` | `initial_consultation`\|`follow_up`\|`review` |
| `mode` | `appointment_mode` | `in_person`\|`video`\|`phone` |
| `meeting_link` | text NULL | 🔒 Practitioner-supplied (FR-M6-002) |
| `location` | text NULL | |
| `status` | `appointment_status` | `scheduled`\|`completed`\|`cancelled`\|`no_show`\|`rescheduled` |
| `rescheduled_to_id` | uuid FK NULL | 🔒 Chain preserved (FR-M6-007) |
| `rescheduled_from_id` | uuid FK NULL | |
| `cancellation_reason` | text NULL | |
| `row_version` | int | Optimistic concurrency |

**Indexes:** `ix_appointments__tenant_starts` (day/week views) · `ix_appointments__client_starts` · `ix_appointments__practitioner_starts`

⚠️ **No overlap constraint.** EC-M6-01 — practitioners sometimes double-book deliberately. Overlap is a **UI warning**, not a database constraint. A Postgres exclusion constraint here would block a legitimate workflow.

⚠️ **No `ck_appointments__future` constraint.** EC-M6-02 permits back-dated records for real record-keeping needs.

🔒 **`rescheduled_to_id`/`from_id` rather than mutating the row** — FR-M6-007 requires the original to remain visible. A reschedule creates a new appointment and links both.

### 10.2 `appointment_history`

Append-only status transitions: `appointment_id`, `from_status`, `to_status`, `changed_by_user_id`, `changed_at`, `note`.

---

## 11. Messaging & Scheduled Message Engine Schema

**Owner:** `messaging` · **Readers:** `admin` (delivery log, FR-M11-004) · **Writers:** `messaging`

🔒 M8.3 — **one engine, not three.** These tables serve every message in the product.

### 11.1 `message_templates`

**Why:** FR-M8-002 — versioned templates with typed variables. Also the WhatsApp approval registry (Arch §12.2).

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `code` | text NOT NULL | e.g. `plan_delivered` |
| `version` | int NOT NULL | |
| `category` | `message_category` | 🔒 `transactional`\|`reminder`\|`nudge`\|`notification` |
| `is_essential` | boolean NOT NULL | 🔒 §11.6 — exempt from quota/frequency |
| `priority` | int NOT NULL | Frequency-cap arbitration (EC-M8-09) |
| `staleness_tolerance_minutes` | int NULL | 🔒 EC-M8-05; NULL = never stale |
| `variables` | jsonb NOT NULL | Typed variable declarations |
| `body_template` | text NOT NULL | Fallback / email / SMS body |
| `default_transport` | `transport_type` | |
| `provider_template_name` | text NULL | 🔒 WhatsApp approved template id |
| `provider_template_status` | `provider_template_status` | 🔒 `pending`\|`approved`\|`rejected`\|`paused` (EC-M8-03) |
| `is_practitioner_disableable` | boolean | FR-M8-027 |
| `status` | `definition_status` | |

🔒 Platform-owned, no `tenant_id` at MVP. Practitioner-editable wording is Phase 2 (FR-M8-029).

🔒 **`provider_template_status` per template** means one revoked WhatsApp template pauses only its own message type (EC-M8-03) — the rest of the engine keeps running. Without per-template state, a single rejection would look like a total outage.

### 11.2 `scheduled_messages`

**Why:** 🔒 The queue of intent — what is *due*, before any send is attempted.

| Column | Type | Notes |
|---|---|---|
| `id` / `tenant_id` | uuid | |
| `client_id` | uuid FK NULL | NULL for practitioner-directed messages |
| `recipient_user_id` | uuid FK NULL | |
| `template_id` | uuid FK | |
| `template_variables` | jsonb | |
| `scheduled_for` | timestamptz NOT NULL | |
| `state` | `scheduled_state` | `pending`\|`dispatched`\|`suppressed`\|`expired`\|`cancelled` |
| `suppression_reason` | `suppression_reason` NULL | 🔒 §11.5 |
| `idempotency_key` | text NOT NULL | 🔒 §11.4 |
| `source_module` / `source_record_id` | | Provenance and cancellation |
| `deferred_from` | timestamptz NULL | 🔒 Quiet-hours audit trail (AC-M8-006) |
| `attempt_count` | int default 0 | |

**Constraints:** 🔒 `uq_scheduled_messages__idempotency` UNIQUE(`tenant_id`, `idempotency_key`)
**Indexes:** 🔒 `ix_scheduled_messages__due` on (`scheduled_for`) `WHERE state='pending'` — **the hot path for the worker (§13)** · `ix_scheduled_messages__source` (cancellation on stage change, EC-M8-08)

### 11.3 `message_dispatches`

**Why:** 🔒 FR-M8-003 — the immutable delivery log. Every attempt, with outcome.

`id`, `tenant_id`, `scheduled_message_id NULL` (NULL for immediate sends), `template_id`, `template_version`, `transport`, `recipient_address` 🔒 (the number/email used — needed for support, EC-M8-08), `provider_message_id NULL`, `status` (`queued`\|`sent`\|`delivered`\|`read`\|`failed`\|`rejected`), `failure_code`, `failure_reason`, `attempt_number`, `cost_amount NULL` 🔒 (per-tenant WhatsApp cost, NFR-088), `sent_at`, `delivered_at`, `created_at`.

🔒 **Append-only.** Status transitions from provider webhooks update the row's status fields only; the attempt record itself is never rewritten or deleted.
**Indexes:** `ix_message_dispatches__tenant_created` · `ix_message_dispatches__client_created` (FR-M8-011) · `uq_message_dispatches__provider_id` (webhook idempotency)

### 11.4 Idempotency

🔒 EC-M8-06 — a retry must never deliver twice.

`idempotency_key` = deterministic composition of `(tenant, recipient, template_code, logical_occasion)`.
🔒 **Enforced by a unique constraint, not application logic** — a database constraint cannot lose a race; a `SELECT` then `INSERT` can.

### 11.5 Suppression

🔒 M8.3 — the six rules (FR-M8-005…009) evaluated in **one place, at dispatch time, not scheduling time.**

```
suppression_reason:
  client_stage_inactive · consent_withdrawn · tenant_suspended
  quota_exceeded · frequency_capped · template_paused · client_unsubscribed
```

🔒 **Why at dispatch:** a client's stage, consent or tenant status can change between scheduling and send. A check-in scheduled Monday for Friday must be suppressed if the client is paused Wednesday. Checking only at scheduling would send it — violating FR-M8-005 in exactly the way that erodes practitioner trust.

Suppressed rows are **retained with their reason** (AC-M8-004), never deleted.

### 11.6 Essential messages

🔒 Arch §10.5. `is_essential = true` (magic links, password resets) exempts a template from quota and frequency limits.

⚠️ A client locked out of their portal because their practitioner hit a nudge quota is unacceptable. Exemption is a **property of the template**, declared once — never a runtime special case.

### 11.7 `checkin_schedules`

**Why:** FR-M8-022…025 — recurring per-client check-ins.

`client_id`, `frequency` (`weekly`\|`fortnightly`\|`monthly`), `day_of_week` 🟡 (defaults to the weekday the client became `active`, FR-M8-023), `time_of_day`, `is_paused` 🔒 (pausable without changing lifecycle stage, FR-M8-024), `paused_at`, `last_generated_for`, `next_due_on`.

🔒 **A schedule generates `scheduled_messages` rows; it does not send.** One engine, one dispatch path.
🔒 **Stops automatically when the client leaves `active`** (FR-M8-025) — driven by the `StageChanged` event, not a nightly reconciliation.

### 11.8 `notification_preferences`

`tenant_id`, `client_id NULL` (NULL = tenant default), `template_code NULL`, `transport`, `is_enabled`, `quiet_hours_start/end` 🟡 (default 08:00–21:00, FR-M8-009), `max_messages_per_week` (FR-M8-008), `updated_at`.

🔒 Two levels: tenant-wide disable (FR-M8-027) and per-client override. Client unsubscribe (US-M8-06) writes here **and** to the consent ledger (§16) — the preference controls behaviour, the ledger is the legal record.

---

## 12. Progress Schema

**Owner:** `progress` · **Readers:** `clients` (timeline), `nutrition` (adherence context) · **Writers:** `progress`

### 12.1 `adherence_logs`

**Why:** FR-M7-004 — one-tap per-meal adherence, and the primary engagement signal.

| Column | Type | Notes |
|---|---|---|
| `id` / `tenant_id` / `client_id` | uuid | |
| `plan_version_id` | uuid FK NULL | Which plan they were following |
| `plan_slot_id` | uuid FK NULL | |
| `logged_for_date` | date NOT NULL | |
| `slot_type` | `meal_slot_type` | Survives plan revision |
| `adherence` | `adherence_value` | 🟡 `followed`\|`partial`\|`not_followed`\|`skipped` |
| `client_timestamp` | timestamptz NULL | 🔒 **Offline queue** (EC-M7-05) |
| `server_recorded_at` | timestamptz NOT NULL | |
| `idempotency_key` | text NOT NULL | 🔒 Offline replay safety |
| `note` | text NULL | Phase 2 |

**Constraints:** 🔒 `uq_adherence_logs__idempotency` UNIQUE(`client_id`, `idempotency_key`)
**Indexes:** `ix_adherence_logs__client_date` · `ix_adherence_logs__tenant_date` (check-in review, FR-M9-003)

🔒 **`client_timestamp` and `server_recorded_at` are both stored.** FR-M7-012 queues offline actions; EC-M7-05 requires nothing be discarded. A log queued Tuesday and synced Thursday is *dated* Tuesday but *recorded* Thursday — both facts matter, and collapsing them would corrupt adherence history.

🔒 **`slot_type` is denormalised alongside `plan_slot_id`** so a log remains interpretable if the plan is later revised (DDR-11). Justified denormalisation per Principle 4.

### 12.2 `progress_snapshots`

🟡 **PROPOSAL** — a periodic rollup for the at-risk view (FR-M9-001) and practice counts (FR-M9-006).

`tenant_id`, `client_id`, `snapshot_date`, `weight_kg NULL`, `adherence_rate_7d`, `adherence_rate_30d`, `last_activity_at`, `days_since_activity`, `is_at_risk`, `computed_at`.

📌 **DDR-13 — precompute at-risk state rather than querying live**

The at-risk view (FR-M9-001) asks *"which active clients have no activity in N days?"* across four activity sources (adherence, measurements, message responses, portal access). Live, that is a four-way outer join per client, every page load.

| Option | Verdict |
|---|---|
| Live query across four sources | ❌ Four-way join across module boundaries (R6 violation) on the most-viewed retention screen |
| **Nightly rollup per active client** | ✅ **Chosen.** ≤200 rows/tenant/night. One indexed read. Staleness of up to 24h is acceptable for a 10-day threshold |

🔒 **Derived data, never a source of truth** — rebuildable at any time.
⚠️ **EC-M9-02** — a client engaging via the practitioner's own WhatsApp appears at-risk despite real engagement. Requires a **practitioner dismissal** (`dismissed_until date`), or the view loses credibility. 🟡 Added to this table.

---

## 13. Job Queue Schema

**Owner:** `kernel` · 🔒 ADR-11 — Postgres queue, no broker.

### 13.1 `jobs`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `tenant_id` | uuid NULL | NULL for platform jobs |
| `job_type` | text NOT NULL | |
| `job_class` | `job_class` | 🔒 `dispatch`\|`generation`\|`rendering`\|`recurring`\|`maintenance` |
| `payload` | jsonb NOT NULL | 🔒 **IDs only — never clinical data** |
| `status` | `job_status` | `pending`\|`claimed`\|`running`\|`succeeded`\|`failed`\|`dead` |
| `priority` | int NOT NULL default 100 | Lower = sooner |
| `run_after` | timestamptz NOT NULL default now() | Backoff scheduling |
| `attempt_count` / `max_attempts` | int | 🔒 `max_attempts=1` for `generation` (Arch §11.2) |
| `claimed_at` / `claimed_by` | | Worker lease |
| `lease_expires_at` | timestamptz NULL | 🔒 §13.3 |
| `last_error` | text NULL | 🔒 Sanitised — no clinical data |
| `idempotency_key` | text NULL | Duplicate suppression |
| `dedupe_key` | text NULL | 🟡 Collapses redundant recurring work |

**Indexes:** 🔒 `ix_jobs__claimable` on (`priority`, `run_after`) `WHERE status='pending'` — **the queue's hot path** · `ix_jobs__lease` on (`lease_expires_at`) `WHERE status IN ('claimed','running')` · `uq_jobs__idempotency` partial unique

### 13.2 Claiming

🔒 `SELECT ... FOR UPDATE SKIP LOCKED LIMIT n` within a transaction, setting `status='claimed'` and a lease.

**Why `SKIP LOCKED`:** multiple workers never collide, and a slow job never blocks the queue. Correct with one worker today (Arch §11.3) and correct with five later — 🔒 concurrency safety is built in now because retrofitting it is far harder.

🔒 **`payload` contains IDs only.** A job row is not an audit record and must not become a clinical data store — NFR-033 applies to job payloads exactly as to logs. This also keeps jobs small and the table cheap to scan.

### 13.3 Lease expiry

🔒 NFR-025 — jobs survive a deploy or crash without loss or duplication.

A killed worker leaves `status='claimed'` with an expired lease. A recovery sweep returns expired-lease jobs to `pending`. Combined with idempotency (§11.4), re-execution is safe.

⚠️ **Lease duration must exceed the job's timeout**, or a long-running job gets re-claimed while still executing. 🟡 **PROPOSAL — lease = timeout × 2.**

### 13.4 `job_runs`

Per-attempt record: `job_id`, `attempt_number`, `started_at`, `finished_at`, `outcome`, `error_class`, `error_message` (sanitised), `duration_ms`, `worker_id`.

🔒 Separate from `jobs` so retry history survives — a job that succeeded on attempt 3 must still show that attempts 1 and 2 failed, which a single mutable row cannot express.
🟡 Pruned after 30 days.

---

## 14. Billing & Entitlements Schema

**Owner:** `billing` (invoices, payments) and `kernel.entitlements` (plans, usage) · 🔒 M10.3 — enforcement is structural, collection is deferred.

### 14.1 `plan_definitions`

🔒 FR-M10-001 — plans as **configuration**, so limits change without a release.

`id`, `code` (`free`\|`starter`\|`growth`\|`clinic`), `name`, `price_amount numeric(10,2)`, `currency_code`, `billing_period`, `limits jsonb`, `features jsonb`, `is_public`, `sort_order`, `effective_from`, `effective_to NULL`.

🔒 **`limits` as `jsonb`** — 🟡 the one place I permit `jsonb` for configuration, because adding a new metered resource must not require a migration (FR-M10-001 explicitly). Keys at MVP: `active_clients`, `ai_generations_per_month`, `whatsapp_messages_per_month`, `storage_mb`, `practitioner_seats`.

🔒 **Versioned by `effective_from`/`effective_to`** — a tenant on last year's pricing keeps it. Mutating a plan row would silently reprice existing customers.

### 14.2 `subscriptions`

`tenant_id UNIQUE`, `plan_definition_id`, `status` (`trialing`\|`active`\|`past_due`\|`suspended`\|`cancelled`), `trial_ends_on`, `current_period_start/end`, `cancel_at_period_end`, `activated_by_operator_id NULL` 🔒 (FR-M10-008 — manual activation at MVP), `notes`.

🔒 **One active subscription per tenant** (unique on `tenant_id`). History lives in §14.3.

### 14.3 `subscription_events`

Append-only: `subscription_id`, `event_type` (`created`\|`activated`\|`plan_changed`\|`suspended`\|`reactivated`\|`cancelled`), `from_plan_id`, `to_plan_id`, `actor_type`, `actor_id`, `reason`, `occurred_at`.

🔒 EC-M10-01 (downgrade while over limit) and EC-M10-05 (payment after suspension) both require knowing *when* state changed, not just current state.

### 14.4 `usage_counters`

🔒 FR-M0-044 / FR-M10-002 — per-tenant metered consumption.

`tenant_id`, `resource_code`, `period_start`, `period_end`, `used_amount numeric`, `limit_amount numeric` 🔒 (snapshotted from the plan, so a mid-period plan change is unambiguous), `warned_at_80pct NULL` (FR-M10-005), `updated_at`.

**Constraint:** `uq_usage_counters__tenant_resource_period`

📌 **DDR-14 — counters plus an append-only event log**

| Option | Verdict |
|---|---|
| Count live from source tables | ❌ Active clients is cheap; AI generations and messages are not. Cross-module counting violates R6 |
| **Counter row + `usage_events`** | ✅ **Chosen.** O(1) enforcement check on the hot path; the event log makes any counter reconcilable |

🔒 **Active clients are the exception — counted live** from `clients WHERE stage='active'` (M1.5). It is a cheap indexed count, and a drifting counter would produce wrong billing on the product's most visible limit.

### 14.5 `usage_events`

Append-only: `tenant_id`, `resource_code`, `amount`, `source_module`, `source_record_id`, `occurred_at`, `is_reconciled`.
🔒 EC-M10-04 — an action that fails after consuming quota must be reversible. A compensating negative event, never a counter decrement, so history stays auditable.

### 14.6 `invoices`, `payments`

**`invoices`** (🔒 FR-M10-011 — GST-compliant, a legal requirement from the first rupee): `tenant_id`, `invoice_number` 🔒 (sequential per financial year, gapless), `status`, `issue_date`, `subtotal`, `tax_amount`, `total_amount`, `currency_code`, `tax_breakdown jsonb` 🟡 (CGST/SGST/IGST — pending ASM-09), `place_of_supply`, `gstin NULL`, `pdf_file_id NULL`, `line_items jsonb`.

⚠️ 🔒 **`invoice_number` must be gapless and sequential per financial year** under Indian GST rules. This requires a dedicated sequence table with a locked increment, **not** a Postgres `SEQUENCE` — sequences skip numbers on rollback, which produces non-compliant invoices. 🟡 **PROPOSAL — `invoice_sequences`** (`financial_year`, `last_number`). Flagged: needs accountant confirmation (ASM-09).

**`payments`**: `tenant_id`, `invoice_id NULL`, `amount`, `currency_code`, `method`, `reference`, `gateway_payment_id NULL`, `status`, `received_on`, `recorded_by_operator_id NULL` 🔒 (manual at MVP), `notes`.
**Constraint:** `uq_payments__gateway_id` — 🔒 EC-M10-08, double-payment protection.

### 14.7 `practitioner_payment_records`

🔒 **Domain 2** (Arch §12.3) — the practitioner's own client collections. **We never custody these funds.**

`tenant_id`, `client_id`, `amount`, `currency_code`, `method` (`upi`\|`phonepe`\|`gpay`\|`bank_transfer`\|`cash`\|`cheque`\|`gateway`), `reference NULL`, `received_on`, `package_description`, `period_start/end NULL`, `recorded_by_user_id`, `gateway_payment_id NULL` 🟡 (Phase 2), `notes`.

🔒 **A completely separate table from `payments`.** Conflating our revenue with the practitioner's revenue would be an architectural and regulatory error — different owner, different legal status, different reporting. **Method-agnostic from day one** (FR-M10-012 approved), so Phase 2 gateway integration becomes an additional *source* of records, not a redesign.

---

## 15. Audit Logging Design

**Owner:** `kernel` · 🔒 FR-M0-031…036, NFR-047.

### 15.1 `audit_log`

| Column | Type | Notes |
|---|---|---|
| `id` | bigint | 🔒 **`bigserial`, not uuid** — append-only, never URL-exposed; sequential ordering is the point |
| `tenant_id` | uuid NULL | NULL for platform-level actions |
| `actor_type` | `actor_type` | `practitioner`\|`client`\|`operator`\|`system` |
| `actor_id` | uuid NULL | |
| `actor_realm` | `auth_realm` NULL | |
| `action` | text NOT NULL | e.g. `client.update` |
| `resource_type` | text NOT NULL | |
| `resource_id` | uuid NULL | 🔒 **Reference only** |
| `outcome` | `audit_outcome` | `success`\|`denied`\|`error` |
| `changed_fields` | text[] NULL | 🔒 **Field names only, never values** (FR-M0-035) |
| `metadata` | jsonb NULL | 🔒 Non-clinical context only |
| `request_id` | text NULL | Correlation |
| `ip_hash` | text NULL | 🔒 Hashed, not raw |
| `occurred_at` | timestamptz NOT NULL | |

**Indexes:** `ix_audit_log__tenant_occurred` · `ix_audit_log__resource` · `ix_audit_log__actor_occurred`

### 15.2 Immutability

🔒 FR-M0-034 — not editable or deletable through **any** application pathway.

📌 **DDR-15 — immutability by database permission, not application discipline**

```
GRANT INSERT, SELECT ON audit_log TO app_user;
-- no UPDATE, no DELETE granted, ever
```

🔒 **The application role physically cannot modify audit history**, regardless of what any code does. Application-level immutability is a convention that a future refactor can break; a missing grant is not.

⚠️ **`app_migrator` retains DDL rights**, so retention pruning is possible — but only as a deliberate, reviewed migration, never at runtime.

### 15.3 What is audited

| Event | Source | Requirement |
|---|---|---|
| Client/clinical create, update, delete | Framework (Arch §5.1) | FR-M0-031 |
| 🔒 **Operator read of tenant data** | `admin` | FR-M0-032 — the only audited reads |
| Authorization denials | `kernel.authz` | Arch §17.1 |
| Consent grant/change/withdrawal | `kernel.consent` | Also in the ledger (§16) |
| Stage transitions | `clients` | Also in domain history (§5.3) |
| Plan issue, entitlement change, operator actions | Various | |

🔒 **Written by the framework, not per endpoint.** A developer cannot forget it, for the same reason they cannot forget authorization.

⚠️ 🔒 **The single most likely violation in this design is a clinical value reaching `changed_fields` or `metadata`.** Mitigation: `changed_fields` is `text[]` — structurally incapable of holding values — and `metadata` population goes through a helper with an allowlist of permitted keys.

### 15.4 Growth

🟡 ~20–50 audit rows per active client per month → at 200 tenants × 100 clients, roughly 400k–1M rows/year. Comfortable for a single Postgres (NFR-092) with no partitioning.
🟡 **PROPOSAL — retention: 7 years for clinical-record access, matching `tenants.data_retention_days`.** Requires legal confirmation (ASM-10).

---

## 16. Consent Ledger Design

**Owner:** `kernel` · 🔒 FR-M0-021…030, NFR-044…053. **DPDP Act 2023 is the governing regime at launch, not HIPAA.**

### 16.1 Four tables

```
consent_purposes  (what we might process, and why)
       │
consent_notices   (versioned notice text presented to a person)
       │
consent_records   (append-only grants and withdrawals)  ◀── the ledger
       │
data_requests     (access, correction, erasure requests)
```

### 16.2 `consent_purposes`

🔒 FR-M0-022 — consent captured **per purpose**, itemised, never blanket.

`code`, `name`, `description`, `is_essential` 🔒 (essential purposes are required for service delivery and cannot be withdrawn while the relationship is active), `data_categories text[]`, `retention_days`, `legal_basis`, `is_active`.

🟡 **PROPOSED purposes:** `service_delivery` (essential) · `clinical_records` (essential for clinical clients) · `whatsapp_communication` · `progress_tracking` · `plan_delivery` · `appointment_reminders` · `marketing` (never essential).

⚠️ **The essential/non-essential split is legally consequential and requires the privacy lawyer review (ASM-10, OD-05).** Marking too much as essential defeats FR-M0-024's requirement that withdrawal be as easy as granting.

### 16.3 `consent_notices`

`id`, `purpose_ids uuid[]`, `version`, `locale`, `title`, `body text`, `effective_from`, `superseded_at NULL`, `requires_reconsent boolean` 🔒 (FR-M0-029 — a material change requires re-consent).

🔒 **Immutable once effective.** A consent record references the exact notice version presented — that is what makes NFR-051 (produce the consent basis for any client) answerable.

### 16.4 `consent_records`

🔒 **The ledger.** Append-only (NFR-047).

| Column | Type | Notes |
|---|---|---|
| `id` | bigint | 🔒 `bigserial` — append-only sequence |
| `tenant_id` | uuid NOT NULL | |
| `subject_type` | `consent_subject` | `client`\|`prospect` |
| `subject_id` | uuid NULL | Client id once known |
| `subject_mobile_hash` | text NULL | 🔒 Pre-client-record consent (enquiry form) |
| `purpose_id` | uuid FK | |
| `notice_id` | uuid FK | 🔒 The exact version presented |
| `action` | `consent_action` | 🔒 `granted`\|`withdrawn`\|`reconfirmed` |
| `captured_via` | `consent_channel` | `enquiry_form`\|`portal`\|`practitioner`\|`whatsapp` |
| `captured_by_actor_type/id` | | |
| `guardian_name` / `guardian_relationship` / `guardian_verification_method` | text NULL | 🔒 FR-M0-028 — minors |
| `evidence` | jsonb NULL | 🔒 Non-clinical: notice hash, UI version, timestamp |
| `occurred_at` | timestamptz NOT NULL | |

🔒 **Same immutability mechanism as the audit log** (DDR-15): INSERT and SELECT only.

🔒 **Current consent state is derived** — the latest record per `(subject, purpose)`. No mutable `is_consented` column exists, because a mutable flag and an append-only ledger would inevitably disagree, and the ledger is the legal record.

⚠️ **`subject_mobile_hash` handles the enquiry-form case** — consent is captured *before* a client record exists (FR-M2-004). Hashed rather than plain so the ledger does not become a second, unprotected copy of contact data.

⚠️ 🔒 **OD-05 (verifiable parental consent for under-18s) is a launch blocker and unresolved.** The columns exist; the *mechanism* needs legal advice. Nutritionists routinely see teenage clients, so this is not an edge case.

### 16.5 `data_requests`

🔒 FR-M0-026/027, NFR-048.

`tenant_id`, `client_id`, `request_type` (`access`\|`correction`\|`erasure`), `status` (`received`\|`in_progress`\|`completed`\|`rejected`), `requested_via`, `requested_at`, `due_by` 🟡 (statutory deadline pending ASM-10), `completed_at`, `handled_by`, `rejection_reason`, `export_file_id NULL`, `erasure_scope jsonb`.

⚠️ 🔒 **Erasure must traverse object storage** (Arch §13.2). Deleting a `client_documents` row while the file persists in storage is a compliance failure and a common oversight. **Launch gate.**

---

## 17. Row Level Security Strategy

🔒 ADR-06, NFR-030 — isolation enforced **below the application layer**, so a forgotten `WHERE tenant_id` cannot leak data.

### 17.1 Policy patterns

Five patterns cover every table. 🔒 No table gets a bespoke policy without a recorded reason.

**Pattern A — tenant-isolated** *(most tables)*
```
USING (tenant_id = current_setting('app.tenant_id')::uuid)
```
Applies to: clients, plans, appointments, measurements, messages, files, invoices…

**Pattern B — shared catalogue** *(§2.2, DDR-03)*
```
USING (tenant_id IS NULL OR tenant_id = current_setting('app.tenant_id')::uuid)
```
Applies to: foods, food_portions, recipes, supplements, assessment_definitions.
🔒 **Write policy is narrower than read:** `WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid)` — a tenant may create custom rows but **can never write a curated (`NULL`) row.** Curated writes go through the operator role only.

**Pattern C — client-realm scoped** *(client portal)*
```
USING (tenant_id = current_setting('app.tenant_id')::uuid
       AND client_id = current_setting('app.actor_id')::uuid)
```
🔒 Applies where the actor realm is `client`. **Consultation notes and client_notes have no client-realm policy at all** — FR-M3-021 (never client-visible) is enforced by the *absence* of a policy, which is stronger than a condition.

**Pattern D — platform tables** — no RLS; unreachable from client-facing paths. Access only via `kernel` or the operator role.

**Pattern E — append-only** *(audit_log, consent_records)* — SELECT policy plus 🔒 **no UPDATE/DELETE grant** (DDR-15).

### 17.2 Practitioner-scope within a tenant

⚠️ 🔒 **FR-M0-017 (a practitioner sees only their assigned clients) is NOT enforced by RLS.**

RLS answers *"which tenant?"* — a coarse, universal boundary. Ownership answers *"which practitioner, for this specific client, given assignments and role?"* Encoding that in RLS would require every policy to join `client_assignments` and read a role, making policies slow, complex and hard to reason about.

🔒 **Ownership is enforced in `kernel.authz`** (ADR-05), where it is testable as a matrix and where the decision is uniform. **RLS is the tenant seatbelt; authz is the steering.** Conflating them is a common design error that produces both weak policies and unmaintainable authorization.

### 17.3 Verification

🔒 AC-M0-003 is a **launch gate**, not a unit test: with the application's tenant filter deliberately removed, cross-tenant reads must still return nothing.

🟡 **PROPOSAL — an automated test that enumerates every table** and asserts RLS is enabled with at least one policy. A new table shipped without RLS is the most likely way isolation silently breaks, and it will happen eventually without this check.

---

## 18. Indexing Strategy

🔒 Every index justified by a named requirement. Unjustified indexes cost write throughput and storage for nothing.

### 18.1 Critical-path indexes

| Index | Table | Serves | Budget |
|---|---|---|---|
| `ix_foods__search` GIN(search_vector) | foods | FR-M4-008 food search | 🔒 NFR-004 ≤300ms |
| `ix_foods__name_trgm` GIN trigram | foods | Typo tolerance | |
| `ix_clients__search` GIN(tsvector) | clients | FR-M1-021 client search | 🔒 NFR-005 ≤300ms |
| `ix_timeline_events__client_occurred` | timeline_events | FR-M1-018 | 🔒 NFR-006 ≤800ms |
| `ix_scheduled_messages__due` partial | scheduled_messages | Worker dispatch | 🔒 NFR-009 ≤60s |
| `ix_jobs__claimable` partial | jobs | Queue claim | Hot path |
| `ix_clients__tenant_stage` | clients | Client list, entitlement count | |
| `ix_measurements__client_date` | measurements | FR-M3-014 trends | |
| `ix_appointments__tenant_starts` | appointments | Day/week views | |
| `ix_adherence_logs__client_date` | adherence_logs | Progress | |

### 18.2 Partial indexes

🔒 Used deliberately — they are dramatically smaller and faster where a predicate is always present:

| Index | Predicate | Why |
|---|---|---|
| `ix_jobs__claimable` | `WHERE status='pending'` | The queue only ever scans pending rows |
| `ix_scheduled_messages__due` | `WHERE state='pending'` | Same |
| `uq_diet_plan_versions__one_draft` | `WHERE state='draft'` | 🔒 Enforces one draft per plan |
| `uq_users__tenant_email` | `WHERE archived_at IS NULL` | Soft-deleted users release their email |
| Most tenant indexes | `WHERE archived_at IS NULL` | Archived rows are excluded from working views |

### 18.3 Composite ordering

🔒 Leading column is always `tenant_id` for tenant-scoped indexes — every query is tenant-filtered, so it is the most selective prefix and lets one index serve many query shapes.

### 18.4 Food search specifically

🔒 ADR-08 — Postgres FTS, no external service.

`foods.search_vector` is a **generated column** combining name, aliases (§8.5), category and food type, weighted so name matches outrank alias matches. Maintained by Postgres on write — 🔒 no application code can forget to update it.

⚠️ **Generated columns cannot reference other tables**, so alias text must be denormalised into `foods` (a maintained text column updated when aliases change) or the vector must be refreshed by trigger. 🟡 **PROPOSAL — trigger-maintained `search_vector`**, since aliases live in a child table. Flagged as a deviation from the pure generated-column approach.

⚠️ **Trigram + GIN + tsvector on the same table is three indexes on a write-light, read-heavy table.** Correct trade here: foods are curated rarely and searched constantly.

### 18.5 Deliberately not indexed

Audit `metadata`, job `payload`, assessment `answers`, snapshot `document`. 🔒 These are stored, not queried by structure. A GIN index on `jsonb` we never query by would be pure cost.

---

## 19. File Storage Metadata Schema

**Owner:** `kernel.storage` · 🔒 Arch §13 — metadata in Postgres, bytes in object storage.

### 19.1 `files`

| Column | Type | Notes |
|---|---|---|
| `id` / `tenant_id` | uuid | |
| `storage_key` | text NOT NULL UNIQUE | 🔒 Opaque, tenant-scoped, non-enumerable |
| `bucket` | text NOT NULL | |
| `original_filename` | text NOT NULL | |
| `content_type` | text NOT NULL | 🔒 Validated allowlist (NFR-036) |
| `size_bytes` | bigint NOT NULL | Quota (FR-M0-040) |
| `checksum` | text NULL | Integrity |
| `file_class` | `file_class` | 🔒 `client_document`\|`plan_pdf`\|`invoice_pdf`\|`branding`\|`export` |
| `contains_clinical_data` | boolean NOT NULL | 🔒 Drives retention and erasure scope |
| `uploaded_by_actor_type/id` | | |
| `status` | `file_status` | 🔒 `pending`\|`confirmed`\|`quarantined`\|`deleted` |
| `confirmed_at` | timestamptz NULL | 🔒 ADR-12 |
| `deleted_at` | timestamptz NULL | Soft delete; purge job removes bytes |

🔒 **`status='pending'` until confirmed** (ADR-12) — direct-to-storage upload creates the row on *confirmation*, so an abandoned upload leaves an orphan object (reaped after 24h, approved proposal #9) rather than a phantom database row.

🔒 **`contains_clinical_data` exists so erasure can find every file** (§16.5). Without it, DPDP erasure would require inspecting each file's purpose at request time — exactly the kind of lookup that gets missed.

⚠️ **No public bucket exists.** Every retrieval is authorized, then issued a short-lived signed URL (NFR-035). 🔒 The signed URL is a delivery mechanism, **not** the access control.

---

## 20. Admin Schema

**Owner:** `admin` · ⚠️ The highest-privilege, cross-tenant surface (M11.3).

### 20.1 `operator_actions`

**Why:** FR-M11-007 — every operator action recorded, in addition to the general audit log.

`operator_id`, `action_type`, `target_tenant_id NULL`, `target_resource_type/id`, `reason text` 🟡 (required for sensitive actions), `outcome`, `occurred_at`, `ip_hash`.

🔒 **Separate from `audit_log` despite overlap.** Operator actions are the surface most likely to be scrutinised in an incident, and they must be reviewable **without querying the full audit log** — which is tenant-partitioned and orders of magnitude larger. Same immutability grants (DDR-15).

🔒 **Impersonation is Phase 2** (FR-M11-012) — no schema for it at MVP, deliberately.

---

## 21. Versioning Strategy

🔒 Four different things are versioned, for three different reasons. Conflating them would be a design error.

| What | Mechanism | Why | Requirement |
|---|---|---|---|
| **Diet plans** | Plan → N versions; copy-on-revise; immutable once issued | Clinical record integrity; weekly adjustment loop | FR-M4-033, EC-M4-03 |
| **Assessments** | Definition versions; responses pin their definition | Structure evolves without migration; old responses stay readable | FR-M3-002/003 |
| **Templates** | 🟡 **Mutable, not versioned** | §21.1 | — |
| **Consent notices** | Immutable versions; records pin the version | Legal evidence of what was agreed | FR-M0-029 |
| **Message templates** | Versioned; dispatches record the version | Provider approval + auditability | FR-M8-002 |
| **Prompts** | Versioned; generations record the version | Quality attribution | FR-M5-005 |

### 21.1 Why templates are deliberately not versioned

📌 **DDR-16 — diet templates are mutable; plans snapshot from them**

A practitioner refining "PCOS vegetarian 1400" expects their *next* plan to use the improved version. They do **not** expect a library of 14 historical template versions to manage.

🔒 Safety comes from the plan side: a plan created from a template **copies** its content (DDR-11) and snapshots on issue (§8.12). Editing a template can never alter an issued plan. **Mutability is safe precisely because plan versioning is rigorous** — the two decisions depend on each other.

### 21.2 Row-level concurrency

🔒 ADR-14, EC-M4-07 — `row_version int` on mutable aggregates (`diet_plan_versions`, `clients`, `appointments`, `assessment_responses`). A write carrying a stale version returns a conflict with a reload path. **No last-write-wins anywhere that matters.**

---

## 22. Soft Delete & Archival Strategy

🔒 FR-M1-010/011 — soft delete for user data; hard delete only via the DPDP erasure pathway.

### 22.1 Three deletion concepts

| Concept | Mechanism | Reversible | Trigger |
|---|---|---|---|
| **Archive** | `archived_at` set | ✅ Yes | User action (FR-M1-010) |
| **Retire** | `is_active = false` | ✅ Yes | Catalogue items still referenced (EC-M4-04) |
| **Erase** | 🔒 Hard delete + storage purge | ❌ No | DPDP request only (FR-M0-027) |

🔒 **Archive and retire are distinct.** Archive hides a *tenant's own record* from working views. Retire removes a *catalogue item* from search while keeping historical references resolvable. Using one mechanism for both would either break historical plans or leave retired foods in search results.

### 22.2 Archive semantics

Archived rows are excluded from default queries by partial indexes (§18.2), not by application filters — 🔒 the same reasoning as RLS: a filter can be forgotten, an index predicate is structural.

| Rule | Detail |
|---|---|
| Archived clients | Excluded from lists, search, entitlement count, all messaging (FR-M1-014) |
| Archived data | Fully retained; timeline and history intact |
| Reactivation | EC-M1-02 — the original record returns, history continuous. 🔒 **Never a new record** |
| Referenced rows | Cannot be archived if an active dependency exists; retire instead |

### 22.3 Erasure

🔒 The only path to hard deletion. Cascade order, executed transactionally:

```
1. Resolve scope: client + all dependent records + all files
2. Purge object storage    🔒 must precede the DB delete — see below
3. Delete dependent rows (measurements, plans, adherence, messages…)
4. Delete the client row
5. 🔒 RETAIN: audit_log and consent_records (evidence of lawful processing)
6. 🔒 RETAIN: financial records where statute requires
7. Write a completion record to data_requests
```

⚠️ 🔒 **Storage purge must precede the database delete.** Delete the row first and a failure mid-way leaves orphaned clinical files with no record pointing at them — undiscoverable and permanently non-compliant. Purge first, and a failure leaves a recoverable, retryable state.

⚠️ **Retaining audit and consent records during erasure is deliberate** and I believe correct — they are the evidence that processing was lawful and that erasure was performed. 🟡 **Requires legal confirmation (ASM-10).** They contain identifiers, not clinical values (FR-M0-035), which is what makes retention defensible.

### 22.4 Retention

`tenants.data_retention_days` (🟡 default ~7 years) drives a maintenance job that purges data past retention for closed tenants (NFR-049).
🔒 **FR-M10-010** — a suspended tenant keeps read-only access for 90 days with export capability **before any purge is considered.** Holding data hostage over payment is both an ethical failure and a probable DPDP violation.

---

## 23. Migration Strategy

🔒 NFR-076 — versioned, reversible migrations via Alembic.

### 23.1 Expand–contract is mandatory

🔒 Arch §16.4 — one instance per process, no blue-green. A breaking migration is a hard outage.

```
Release N:   add nullable column / new table          (backward compatible)
Release N:   deploy code that writes both old and new
Release N+1: backfill existing rows
Release N+1: deploy code that reads new
Release N+2: add NOT NULL / drop old column
```

🔒 **Every migration must be safe against the currently-running previous version.** This is not a guideline — with a single instance, violating it is downtime.

### 23.2 Forbidden in a single migration

| Never | Instead |
|---|---|
| Rename a column | Add new → dual-write → backfill → drop old |
| Change a type destructively | Add new column → migrate → swap |
| Add NOT NULL without a default | Add nullable → backfill → set NOT NULL |
| Drop a column still read by running code | Deploy the read change first |
| 🔒 Create an index non-concurrently on a large table | `CREATE INDEX CONCURRENTLY` |
| Long-running data backfill inside a migration | 🔒 A job (§13), not a migration |

⚠️ **`CREATE INDEX` without `CONCURRENTLY` takes an exclusive lock.** On `clients` or `foods` in production, that is an outage. 🔒 Concurrent index creation cannot run inside a transaction, so index migrations are separated from schema migrations.

### 23.3 Down migrations

🔒 Arch §16.4 — **never automated.** Reversibility means a reviewed, deliberate down-migration. Automated rollback of a production schema destroys data.

**Rollback strategy is forward-only:** redeploy the previous application image (safe, because expand–contract guarantees the old code works against the new schema) and, if needed, write a corrective forward migration.

### 23.4 Seed data

🔒 Distinguished from schema:

| Seed | Managed as | Why |
|---|---|---|
| `measure_units`, `nutrients`, `consent_purposes`, `plan_definitions`, `message_templates` | Idempotent migration | Reference data the code depends on |
| 🔒 **Curated foods, portions, aliases, recipes** | **Versioned data files + a loader job** | Large, iterated frequently, curated by non-developers. 🔒 **Must not be Python migration code** |
| `assessment_definitions` v1 | Versioned data file | Same — clinical content changes independently of releases |

📌 **DDR-17 — curated catalogue data ships as versioned data files, not migrations**

The food database is the moat (M4.2) and will be corrected continuously (FR-M11-010). Embedding thousands of food rows in migration files makes them unreviewable, unrepeatable and impossible for a non-developer to contribute to. A loader job applying idempotent upserts from versioned data files keeps curation separate from schema evolution.

---

## 24. Backup & Recovery Considerations

🔒 NFR-021 (RPO ≤24h), NFR-022 (RTO ≤8h), NFR-023, **NFR-024 (restore tested before launch)**.

| Asset | Backup | Gap |
|---|---|---|
| Postgres | Supabase daily automated + PITR on Pro | ✅ Covered |
| Object storage | ⚠️ 🔒 **UNVERIFIED** (Arch §23) | **Must confirm before launch** |
| Curated catalogue data | Versioned data files in git (DDR-17) | ✅ Reproducible |
| Configuration | 🟡 Encrypted store (approved proposal #13) | |

⚠️ 🔒 **The clinical documents in object storage may be the least-recoverable asset in the system.** If Supabase Storage lacks point-in-time recovery, a scheduled export job is required. **Verify in S0** — this is the kind of gap discovered only during an incident.

**Recovery ordering matters:** the database references `files.storage_key`. Restoring the database to an earlier point than storage produces rows pointing at objects that do not yet exist, and vice versa. 🟡 **PROPOSAL — a post-restore reconciliation job** that flags `files` rows whose objects are missing, rather than failing silently at read time.

🔒 **NFR-024 launch gate:** perform a full restore into a scratch environment and verify the application runs against it. An untested backup is not a backup.

---

## 25. Performance Considerations

### 25.1 Volume at the 18-month target

🔒 NFR-092 — 200 tenants, 20k clients.

| Table | Est. rows | Notes |
|---|---|---|
| `clients` | ~20,000 | Trivial |
| `foods` | ~5,000 curated + custom | 🔒 Search must stay ≤300ms |
| `food_portions` | ~15,000 | |
| `plan_items` | ~2M | Largest domain table (~40/plan × ~50k plans) |
| `message_dispatches` | ~3M/year | Largest overall |
| `audit_log` | ~1M/year | |
| `adherence_logs` | ~2M/year | |
| `jobs` / `job_runs` | High churn | 🟡 Pruned after 30 days |

🔒 **Comfortable on a single Postgres instance. No partitioning, no sharding, no read replicas** (NFR-093).

### 25.2 Known hot paths

| Path | Mitigation |
|---|---|
| Food search | GIN index; alias denormalisation; dietary filter **inside** the query, never post-filter |
| Plan totals while editing | 🔒 Computed live (ADR-07). Bounded: ~40 items × 5 nutrients. Debounced from the client (Arch §4.4) |
| Queue claim | Partial index; `SKIP LOCKED` |
| Timeline | Materialised (DDR-06); single indexed read |
| At-risk view | Precomputed nightly (DDR-13) |
| Entitlement check | Counter row (DDR-14), O(1) — except active clients, counted live |

### 25.3 Anticipated first bottleneck

🟡 **`plan_items` aggregation during authoring.** Every edit recomputes totals across a join of items → portions → nutrients.

🔒 **First response is index tuning, not caching** (ADR-13, Arch §19.2). If insufficient: materialise per-slot subtotals on write. **Not** a cache tier.

### 25.4 Connection management

⚠️ 🔒 **The pooler must run in transaction mode** (§2.3) — `SET LOCAL` isolation depends on it. Session-mode pooling with connection reuse breaks tenant isolation entirely. **Launch gate.**

Two processes (web + worker) share the pool. 🟡 **PROPOSAL — separate pool limits per process**, so a worker running long jobs cannot starve web request capacity.

---

## 26. Security Considerations

🔒 Arch §15, applied at the data layer.

| Control | Mechanism | Requirement |
|---|---|---|
| No credentials stored | 🔒 No password column exists (§4.2) | NFR-029 |
| Tokens hashed | `magic_links.token_hash`, `sessions.refresh_token_hash` | DDR-04 |
| Tenant isolation | RLS on every tenant table (§17) | NFR-030 |
| No RLS bypass | 🔒 `app_user` lacks `BYPASSRLS` (§2.4) | ADR-02 |
| Audit immutability | 🔒 No UPDATE/DELETE grant (DDR-15) | FR-M0-034 |
| No clinical data in operational tables | Job payloads, audit metadata, timeline summaries carry IDs only | NFR-033 |
| Non-enumerable identifiers | UUID PKs (DDR-01) | — |
| File access control | Authorized retrieval; no public bucket | NFR-035 |
| IP and user-agent hashed | Never stored raw | NFR-033 |
| Encryption at rest | Supabase-managed | NFR-028 |

### 26.1 The three highest residual risks

| Risk | Why it is the biggest | Mitigation |
|---|---|---|
| ⚠️ 🔒 **Pooler in session mode** | Silently disables tenant isolation across the entire system | Launch gate (§25.4) |
| ⚠️ 🔒 **Service-role key used for data access** | Bypasses every RLS policy — the exact V1 failure ADR-02 prevents | 🔒 `app_user` only; service key restricted to Auth/Storage admin |
| ⚠️ 🔒 **Clinical values leaking into audit/logs/jobs** | Breaches survive database encryption entirely | Structural: `changed_fields` is `text[]`; job payloads are IDs; metadata allowlist |

### 26.2 Table-level PII classification

🟡 **PROPOSAL** — every table tagged in a comment as `no_pii`, `pii`, or `clinical`. Cheap to add, and it makes erasure scope, retention policy and log-scrubbing rules derivable from the schema rather than from memory.

---

## 27. Implementation Dependency Graph & Build Order

🔒 Refines Arch §20 to table granularity. Ordering is driven by foreign-key dependency, structural-before-functional, and risk-front-loading.

### 27.1 Dependency graph

```
┌─────────────────────────────────────────────────────────────┐
│ D0  PLATFORM CORE                                            │
│ tenants → users → operators → sessions → magic_links         │
│ client_access_grants                                         │
└────────────────────────┬─────────────────────────────────────┘
                         │ everything below requires tenants + users
┌────────────────────────▼─────────────────────────────────────┐
│ D1  GOVERNANCE & KERNEL                                       │
│ audit_log · consent_purposes/notices/records · data_requests  │
│ plan_definitions · subscriptions · usage_counters/events      │
│ files · jobs · job_runs                                       │
└────────────────────────┬─────────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────────┐
│ D2  CLIENT SPINE                                              │
│ clients → client_stage_history · client_notes · tags          │
│ client_assignments · timeline_events                          │
│ enquiry_forms · enquiry_submissions                           │
└──────┬──────────────────────────────────────┬────────────────┘
       │                                       │
┌──────▼───────────────────────┐   ┌───────────▼────────────────┐
│ D3  NUTRITION CATALOGUE ⭐    │   │ D4  CLINICAL FRAMEWORK      │
│ food_categories · foods       │   │ assessment_definitions      │
│ food_aliases · food_nutrients │   │ assessment_responses        │
│ measure_units · food_portions │   │ client_nutrition_profile 🔑 │
│ recipes · recipe_items        │   │ measurements                │
│ supplements · dietary_rules   │   │ consultation_notes          │
└──────┬───────────────────────┘   │ client_documents            │
       │                            └───────────┬────────────────┘
       │      ┌─────────────────────────────────┘
       │      │  client_nutrition_profile feeds plan filtering
┌──────▼──────▼────────────────────────────────────────────────┐
│ D5  PLAN AUTHORING ⭐                                          │
│ meals · meal_items · diet_templates · template_*              │
│ diet_plans · diet_plan_versions · plan_days/slots/items       │
│ plan_item_alternatives · plan_supplements · nutrition_targets  │
│ plan_snapshots                                                 │
└──────┬─────────────────────────────────────┬─────────────────┘
       │                                      │
┌──────▼──────────────────┐      ┌────────────▼────────────────┐
│ D6  MESSAGING            │      │ D7  AI DRAFTING              │
│ message_templates        │      │ ai_prompt_versions           │
│ scheduled_messages       │      │ ai_generations               │
│ message_dispatches       │      │ (requires D3 candidate sets, │
│ checkin_schedules        │      │  D4 profile, D5 plan target) │
│ notification_preferences │      └──────────────────────────────┘
└──────┬───────────────────┘
       │
┌──────▼───────────────────┐      ┌──────────────────────────────┐
│ D8  APPOINTMENTS          │      │ D9  PROGRESS                  │
│ appointments              │      │ adherence_logs                │
│ appointment_history       │      │ progress_snapshots            │
└───────────────────────────┘      └──────────────────────────────┘
                    │                          │
┌───────────────────▼──────────────────────────▼────────────────┐
│ D10  BILLING & ADMIN                                            │
│ invoices · invoice_sequences · payments                         │
│ practitioner_payment_records · operator_actions                 │
│ food_search_misses                                              │
└─────────────────────────────────────────────────────────────────┘
```

### 27.2 Build order and reasoning

| # | Group | Why here | Blocks |
|---|---|---|---|
| **D0** | Platform core | 🔒 `tenants` is the FK target of every tenant-scoped table. Nothing can be created before it exists | Everything |
| **D1** | Governance & kernel | 🔒 Audit, consent, entitlements and jobs are **framework-level** (Arch §5.1) — they must exist before the first domain table, or they get retrofitted into every module. This is V1's *"auth and permissions became unmaintainable"* failure, prevented structurally | All domain modules |
| **D2** | Client spine | 🔒 Every clinical, nutrition, appointment and message row has a `client_id`. Includes `leads` because enquiry submissions create client rows and need nothing else | D3–D10 |
| **D3** | Nutrition catalogue ⭐ | 🔒 **The wedge.** Built before plans because plans reference foods and portions. Also the largest curation effort — 🔒 **seed data collection starts at S0, in parallel with all code** | D5, D7 |
| **D4** | Clinical framework | 🔒 Per your approved refinement: the **real versioned framework** ships now, seeded with a v1 definition holding only nutrition-calculation fields. `client_nutrition_profile` (🔑) is the key artefact — it decouples nutrition from assessment structure permanently. Later clinical depth is a new *definition version*, not a migration | D5, D7 |
| **D5** | Plan authoring ⭐ | Requires D3 (foods) and D4 (profile → dietary filtering, targets). The largest and most valuable module | D6, D7, D9 |
| **D6** | Messaging | 🔒 Plans exist but cannot be delivered. Also front-loads ASM-03 (WhatsApp approval), the largest external risk — 🔒 **Meta verification starts at S0** | D8, D9 |
| **D7** | AI drafting | 🔒 Requires D3 candidate sets, D4 profile with allergens, D5 plan structure to write into, and a practitioner template library to draft "in their style". Building it earlier would produce generic output and prove nothing | — |
| **D8** | Appointments | Thin; requires D6 for reminders. Nothing depends on it | — |
| **D9** | Progress | Requires accumulated data from D4, D5, D6. Building against empty tables proves nothing | — |
| **D10** | Billing & admin | 🔒 Enforcement lives in D1 from the start; only *collection* is deferred (M10.3). Admin last — support is needed only once customers exist | — |

### 27.3 Why D4 sits where it does

⚠️ 🔒 The Architecture placed the full clinical workspace at S7 because PRD §9 is unvalidated. **Your approved refinement changes this correctly:** the *framework* ships early with minimal fields, and clinical depth arrives as later definition versions.

**Consequence for the schema — this is why DDR-07 and DDR-08 matter:**

| | |
|---|---|
| Ships at D4 | The versioned definition mechanism, `assessment_responses`, and `client_nutrition_profile` |
| Ships later | Medical history, biochemical panels, FFQ, household context, behavioural readiness — as **definition versions** |
| Requires no migration | 🔒 Because the definition is `jsonb` and the calculation-critical projection is a stable, small, typed contract |

🔒 **This is the concrete meaning of "support future expansion without redesign."** The four unresolved clinical decisions (OD-06, 07, 08, 13) can be answered *after* D4 ships without touching a single table — OD-06 becomes a `dietary_rules` row, OD-08 a `clinical_reference_ranges` row, OD-07 and OD-13 new definition versions.

### 27.4 Parallel non-engineering tracks

⏳ 🔒 Start at S0, not gated on code:

| Track | Blocks | Why it is urgent |
|---|---|---|
| **Food data curation** | D3 | Largest non-code effort; determines whether the moat is real |
| **IFCT 2017 licensing** | D3 | Unresolved (ASM-06) |
| **Meta Business Verification** | D6 | 2–6 weeks, can be rejected |
| **Razorpay KYC** | D10 | |
| **Privacy lawyer** (OD-05, ASM-10) | 🔒 Launch | Minor consent is a launch blocker |
| **Accountant — GST** (ASM-09) | D10 | Invoice numbering must be compliant from the first rupee |
| **Practitioner validation** (OD-01/03/06/08/13) | Data, not schema | 🔒 Answerable after D4 ships |

---

## 28. Database Decision Records

| ID | Decision | Rationale | Reversibility |
|---|---|---|---|
| **DDR-01** | UUID primary keys | Non-enumerable; safe in URLs and offline generation | Hard |
| **DDR-02** | ENUM for closed sets, lookup tables for open sets | Type safety where fixed; extensibility where not | Moderate |
| **DDR-03** | Nullable `tenant_id` for shared catalogue | 🔒 One table for curated + custom (M4.3) | Hard |
| **DDR-04** | Store token hashes, never tokens | A database read must not yield credentials | Easy |
| **DDR-05** | Rotating refresh tokens with reuse detection | 🔒 Makes 30-day sessions safe | Moderate |
| **DDR-06** | Materialised timeline via events | Satisfies FR-M1-018 without violating R6 | Easy (derived) |
| **DDR-07** | Versioned `jsonb` assessment definitions | FR-M3-002; expansion without redesign | Hard |
| **DDR-08** | Project calculation fields into typed columns | 🔒 Decouples nutrition from assessment structure | Moderate |
| **DDR-09** | Nutrients as rows, not columns | Phase 2 micronutrients need no migration | Moderate |
| **DDR-10** | Polymorphic items via mutually-exclusive nullable FKs | Preserves referential integrity | Moderate |
| **DDR-11** | Plan/version split with copy-on-revise | 🔒 FR-M4-033 + EC-M4-03 | Hard |
| **DDR-12** | One snapshot serves PDF, portal and offline sync | Single rendering payload; hash-based cache validation | Moderate |
| **DDR-13** | Precompute at-risk state nightly | Avoids a four-way cross-module join on a hot screen | Easy (derived) |
| **DDR-14** | Usage counters + append-only events | O(1) enforcement, reconcilable | Moderate |
| **DDR-15** | Audit immutability by database grant | 🔒 Application discipline cannot be relied upon | Easy |
| **DDR-16** | Diet templates mutable, not versioned | Practitioner expectation; safety comes from plan snapshots | Moderate |
| **DDR-17** | Curated catalogue as versioned data files | Curation is not schema evolution | Easy |

---

## 29. Proposals Requiring Approval

🟡 New in this phase, not derived from the PRD or Architecture.

| # | Proposal | § | If rejected |
|---|---|---|---|
| 1 | 🔒 **Meal locking semantics** (item/slot level, AI must not alter locked items) | 8.10 | Needs your definition — you specified the capability, not the semantics |
| 2 | `clinical_reference_ranges` table for BMI thresholds | 7.5 | Thresholds hardcoded; OD-08 changes need a release |
| 3 | `client_nutrition_profile` as a typed projection | 7.4 | Nutrition parses assessment `jsonb`, coupling the modules |
| 4 | `dietary_rules` as data with `hard`/`soft` severity | 8.15 | Clinical rules become code; OD-06 needs a release |
| 5 | `progress_snapshots` with practitioner dismissal | 12.2 | Live four-way join; EC-M9-02 unaddressed |
| 6 | `invoice_sequences` table for gapless GST numbering | 14.6 | Postgres sequences skip on rollback → non-compliant invoices |
| 7 | `usage_events` append-only alongside counters | 14.5 | Counters unreconcilable if they drift |
| 8 | Trigger-maintained `foods.search_vector` | 18.4 | Aliases excluded from search, or a stale vector |
| 9 | Automated test asserting RLS on every table | 17.3 | A new table can ship without isolation |
| 10 | Post-restore file reconciliation job | 24 | Silent broken file references after recovery |
| 11 | Separate connection pool limits per process | 25.4 | Worker jobs can starve web capacity |
| 12 | Table-level PII classification comments | 26.2 | Erasure scope and retention derived from memory |
| 13 | `supplements` explicitly vendor-neutral (no brand/price/vendor columns) | 8.14 | Confirm this matches your intent |
| 14 | `food_search_misses` owned by `admin`, written by `nutrition` via port | 8.17 | Ownership ambiguity |
| 15 | Lease duration = job timeout × 2 | 13.3 | Long jobs re-claimed while running |

---

## 30. Open Items Carried Forward

| Item | Blocks | Resolution path |
|---|---|---|
| ⚠️ 🔒 Pooler transaction mode | **Launch** | Verify in S0 — highest-severity assumption |
| ⚠️ 🔒 Object storage backup guarantees | **Launch** | Verify in S0 |
| ⚠️ OD-05 minor consent mechanism | **Launch** | Privacy lawyer |
| ⚠️ OD-08 Indian BMI thresholds | Display only | Practitioner + citable source → config row |
| ⚠️ OD-06 CKD / eating-disorder policy | AI drafting | Clinical decision → `dietary_rules` row |
| ⚠️ OD-13 energy equation | Targets | Practitioner → `calculation_method` |
| ⚠️ OD-03 katori reference values | Food data | 🔒 Practitioner-led curation |
| ⚠️ OD-01 lifecycle stages | `client_stage` enum | Practitioner validation |
| ⚠️ ASM-06 IFCT licensing | D3 seed data | Non-engineering |
| ⚠️ ASM-09 GST treatment | D10 invoicing | Accountant |
| ⚠️ ASM-10 DPDP operational detail | Consent design | Privacy lawyer |

---

**END OF DOCUMENT**

*Phase 4 of 11 complete. Awaiting review before Phase 5 — API Specification.*
