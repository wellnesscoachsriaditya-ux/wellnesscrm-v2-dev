# WellnessCRM V2 — API Specification

**Status:** Draft v0.1 — awaiting review
**Owner:** Founder / CTO
**Date:** 2026-08-04
**Phase:** 5 of 11 (API Specification)
**Derives from:** `WellnessCRM-V2-PRD.md`, `WellnessCRM-V2-ARCHITECTURE.md`, `WellnessCRM-V2-DATABASE.md` (all approved)

---

## Document Control

### Purpose

The definitive contract between frontend and backend. 🔒 Per NFR-079, the TypeScript client is **generated** from this contract's OpenAPI output — so this document is not documentation *about* the API, it is the source of the API's shape.

| Defines | Does not define |
|---|---|
| Endpoint groups, paths, methods | Handler implementation (Phase 9) |
| Request/response contracts and field semantics | Screen designs (Phase 6) |
| Validation rules and error envelope | Sprint sequencing (Phase 8) |
| Auth realms, scopes, rate limits | SQL or ORM code |
| Idempotency, pagination, versioning | |
| Workflow sequences for plans, AI, uploads | |

### Conventions

> 🔒 **BINDING** — derives from an approved requirement.
> 🟡 **PROPOSAL** — new here; requires approval (§20).
> ⚠️ **RISK** — known hazard.
> 📌 **ADR-A{nn}** — API Decision Record (§19).

### Notation

Contracts are expressed as **field tables**, not code. `?` marks optional. Types are logical (`uuid`, `string`, `int`, `decimal`, `date`, `datetime`, `enum`, `object`, `array`).

---

## 1. API Design Principles

| # | Principle | Source | Test |
|---|---|---|---|
| 1 | **One API, three realms** | Arch §6.1 | Same pipeline, same conventions; realm changes *what*, never *how* |
| 2 | **Resource-oriented, workflow-explicit** | — | CRUD for records; named actions for state transitions |
| 3 | **The client never computes domain outcomes** | NFR-068, Arch §4.4 | Every derived value is server-supplied |
| 4 | **Every response is envelope-consistent** | NFR-063 | One success shape, one error shape, no exceptions |
| 5 | **Contracts are additive** | NFR-079 | Breaking changes require a version, not a patch |
| 6 | **Native-app-ready from day one** | 🔒 PWA memory | No web-only assumptions: no cookies-only auth, no server-rendered coupling |
| 7 | **Authorization is declared, never inferred** | ADR-05 | Every endpoint declares its action; undeclared = startup failure |

### 1.1 Base URL and realm segmentation

📌 **ADR-A01 — realm is a path segment, not a header**

```
/api/v1/app/...       practitioner realm
/api/v1/portal/...    client realm
/api/v1/admin/...     operator realm
/api/v1/public/...    unauthenticated
```

| Option | Verdict |
|---|---|
| Single namespace, realm from token | ❌ Impossible to rate-limit, audit or firewall differently. A routing mistake could expose an admin handler to a client token |
| Realm in a header | ❌ Invisible in logs and access rules; easy to omit |
| **Realm in the path** | ✅ **Chosen.** Realm-specific rate limits, middleware and audit rules are declarative. 🔒 A client token presented to `/admin/*` fails signature verification (Arch §6.1), not merely a claim check |

🔒 **`/public/*` is the only unauthenticated surface** (Arch §15.3) — enumerable in one place, reviewable as a unit.

---

## 2. Authentication

🔒 Arch §6, DB §4. Three realms, separate signing keys.

### 2.1 Token model

| Token | Lifetime | Transport | Storage |
|---|---|---|---|
| **Access token** | 🟡 15 min | `Authorization: Bearer <jwt>` | Memory only |
| **Refresh token** | 🔒 ~30 days (approved) | 🔒 HttpOnly, Secure, SameSite=Strict cookie | Browser cookie jar |

📌 **ADR-A02 — short access token in memory, refresh token in an HttpOnly cookie**

| Option | Verdict |
|---|---|
| Access token in localStorage | ❌ Readable by any XSS payload; a stolen token is valid for its full life |
| Both in cookies | ❌ CSRF exposure on every mutation; awkward for native apps later |
| **Access in memory, refresh in HttpOnly cookie** | ✅ **Chosen.** XSS cannot read the refresh token; short access life bounds theft; native apps can use the same endpoints with the refresh token in secure storage (Principle 6) |

🔒 **Access token claims:** `sub`, `realm`, `tenant_id` (absent for operators), `role`, `session_id`, `exp`, `iat`. 🔒 **No PII, no email, no name** — a JWT is base64, not encrypted, and NFR-033 applies.

### 2.2 Practitioner endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/public/auth/register` | Create tenant + owner user (FR-M0-001) |
| POST | `/api/v1/public/auth/verify-email` | FR-M0-002 |
| POST | `/api/v1/public/auth/login` | Issue tokens |
| POST | `/api/v1/public/auth/refresh` | 🔒 Rotate (DDR-05) |
| POST | `/api/v1/app/auth/logout` | Revoke session (NFR-042) |
| POST | `/api/v1/public/auth/password-reset/request` | FR-M0-003 |
| POST | `/api/v1/public/auth/password-reset/confirm` | |

**`POST /public/auth/register`**

| Field | Type | Validation |
|---|---|---|
| `email` | string | RFC-valid, ≤254, lowercased |
| `password` | string | 🟡 ≥10 chars; checked against a common-password list, not composition rules |
| `full_name` | string | 1–120 |
| `practice_name` | string | 1–120 |
| `mobile` | string? | E.164 (NFR-100) |
| `accepted_terms_version` | string | Required |

**201** → `{ user: {...}, tenant: {...}, email_verification_required: true }`
⚠️ 🔒 **Never returns tokens.** Email verification precedes access (FR-M0-002).
⚠️ 🔒 **Registration must not reveal whether an email exists** (NFR-043) — a duplicate returns the same 201 shape and sends a "someone tried to register with your email" notification instead.

**`POST /public/auth/refresh`** — 🔒 DDR-05 rotating refresh with reuse detection.
Reads the cookie, issues a new access token *and* a new refresh cookie, invalidates the old.
⚠️ 🔒 **If a previously-rotated token is presented, the entire session family is revoked and this returns 401.** That is the signature of a replayed stolen token; the correct response is to log the family out, not to allow access.

### 2.3 Client portal authentication

