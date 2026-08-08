# S2 — Client Spine: slice plan

Derived from the PRD (M1, M2), DB §5–6, API §7.1–7.2 and §11, and the
implementation plan's S2 section. Ordered by hard dependency.

## What S2 is

"A practitioner can sign up and manage their whole client base." The first
genuinely usable release — it replaces the spreadsheet.

**Modules:** `clients`, `leads` — the first domain modules in the codebase.
Everything before this was `kernel` and `platform`.

## The three constraints that shape the slicing

1. **🔒 M1.3 — leads and clients are ONE entity** distinguished by lifecycle
   stage (DB §5.1: "the single most important modelling decision in the
   schema"). There is no separate leads table. `leads` owns only the *enquiry*
   tables; the record an enquiry creates is a `clients` row at stage `lead`.

2. **🔒 R3 — modules must not import each other, at all.** `leads` needs to
   match a submission against existing clients (EC-M2-02) and `leads` has
   "Readers: none". So the seam is a **kernel port** — DB §5 states the pattern
   explicitly ("Readers: via kernel ports"), and it is the same shape as
   `CredentialStore` (S1-B) and `StorageBackend` (S1-F).

3. **🔒 DDR-06 — the timeline is materialised by event subscribers.** The S1
   event bus already exists and its docstring names DDR-06 as the motivating
   case, so no new infrastructure is needed — but the timeline slice must come
   *after* the events it records exist, or its subscriber has nothing to hear.

## Slices

| # | Slice | Delivers | Depends on |
|---|---|---|---|
| A | **Client spine** ✅ | `clients` + `client_stage_history`, enums, search index, kernel client port, contact/stage rules, create/read/update | S1 (all) |
| B | **Lifecycle** ✅ | Stage transitions as named actions (ADR-A06), entitlement on `→ active` (FR-M1-002), archive/restore, first Practitioner screens | A, S1-E |
| C | **Collaboration** | Notes, tags, assignments, practitioner scoping (AC-M1-006) | A |
| D | **Timeline** | `timeline_events`, DDR-06 subscriber, timeline read (NFR-006) | A, B, C |
| E | **Discovery** | List + search endpoints, filters, sort, cursor pagination (NFR-005) | A, C |
| F | **Lead capture** | `enquiry_forms`/`enquiry_submissions`, public endpoints, captcha, rate limit, silent duplicate matching, consent, needs-response view, acknowledgement (logged only) | A, D, S1-D, S1-F |
| G | **Practitioner UI** | Client list, timeline, notes, tags; click budgets NFR-011/012 | A–F |
| H | **Public form UI** | Standalone mobile-first enquiry form with consent capture | F |

### Decisions taken in Slice B

- 🔒 **Archiving does not change `stage`.** `archived_at` is the archive flag and
  `stage` records lifecycle position independently, which is what DB §5.2's
  two-clause counting predicate assumes and what lets a restore return a client
  to what they were (EC-M1-02). The consequence is that `ClientStage.ARCHIVED`
  is unreachable: refused by `assert_transition_allowed`, absent from the API's
  `to_stage` union, and rejected at the table by migration 0010. The enum value
  stays — OD-01 has not settled the vocabulary and dropping a PostgreSQL enum
  value means rewriting the type.
- 🔒 **The transition graph is permissive** — any stage to any other, except a
  no-op and except `archived`. The PRD constrains no ordering and FR-M1-017 puts
  rule-driven transitions in Phase 3, so a graph invented now would be wrong the
  first time a real funnel disagreed with it.
- 🔒 **No `If-Match` on the lifecycle actions**, unlike PATCH. A precondition
  prevents a lost update; a concurrent stage change loses nothing, since both
  transitions are recorded and the final stage is one somebody asked for. The
  race that does matter — two activations passing one entitlement check — is
  handled by a row lock plus a per-tenant advisory lock.
- ⚠️ **`POST /clients` at `stage=active` is now metered**, closing a gap Slice A
  left open: the limit could be walked past by creating clients already active
  rather than converting them.


### Why this order

- **A before everything** — every other slice writes to or reads `clients`.
- **B before D** — the timeline's first real event is a stage change; building
  the subscriber before there is anything to subscribe to would mean testing it
  against a fabricated event.
- **C before D** — notes are a timeline event type too, so D can cover both
  sources at once rather than being revisited.
- **D before F** — an enquiry must land on the timeline (AC-M1-004, J1), so the
  subscriber must exist before lead capture writes through it.
- **E after C** — the list response embeds tags and owner (API §7.1), so
  filtering by tag requires the tag tables.
- **G/H last** — the generated API client is a CI gate ("API client freshness"),
  so the UI is built against a settled contract rather than one that moves under
  it.

## Deferred within S2, by the plan's own scoping

- FR-M1-012 custom fields, FR-M1-013 bulk import/export, FR-M1-024 merge —
  Phase 2.
- FR-M2-012 custom form questions, FR-M2-013 kanban, FR-M2-014 sequences —
  Phase 2. `enquiry_forms.fields jsonb` ships as the column, unused.
- **Transports.** FR-M2-006/007 (notify practitioner, acknowledge prospect) are
  logged only until S5 — the S1 notifications port has no adapters, by design.

## ⚠️ Carried risk

The implementation plan says the Supabase pooler must be **confirmed in
transaction mode** before S2 (DB §2.3, plan line 387): "if it is session-mode
with reuse, stop and resolve before S2". That cannot be verified without a
deployed environment, which does not exist. Flagged at the end of S1 and again
here — proceeding on the user's explicit instruction to start S2. If the pooler
turns out to be session-mode, `SET LOCAL` tenant isolation fails and every RLS
policy in the codebase becomes decorative; the remedy is a pooler config change,
not a code change, so building on is not wasted work.