🔒 FR-M0-005 — passwordless. 🔒 Magic link expires in **15–30 minutes** (approved).

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/public/portal/access/request` | 🔒 Self-service link re-request |
| POST | `/api/v1/public/portal/access/redeem` | Exchange token for a session |

**`POST /public/portal/access/request`** — `{ mobile_or_email: string }`
🔒 **Always returns 202 with an identical body**, whether or not the identifier matches. Anything else turns this into a client-enumeration oracle (NFR-043).
🔒 Rate-limited hard (§14).

⚠️ 🔒 **This endpoint is on the critical path, not an error path.** With a 15–30 minute expiry, a client opening a WhatsApp message an hour later *will* need a new link. It must be reachable in one tap from the expired-link screen and **must never require the practitioner** (EC-M7-01). Flagged for Phase 6 as a primary flow.

**`POST /public/portal/access/redeem`** — `{ token: string }`
**200** → `{ access_token, expires_in, client: { id, full_name, tenant_name, tenant_branding }, target: { type, ref } }`
🔒 `target` carries the deep-link destination (FR-M7-013) so redemption and navigation are one hop.
🔒 Single-use: enforced by a conditional update on `consumed_at`, not an application check (DDR-04).

### 2.4 Operator authentication

`POST /api/v1/public/admin/auth/login` → 🔒 always a two-step 2FA challenge (FR-M0-009). No bypass, no "remember this device" at MVP.
🔒 Short session (DB §4.6). Separate signing key.

---

## 3. Authorization

🔒 ADR-05 — one decision point. The API's contribution is **declaring** the action; the kernel decides.

### 3.1 Action declaration

Every endpoint declares `(action, resource_type)` — e.g. `client.read`, `plan.issue`, `food.create_custom`. 🔒 An endpoint without a declared action **fails at application startup**, not at runtime (Arch §6.3). The full permission surface is therefore enumerable from the route manifest rather than by auditing handler bodies.

### 3.2 Role matrix (MVP)

| Action group | Owner | Practitioner | Client | Operator |
|---|---|---|---|---|
| Clients (all in tenant) | ✅ | 🔒 assigned only | ❌ | 🔒 aggregate only |
| Clinical records | ✅ | assigned only | 🔒 own, shared subset | 🔒 counts only |
| Plans — author/issue | ✅ | assigned only | ❌ | ❌ |
| Plans — view issued | ✅ | assigned only | 🔒 own, issued only | ❌ |
| Food catalogue — read | ✅ | ✅ | ❌ | ✅ |
| Food catalogue — curate | ❌ | ❌ | ❌ | 🔒 ✅ only |
| Team & billing | ✅ | ❌ | ❌ | 🔒 plan assignment |
| Message templates | ❌ | ❌ | ❌ | ✅ |

🔒 **Ownership is part of the decision, not a later filter.** "Practitioner may read a client" is insufficient — it must be "may read *this* client" (§17.2 of DB). A 403 on a resource in another tenant is returned as **404** (§5.4).

### 3.3 Entitlement enforcement

🔒 FR-M0-045 — enforced at the point of action, before the domain call. Returns **402** with the limit and upgrade path (§5.5).
🔒 FR-M0-046 fail-safe: if entitlement state is indeterminate, reads succeed and only metered writes fail.

---

## 4. Request & Response Conventions

### 4.1 Envelopes

📌 **ADR-A03 — bare resource objects on success, a single structured envelope on error**

| Option | Verdict |
|---|---|
| `{data: ...}` wrapper everywhere | ❌ Adds a hop to every access for no benefit; noisier generated types |
| **Bare object; `{items, page}` for collections; `{error}` for failures** | ✅ **Chosen.** Cleanest generated TypeScript; unambiguous — presence of `error` is the discriminator |

**Single resource** → the object.
**Collection** → `{ items: [...], page: { next_cursor?, has_more, total? } }`
**Action with no body** → `204`.
**Error** → §5.

### 4.2 Universal headers

| Header | Direction | Purpose |
|---|---|---|
| `Authorization: Bearer` | → | All authenticated requests |
| `Idempotency-Key` | → | 🔒 Required on specified POSTs (§13) |
| `If-Match` | → | 🔒 Required on optimistic-concurrency writes (§4.4) |
| `X-Request-Id` | ↔ | Correlation; echoed in errors |
| `ETag` | ← | On versioned resources |
| `RateLimit-*` | ← | §14 |
| `Deprecation` / `Sunset` | ← | §12.3 |

### 4.3 Field conventions

🔒 Consistency is a contract property:

| Rule | Detail |
|---|---|
| Naming | `snake_case` — matches the database, avoids a translation layer |
| Timestamps | 🔒 ISO-8601 with offset, always UTC on the wire (`2026-08-04T09:30:00Z`) |
| Dates | `YYYY-MM-DD`, no time (NFR-099) |
| Decimals | 🔒 **String-encoded** — see below |
| Money | `{ amount: "1799.00", currency_code: "INR" }` (NFR-098) |
| Enums | Lowercase snake_case strings, never integers |
| Nulls | Explicit `null` for "known empty"; field omitted for "not requested" |
| IDs | UUID strings |

📌 **ADR-A04 — decimals are transported as strings**

🔒 Nutrition values, weights and money are `numeric` in Postgres (DB §1.2) precisely to avoid float error. JSON numbers are IEEE-754 doubles — `0.1 + 0.2 !== 0.3`. Sending `"12.345"` preserves exactness end-to-end. **In a clinical product, silent rounding in nutrition totals is a defect, not a rounding artefact.**

⚠️ **Cost:** the frontend must not do arithmetic on these. Consistent with Principle 3 — the server computes, the client renders.

### 4.4 Optimistic concurrency

🔒 ADR-14, EC-M4-07. Resources carrying `row_version` (plan versions, clients, appointments, assessment responses) return an `ETag`. Mutations **must** send `If-Match`.

- Missing `If-Match` on a guarded resource → **428 Precondition Required**
- Stale `If-Match` → **409 Conflict** with the current state, so the UI can offer reload
- 🔒 **No last-write-wins anywhere that matters.**

---

## 5. Error Model

🔒 Arch §17.1. One envelope, one taxonomy.

### 5.1 Envelope

```
{
  "error": {
    "type": "entitlement_exceeded",
    "message": "You've reached 30 active clients on the Starter plan.",
    "action": "Move a client to Paused, or upgrade to Growth.",
    "request_id": "req_01J...",
    "details": { ... }          // type-specific, optional
  }
}
```

🔒 **`message` + `action` are both mandatory** — NFR-063 requires stating what went wrong *and* what to do next. A message without an action is an incomplete error.
🔒 **`type` is a stable machine identifier.** The frontend branches on `type`, never on `message` — messages are localisable (NFR-096) and will change.

### 5.2 Taxonomy

| `type` | Status | Notes |
|---|---|---|
| `validation_failed` | 422 | `details.fields[]` with per-field messages |
| `unauthenticated` | 401 | 🔒 Generic — never reveals account existence |
| `forbidden` | 403 | 🔒 Audited (FR-M0-032) |
| `not_found` | 404 | 🔒 Also used for cross-tenant resources (§5.4) |
| `entitlement_exceeded` | 402 | 🔒 `details`: resource, limit, used, upgrade path |
| `consent_required` | 403 | `details.purpose` |
| `conflict` | 409 | `details.current_state` |
| `precondition_required` | 428 | Missing `If-Match` |
| `idempotency_conflict` | 409 | Same key, different payload (§13) |
| `rate_limited` | 429 | `Retry-After` |
| `domain_rule_violated` | 422 | 🔒 `details.rule_code`, `details.severity` (§8.7) |
| `integration_unavailable` | 503 | 🔒 Work never lost; state explained |
| `internal_error` | 500 | Generic + `request_id` only |

### 5.3 Validation detail

```
"details": { "fields": [
  { "field": "mobile", "code": "invalid_format",
    "message": "Enter a 10-digit Indian mobile number." } ] }
```
🔒 Field-level, actionable, never "invalid input" (NFR-063).

### 5.4 404 for unauthorized resources

🔒 A resource in another tenant returns **404, not 403** — a 403 confirms existence. The attempt is still audited (AC-M0-002). Applies to cross-tenant access and to a practitioner requesting an unassigned client.

### 5.5 Entitlement detail

```
"details": { "resource": "active_clients", "limit": 30, "used": 30,
             "plan_code": "starter", "upgrade_to": "growth",
             "upgrade_price": { "amount": "1799.00", "currency_code": "INR" } }
```
🔒 Everything the UI needs to render FR-M0-045 without a second call.

---

## 6. Pagination, Filtering, Sorting

### 6.1 Cursor pagination

📌 **ADR-A05 — cursor-based, not offset**

| Option | Verdict |
|---|---|
| `?page=2&per_page=25` | ❌ Rows shift between pages as data changes; `OFFSET` degrades on large tables |
| **`?cursor=<opaque>&limit=25`** | ✅ **Chosen.** Stable under concurrent writes; index-friendly; correct for infinite scroll on the client PWA |

Request: `limit` (1–100, default 25), `cursor?`
Response: `{ items, page: { next_cursor?, has_more, total? } }`

🔒 **`total` is omitted by default** — a `COUNT(*)` on every list request is wasteful. Available via `?include_total=true` where the UI genuinely needs it (e.g. "30 of 100 clients").

### 6.2 Filtering

🔒 Explicit named parameters only. No generic query language — that would be an unversionable, unindexable, unauthorizable surface.

`GET /app/clients?stage=active&stage=paused&tag_id=<uuid>&owner_user_id=<uuid>&q=priya`
🔒 Repeated params are OR within a field, AND across fields. Predictable, and maps to indexable SQL.

### 6.3 Sorting

`?sort=-updated_at` (`-` = descending). 🔒 Allowlisted per endpoint — an unindexed sort column is a performance defect waiting to happen.

### 6.4 Sparse fieldsets

🟡 **PROPOSAL — `?fields=` is NOT supported at MVP.** It complicates generated types (every field becomes optional) for marginal benefit. If the client PWA's bundle or payload budget (NFR-002) demands it, purpose-built compact endpoints are the answer, not a generic mechanism.

---

## 7. Endpoint Groups — Practitioner Realm

🔒 `/api/v1/app/*`. Grouped by owning module (Arch §3.3).

### 7.1 Clients — `clients`

| Method | Path | Action | Notes |
|---|---|---|---|
| GET | `/app/clients` | `client.list` | Filter, sort, search (FR-M1-021/022) |
| POST | `/app/clients` | `client.create` | 🔒 Metered if stage=`active` |
| GET | `/app/clients/{id}` | `client.read` | Returns `ETag` |
| PATCH | `/app/clients/{id}` | `client.update` | Requires `If-Match` |
| POST | `/app/clients/{id}/stage` | `client.change_stage` | 🔒 Named action, not a PATCH |
| POST | `/app/clients/{id}/archive` | `client.archive` | Soft delete (FR-M1-010) |
| POST | `/app/clients/{id}/restore` | `client.restore` | EC-M1-02 |
| GET | `/app/clients/{id}/timeline` | `client.read_timeline` | FR-M1-018, cursor |
| GET/POST | `/app/clients/{id}/notes` | `client.*_note` | |
| PUT/DELETE | `/app/clients/{id}/tags/{tag_id}` | `client.manage_tags` | |
| GET | `/app/clients/{id}/portal-access` | `client.read_access` | Link status |
| POST | `/app/clients/{id}/portal-access/send` | `client.send_access` | 🔒 Issues a magic link |

📌 **ADR-A06 — state transitions are named POST actions, not PATCH on a status field**

🔒 `PATCH {stage: "active"}` looks simpler but hides consequential logic: entitlement checks, `activated_at`, check-in scheduling, message cancellation, history rows. A named action makes the operation explicit, separately authorizable, separately audited, and able to return transition-specific errors (402 at the limit).

**`POST /app/clients/{id}/stage`** — `{ to_stage: enum, reason?: string }`
**200** → updated client. **402** if moving to `active` at the limit (FR-M1-002).

**`GET /app/clients` — response item**

| Field | Type | Notes |
|---|---|---|
| `id`, `full_name`, `mobile?`, `email?` | | |
| `stage` | enum | |
| `owner_user_id`, `owner_name` | | 🔒 Denormalised to avoid N+1 in the list |
| `tags` | array | `{id, name, color}` |
| `dietary_class?` | enum | |
| `last_activity_at?` | datetime | |
| `active_plan_version_id?` | uuid | 🔒 Lets the list link straight to the plan |
| `is_at_risk` | bool | 🔒 Server-computed (DDR-13) |
| `created_at`, `updated_at` | datetime | |

🔒 `is_at_risk` and `owner_name` are server-supplied per Principle 3 — the client renders, never derives.

### 7.2 Leads — `leads`

| Method | Path | Action |
|---|---|---|
| GET | `/app/enquiry-forms` | `enquiry_form.list` |
| PATCH | `/app/enquiry-forms/{id}` | `enquiry_form.update` |
| GET | `/app/enquiries` | `enquiry.list` |
| GET | `/app/enquiries/needs-response` | `enquiry.list` — FR-M2-011, ordered by age |

🔒 The public submission endpoint is §11.

### 7.3 Clinical — `clinical`

| Method | Path | Action | Notes |
|---|---|---|---|
| GET | `/app/assessment-definitions` | `assessment.read_definitions` | Published versions |
| GET | `/app/clients/{id}/assessments` | `assessment.list` | All administrations (FR-M3-007) |
| POST | `/app/clients/{id}/assessments` | `assessment.create` | Starts a response |
| GET | `/app/assessments/{id}` | `assessment.read` | Definition + answers |
| PATCH | `/app/assessments/{id}` | `assessment.update` | 🔒 Partial save (FR-M3-005) |
| POST | `/app/assessments/{id}/complete` | `assessment.complete` | 🔒 Triggers the profile projection |
| POST | `/app/assessments/{id}/send-invite` | `assessment.invite` | Magic link to client |
| GET/POST | `/app/clients/{id}/measurements` | `measurement.*` | |
| PATCH/DELETE | `/app/measurements/{id}` | `measurement.*` | |
| GET/POST | `/app/clients/{id}/notes/consultation` | `consultation_note.*` | 🔒 Never client-visible |
| GET/POST | `/app/clients/{id}/documents` | `document.*` | §10 upload flow |
| GET | `/app/clients/{id}/nutrition-profile` | `nutrition_profile.read` | 🔒 The projection (DB §7.4) |

**`POST /app/assessments/{id}/complete`**
🔒 Projects `calculation_bindings` into `client_nutrition_profile` (DDR-08) and emits an event. **200** → `{ assessment, nutrition_profile, derived: {...} }`

🔒 **`derived` is server-computed** — BMI, waist-hip ratio, estimated energy requirement:
```
"derived": { "bmi": "24.6", "bmi_band": null, "bmi_band_source": null,
             "waist_hip_ratio": "0.82",
             "estimated_energy_kcal": null, "calculation_method": null }
```
⚠️ 🔒 **`bmi_band` and `estimated_energy_kcal` are `null` until OD-08 and OD-13 are resolved.** The contract carries the fields so no change is needed later, but 🔒 **the API must not return a clinical threshold or equation result we cannot cite.** `bmi_band_source` exists so the UI can display the citation alongside the band — no citation, no band.

**Assessment answers contract** — 🔒 shaped by the definition version, not by this spec:
```
{ "definition": { "id", "code", "version", "schema": {...} },
  "answers": { "<field_id>": <value> },
  "completed_sections": ["a","b"], "status": "in_progress" }
```
🔒 The client renders from `schema`. Adding assessment fields requires **no API change and no frontend release** — this is what makes the approved "expansion without redesign" refinement real at the contract level.

### 7.4 Nutrition catalogue — `nutrition`

| Method | Path | Action | Notes |
|---|---|---|---|
| GET | `/app/foods/search` | `food.search` | 🔒 NFR-004 ≤300ms |
| GET | `/app/foods/{id}` | `food.read` | With portions + nutrients |
| POST | `/app/foods` | `food.create_custom` | 🔒 Inline creation (FR-M4-012) |
| PATCH | `/app/foods/{id}` | `food.update_custom` | 🔒 Custom only |
| POST | `/app/foods/{id}/retire` | `food.retire` | EC-M4-04 |
| GET | `/app/food-categories` | `food.read_categories` | |
| GET | `/app/measure-units` | `food.read_units` | |
| GET/POST | `/app/recipes` | `recipe.*` | Read MVP, write Phase 2 |
| GET/POST/PATCH/DELETE | `/app/meals` | `meal.*` | FR-M4-018/020 |
| GET | `/app/supplements` | `supplement.list` | 🔒 Vendor-neutral (approved) |
| POST | `/app/supplements` | `supplement.create_custom` | |

**`GET /app/foods/search`**

| Param | Notes |
|---|---|
| `q` | ≥1 char |
| `client_id?` | 🔒 **Applies that client's dietary rules and allergens** |
| `category_id?`, `slot_type?`, `food_type?` | |
| `include_custom` | default true |
| `limit` | 1–50, default 20 |

🔒 **`client_id` is the critical parameter.** Passing it filters at the query level using `client_nutrition_profile` — excluded allergens, dietary class, onion/garlic, root vegetables (FR-M4-035, EC-M4-09). 🔒 **Filtering happens inside the SQL, never post-retrieval** (DB §18.4), or ranking and result counts break.

Response item: `id`, `name`, `category_name`, `food_type`, `dietary_class`, `is_custom`, `verification_status`, `source?`, `default_portion { measure_code, display, quantity, grams }`, `available_portions[]`, `nutrients_per_base { energy_kcal, protein_g, ... }`, `base_quantity_g`, plus 🔒 `excluded_reason?` when a food is returned but flagged rather than hidden (soft rules).

🔒 **Search misses are recorded server-side** (FR-M4-014) when `result_count = 0`. No client involvement — it is our highest-value curation signal and must not depend on the frontend remembering to report it.

**`POST /app/foods`** — 🔒 must not lose plan state (FR-M4-012). Body: `name`, `category_id`, `food_type`, `dietary_class`, `base_quantity_g`, `nutrients{}`, `portions[]?`, `contains_onion_garlic?`, `is_root_vegetable?`, `aliases[]?`
🔒 Created with `verification_status: "user_submitted"`, `tenant_id` = caller's tenant. 🟡 Prompts for a household measure (approved proposal #3) — `portions` optional, but the UI should ask.

---

## 8. Nutrition Engine Workflows ⭐

🔒 The wedge. These are the API's most consequential contracts.

### 8.1 Resources

| Method | Path | Action |
|---|---|---|
| GET/POST | `/app/diet-templates` | `template.*` |
| GET/PATCH/DELETE | `/app/diet-templates/{id}` | `template.*` |
| POST | `/app/diet-templates/{id}/duplicate` | `template.duplicate` |
| GET | `/app/clients/{id}/plans` | `plan.list` |
| POST | `/app/clients/{id}/plans` | `plan.create` |
| GET | `/app/plans/{id}` | `plan.read` |
| GET | `/app/plan-versions/{id}` | `plan_version.read` |
| PATCH | `/app/plan-versions/{id}` | `plan_version.update` |
| POST | `/app/plan-versions/{id}/items` | `plan_version.add_item` |
| PATCH/DELETE | `/app/plan-items/{id}` | `plan_version.*_item` |
| POST | `/app/plan-items/{id}/lock` | 🔒 `plan_version.lock_item` |
| POST | `/app/plan-items/{id}/unlock` | 🔒 `plan_version.unlock_item` |
| POST | `/app/plan-slots/{id}/lock` | 🔒 `plan_version.lock_slot` |
| POST | `/app/plan-versions/{id}/recalculate` | 🔒 `plan_version.recalculate` — §8.5 |
| POST | `/app/plan-versions/{id}/issue` | 🔒 `plan_version.issue` — §8.6 |
| POST | `/app/plan-versions/{id}/revise` | `plan_version.revise` |
| POST | `/app/plan-versions/{id}/discard` | `plan_version.discard` |
| POST | `/app/plan-versions/{id}/save-as-template` | `template.create` |
| GET | `/app/plan-versions/{id}/nutrition` | `plan_version.read_nutrition` |
| GET | `/app/plan-versions/{id}/snapshot` | `plan_version.read_snapshot` |
| GET/PUT | `/app/clients/{id}/nutrition-targets` | `nutrition_target.*` |

### 8.2 Plan creation

**`POST /app/clients/{id}/plans`**

| Field | Type | Notes |
|---|---|---|
| `title` | string | |
| `source` | enum | 🔒 `blank` \| `template` \| `previous_plan` \| `ai_draft` (FR-M4-032) |
| `source_template_id?` / `source_plan_version_id?` | uuid | |
| `day_count` | int | 1 or 7 |
| `valid_from?` / `valid_to?` | date | |
| `target_override?` | object | |

**201** → `{ plan, draft_version: {...} }`
🔒 Creates the plan **and its first `draft` version** — a plan without a version is not a usable state, so the API never produces one.
🔒 `uq_diet_plan_versions__one_draft` means a second draft attempt returns **409**.

🔒 **Instantiating from a template preserves `is_locked`** (approved) — and 🔒 locks **remain editable before approval**, which is why `/plan-items/{id}/unlock` exists and is always available to the practitioner.

### 8.3 The plan version contract

**`GET /app/plan-versions/{id}`**

```
{ "id", "plan_id", "version_number", "state", "origin",
  "row_version", "valid_from", "valid_to",
  "targets": { "energy_kcal": "1400.000", ... },
  "nutrition_budget": { ... },              // 🔒 §8.4
  "days": [ { "day_number", "label",
      "slots": [ { "id", "slot_type", "custom_label?", "target_time?",
          "is_locked": false, "sort_order",
          "items": [ { "id", "item_type", "food_id?", "recipe_id?", "meal_id?",
              "display_name", "quantity", "measure_unit_code",
              "measure_display",           // 🔒 "1 katori"
              "resolved_grams",            // 🔒 server-resolved
              "nutrition": {...},          // 🔒 server-computed
              "is_locked", "client_note?", "sort_order",
              "alternatives": [...] } ],
          "slot_totals": {...} } ],        // 🔒 server-computed
      "day_totals": {...} } ],
  "supplements": [ { "id", "supplement_id", "name", "dosage", "unit",
                     "frequency", "timing", "is_locked" } ],
  "plan_totals": {...},
  "warnings": [ ... ],                      // 🔒 §8.7
  "practitioner_notes?" }
```

🔒 **Every nutrition figure and `resolved_grams` is server-computed** (Principle 3, NFR-072). The client never converts portions or sums nutrients — a mismatch between displayed and stored nutrition would be a clinical defect.
🔒 `measure_display` ("1 katori") is server-rendered so household-measure formatting lives in one place.

### 8.4 The nutrition budget — locking as constraints

🔒 **This is the direct expression of your approved refinement:** *"treat locked items as fixed nutritional constraints and recalculate only the remaining meals to satisfy the client's nutritional targets."*

```
"nutrition_budget": {
  "target":              { "energy_kcal": "1400.000", "protein_g": "70.000", ... },
  "locked_consumed":     { "energy_kcal": "420.000",  "protein_g": "22.000", ... },
  "unlocked_current":    { "energy_kcal": "760.000",  "protein_g": "31.000", ... },
  "remaining_available": { "energy_kcal": "220.000",  "protein_g": "17.000", ... },
  "locked_item_count": 3,
  "locked_slot_count": 1,
  "is_within_tolerance": false,
  "tolerance_pct": "5.0"
}
```

📌 **ADR-A07 — the nutrition budget is a first-class, server-computed response field**

🔒 Without it, the practitioner cannot see how much nutritional room the unlocked slots have, and the recalculation contract (§8.5) has no observable input. `remaining_available = target − locked_consumed − unlocked_current` is exactly the quantity a redistribution must satisfy, and computing it client-side would duplicate the engine (NFR-072).

⚠️ 🔒 **`remaining_available` may be negative** when locked items alone exceed the target. That is a legitimate clinical state — the practitioner has deliberately fixed items that overshoot. The API reports it as a **soft warning** (§8.7) and 🔒 **never blocks**; practitioner judgement always prevails (EC-M4-05).

### 8.5 Recalculation

**`POST /app/plan-versions/{id}/recalculate`** — 🔒 the approved locking behaviour, made operational.

| Field | Type | Notes |
|---|---|---|
| `strategy` | enum | 🟡 `redistribute_portions` \| `suggest_additions` \| `report_only` |
| `scope?` | object | Limit to specific days or slots |
| `respect_locks` | bool | 🔒 **Always true; rejected if false** |

**200** → `{ plan_version: {...}, changes: [...], unchanged_locked: [...], warnings: [...] }`

🔒 **Guarantees:**
1. 🔒 **No locked item, slot or supplement is altered.** Present in `unchanged_locked`, byte-identical in the response.
2. 🔒 Locked nutrition is **subtracted from the target first**; only the remainder is distributed across unlocked slots.
3. 🔒 `respect_locks: false` returns **422 `domain_rule_violated`** — 🔒 there is no API path to automatic modification of a locked item. The practitioner unlocks explicitly (`/unlock`) or edits directly.
4. `changes[]` names every modification, so the UI can show what moved.
5. 🔒 Deterministic — no LLM involvement. Same input, same output.

⚠️ 🔒 **`redistribute_portions` adjusts quantities of existing unlocked items; it never adds or removes foods.** Food *selection* is a clinical decision. Silent substitution would violate the spirit of the approved requirement even where the item is technically unlocked. `suggest_additions` returns suggestions the practitioner applies explicitly — it does not mutate.

### 8.6 Issue

**`POST /app/plan-versions/{id}/issue`** — 🔒 the state transition that makes a plan real.

`{ deliver: bool, delivery_channels?: ["whatsapp"], client_message?: string }`
Requires `Idempotency-Key` and `If-Match`.

🔒 **Atomically** (DB §8.12): resolve all portions → compute totals → write `computed_totals` → freeze `resolved_grams` → build the immutable `plan_snapshots.document` → `state='issued'` → supersede the prior issued version → enqueue PDF render → enqueue delivery if requested → emit `PlanApproved`.

**200** → `{ plan_version, snapshot: { id, content_hash, pdf_status: "pending" }, delivery?: { scheduled_message_ids } }`

🔒 **This endpoint is the only path from `draft` to `issued`** — and there is no path from `draft` to delivered. FR-M5-003 (no AI auto-send) is enforced here and in the schema, not by policy.
⚠️ PDF is asynchronous (ADR-09); `pdf_status: "pending"` is normal. The UI polls or receives the snapshot when ready. 🔒 **Delivery does not wait for the PDF** — the portal link works immediately from the snapshot document (DDR-12).

### 8.7 Warnings — soft rules

🔒 `dietary_rules` with `severity: soft` (DB §8.15) surface as warnings, never errors:

```
"warnings": [ { "rule_code": "energy_below_target", "severity": "soft",
                "message": "Daily energy is 220 kcal below the target.",
                "scope": { "type": "day", "day_number": 1 } } ]
```
🔒 **Hard rules are different:** allergens and dietary-class violations return **422 `domain_rule_violated`** and the write is rejected. Soft rules inform; hard rules block. That distinction is the whole point of the severity column.

---

## 9. AI Request Flows

🔒 M5, ADR-10. The API's job is to make the safety model observable and the failure modes non-destructive.

### 9.1 Endpoints

| Method | Path | Action | Notes |
|---|---|---|---|
| POST | `/app/clients/{id}/ai/plan-drafts` | `ai.generate_draft` | 🔒 Metered, async |
| GET | `/app/ai/generations/{id}` | `ai.read_generation` | Poll status |
| GET | `/app/clients/{id}/ai/eligibility` | `ai.read_eligibility` | 🔒 §9.5 — check before offering |
| GET | `/app/ai/usage` | `ai.read_usage` | Quota remaining |

### 9.2 Requesting a draft

**`POST /app/clients/{id}/ai/plan-drafts`** — requires `Idempotency-Key`.

| Field | Type | Notes |
|---|---|---|
| `day_count` | int | 1 or 7 |
| `target_override?` | object | Otherwise from `nutrition_targets` |
| `style_source?` | enum | 🟡 `my_templates` (default) \| `curated_only` |
| `emphasis_template_ids?` | uuid[] | Bias the candidate set |
| `slot_structure?` | array | Otherwise the tenant default |
| `notes_for_ai?` | string | ≤500 chars, practitioner guidance |
| `base_plan_version_id?` | uuid | 🔒 §9.4 — regenerate respecting locks |

**202 Accepted** →
```
{ "generation_id": "...", "status": "pending",
  "estimated_seconds": 20, "quota": { "used": 12, "limit": 40, "period_end": "..." } }
```

📌 **ADR-A08 — AI generation is asynchronous with polling, not a long-lived request**

| Option | Verdict |
|---|---|
| Synchronous request held open ~20–30s | ❌ Ties up a web worker on a small instance (NFR-087); vulnerable to proxy timeouts; a dropped connection loses the result *and* the quota |
| WebSocket / SSE progress stream | ❌ Real-time infrastructure for one feature. Violates NFR-077 |
| **202 + poll `GET /ai/generations/{id}`** | ✅ **Chosen.** Runs in the worker (Arch §11.2). A dropped connection loses nothing. 🔒 Consistent with "external calls never happen inside a request transaction" (Arch §5.3) |

🟡 **PROPOSAL — poll interval 2s, client-side ceiling 90s** before offering "keep waiting / build manually."

### 9.3 Generation status

**`GET /app/ai/generations/{id}`**

```
{ "id", "status", "created_at", "completed_at?",
  "quota_consumed": true,
  "resulting_plan_version_id?": "...",
  "grounding": { "candidate_food_count": 312, "template_influence_count": 4,
                 "prompt_version": "diet_draft_v3" },
  "rationale?": "...",
  "failure?": { "type", "message", "action" } }
```

🔒 `status`: `pending` | `succeeded` | `validation_failed` | `provider_failed` | `blocked`

🔒 **`quota_consumed` is returned explicitly** (DB §9.3) so the UI can state truthfully that a failure cost nothing. Only `succeeded` consumes quota (FR-M5-010, EC-M10-04).

🔒 **`grounding` is exposed deliberately.** FR-M5-011 requires a rationale, and US-M5-04 wants the practitioner to understand the draft's choices. Showing *"grounded in 312 of your permitted foods, influenced by 4 of your templates"* is what converts a black box into an assistant they trust.

🔒 **On success, `resulting_plan_version_id` points at an ordinary `draft` plan version** — editable through the standard endpoints (§8), per FR-M5-004. 🔒 **There is no AI-specific editing surface**, which is what prevents a second implementation of plan-building logic.

### 9.4 Regeneration with locks

🔒 Your approved refinement applies to AI generation as strictly as to recalculation.

When `base_plan_version_id` is supplied:

1. 🔒 Locked items, slots and supplements are **carried through unchanged** — they are not sent to the model as mutable content.
2. 🔒 Their nutrition is **subtracted from the target**; the model is asked to fill only `remaining_available` (§8.4).
3. 🔒 Post-validation asserts every locked item is byte-identical. **A violation fails the generation** (`validation_failed`, no quota consumed) rather than silently accepting altered content.

⚠️ 🔒 **This is enforced in code before and after the model call, never by prompt instruction** (ADR-10). The same reasoning as allergen enforcement: *"the model was told not to"* is not a guarantee.

### 9.5 Eligibility — the OD-06 seam

**`GET /app/clients/{id}/ai/eligibility`**

```
{ "is_eligible": true, "blocking_rules": [], "warnings": [],
  "missing_inputs": ["height_cm"],
  "quota": { "used": 12, "limit": 40, "remaining": 28 } }
```

🔒 Checked **before** offering generation, so the UI never presents a button that will fail.

⚠️ 🔒 **This endpoint is where OD-06 lands.** Once the clinical decision on CKD and eating disorders is made, it becomes a `dietary_rules` row with `rule_type: generation_block` and `applies_to_ai: true` — appearing here as a `blocking_rule`. 🔒 **No API change, no code change, no migration.** Until OD-06 is resolved, no such rule exists and nothing is blocked.

`missing_inputs` handles EC-M5-01: generation proceeds where data is sufficient, and is blocked with an explanation where it is not.

### 9.6 Failure contracts

| Failure | Status | `quota_consumed` | Practitioner impact |
|---|---|---|---|
| `provider_failed` | 200 (status field) | false | 🔒 Manual path unaffected (FR-M5-010) |
| `validation_failed` | 200 (status field) | false | 🔒 Our defect, not theirs |
| `blocked` | 200 (status field) | false | Explains the rule |
| Quota exhausted | **402** at request time | — | 🔒 Manual building still available |

🔒 **Failures return 200 with a status field, not an HTTP error**, because the *generation request* succeeded — the generation itself did not. Only pre-flight rejections (quota, authorization) are HTTP errors. This distinction keeps polling logic simple.

---

## 10. File Upload Flows

🔒 ADR-12, DB §19 — direct-to-storage with a server-issued scoped credential. Bytes never pass through FastAPI.

### 10.1 Three-step flow

```
1. POST  /app/uploads/authorize      → server checks authz, quota, type
                                     → returns file_id + scoped upload target
2. PUT   <upload_url>                → client uploads directly to storage
3. POST  /app/uploads/{file_id}/confirm → server verifies and records
```

**Step 1 — `POST /app/uploads/authorize`**

| Field | Type | Validation |
|---|---|---|
| `file_class` | enum | 🔒 `client_document` \| `branding` |
| `original_filename` | string | ≤255 |
| `content_type` | string | 🔒 Allowlist (NFR-036) |
| `size_bytes` | int | 🔒 Checked against quota **and** per-class max |
| `client_id?` | uuid | Required for `client_document` |
| `document_type?` | enum | `lab_report` \| `prescription` \| `photo` \| `other` |

**201** → `{ file_id, upload_url, upload_method: "PUT", upload_headers: {...}, expires_at, max_size_bytes }`

🔒 **402** if the storage quota is exhausted (FR-M0-040, EC-M3-07) — 🔒 checked *before* the upload begins, not after, so the client never wastes bandwidth on a rejected file.
🔒 **422** for a disallowed content type — with the permitted list in `details` (EC-M3-04).

🟡 **PROPOSED allowlist:** `application/pdf`, `image/jpeg`, `image/png`, `image/heic`, `image/webp`.
🟡 **PROPOSED size caps:** client documents 10 MB, branding 2 MB.
🔒 **Never** `text/html`, SVG or anything executable (NFR-036) — an SVG with embedded script served from our domain is a stored-XSS vector.

**Step 3 — `POST /app/uploads/{file_id}/confirm`**
Server verifies the object exists and its size matches, then sets `status='confirmed'`, increments storage usage, creates the `client_documents` row if applicable, and emits a timeline event.
🔒 **The database record is only usable after confirmation** (DB §19.1) — an abandoned upload leaves an orphan object reaped after 24h, never a phantom row.

### 10.2 Download

**`GET /app/files/{id}/download`** → **302** to a short-lived signed URL, or **200** with `{ url, expires_at }` when `?redirect=false`.

🔒 Authorization is checked on **every** retrieval (NFR-035, FR-M0-038). 🔒 The signed URL is a delivery mechanism with a short life, **never the access control** — an unguessable URL is explicitly insufficient.
🟡 **PROPOSED signed-URL lifetime: 5 minutes.**

### 10.3 Client portal upload

🔒 Identical three-step flow at `/portal/uploads/*` (FR-M3-025). Constraints: `file_class` forced to `client_document`, `client_id` forced to the authenticated client, 🔒 stricter rate limit (§14), and the practitioner is notified on confirmation.

---

## 11. Public Endpoints

🔒 Arch §15.3 — the entire unauthenticated surface, enumerable in one place.

| Method | Path | Rate limit | Notes |
|---|---|---|---|
| GET | `/public/forms/{tenant_slug}` | 60/min/IP | Form definition + consent notice |
| POST | `/public/forms/{tenant_slug}/submit` | 🔒 5/min/IP, 20/hr/IP | FR-M2-005 |
| POST | `/public/portal/access/request` | 🔒 3/min/identifier | §2.3 |
| POST | `/public/portal/access/redeem` | 10/min/IP | |
| POST | `/public/auth/*` | §14 | §2.2 |
| GET | `/public/health` | unlimited | NFR-086 |
| POST | `/public/webhooks/{provider}` | 🔒 signature-verified | §11.3 |

### 11.1 Enquiry form definition

**`GET /public/forms/{tenant_slug}`** → `{ tenant: { name, branding? }, form: { id, title, intro_text, fields[] }, consent: { notice_id, notice_version, title, body, purposes[] } }`

🔒 Returns only the practitioner's public identity — 🔒 **never client counts, plan details, or any tenant-internal state.** `tenant_slug` is publicly enumerable by design (DB §4.1), so this response is effectively public information.
**404** for an unknown or inactive slug. 🔒 A suspended tenant returns 404 with a neutral message (EC-M2-07) — never "this practitioner hasn't paid."

### 11.2 Enquiry submission

**`POST /public/forms/{tenant_slug}/submit`**

| Field | Validation |
|---|---|
| `full_name` | 1–120, required |
| `mobile` | E.164 India at MVP, required unless email given |
| `email?` | RFC-valid |
| `primary_goal` | required (FR-M2-003) |
| `answers?` | object, matched against the form definition |
| `consent_granted` | 🔒 must be `true` |
| `consent_notice_id` | 🔒 must match the current notice |
| `captcha_token` | 🔒 required (FR-M2-008) |

**202 Accepted** → `{ submitted: true, message: "..." }`

⚠️ 🔒 **The response is identical whether the mobile matches an existing client or not.** EC-M2-02 duplicate matching happens server-side, silently. Returning "we already have you" would make this endpoint a client-enumeration oracle against a practitioner's client list — the most serious privacy leak available on the public surface.

🔒 **403** if `consent_granted` is false (EC-M2-04) — no record is created.
🔒 Leads are **never metered** (FR-M1-003), so a tenant at their client limit still accepts enquiries (EC-M2-06).

### 11.3 Webhooks

**`POST /public/webhooks/{provider}`** — `whatsapp` | `razorpay`

🔒 Contract for all providers:
1. 🔒 **Verify the signature before parsing the body.** An unverified webhook is an unauthenticated write endpoint.
2. 🔒 **Respond 200 within ~2 seconds**, before processing. Providers retry aggressively on slow responses, causing duplicate storms.
3. 🔒 **Enqueue a job** for actual processing (Arch §12.5).
4. 🔒 **Idempotent by provider event id** — `uq_message_dispatches__provider_id` and `uq_payments__gateway_id` (DB §11.3, §14.6).

**200** → `{ received: true }` — 🔒 always this shape, even for events we ignore. Signature failure returns **401** with no body.

⚠️ 🔒 **MVP handles delivery status only** (Arch §12.2). Inbound WhatsApp *replies* are not processed — they reach the practitioner's own WhatsApp (EC-M8-07). The endpoint accepts and discards them rather than erroring, so Meta does not mark our webhook unhealthy.

---

## 12. Client Portal API

🔒 `/api/v1/portal/*`. Persona P3: mid-range Android, 4G, 60-second tasks.

### 12.1 Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/portal/me` | Client + practitioner identity + branding |
| GET | `/portal/today` | 🔒 §12.2 — the single most important endpoint |
| GET | `/portal/plan` | Current issued plan (full) |
| GET | `/portal/plan/versions/{id}` | A specific issued version |
| GET | `/portal/plan/pdf` | 302 to signed URL |
| POST | `/portal/adherence` | 🔒 Idempotent (§13) |
| POST | `/portal/measurements` | Weight etc. |
| GET | `/portal/progress` | Trend + adherence rate |
| GET | `/portal/appointments` | Upcoming with join links |
| GET | `/portal/assessments/{id}` | Assigned assessment |
| PATCH | `/portal/assessments/{id}` | 🔒 Partial save |
| POST | `/portal/assessments/{id}/complete` | |
| POST | `/portal/uploads/*` | §10.3 |
| GET/PUT | `/portal/consent` | 🔒 FR-M7-015 |
| POST | `/portal/sync` | 🔒 §12.4 — offline queue |

🔒 **No `client_id` in any path.** It is derived from the token. A `client_id` parameter would be an authorization decision waiting to be forgotten.

### 12.2 `GET /portal/today` — the aggregate

📌 **ADR-A09 — one purpose-built aggregate endpoint for the portal landing view**

🔒 M7.3's 60-second rule and NFR-002 (≤2.5s on 4G) cannot be met by four sequential round trips on a high-latency mobile connection.

```
{ "date": "2026-08-04",
  "plan": { "version_id", "content_hash", "day_number",
    "slots": [ { "slot_id", "slot_type", "label", "target_time?",
        "items": [ { "display_name", "quantity_display",   // 🔒 "2 chapati (70 g)"
                     "note?", "alternatives": [...] } ],
        "adherence": { "logged": false, "value": null } } ] },
  "prompts": { "weight_due": true, "assessment_pending_id": null },
  "progress_teaser": { "weight_change_kg": "-1.2", "period_days": 14 },
  "next_appointment?": { "starts_at", "mode", "meeting_link?" },
  "practitioner": { "name", "branding?" } }
```

🔒 **`content_hash` drives service-worker caching** (DDR-12, FR-M7-011). 🔒 **`quantity_display` is server-rendered** — household-measure formatting lives in one place (NFR-072).
🔒 **No nutrition figures by default.** 🟡 Calories and macros are shown only if the practitioner enables it per client — a client fixating on numbers is a clinical concern, and this is the practitioner's call, not ours.

⚠️ **This is a deliberate deviation from strict resource orientation** (Principle 2). Justified: the alternative fails a hard non-functional requirement. 🔒 It is a **read-only projection** — every write still goes to its own resource endpoint, so there is no duplicated write logic.

### 12.3 Adherence logging

**`POST /portal/adherence`** — 🔒 requires `Idempotency-Key` (offline replay safety).

| Field | Type | Notes |
|---|---|---|
| `logged_for_date` | date | 🔒 Within a bounded window (EC-M7-04) |
| `slot_id?` / `slot_type` | | `slot_type` survives plan revision |
| `adherence` | enum | `followed` \| `partial` \| `not_followed` \| `skipped` |
| `client_timestamp` | datetime | 🔒 When the client actually acted |
| `idempotency_key` | string | 🔒 Client-generated (UUID) |

🔒 Both `client_timestamp` and server time are stored (DB §12.1). A log queued Tuesday and synced Thursday is *dated* Tuesday and *recorded* Thursday — collapsing them would corrupt adherence history.
🟡 **PROPOSED backdating window: 7 days.**

### 12.4 Offline sync

**`POST /portal/sync`** — 🔒 FR-M7-012, EC-M7-05.

```
{ "operations": [ { "op_id": "<uuid>", "type": "adherence.log",
                    "payload": {...}, "client_timestamp": "..." } ],
  "known_plan_hash": "sha256:..." }
```

**200** →
```
{ "results": [ { "op_id", "status": "applied|duplicate|rejected", "error?" } ],
  "plan_changed": true, "current_plan_hash": "sha256:..." }
```

🔒 **Guarantees:**
1. 🔒 Per-operation results — **a single bad operation never fails the batch.** One rejected log must not block a week of queued data.
2. 🔒 `duplicate` is a **success** state — replaying a queue is expected, not an error.
3. 🔒 **Server is authoritative for plan content; client logs are never discarded** (EC-M7-05).
4. 🔒 `plan_changed` tells the PWA to refresh rather than silently swapping content while the client reads it (EC-M7-03).

⚠️ 🟡 **PROPOSED batch cap: 100 operations.** A larger queue syncs in pages.

### 12.5 Consent management

**`GET /portal/consent`** → itemised purposes with current state and the notice version granted against.
**`PUT /portal/consent`** → `{ purposes: [{ purpose_code, granted: bool }] }`

🔒 FR-M0-024 — withdrawal must be as easy as granting. 🔒 Essential purposes return **422** if withdrawal is attempted while the relationship is active, with an explanation and the path to ending the engagement (FR-M0-025).
🔒 Every change appends to the consent ledger (DB §16.4).

### 12.6 Portal degradation states

🔒 The portal must fail gracefully and 🔒 **never expose the practitioner's account state** (EC-M7-08):

| Situation | Response |
|---|---|
| No active plan (EC-M7-02) | **200** with `plan: null` and a meaningful empty state |
| Client `paused` (EC-M7-06) | **200**, read-only, `capabilities.can_log: false`, neutral message |
| Tenant suspended (EC-M7-08) | 🔒 **200** with a neutral service message — never a billing reason |
| Magic link expired (EC-M7-01) | **401** `type: "link_expired"` with a self-service re-request path |

🔒 Every portal response carries a `capabilities` object so the UI never has to infer what is permitted:
`{ "can_log_adherence": true, "can_log_measurements": true, "can_upload": true, "can_view_plan": true }`

---

## 13. Idempotency

🔒 EC-M8-06, EC-M10-08, and offline replay (FR-M7-012).

### 13.1 Where required

| Endpoint | Reason |
|---|---|
| `POST /app/plan-versions/{id}/issue` | 🔒 Double-issue would send two plans and supersede incorrectly |
| `POST /app/clients/{id}/ai/plan-drafts` | 🔒 Costs money |
| `POST /portal/adherence` | 🔒 Offline replay |
| `POST /portal/sync` | Per-operation `op_id` |
| `POST /app/uploads/{id}/confirm` | Double quota increment |
| `POST /admin/tenants/{id}/payments` | 🔒 Double-charge (EC-M10-08) |
| `POST /public/forms/{slug}/submit` | 🟡 Optional — server-side dedupe covers it |

### 13.2 Contract

📌 **ADR-A10 — server-stored idempotency records keyed on (tenant, endpoint, key)**

| Situation | Behaviour |
|---|---|
| First request | Execute, store `(key → request fingerprint, response)`, return |
| Replay, same payload | 🔒 Return the **stored response**, do not re-execute |
| Replay, different payload | 🔒 **409 `idempotency_conflict`** — a reused key with new content is a client bug and must be loud |
| Concurrent duplicate | 🔒 **409** while the first is in flight |

🟡 **PROPOSED retention: 24 hours** — long enough for any realistic retry, short enough to keep the table small.
🔒 Keys are client-generated UUIDs. 🔒 The **stored response** is returned, not a fresh computation — a re-execution that produced a different result would defeat the purpose.

---

## 14. Rate Limiting

🔒 NFR-039. 🟡 All values are proposals.

### 14.1 Limits by surface

| Surface | Limit | Key | Why |
|---|---|---|---|
| `/public/auth/login` | 5/min, 20/hr | IP + email | Credential stuffing |
| `/public/auth/register` | 3/hr | IP | Spam tenants |
| `/public/portal/access/request` | 🔒 3/min, 10/hr | identifier + IP | 🔒 Enumeration and WhatsApp cost |
| `/public/forms/*/submit` | 5/min, 20/hr | IP | Spam leads |
| `/public/webhooks/*` | 1000/min | provider | Must not throttle real traffic |
| `/app/*` general | 300/min | session | Runaway client protection |
| `/app/foods/search` | 🔒 120/min | session | 🔒 As-you-type; must be generous |
| `/app/**/ai/plan-drafts` | 🔒 10/min | tenant | 🔒 Cost, above quota |
| `/portal/*` general | 120/min | session | |
| `/portal/uploads/*` | 10/hr | client | Storage abuse |
| `/admin/*` | 300/min | operator | |

### 14.2 Response

**429** with `Retry-After`, plus `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset` on every response.

⚠️ 🔒 **Rate limiting is not entitlement enforcement.** 429 means "too fast, try again"; 402 means "your plan doesn't include more of this." Conflating them would tell a paying customer to wait when they need to upgrade, and vice versa.

⚠️ 🟡 **Implementation is in Postgres**, consistent with ADR-13 (no cache tier). At 200 tenants this is acceptable. 🔒 Documented revisit trigger: if rate-limit writes become a measurable share of database load, move to in-process counters before adding Redis.

---

## 15. Admin API

🔒 `/api/v1/admin/*`. ⚠️ The highest-privilege, cross-tenant surface (M11.3).

### 15.1 Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/admin/tenants` | Search by practitioner email, mobile, name |
| GET | `/admin/tenants/{id}` | 🔒 Account state only |
| GET | `/admin/tenants/{id}/aggregates` | 🔒 **Counts, no client-identifying content** |
| GET | `/admin/tenants/{id}/message-log` | FR-M11-004 |
| POST | `/admin/tenants/{id}/plan` | Assign / extend |
| POST | `/admin/tenants/{id}/suspend` / `/reactivate` | |
| POST | `/admin/tenants/{id}/payments` | 🔒 Manual collection (M10.3) |
| POST | `/admin/users/{id}/password-reset` | 🔒 Trigger only — never sets or sees a password |
| GET | `/admin/health` | Platform health (FR-M11-009) |
| GET | `/admin/jobs` | Queue depth, dead letters |
| GET/POST/PATCH | `/admin/catalogue/foods` | 🔒 FR-M11-010 — curation |
| POST | `/admin/catalogue/foods/{id}/retire` | |
| GET/POST | `/admin/catalogue/portions` | |
| GET/POST | `/admin/catalogue/aliases` | 🔒 Vernacular search (FR-M4-009) |
| GET | `/admin/catalogue/search-misses` | 🔒 FR-M11-011 — curation priority |
| GET/POST | `/admin/message-templates` | Provider approval state |
| GET/POST | `/admin/dietary-rules` | 🔒 §15.4 |

### 15.2 Aggregates only — no clinical content

**`GET /admin/tenants/{id}/aggregates`** → counts of clients by stage, plans issued, appointments, messages sent/failed, storage used, AI generations, last practitioner activity.

🔒 **FR-M11-003 — no client names, no clinical values, no plan contents.** 🔒 Most support cases (AC-M11-002) are resolved from the message log plus these counts, which is precisely why impersonation can be deferred to Phase 2 (FR-M11-012).
🔒 **Every read here is audited** (FR-M0-032) — the only reads in the system that are.
🔒 **No cross-tenant export endpoint exists** (AC-M11-008).

### 15.3 Curation

**`POST /admin/catalogue/foods`** — creates a curated food (`tenant_id: null`).
🔒 Only the operator realm can write catalogue rows (DB §17.1 Pattern B write policy).
⚠️ 🔒 **Corrections do not alter issued plans** (EC-M4-03, EC-M11-03). The response states this explicitly: `{ "affected_future_plans_only": true, "issued_plans_unchanged": 1247 }` — so an operator cannot mistakenly believe they have retroactively fixed a clinical record.

**`GET /admin/catalogue/search-misses`** → aggregated failed queries with counts, ranked. 🔒 The highest-value curation signal we have (M4.2).

### 15.4 Dietary rules — the clinical policy seam

**`POST /admin/dietary-rules`** — 🔒 where unresolved clinical decisions land as data.

| Field | Notes |
|---|---|
| `code`, `rule_type`, `severity` | 🔒 `hard` \| `soft` |
| `trigger_condition` | Profile predicate |
| `effect` | Exclusion / warning / block |
| `applies_to_ai` / `applies_to_manual` | 🔒 Different enforcement per path |
| `message`, `source_citation` | 🔒 Citation required for clinical rules |

🔒 **OD-06 (CKD, eating disorders) becomes one row here** once the clinical decision is made — `rule_type: generation_block`, `applies_to_ai: true`, `applies_to_manual: false`. 🔒 **No code change, no migration, no release.**
🔒 `source_citation` is required for hard clinical rules. A blocking clinical rule without a citation is not defensible.

---

## 16. Versioning Strategy

📌 **ADR-A11 — URL-path major versioning; additive change within a version**

| Option | Verdict |
|---|---|
| Header negotiation | ❌ Invisible in logs and browser tools; harder to route |
| Date-based versions | ❌ Sophisticated, and unnecessary with one first-party consumer |
| **`/api/v1/` in the path** | ✅ **Chosen.** Obvious, routable, cacheable. Adequate until a public API exists (Phase 3) |

### 16.1 What is additive vs breaking

| Additive (no version bump) | Breaking (requires `/v2/`) |
|---|---|
| New endpoint | Removing or renaming a field |
| New optional request field | Making an optional field required |
| New response field | Changing a field's type |
| New enum value in a **response** | 🔒 New enum value in a **request** the client must handle |
| New optional query parameter | Changing status codes or error `type` values |

🔒 **New response enum values are additive; the frontend must handle unknown values gracefully.** This is a contract obligation on the client, stated here so it is designed for rather than discovered.

### 16.2 The generated-client guarantee

🔒 NFR-079 — TypeScript is generated from OpenAPI, and 🔒 **CI fails if the committed client is stale.** A backend contract change that breaks the frontend fails at build time, not in production. This is what makes a split-language stack safe for a solo developer.

### 16.3 Deprecation

🔒 When `/v2/` eventually arrives: `Deprecation` and `Sunset` headers on v1, 🟡 minimum 90 days overlap, and no v1 removal until telemetry shows zero traffic.

---

## 17. OpenAPI Conventions

🔒 The OpenAPI document is generated from the implementation, not hand-written — a hand-maintained spec drifts.

### 17.1 Requirements

| Rule | Reason |
|---|---|
| Every endpoint has a stable `operationId` | 🔒 Determines generated client method names; renaming one is a frontend breaking change |
| Every endpoint declares `tags` matching its module | Navigable docs, module traceability |
| Every schema is a named component | Generated types are named, not inline |
| Every endpoint documents all its error types | 🔒 The frontend must know what to handle |
| Enums are explicit, never open strings | Type safety |
| Examples on every non-trivial schema | Doubles as documentation |
| 🔒 Realm and required action documented per endpoint | Makes the permission surface auditable from the spec |
| `nullable` used precisely | 🔒 Distinguishes "known empty" from "absent" |

### 17.2 Grouping

Tags mirror modules exactly (Arch §3.3): `auth`, `clients`, `leads`, `clinical`, `nutrition-catalogue`, `nutrition-plans`, `ai-drafting`, `appointments`, `messaging`, `progress`, `billing`, `files`, `portal`, `admin`, `public`.

🔒 **One tag per endpoint.** A multi-tagged endpoint is a sign it belongs to two modules — a boundary violation to fix in code, not to paper over in documentation.

### 17.3 Excluded from the public schema

🔒 `/admin/*` is served in a **separate OpenAPI document**, not exposed publicly and not included in the practitioner client bundle. 🔒 Operator endpoint shapes should not be discoverable from a practitioner's browser — the same reasoning as building the operator console as a separate frontend app (Arch §4.1).

---

## 18. Future Public API Considerations

🔒 No public API in V2 (approved). This section records what the current design already preserves, so the option stays cheap.

| Already true | Why it matters later |
|---|---|
| 🔒 Realm-segmented paths | A `/partner/` realm slots in without disturbing existing surfaces |
| 🔒 Declared action per endpoint | Scoped API keys map onto existing actions rather than needing a new model |
| 🔒 Cursor pagination | Stable for third-party iteration |
| 🔒 Stable error `type` values | Third parties can branch on them |
| 🔒 Idempotency infrastructure | Mandatory for any public write API |
| 🔒 Rate-limit headers | Already the standard contract |
| 🔒 No web-only assumptions (ADR-A02) | Native apps and server-to-server both work |

**What a public API would still need:** API key issuance and rotation, per-key scoping and quotas, webhook *delivery* (we only receive today), sandbox environment, versioning commitments with real deprecation cost, and public documentation.

⚠️ 🔒 **The genuine cost of a public API is not building it — it is that breaking changes stop being possible.** That is why it stays a Phase 3 decision, made when a paying customer requires it.

---

## 19. API Decision Records

| ID | Decision | Rationale | Reversibility |
|---|---|---|---|
| **ADR-A01** | Realm as a path segment | Declarative rate limits, audit, firewalling; routing mistakes cannot cross realms | Hard |
| **ADR-A02** | Access token in memory, refresh in HttpOnly cookie | XSS cannot read refresh; native-app compatible | Moderate |
| **ADR-A03** | Bare objects; envelope only on error | Cleanest generated types | Moderate |
| **ADR-A04** | Decimals as strings | 🔒 Float error in clinical values is a defect | Hard |
| **ADR-A05** | Cursor pagination | Stable under concurrent writes | Moderate |
| **ADR-A06** | State transitions as named actions | Explicit, separately authorizable and auditable | Easy |
| **ADR-A07** | Nutrition budget as a first-class response field | 🔒 Makes locked-item constraints observable | Easy |
| **ADR-A08** | AI generation async with polling | Protects web capacity; dropped connections lose nothing | Moderate |
| **ADR-A09** | `/portal/today` aggregate endpoint | 🔒 Only way to meet NFR-002 on 4G | Easy |
| **ADR-A10** | Server-stored idempotency records | Correct replay semantics | Easy |
| **ADR-A11** | URL-path major versioning | Obvious and routable | Hard |

---

## 20. Proposals Requiring Approval

| # | Proposal | § | If rejected |
|---|---|---|---|
| 1 | Access token 15 min | 2.1 | Choose another lifetime |
| 2 | Password ≥10 chars, common-password check, no composition rules | 2.2 | Specify a policy |
| 3 | AI poll interval 2s, client ceiling 90s | 9.2 | Choose values |
| 4 | `style_source` options (`my_templates` \| `curated_only`) | 9.2 | Simplify to one behaviour |
| 5 | Recalculation strategies (`redistribute_portions` \| `suggest_additions` \| `report_only`) | 8.5 | Reduce scope |
| 6 | 🔒 `redistribute_portions` adjusts quantities only, never adds/removes foods | 8.5 | Confirm — food selection stays clinical |
| 7 | Upload allowlist and size caps (10 MB / 2 MB) | 10.1 | Adjust |
| 8 | Signed-URL lifetime 5 min | 10.2 | Adjust |
| 9 | Adherence backdating window 7 days | 12.3 | Adjust |
| 10 | Sync batch cap 100 operations | 12.4 | Adjust |
| 11 | 🔒 Nutrition figures hidden from clients unless practitioner enables | 12.2 | Confirm — clinically motivated |
| 12 | Idempotency record retention 24h | 13.2 | Adjust |
| 13 | All rate-limit values | 14.1 | Adjust |
| 14 | Rate limiting in Postgres, not Redis | 14.2 | Accept a cache tier |
| 15 | No sparse fieldsets (`?fields=`) at MVP | 6.4 | Add the mechanism |
| 16 | `include_total=true` opt-in rather than always counting | 6.1 | Always return totals |
| 17 | Deprecation overlap minimum 90 days | 16.3 | Adjust |
| 18 | Admin OpenAPI served separately | 17.3 | Single document |

---

## 21. Open Items Carried Forward

| Item | Blocks | Notes |
|---|---|---|
| ⚠️ 🔒 OD-08 Indian BMI thresholds | `bmi_band` stays `null` | 🔒 Contract exists; **API must not return an uncitable threshold** |
| ⚠️ 🔒 OD-13 energy equation | `estimated_energy_kcal` stays `null` | Same |
| ⚠️ 🔒 OD-06 CKD / eating-disorder policy | Nothing — becomes a `dietary_rules` row | §9.5, §15.4 seams ready |
| ⚠️ OD-07 required assessment sections | Definition v1 content | No API change |
| ⚠️ OD-03 katori reference values | Catalogue data | No API change |
| ⚠️ OD-01 lifecycle stages | `client_stage` enum values | 🔒 Request-enum change = breaking (§16.1) |
| ⚠️ ASM-09 GST invoice fields | Invoice response shape | Accountant |
| ⚠️ ASM-10 DPDP consent purposes | `/portal/consent` purpose list | Privacy lawyer |

🔒 **Four of the six clinical open items require no API change** — the contracts carry the fields and the seams already exist. That is the intended consequence of DDR-08 (typed projection) and `dietary_rules`-as-data.

---

**END OF DOCUMENT**

*Phase 5 of 11 complete. Awaiting review before Phase 6 — Implementation Planning.*
