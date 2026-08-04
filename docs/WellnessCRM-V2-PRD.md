# WellnessCRM V2 — Product Requirements Document

**Status:** Draft v0.1 — awaiting review
**Owner:** Founder / CTO
**Date:** 2026-08-04
**Phase:** 2 of 11 (Product Requirements)
**Supersedes:** WellnessCRM V1 (research prototype, never launched)

---

## Document Control

### Purpose of this document

This PRD is the **single source of truth** for WellnessCRM V2. It defines *what* we are building and *why*. It deliberately does not define *how*.

| This document DOES define | This document does NOT define |
|---|---|
| Personas and their jobs-to-be-done | Database schema (Phase 4) |
| Functional requirements per module | API contracts (Phase 5) |
| Acceptance criteria | Screen layouts or components (Phase 6) |
| Edge cases and failure behaviour | Technology choices (Phase 3) |
| MVP / Phase 2 / Phase 3 scope boundaries | Sprint sequencing (Phase 8) |
| Non-functional requirements | Any code whatsoever (Phase 9) |

### How to read the identifiers

| Prefix | Meaning | Example |
|---|---|---|
| `M{n}` | Module | `M4` — Nutrition Engine |
| `US-M{n}-{nn}` | User story | `US-M4-03` |
| `FR-M{n}-{nnn}` | Functional requirement | `FR-M4-012` |
| `AC-M{n}-{nnn}` | Acceptance criterion | `AC-M4-012` |
| `EC-M{n}-{nn}` | Edge case | `EC-M4-05` |
| `NFR-{nnn}` | Non-functional requirement | `NFR-014` |
| `ASM-{nn}` | Assumption requiring validation | `ASM-07` |
| `OD-{nn}` | Open decision awaiting the founder | `OD-03` |

**Requirement language.** *MUST* = mandatory for the stated scope. *SHOULD* = strongly expected; deviation requires a recorded reason. *MAY* = optional.

### Marking conventions

> 🟡 **PROPOSED** — content I have drafted from documented practice or reasonable inference. It requires validation by a practising professional before implementation. Do not treat as authoritative.

> ⚠️ **RISK** — a known hazard with commercial, legal or delivery impact.

> 🔒 **BINDING** — a constraint that derives from a Phase 1 decision or a V1 failure. Changing it requires an explicit decision, not a code review.

### Change log

| Version | Date | Change |
|---|---|---|
| v0.1 | 2026-08-04 | Initial draft following Phase 1 approval |

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Product Vision & Strategy](#2-product-vision--strategy)
3. [Market & Competitive Context](#3-market--competitive-context)
4. [Personas](#4-personas)
5. [The Core Value Loop](#5-the-core-value-loop)
6. [Scope Framework](#6-scope-framework)
7. [Module Map](#7-module-map)
8. [Modules](#8-modules) — M0 … M11
9. [Nutrition Assessment — PROPOSED](#9-nutrition-assessment--proposed-for-review)
10. [Non-Functional Requirements](#10-non-functional-requirements)
11. [Success Metrics](#11-success-metrics)
12. [Out of Scope](#12-out-of-scope)
13. [Assumptions Register](#13-assumptions-register)
14. [Open Decisions](#14-open-decisions)

---

## 1. Executive Summary

WellnessCRM V2 is practice-management software for **Indian clinical dietitians, nutritionists and health coaches** working one-to-one with clients.

The practitioner we serve manages 20–150 clients using WhatsApp, Google Sheets, Google Forms and diet charts typed in Word or Excel. Their business works, but it consumes hours every week in manual, repetitive labour: rebuilding similar diet plans from scratch, chasing clients for weights and adherence, and retyping intake answers collected over chat.

**We are not selling better software than Practice Better. We are selling back four hours a week.**

The product delivers a single continuous loop — capture a lead, convert them, manage them as a client, build a nutrition plan, follow up consistently, retain them — with three deliberate bets:

1. **A curated Indian food database** with household measures (katori, roti, cup) is the defensible asset. Generic nutrition data does not describe how Indians actually eat, and this is where every foreign competitor fails.
2. **AI drafts plans in the practitioner's own style**, grounded in their templates and food choices, under mandatory human review. It removes the labour without removing the professional.
3. **WhatsApp is the delivery layer, not an integration.** In India, if a plan does not arrive on WhatsApp, it does not arrive.

**Constraints that shape everything:** one developer, under ₹5,000/month fixed infrastructure, first paying customer in 4–6 months, 50–200 practitioners at 18 months, India-only at launch under the DPDP Act 2023.

---

## 2. Product Vision & Strategy

### 2.1 Vision statement

> Every independent nutrition practitioner in India should be able to run a professional, personal, evidence-based practice without an assistant — and without losing their evenings to paperwork.

### 2.2 Strategic position

| Dimension | Our position |
|---|---|
| **Category** | Practice management for 1:1 nutrition and health coaching |
| **Wedge** | Diet plan creation and delivery — the largest recurring time sink |
| **Moat (12 months)** | Curated Indian food and portion data + the practitioner's own accumulated templates |
| **Moat (36 months)** | Switching cost of client history, plans and outcome data |
| **Distribution** | Practitioner community, referral, and the client-facing surface of the free tier |
| **Price posture** | Below the psychological threshold of one client's monthly fee |

### 2.3 What we are deliberately not building

🔒 **BINDING** — these were decided in Phase 1 and are not open in V2.

| Not building | Why |
|---|---|
| A fitness/workout engine (Trainerize) | Different buyer, different data model, loses focus |
| A sales/marketing automation suite (HubSpot) | Our buyer has 5–15 enquiries a month, not a pipeline problem |
| Insurance claims / superbills | Irrelevant in the Indian cash-pay market |
| Embedded video calling | Practitioners already use Zoom, Meet or WhatsApp calls |
| In-app chat | WhatsApp is where the client relationship lives; a second inbox splits it |
| Native mobile apps | PWA covers the need until product-market fit is proven |
| Ayurveda-specific clinical models | Genuinely different domain, not a renamed SOAP note |
| Group programs and cohorts | 1:1 is the proven wedge; groups are a Phase 3 expansion |

### 2.4 Strategic risks

| Risk | Severity | Mitigation |
|---|---|---|
| Free WhatsApp+Sheets is "good enough" | **High** | Time-saving must be immediate and obvious in the first session, not after setup |
| Practitioners distrust AI-generated clinical content | **High** | AI never sends. Practitioner reviews every plan. Framed as a drafting assistant |
| Food database quality is judged in the first 5 minutes | **High** | Seed quality over quantity; cover the foods that appear in real plans first |
| Meta Business Verification delays or rejects | **Medium** | Begin verification in week 1, independent of engineering |
| Solo developer capacity | **High** | Ruthless MVP scope; every module independently shippable |
| Client-side engagement fails (clients ignore the PWA) | **Medium** | WhatsApp drives clients into the PWA; PWA is never the only channel |

---

## 3. Market & Competitive Context

### 3.1 The real competitive set

🔒 **BINDING** — our competitor is the practitioner's current manual workflow, not a software incumbent.

| Competitor | Nature | Why they choose it | How we win |
|---|---|---|---|
| **WhatsApp + Google Sheets** | Free, universal, already working | Zero cost, zero learning curve, client already there | We do not replace WhatsApp — we automate what they type into it |
| **Word / Excel diet charts** | Manual, per-client | Total control over format | Templates + food database produce a better chart in a fraction of the time |
| **Google Forms** for intake | Free | Familiar | Structured assessment that flows directly into the plan, not a dead spreadsheet |
| **Practice Better / Healthie** | Foreign SaaS | Feature depth | ₹5,000–8,500/mo, no Indian food data, no WhatsApp, no UPI — they do not compete here |
| **Local practice tools** | Regional SaaS | Price | Depth of nutrition workflow and AI drafting |

### 3.2 Why foreign incumbents do not threaten the India launch

| Barrier | Detail |
|---|---|
| **Price** | ₹5,000–8,500/mo vs a market that pays ₹800–3,500 |
| **Food data** | "Grilled chicken with quinoa" for a client who eats idli, sambhar and 2 rotis |
| **Portions** | Grams and ounces, not katori, roti, glass |
| **Channel** | Email-first in a WhatsApp-first market |
| **Payments** | No UPI, no Indian gateway support for practitioner collections |
| **Compliance** | HIPAA-shaped, not DPDP-shaped |

⚠️ **RISK** — this window is not permanent. Assume 24–36 months before a well-funded local or adapted foreign competitor arrives. The moat must be the food data and accumulated practitioner content, not the feature list.

---

## 4. Personas

### 4.1 P1 — Priya, Clinical Dietitian *(PRIMARY — design for her)*

| | |
|---|---|
| **Role** | Independent clinical dietitian, 6 years experience, MSc Dietetics, RD |
| **Scale** | 45 active clients, 8–12 new enquiries/month |
| **Pricing** | ₹2,500–4,000/month per client package |
| **Revenue** | ₹1.1–1.4 lakh/month |
| **Today's stack** | WhatsApp Business, Google Sheets client tracker, Word diet charts → PDF, Google Pay/UPI, Google Calendar |
| **Devices** | Windows laptop for plan building; Android phone for everything else |
| **Tech comfort** | Confident with consumer apps; not technical; will not read documentation |

**Jobs to be done**
1. Turn an Instagram enquiry into a paying client without losing them to slow follow-up.
2. Produce a personalised, professional diet plan in minutes, not an hour.
3. Know which clients are drifting *before* they stop paying.
4. Look organised and credible to a client who is comparing her to a hospital dietitian.
5. Stop retyping the same information in three places.

**Pain points (ranked)**
1. Diet chart creation — the single largest time sink. Rebuilds similar plans repeatedly.
2. Chasing clients for weight, measurements and adherence.
3. Intake data arrives as unstructured WhatsApp messages and must be retyped.
4. No visibility of which clients are disengaged until they cancel.
5. Client history scattered across chats, sheets and files.

**What makes her switch** — she can produce her *next* diet plan faster than her current method, on day one, without a migration project.

**What makes her churn** — the food database does not contain the foods she uses; the plan output looks less professional than her Word template; or she has to do double entry.

---

### 4.2 P2 — Rahul, Health & Lifestyle Coach *(SECONDARY)*

| | |
|---|---|
| **Role** | Certified health/lifestyle coach, no clinical qualification |
| **Scale** | 30 active clients, some corporate wellness cohorts |
| **Pricing** | ₹1,500–3,000/month |
| **Focus** | Habit change, sleep, stress, general nutrition guidance — *not* clinical prescription |

**Jobs to be done**
1. Keep clients accountable between sessions.
2. Show visible progress to justify renewal.
3. Deliver structured programs without clinical depth he is not qualified for.

**Design implication** 🔒 — clinical depth (biochemical markers, medical history, lab documents) **MUST be optional**, not a required step. Forcing Rahul through a clinical assessment he cannot interpret will cause abandonment. The assessment must degrade gracefully to a lighter form.

---

### 4.3 P3 — Anjali, End Client *(USER, NOT BUYER)*

| | |
|---|---|
| **Role** | 34, marketing manager, wants to lose 8kg, mild PCOS |
| **Device** | Android mid-range phone, 4G, data-conscious |
| **Habits** | Lives in WhatsApp. Will not install an app for a 3-month engagement. Will not check email |

**Jobs to be done**
1. Know what to eat today, without hunting through chat history.
2. Log weight and adherence in under 30 seconds.
3. See that she is progressing.
4. Reach her dietitian easily when something changes.

**Design implications** 🔒
- WhatsApp is the notification channel. The PWA is the destination, never the entry point.
- Every client interaction must be achievable in **under 60 seconds on a phone**.
- The PWA must be installable but fully usable **without** installing.
- Assume slow, intermittent 4G and limited data allowance.

---

### 4.4 P4 — Dr. Meera, Clinic Owner *(FUTURE BUYER — architecture only)*

Owns a 4-practitioner nutrition clinic. **Not a launch persona.** Included because multi-tenancy and role separation must accommodate her without rearchitecting.

**Requirements she implies:** multiple practitioners under one account; clients assigned to a practitioner; owner sees all clients, practitioner sees only their own; consolidated billing.

🔒 The **data model and authorization model MUST support these from day one.** The *user interface* for clinic management is Phase 3.

---

### 4.5 P5 — Platform Support Operator *(INTERNAL)*

Initially the founder. Needs to diagnose a practitioner's problem without asking for their password, and without unrestricted access to clinical data.

⚠️ **RISK** — this is the highest-privilege, cross-tenant surface in the product. See M11.

---

## 5. The Core Value Loop

🔒 **BINDING** — the MVP MUST support all six steps end to end. Scope cuts remove *depth*, never *reach*. A practitioner must be able to traverse the entire loop on day one, even if each step is thin.

```
   ┌──────────┐    ┌──────────┐    ┌──────────┐
   │ CAPTURE  │──▶ │ CONVERT  │──▶ │  MANAGE  │
   │  a lead  │    │ to client│    │  client  │
   └──────────┘    └──────────┘    └────┬─────┘
        ▲                                │
        │                                ▼
   ┌────┴─────┐    ┌──────────┐    ┌──────────┐
   │  RETAIN  │◀── │ FOLLOW UP│◀── │   PLAN   │
   │  client  │    │consistently   │ nutrition│
   └──────────┘    └──────────┘    └──────────┘
```

### 5.1 Journey J1 — Enquiry to first plan delivered

**Actor:** Priya (P1) · **Trigger:** enquiry via Instagram DM · **Target:** ≤ 48 hours, ≤ 25 minutes of practitioner effort

| # | Step | Module | Success condition |
|---|---|---|---|
| 1 | Prospect submits the public enquiry form | M2 | Record created at stage `Lead`, practitioner notified |
| 2 | Priya reviews and marks contacted | M1 | Stage → `Contacted` in ≤ 2 clicks |
| 3 | Priya books a consultation | M6 | Appointment created; WhatsApp confirmation sent to prospect |
| 4 | Prospect completes the nutrition assessment | M3 | Structured data captured before the consultation |
| 5 | Consultation occurs; Priya records notes and measurements | M3 | Recorded during or immediately after the call |
| 6 | Priya converts to client | M1 | Stage → `Active`; entitlement counter increments |
| 7 | Priya generates a plan draft from a template | M4, M5 | Draft ready in < 60 seconds |
| 8 | Priya reviews, edits and approves the plan | M4, M5 | No plan reaches a client unreviewed |
| 9 | Plan delivered via WhatsApp | M8 | Client receives a link and/or PDF |
| 10 | Client opens the plan in the PWA | M7 | Viewed event recorded |

### 5.2 Journey J2 — The weekly follow-up cycle

**Actor:** Priya + Anjali · **Trigger:** scheduled · **Target:** near-zero practitioner effort

| # | Step | Module | Success condition |
|---|---|---|---|
| 1 | Scheduled check-in fires | M8 | WhatsApp nudge sent on the configured day |
| 2 | Anjali logs weight and adherence | M7 | Completed in < 60 seconds on mobile |
| 3 | Progress updates | M9 | Visible to both parties |
| 4 | Priya sees the week's check-ins in one view | M9 | One screen, no per-client navigation |
| 5 | Non-responders surface automatically | M9 | At-risk list, no manual tracking |
| 6 | Priya adjusts the plan or messages the client | M4, M8 | Adjustment reuses the existing plan, does not rebuild it |

### 5.3 Journey J3 — Client's daily experience

**Actor:** Anjali (P3) · **Target:** under 60 seconds, mobile, poor network

| # | Step | Module | Success condition |
|---|---|---|---|
| 1 | Receives WhatsApp message with plan link | M8 | Deep link opens directly to the correct content |
| 2 | Views today's meals | M7 | Loads on 4G; readable without zoom; works offline once cached |
| 3 | Logs adherence | M7 | Single tap per meal |
| 4 | Logs weight when prompted | M7 | Two taps and a number |
| 5 | Sees progress | M9 | Simple, encouraging, honest |

---

## 6. Scope Framework

### 6.1 Definitions

| Tier | Meaning | Test |
|---|---|---|
| **MVP** | Required for the first paying customer | Without it, the six-step loop breaks or the product cannot be sold |
| **Phase 2** | Post-launch, demand-driven, 0–9 months after launch | Improves depth or removes friction, but the loop works without it |
| **Phase 3** | Scale, expansion or new segments, 9+ months | Requires more customers, a team, or a new market |

### 6.2 MVP scope test

A feature is in the MVP **only if** it satisfies at least one:
1. The six-step loop cannot complete without it.
2. It is legally required at launch (DPDP consent, audit).
3. It is structurally expensive to retrofit (tenancy, authorization, entitlement metering, consent ledger).
4. It is the wedge itself (nutrition engine, WhatsApp delivery).

Everything else is Phase 2 or later, regardless of how attractive it is.

---

## 7. Module Map

| ID | Module | MVP | Rationale |
|---|---|---|---|
| **M0** | Platform Foundation | ✅ Full | Structural; cannot be retrofitted |
| **M1** | Client Record (Core CRM) | ✅ Full | The spine of the product |
| **M2** | Lead Capture & Conversion | ✅ Thin | Loop step 1–2; thin = form + stages |
| **M3** | Clinical Workspace | ✅ Core | Assessment, measurements, notes, documents |
| **M4** | Nutrition Engine | ✅ Full | **The wedge** |
| **M5** | AI Plan Drafting | ✅ Core | **The differentiator** |
| **M6** | Appointments | ✅ Thin | Manual booking + reminders only |
| **M7** | Client PWA | ✅ Core | Engagement loop |
| **M8** | Messaging & Scheduling Engine | ✅ Full | WhatsApp is the delivery layer |
| **M9** | Progress & Retention | ✅ Thin | At-risk view + charts |
| **M10** | Subscription & Entitlements | ✅ Partial | Enforcement yes, automated collection no |
| **M11** | Super Admin & Support | ✅ Minimal | Support without a support team |

### 7.1 Module dependency order

```
M0 Foundation
 └─ M1 Client Record
     ├─ M2 Capture & Convert
     ├─ M3 Clinical Workspace
     │   └─ M4 Nutrition Engine
     │       └─ M5 AI Plan Drafting
     ├─ M6 Appointments
     └─ M8 Messaging Engine
         └─ M7 Client PWA
             └─ M9 Progress & Retention
M10 Entitlements (cross-cutting, from M0)
M11 Super Admin (after M1)
```

---

## 8. Modules

---

## M0 — Platform Foundation

### M0.1 Purpose

The shared substrate every other module depends on: identity, tenancy, authorization, consent, audit, file storage, notification abstraction and entitlement metering.

### M0.2 Business value

Invisible to users; determines whether the product survives. 🔒 V1 failed here specifically — *"authentication, routing and permissions became difficult to maintain"* and *"business logic was duplicated across multiple screens."* M0 exists to make those failures structurally impossible rather than merely discouraged.

### M0.3 User stories

| ID | Story |
|---|---|
| US-M0-01 | As a practitioner, I want to sign up and reach a usable workspace in under 3 minutes, so I can evaluate the product immediately. |
| US-M0-02 | As a practitioner, I want certainty that no other practitioner can ever see my clients, so I can trust the platform with confidential health information. |
| US-M0-03 | As a client, I want to control what data is collected and to withdraw consent, so I retain control of my health information. |
| US-M0-04 | As a clinic owner, I want practitioners to see only their assigned clients, so client confidentiality is preserved within my clinic. |
| US-M0-05 | As the platform operator, I want an immutable record of who accessed which client record, so I can investigate any incident. |

### M0.4 Functional requirements

**Identity & authentication**

| ID | Requirement | Scope |
|---|---|---|
| FR-M0-001 | The system MUST support practitioner registration via email + password. | MVP |
| FR-M0-002 | The system MUST verify email ownership before granting full access. | MVP |
| FR-M0-003 | The system MUST support password reset via a time-limited single-use link. | MVP |
| FR-M0-004 | The system MUST maintain **three separate identity realms**: practitioner, end client, platform operator. A credential in one realm MUST NOT authenticate into another. | MVP |
| FR-M0-005 | End clients MUST be able to access their portal without creating a password, via a time-limited magic link delivered over WhatsApp or SMS. | MVP |
| FR-M0-006 | Sessions MUST expire after a defined period of inactivity, configurable per realm. | MVP |
| FR-M0-007 | The system MUST support Google sign-in for practitioners. | Phase 2 |
| FR-M0-008 | The system MUST support two-factor authentication for practitioners. | Phase 2 |
| FR-M0-009 | Platform operator accounts MUST require two-factor authentication. | MVP |

> **Rationale for FR-M0-005** — Anjali (P3) will not create and remember a password for a 3-month engagement. Password-based client login is a primary cause of portal abandonment. The magic link must be re-requestable by the client without practitioner involvement.

**Tenancy**

| ID | Requirement | Scope |
|---|---|---|
| FR-M0-010 | Every business-domain record MUST belong to exactly one tenant. | MVP |
| FR-M0-011 | Tenant isolation MUST be enforced **below the application layer**, so that application code omitting a tenant filter cannot leak data across tenants. | MVP |
| FR-M0-012 | A tenant MUST support multiple practitioner users with distinct roles. | MVP |
| FR-M0-013 | Records MUST carry a region attribute enabling future region-specific handling without redesign. | MVP |
| FR-M0-014 | The system MUST NOT hardcode region-specific compliance rules into business logic; regional rules MUST be expressed as policy configuration. | MVP |

**Authorization**

| ID | Requirement | Scope |
|---|---|---|
| FR-M0-015 | 🔒 There MUST be exactly **one** authorization decision point in the system. No module may implement its own permission logic. | MVP |
| FR-M0-016 | Roles at MVP: `Owner`, `Practitioner`, `Client`, `PlatformOperator`. | MVP |
| FR-M0-017 | `Owner` MUST access all clients within their tenant. `Practitioner` MUST access only clients assigned to them, unless granted broader access by the Owner. | MVP |
| FR-M0-018 | `Client` MUST access only their own records, and only those explicitly shared with them. | MVP |
| FR-M0-019 | Authorization decisions MUST be denied by default. | MVP |
| FR-M0-020 | Custom roles with granular permissions. | Phase 3 |

**Consent & data protection (DPDP Act 2023)**

| ID | Requirement | Scope |
|---|---|---|
| FR-M0-021 | The system MUST present a consent notice to every end client stating what data is collected, by whom, for what purposes, and how to withdraw. | MVP |
| FR-M0-022 | Consent MUST be captured **per purpose**, itemised, not as a single blanket acceptance. | MVP |
| FR-M0-023 | Every consent grant, modification and withdrawal MUST be recorded in an **append-only consent ledger** with timestamp, purpose, version of the notice, and method of capture. | MVP |
| FR-M0-024 | Clients MUST be able to withdraw consent for any purpose at any time, as easily as it was given. | MVP |
| FR-M0-025 | Withdrawal of consent MUST halt the associated processing without deleting records the practitioner is independently entitled to retain. | MVP |
| FR-M0-026 | The system MUST support a client's request for access to their data, and produce it in a readable form. | MVP |
| FR-M0-027 | The system MUST support correction and erasure requests, with erasure honouring lawful retention limits. | MVP |
| FR-M0-028 | 🟡 **PROPOSED** — where a client is under 18, the system MUST capture verifiable parental/guardian consent and MUST NOT enable behavioural tracking or targeted communication for that client. | MVP |
| FR-M0-029 | Consent notices MUST be versioned; re-consent MUST be requested when a notice materially changes. | MVP |
| FR-M0-030 | Consent notice available in English and major Indian regional languages. | Phase 2 |

> ⚠️ **RISK — FR-M0-028.** Nutritionists routinely see teenage clients. DPDP requires verifiable parental consent for data principals under 18, which is stricter than most Western regimes. Verification method needs legal advice — see OD-05. This is a launch blocker if unresolved.

**Audit**

| ID | Requirement | Scope |
|---|---|---|
| FR-M0-031 | The system MUST record an immutable audit entry for every create, update and delete of a client record or clinical data. | MVP |
| FR-M0-032 | The system MUST record an audit entry for every **read** of clinical data by a platform operator. | MVP |
| FR-M0-033 | Audit entries MUST capture actor, action, target, timestamp and originating context. | MVP |
| FR-M0-034 | Audit entries MUST NOT be editable or deletable through any application pathway. | MVP |
| FR-M0-035 | Audit entries MUST NOT contain the clinical values themselves, only references to the affected record. | MVP |
| FR-M0-036 | Practitioner-visible audit trail per client record. | Phase 2 |

**File storage**

| ID | Requirement | Scope |
|---|---|---|
| FR-M0-037 | The system MUST store uploaded documents (lab reports, images, generated PDFs) with tenant-scoped access control. | MVP |
| FR-M0-038 | File access MUST require authorization on every retrieval; unguessable URLs alone are NOT sufficient. | MVP |
| FR-M0-039 | Files MUST be scanned or constrained by type and size on upload. | MVP |
| FR-M0-040 | Per-tenant storage quotas MUST be enforced as an entitlement. | MVP |

**Notification abstraction**

| ID | Requirement | Scope |
|---|---|---|
| FR-M0-041 | 🔒 The system MUST provide **one** notification abstraction supporting interchangeable transports (WhatsApp, SMS, email, web push). Modules MUST NOT integrate a transport directly. | MVP |
| FR-M0-042 | Every outbound message MUST be recorded with transport, template, recipient, status and failure reason. | MVP |
| FR-M0-043 | The system MUST support transport fallback (e.g. WhatsApp → SMS) per message type. | Phase 2 |

**Entitlements & metering**

| ID | Requirement | Scope |
|---|---|---|
| FR-M0-044 | The system MUST track per-tenant usage of metered resources: active clients, AI generations, WhatsApp messages, storage. | MVP |
| FR-M0-045 | The system MUST enforce plan limits at the point of action, with a clear message stating the limit and the upgrade path. | MVP |
| FR-M0-046 | Enforcement MUST fail safe: if entitlement state cannot be determined, existing data MUST remain readable and only *new* metered actions blocked. | MVP |

### M0.5 Acceptance criteria

| ID | Criterion |
|---|---|
| AC-M0-001 | A newly registered practitioner reaches a usable workspace within 3 minutes, without contacting support. |
| AC-M0-002 | An attempt to access another tenant's client record fails, and the failure is recorded. |
| AC-M0-003 | With tenant filtering deliberately removed from an application query in a test environment, no cross-tenant data is returned. |
| AC-M0-004 | A client can withdraw consent for one purpose while retaining others, and the withdrawal appears in the consent ledger within one second. |
| AC-M0-005 | An audit entry exists for every clinical record mutation performed during a full run of Journey J1. |
| AC-M0-006 | A practitioner at their active-client limit sees a message naming the limit and the upgrade path, and existing clients remain fully accessible. |
| AC-M0-007 | A client magic link expires after its validity window and cannot be reused after consumption. |
| AC-M0-008 | A practitioner credential cannot authenticate against the client portal or the operator console. |

### M0.6 Edge cases

| ID | Case | Required behaviour |
|---|---|---|
| EC-M0-01 | Client forwards their magic link to another person | Link is single-use and time-limited; consumption is audited |
| EC-M0-02 | Practitioner deletes their account with active clients | Account closure requires explicit acknowledgement; client data retained per policy, then purged on schedule; clients notified |
| EC-M0-03 | Client withdraws all consent mid-engagement | Processing halts; practitioner notified; plans and messaging stop; record becomes read-only |
| EC-M0-04 | Two practitioners in a clinic are assigned the same client | Supported: one owning practitioner, additional explicit grants; all access audited |
| EC-M0-05 | Entitlement service unavailable | Read access preserved; new metered actions blocked; incident logged |
| EC-M0-06 | Client is a minor turning 18 mid-engagement | 🟡 **PROPOSED** — system prompts for direct consent from the client at the transition; parental consent record retained |
| EC-M0-07 | Same person is a client of two different practitioners on the platform | Two independent records in two tenants; no linkage; no cross-tenant visibility |
| EC-M0-08 | Practitioner changes email to one already registered | Rejected with a non-enumerating error message |

### M0.7 Scope summary

| MVP | Phase 2 | Phase 3 |
|---|---|---|
| Email auth, 3 identity realms, client magic links, tenancy with data-layer isolation, 4 roles, single authorization point, DPDP consent ledger, audit log, file storage with quotas, notification abstraction, entitlement metering | Google sign-in, practitioner 2FA, transport fallback, practitioner-visible audit trail, regional language notices | Custom roles, SSO/SAML, region-pinned data residency, granular permission editor |

---

## M1 — Client Record (Core CRM)

### M1.1 Purpose

The single record representing a person the practitioner works with, from first enquiry through active engagement to churn. Every other module attaches to it.

### M1.2 Business value

🔒 **This is the spine of the product and the primary switching cost.** A practitioner with two years of client history in WellnessCRM will not migrate away.

### M1.3 Design decision — one record, not two

🔒 **BINDING** — **Leads and clients are the same entity, distinguished by a lifecycle stage.**

V1's lesson (*"business logic duplicated across multiple screens"*) and standard CRM failure modes both point the same way. Separate lead and client entities produce: duplicated contact fields, a fragile conversion step that copies data, "which record is real" defects, permanently double-counted metrics, and two sets of search, tagging and messaging logic.

**Consequence:** conversion is a status change, not a data migration. Nothing is copied, nothing is lost, history is continuous from first enquiry.

### M1.4 Lifecycle stages

🟡 **PROPOSED — requires practitioner validation**

| Stage | Meaning | Counts toward plan limit? |
|---|---|---|
| `Lead` | Enquiry received, not yet engaged | ❌ No |
| `Contacted` | Practitioner has responded | ❌ No |
| `Consultation Scheduled` | Appointment booked, not yet a paying client | ❌ No |
| `Active` | Engaged, receiving plans and follow-up | ✅ **Yes** |
| `Paused` | Temporarily inactive; history retained; no new plans or messages | ❌ No |
| `Churned` | Engagement ended | ❌ No |
| `Archived` | Hidden from all working views | ❌ No |

> **OD-01 — validate with practitioners.** Are these the stages Priya actually thinks in? Is `Contacted` a meaningful state or noise? Do practitioners distinguish "lost lead" from "churned client"? Getting this wrong makes the pipeline view useless.

### M1.5 The "active client" definition

🔒 This is a **billing boundary** and MUST be predictable without the practitioner thinking about it.

**DEFINITION:** *A client counts toward the plan limit if, and only if, their lifecycle stage is `Active`.*

**Why stage-based and not activity-based.** An earlier proposal defined active as "any client with a session, message or plan update in the trailing 30 days." That is rejected: it fluctuates without the practitioner acting, cannot be predicted, and produces surprise limit errors mid-workflow. Stage-based counting is fully under practitioner control, explainable in one sentence, and visible on screen.

**Anti-gaming.** A practitioner could mark everyone `Paused` and keep working with them. This is self-defeating by design: `Paused` clients cannot receive new plans, messages or scheduled check-ins (FR-M1-014). The limit is enforced by capability, not by policing.

| ID | Requirement | Scope |
|---|---|---|
| FR-M1-001 | The system MUST display the current active-client count and plan limit persistently in the practitioner interface. | MVP |
| FR-M1-002 | Moving a client to `Active` when at the limit MUST be blocked with a message naming the limit and the upgrade path. | MVP |
| FR-M1-003 | Leads MUST NOT count toward the limit at any stage prior to `Active`. | MVP |

> **Rationale for FR-M1-003** — free lead capture encourages practitioners to put their entire funnel into the product, which increases switching cost and makes the pipeline genuinely useful. Charging for leads would push them back to WhatsApp.

### M1.6 User stories

| ID | Story |
|---|---|
| US-M1-01 | As Priya, I want one place showing everyone I am working with and their status, so I stop maintaining a spreadsheet. |
| US-M1-02 | As Priya, I want to find any client in seconds by name or phone, so I can respond while on a call. |
| US-M1-03 | As Priya, I want a client's complete history on one screen, so I do not search WhatsApp for what we discussed. |
| US-M1-04 | As Priya, I want to convert an enquiry into a client without re-entering their details. |
| US-M1-05 | As Priya, I want to record notes against a client quickly, so I capture context before I forget it. |
| US-M1-06 | As Priya, I want to group clients by my own labels, so I can work the way I think. |
| US-M1-07 | As Dr. Meera, I want each client assigned to a practitioner, so my team sees only their own caseload. |

### M1.7 Functional requirements

**Record management**

| ID | Requirement | Scope |
|---|---|---|
| FR-M1-004 | The system MUST allow creating a client record with a minimum of name plus one contact method. | MVP |
| FR-M1-005 | The system MUST capture: full name, mobile number, email, date of birth, sex, city, preferred language, source, and lifecycle stage. | MVP |
| FR-M1-006 | Mobile number MUST be stored in international format and MUST be validated for Indian numbers at MVP. | MVP |
| FR-M1-007 | The system MUST support free-text notes with timestamp and author, appended chronologically. | MVP |
| FR-M1-008 | The system MUST support practitioner-defined tags. | MVP |
| FR-M1-009 | The system MUST assign each client to an owning practitioner. | MVP |
| FR-M1-010 | The system MUST support soft deletion (archive) but MUST NOT support hard deletion through the normal interface. | MVP |
| FR-M1-011 | Hard deletion MUST be available only through a DPDP erasure request pathway (FR-M0-027). | MVP |
| FR-M1-012 | Practitioner-defined custom fields. | Phase 2 |
| FR-M1-013 | Bulk import from CSV/Google Sheets, and bulk export. | Phase 2 |

**Stage management**

| ID | Requirement | Scope |
|---|---|---|
| FR-M1-014 | Clients in `Paused`, `Churned` or `Archived` MUST NOT receive new plans, scheduled messages or check-in nudges. | MVP |
| FR-M1-015 | Every stage transition MUST be recorded with timestamp and actor. | MVP |
| FR-M1-016 | Stage changes MUST be achievable in ≤ 2 interactions from the client list and the client detail view. | MVP |
| FR-M1-017 | Automated stage transitions based on rules. | Phase 3 |

**Timeline**

| ID | Requirement | Scope |
|---|---|---|
| FR-M1-018 | Each client MUST have a unified reverse-chronological timeline aggregating: stage changes, appointments, assessments, measurements, plans issued, messages sent, notes, documents and client-side activity. | MVP |
| FR-M1-019 | The timeline MUST be filterable by event type. | MVP |
| FR-M1-020 | The timeline MUST load its most recent 20 events within the performance budget and page thereafter. | MVP |

**Search & list**

| ID | Requirement | Scope |
|---|---|---|
| FR-M1-021 | The system MUST provide search across name, mobile and email, returning results as the practitioner types. | MVP |
| FR-M1-022 | The client list MUST be filterable by stage, tag and owning practitioner, and sortable by name, recent activity and creation date. | MVP |
| FR-M1-023 | The system MUST provide a saved default view per practitioner. | Phase 2 |
| FR-M1-024 | Duplicate detection on mobile number at creation time, with a merge option. | Phase 2 |

### M1.8 Acceptance criteria

| ID | Criterion |
|---|---|
| AC-M1-001 | A client record is created with name and mobile only, in ≤ 3 interactions from anywhere in the practitioner interface. |
| AC-M1-002 | Searching a partial name or the last 4 digits of a mobile returns the matching client within the performance budget. |
| AC-M1-003 | Converting a `Lead` to `Active` retains the original record, its identifier, and all prior history including the enquiry. |
| AC-M1-004 | The client timeline shows every event from Journey J1 in correct chronological order. |
| AC-M1-005 | A `Paused` client is excluded from the active-client count and receives no scheduled messages. |
| AC-M1-006 | In a two-practitioner tenant, a non-owner practitioner cannot see or search clients assigned to the other. |
| AC-M1-007 | Archiving a client removes them from all default views without deleting any data. |

### M1.9 Edge cases

| ID | Case | Required behaviour |
|---|---|---|
| EC-M1-01 | Two family members share one mobile number | Allowed; duplicate warning shown, not enforced; both records independently addressable |
| EC-M1-02 | Client returns after churning | Reactivate the original record; history continuous; MUST NOT create a second record |
| EC-M1-03 | Client changes mobile number | Number updated; prior number retained in history; messaging switches to the new number |
| EC-M1-04 | Practitioner leaves a clinic | Owner reassigns their clients in bulk; assignment history retained |
| EC-M1-05 | Client requests erasure while a plan is in progress | Erasure pathway invoked; practitioner notified; retention-limited records preserved per policy |
| EC-M1-06 | Practitioner exceeds their limit after a downgrade | Existing clients remain fully accessible and editable; no new client may enter `Active` until under the limit |
| EC-M1-07 | Lead submits the enquiry form twice | Duplicate detected on mobile; second submission appended to the existing record, not duplicated |
| EC-M1-08 | Client has no mobile, only email | Permitted; WhatsApp delivery unavailable; system falls back to email and states the limitation |

### M1.10 Scope summary

| MVP | Phase 2 | Phase 3 |
|---|---|---|
| Unified client record, 7 lifecycle stages, notes, tags, practitioner assignment, unified timeline, search, filtering, archive | Custom fields, bulk import/export, duplicate merge, saved views | Rule-based automated stage transitions, relationship linking (family accounts) |

---

## M2 — Lead Capture & Conversion

### M2.1 Purpose

Get enquiries into the system without manual typing, and move them toward becoming clients.

### M2.2 Business value

Priya receives 8–12 enquiries a month across Instagram DMs, WhatsApp and referrals. She loses some to slow or forgotten follow-up. Each lost lead is ₹2,500–4,000/month of recurring revenue. **Recovering two per month pays for the subscription many times over** — this is the clearest ROI story in the product.

### M2.3 Scope discipline

🔒 MVP is **capture and convert**, not pipeline management. A full pipeline board with automation solves a problem this persona does not yet have. Because leads and clients share one record (M1.3), pipeline depth can be added later as a *view* at low cost.

### M2.4 User stories

| ID | Story |
|---|---|
| US-M2-01 | As Priya, I want a link I can put in my Instagram bio that collects enquiries directly, so I stop copying details out of DMs. |
| US-M2-02 | As Priya, I want to be notified immediately when an enquiry arrives, so I respond while their interest is high. |
| US-M2-03 | As Priya, I want to see all enquiries needing a response in one place, so none are forgotten. |
| US-M2-04 | As Priya, I want to know which channel produces enquiries, so I focus my effort. |
| US-M2-05 | As a prospect, I want to submit my enquiry in under a minute on my phone. |

### M2.5 Functional requirements

| ID | Requirement | Scope |
|---|---|---|
| FR-M2-001 | Each tenant MUST have a publicly accessible enquiry form at a stable, shareable URL. | MVP |
| FR-M2-002 | The form MUST be mobile-first and usable without an account. | MVP |
| FR-M2-003 | The form MUST collect at minimum: name, mobile, and primary goal. | MVP |
| FR-M2-004 | The form MUST present a DPDP consent notice and capture explicit consent before submission (FR-M0-021). | MVP |
| FR-M2-005 | Submission MUST create a client record at stage `Lead`. | MVP |
| FR-M2-006 | The practitioner MUST be notified of a new enquiry via their configured channel within 60 seconds. | MVP |
| FR-M2-007 | The prospect MUST receive an immediate acknowledgement. | MVP |
| FR-M2-008 | The form MUST be protected against automated submission. | MVP |
| FR-M2-009 | The system MUST record a lead source, either selected by the prospect or derived from a link parameter. | MVP |
| FR-M2-010 | The practitioner MUST be able to add a lead manually in ≤ 3 interactions. | MVP |
| FR-M2-011 | The system MUST provide a view of leads awaiting response, ordered by age. | MVP |
| FR-M2-012 | Practitioners MUST be able to customise the enquiry form's questions. | Phase 2 |
| FR-M2-013 | Kanban pipeline board with drag-and-drop stages. | Phase 2 |
| FR-M2-014 | Automated follow-up sequences for unresponsive leads. | Phase 2 |
| FR-M2-015 | Lead scoring, and WhatsApp click-to-chat lead capture. | Phase 3 |

### M2.6 Acceptance criteria

| ID | Criterion |
|---|---|
| AC-M2-001 | A prospect completes the enquiry form on a mid-range Android phone over 4G in under 60 seconds. |
| AC-M2-002 | The lead appears in the practitioner's list, and the practitioner is notified, within 60 seconds of submission. |
| AC-M2-003 | The prospect receives an acknowledgement without practitioner action. |
| AC-M2-004 | Consent is recorded in the consent ledger with the notice version at the moment of submission. |
| AC-M2-005 | Leads awaiting response are visible in a single view with their age. |
| AC-M2-006 | A lead converted to `Active` requires no re-entry of any previously captured field. |

### M2.7 Edge cases

| ID | Case | Required behaviour |
|---|---|---|
| EC-M2-01 | Form submitted with an invalid mobile | Rejected inline with a clear message before submission |
| EC-M2-02 | Existing client resubmits the enquiry form | Matched on mobile; submission appended to the existing record; no duplicate; practitioner notified |
| EC-M2-03 | Automated/spam submissions | Blocked before record creation; not counted in metrics |
| EC-M2-04 | Prospect declines consent | Submission not accepted; explanation shown; no record created |
| EC-M2-05 | Practitioner's WhatsApp notification fails | Falls back to another transport; failure visible in the message log |
| EC-M2-06 | Lead arrives while practitioner is at their client limit | Lead is accepted and stored — leads are unmetered; limit applies only at conversion to `Active` |
| EC-M2-07 | Tenant is suspended for non-payment | Public form disabled with a neutral message; no data loss |

### M2.8 Scope summary

| MVP | Phase 2 | Phase 3 |
|---|---|---|
| Public enquiry form, consent capture, instant notification, prospect acknowledgement, spam protection, source tracking, needs-response view, manual lead entry | Customisable form questions, Kanban pipeline, automated follow-up sequences, multiple forms per tenant | Lead scoring, WhatsApp click-to-chat capture, referral tracking, landing pages |

---

## M3 — Clinical Workspace

### M3.1 Purpose

Capture and organise everything the practitioner knows about a client's health, diet and progress: the nutrition assessment, measurements, consultation notes and documents.

### M3.2 Business value

Two effects. **Time:** structured intake removes retyping from WhatsApp messages. **Credibility:** a structured assessment is what separates a professional practice from an informal one, and it is what justifies premium fees to the client.

🔒 The assessment output is the direct input to the nutrition plan and the AI drafting prompt. Quality here determines quality everywhere downstream.

### M3.3 User stories

| ID | Story |
|---|---|
| US-M3-01 | As Priya, I want clients to complete a structured assessment before our first consultation, so I arrive prepared instead of spending the session on data collection. |
| US-M3-02 | As Priya, I want the assessment to capture how Indians actually eat, so it reflects real practice. |
| US-M3-03 | As Priya, I want to record weight and measurements over time and see the trend. |
| US-M3-04 | As Priya, I want consultation notes attached to the client, so I recall what we agreed. |
| US-M3-05 | As Priya, I want to store lab reports against the client, so everything is in one place. |
| US-M3-06 | As Rahul, I want to skip clinical sections I am not qualified to interpret, without the product feeling broken. |
| US-M3-07 | As Anjali, I want to complete the assessment on my phone in manageable pieces, so I do not abandon it. |

### M3.4 Functional requirements

**Assessment**

| ID | Requirement | Scope |
|---|---|---|
| FR-M3-001 | The system MUST provide a structured nutrition assessment (structure defined in §9, marked PROPOSED). | MVP |
| FR-M3-002 | 🔒 The assessment MUST be defined as **versioned data**, not as fixed application logic, so its structure can change without a code change or data migration. | MVP |
| FR-M3-003 | Completed assessments MUST remain readable under the structure version in force when they were captured. | MVP |
| FR-M3-004 | The assessment MUST be completable by the client via a link, or by the practitioner on the client's behalf. | MVP |
| FR-M3-005 | Progress MUST be saved continuously; a client MUST be able to leave and resume without data loss. | MVP |
| FR-M3-006 | Clinical sections MUST be optional and skippable, individually and as a group. | MVP |
| FR-M3-007 | The assessment MUST be re-administrable, with each administration retained separately for comparison. | MVP |
| FR-M3-008 | The practitioner MUST be able to view a completed assessment as a single readable summary. | MVP |
| FR-M3-009 | Practitioners MAY add their own custom questions to the standard assessment. | Phase 2 |
| FR-M3-010 | Practitioners MAY create entirely custom assessment forms. | Phase 3 |

> **OD-02** — whether practitioners can customise the assessment at MVP. Recommendation: **no**. A fixed structure keeps AI grounding reliable and the food/plan mapping coherent; customisation is the single largest complexity multiplier in this module.

**Measurements**

| ID | Requirement | Scope |
|---|---|---|
| FR-M3-011 | The system MUST record dated measurements: weight, height, waist, hip, and body fat percentage. | MVP |
| FR-M3-012 | The system MUST derive BMI and waist-hip ratio rather than storing them as entered values. | MVP |
| FR-M3-013 | Measurements MUST be recordable by both practitioner and client. | MVP |
| FR-M3-014 | The system MUST display measurement history as a trend over time. | MVP |
| FR-M3-015 | The system MUST use metric units, with height accepted in either centimetres or feet/inches and normalised on entry. | MVP |
| FR-M3-016 | Additional circumference sites and progress photographs. | Phase 2 |
| FR-M3-017 | Bioimpedance device import. | Phase 3 |

> 🟡 **PROPOSED — FR-M3-012.** Indian populations have different BMI risk thresholds than Western populations; Indian guidelines commonly use lower cut-offs for overweight and obesity. The exact thresholds we display require practitioner and clinical-source validation before implementation. See ASM-04.

**Consultation notes**

| ID | Requirement | Scope |
|---|---|---|
| FR-M3-018 | The system MUST support dated consultation notes linked to a client and, where applicable, an appointment. | MVP |
| FR-M3-019 | Notes MUST support a free-text mode and 🟡 a **PROPOSED** lightweight structured mode. | MVP |
| FR-M3-020 | Notes MUST be editable by their author, with edits recorded in the audit log. | MVP |
| FR-M3-021 | Notes MUST NOT be visible to the client unless explicitly shared. | MVP |
| FR-M3-022 | Note templates. | Phase 2 |
| FR-M3-023 | Voice-to-text note capture. | Phase 3 |

**Documents**

| ID | Requirement | Scope |
|---|---|---|
| FR-M3-024 | The system MUST allow upload of documents against a client, with a type label and date. | MVP |
| FR-M3-025 | Clients MUST be able to upload documents from the client portal. | MVP |
| FR-M3-026 | Documents MUST be viewable in-browser without download where the format allows. | MVP |
| FR-M3-027 | Uploads MUST be constrained by file type and size, and MUST count against the tenant storage quota. | MVP |
| FR-M3-028 | Automated extraction of values from lab reports. | Phase 3 |

### M3.5 Acceptance criteria

| ID | Criterion |
|---|---|
| AC-M3-001 | A client completes the full assessment on a mid-range Android phone over 4G, in multiple sessions, without data loss. |
| AC-M3-002 | A coach persona completes an assessment while skipping every clinical section, and the result remains usable for planning. |
| AC-M3-003 | The assessment structure is modified and existing completed assessments remain readable and correctly rendered under their original version. |
| AC-M3-004 | Weight recorded on three dates renders as a trend, and derived BMI matches an independent calculation. |
| AC-M3-005 | A client uploads a lab report from their phone and the practitioner views it without downloading. |
| AC-M3-006 | Consultation notes are invisible to the client in every client-facing surface. |
| AC-M3-007 | A repeat assessment is stored separately, and both administrations are viewable side by side. |

### M3.6 Edge cases

| ID | Case | Required behaviour |
|---|---|---|
| EC-M3-01 | Client abandons the assessment halfway | Partial responses retained; practitioner sees completion status; resume link re-sendable |
| EC-M3-02 | Client gives a medically implausible value (e.g. 400 kg) | Soft warning with confirmation; value accepted if confirmed; flagged for practitioner review |
| EC-M3-03 | Assessment structure changes while a client is mid-completion | Client completes under the version they started; new version applies to subsequent administrations |
| EC-M3-04 | Client uploads an unsupported or oversized file | Rejected with a clear explanation of limits before upload begins |
| EC-M3-05 | Practitioner and client record conflicting weights on the same date | Both retained with source attribution; practitioner value takes display precedence |
| EC-M3-06 | Client declines consent for clinical data | Clinical sections suppressed entirely; non-clinical workflow continues |
| EC-M3-07 | Tenant exceeds storage quota | New uploads blocked with a clear message; existing documents remain accessible |
| EC-M3-08 | Client reports a condition requiring medical referral | 🟡 **PROPOSED** — assessment surfaces a non-diagnostic prompt advising practitioner review; the system MUST NOT diagnose or triage |

### M3.7 Scope summary

| MVP | Phase 2 | Phase 3 |
|---|---|---|
| Versioned structured assessment, client and practitioner completion, resumable, optional clinical sections, repeat administration, measurements with derived metrics and trends, consultation notes, document upload | Practitioner custom questions, note templates, progress photos, extra measurement sites | Fully custom assessment forms, lab report extraction, voice notes, device import |

---

## M4 — Nutrition Engine ⭐

### M4.1 Purpose

The food data, portion logic and plan construction tools that turn a practitioner's clinical judgement into a delivered diet plan.

### M4.2 Business value

🔒 **This is the wedge and the moat.**

Priya spends 30–60 minutes building a diet plan in Word. She does this 8–15 times a month, plus revisions. That is **6–12 hours monthly on formatting and retyping** rather than on clinical thinking.

Two distinct assets accumulate here:

| Asset | Why defensible |
|---|---|
| **Curated Indian food data with household measures** | Expensive and slow to build correctly. Foreign competitors do not have it and cannot easily acquire it. Improves with every practitioner who uses it. |
| **The practitioner's own templates and meal library** | Grows with usage. Directly increases switching cost. After six months, leaving means rebuilding their entire library. |

⚠️ **RISK — first-five-minutes judgement.** A practitioner will search for 3–5 foods they use daily. If those are missing, or the portions are wrong, they conclude the product does not understand Indian food and never return. Seed coverage of *commonly used* foods matters far more than total food count.

### M4.3 The food data hierarchy

🟡 **PROPOSED — requires practitioner validation**

| Level | Definition | Example | Who creates it |
|---|---|---|---|
| **Food** | A single ingredient or item with nutritional values per standard quantity | Chapati (whole wheat), Toor dal (cooked), Paneer | Platform (curated) + practitioner (custom) |
| **Portion** | A household measure mapped to a weight for a given food | 1 katori = 150 g cooked dal; 1 chapati = 35 g | Platform (curated) |
| **Recipe** | A named combination of foods with quantities, yielding a dish with derived nutrition | Palak paneer, Poha | Platform (seed) + practitioner |
| **Meal** | A reusable named set of foods/recipes with portions | "Standard South Indian breakfast" | Practitioner |
| **Plan Template** | A reusable full-day or multi-day structure | "PCOS — vegetarian — 1400 kcal" | Practitioner |
| **Diet Plan** | A template instantiated and personalised for one client | Anjali's plan, week of 12 Aug | Practitioner |

🔒 **Curated and custom entries MUST share one structure with an ownership marker, not exist as parallel duplicate types.** Custom foods MUST be tenant-private and MUST NOT leak across tenants.

### M4.4 Food database seed requirements

🟡 **PROPOSED — the composition of the seed set requires practitioner validation (OD-03)**

| Category | Target coverage | Notes |
|---|---|---|
| Cereals & grains | Wheat, rice varieties, millets (bajra, jowar, ragi), semolina, poha, oats | Millets are clinically significant in Indian practice |
| Pulses & legumes | All common dals, chana, rajma, sprouts | Raw vs cooked values MUST be distinguished |
| Vegetables | Common and regional, seasonal | |
| Fruits | Common and regional, seasonal | |
| Dairy | Milk (by fat percentage), curd, paneer, buttermilk, ghee, cheese | |
| Non-vegetarian | Chicken, egg, fish varieties, mutton | Must be suppressible for vegetarian clients |
| Fats & oils | Regional oils, ghee, butter, nuts, seeds | |
| Prepared dishes | Idli, dosa, upma, poha, paratha, khichdi, sambhar, common sabzis | **Highest-value category** — practitioners plan in dishes, not ingredients |
| Packaged products | Common Indian brands with label values | Requires a maintenance strategy — see ASM-06 |
| Beverages | Tea, coffee (with sugar/milk variants), juices, aerated drinks | Indian tea/coffee sugar habits are a routine intervention point |

**Base source:** IFCT 2017 (Indian Food Composition Tables, NIN Hyderabad) as the authoritative reference for composition values.

⚠️ **RISK — packaged product data decays.** Brands reformulate and relabel. A stale value is worse than an absent one for a practitioner making clinical decisions. Needs a stated freshness policy (ASM-06).

### M4.5 Household measures

🔒 **This is where foreign products fail and where we must be precise.**

| ID | Requirement | Scope |
|---|---|---|
| FR-M4-001 | Every curated food MUST support at least one household measure in addition to weight. | MVP |
| FR-M4-002 | Supported measures MUST include at minimum: katori, cup, glass, tablespoon, teaspoon, piece, number, and slice. | MVP |
| FR-M4-003 | Each household measure MUST map to a specific gram weight **per food**, not globally. | MVP |
| FR-M4-004 | Portion conversion logic MUST exist in exactly **one** place in the system. | MVP |
| FR-M4-005 | Practitioners MUST be able to express quantities in household measures throughout plan building, never being forced into grams. | MVP |
| FR-M4-006 | The system MUST display both the household measure and its gram equivalent where space allows. | MVP |
| FR-M4-007 | Practitioner-defined custom household measures. | Phase 2 |

> 🟡 **PROPOSED — measure standardisation.** "1 katori" varies by household. The industry practice is a standardised reference (commonly ~150 ml for a medium katori) with the gram weight varying by food density. **The exact reference values require practitioner validation** — this is precisely the kind of detail that determines whether Priya trusts the product. See OD-03.

### M4.6 User stories

| ID | Story |
|---|---|
| US-M4-01 | As Priya, I want to build a diet plan from a template I have already made, so I am not starting from a blank page. |
| US-M4-02 | As Priya, I want to search Indian foods and add them in katoris and rotis, not grams. |
| US-M4-03 | As Priya, I want to save a plan I am proud of as a template, so my library compounds. |
| US-M4-04 | As Priya, I want to add foods that are missing, so a gap never blocks me mid-plan. |
| US-M4-05 | As Priya, I want to see the nutritional totals of a plan against my target, so I know it is clinically sound. |
| US-M4-06 | As Priya, I want to swap one food for an alternative quickly, so I can personalise without recalculating everything. |
| US-M4-07 | As Priya, I want the delivered plan to look more professional than my Word document. |
| US-M4-08 | As Priya, I want to revise an existing plan rather than rebuild it, so weekly adjustments take minutes. |

### M4.7 Functional requirements

**Food search & management**

| ID | Requirement | Scope |
|---|---|---|
| FR-M4-008 | The system MUST provide food search returning results as the practitioner types, within the performance budget. | MVP |
| FR-M4-009 | Search MUST match common regional and vernacular names for the same food. | MVP |
| FR-M4-010 | Foods MUST carry: energy, protein, carbohydrate, fat, and fibre per standard quantity. | MVP |
| FR-M4-011 | Foods MUST carry category, tags, meal-suitability and dietary classification (vegetarian / eggetarian / non-vegetarian / vegan / Jain). | MVP |
| FR-M4-012 | Practitioners MUST be able to create a custom food with the same fields, in ≤ 5 interactions, **without leaving the plan they are building**. | MVP |
| FR-M4-013 | Custom foods MUST be private to the creating tenant. | MVP |
| FR-M4-014 | The system MUST record which foods practitioners search for and fail to find. | MVP |
| FR-M4-015 | Micronutrient values (iron, calcium, vitamin D, B12, sodium, potassium). | Phase 2 |
| FR-M4-016 | Glycaemic index and glycaemic load. | Phase 2 |
| FR-M4-017 | A pathway to promote a frequently created custom food into the curated set. | Phase 2 |

> **Rationale for FR-M4-014** — the list of failed food searches is the highest-value product signal we will have. It tells us exactly what to add to the curated set, ranked by real demand.
> **Rationale for FR-M4-012** — if adding a missing food requires leaving the plan builder, the practitioner loses their work-in-progress and their patience. This must be inline.

**Recipes & meals**

| ID | Requirement | Scope |
|---|---|---|
| FR-M4-018 | The system MUST allow a reusable meal to be composed of foods with portions. | MVP |
| FR-M4-019 | Meals MUST derive their nutritional totals from their constituent foods. | MVP |
| FR-M4-020 | Practitioners MUST be able to save any meal built inside a plan as a reusable meal. | MVP |
| FR-M4-021 | The system MUST ship with a seed set of common Indian recipes. | MVP |
| FR-M4-022 | Practitioner-authored recipes with a yield and per-serving derivation. | Phase 2 |
| FR-M4-023 | Cooking method adjustments to nutritional values. | Phase 3 |

**Plan building**

| ID | Requirement | Scope |
|---|---|---|
| FR-M4-024 | The system MUST support a diet plan structured as named meal slots across one or more days. | MVP |
| FR-M4-025 | 🟡 **PROPOSED** default meal slots: Early Morning, Breakfast, Mid-Morning, Lunch, Evening Snack, Dinner, Bedtime. Practitioners MUST be able to add, rename, remove and reorder slots. | MVP |
| FR-M4-026 | The system MUST support single-day and 7-day plan structures. | MVP |
| FR-M4-027 | The system MUST display running nutritional totals per meal and per day as the plan is built. | MVP |
| FR-M4-028 | The system MUST allow a nutritional target to be set per client and show progress against it. | MVP |
| FR-M4-029 | The system MUST support free-text guidance alongside structured food items, at plan and meal level. | MVP |
| FR-M4-030 | The system MUST support alternatives/substitutions for a food item within a meal. | MVP |
| FR-M4-031 | The system MUST allow a plan to be saved as a template. | MVP |
| FR-M4-032 | The system MUST allow a new plan to be created from a template, from a previous plan for the same client, or from blank. | MVP |
| FR-M4-033 | Plans MUST be versioned; issuing a revision MUST NOT destroy the previously issued version. | MVP |
| FR-M4-034 | The system MUST support a plan validity period. | MVP |
| FR-M4-035 | Plan-level dietary filtering MUST exclude non-conforming foods from search (e.g. no non-vegetarian foods for a vegetarian client). | MVP |
| FR-M4-036 | Multi-week plan cycling and rotation. | Phase 2 |
| FR-M4-037 | Automatic portion optimisation to hit a macro target. | Phase 3 |

> **Rationale for FR-M4-033** — weekly plan adjustment is the core of the follow-up loop (Journey J2). If revising a plan overwrites history, the practitioner loses the ability to explain what changed and why, and the client loses their record of what they were previously asked to do.

**Plan output & delivery**

| ID | Requirement | Scope |
|---|---|---|
| FR-M4-038 | The system MUST generate a client-facing plan document that is clean, professional and printable. | MVP |
| FR-M4-039 | The document MUST carry the practitioner's name and, where provided, their logo and clinic name. | MVP |
| FR-M4-040 | The document MUST be deliverable as a link and as a downloadable file. | MVP |
| FR-M4-041 | Plans MUST also be viewable natively in the client portal (M7). | MVP |
| FR-M4-042 | Practitioner-configurable document templates and branding. | Phase 2 |
| FR-M4-043 | Regional language plan output. | Phase 2 |

> ⚠️ **RISK — FR-M4-038 is a switching blocker.** Priya has refined her Word template over years and is proud of it. If our output looks worse than hers, no amount of time saved will convince her. This requires design attention disproportionate to its apparent simplicity, and validation with real practitioners before launch.

### M4.8 Acceptance criteria

| ID | Criterion |
|---|---|
| AC-M4-001 | A practitioner builds a complete 1-day, 6-slot plan from a template in **under 5 minutes**. |
| AC-M4-002 | A practitioner builds the same plan from blank in **under 15 minutes**. |
| AC-M4-003 | Food search returns results within the performance budget for a database at full seed size. |
| AC-M4-004 | A practitioner adds a missing custom food without losing the plan in progress, and immediately uses it. |
| AC-M4-005 | Quantities are enterable in katori, roti, cup, piece and spoon for every applicable curated food. |
| AC-M4-006 | Daily nutritional totals match an independent manual calculation from the same food values. |
| AC-M4-007 | A plan is saved as a template, and a new client plan created from it in ≤ 3 interactions. |
| AC-M4-008 | A plan revision is issued while the prior version remains retrievable and clearly dated. |
| AC-M4-009 | For a client marked vegetarian, no non-vegetarian food appears in plan-building search results. |
| AC-M4-010 | The generated plan document is judged **equal to or better than** their current Word template by at least 3 practitioners in review. |

### M4.9 Edge cases

| ID | Case | Required behaviour |
|---|---|---|
| EC-M4-01 | Practitioner needs a food that does not exist, mid-plan | Inline creation without losing plan state (FR-M4-012) |
| EC-M4-02 | Custom food is created with implausible values | Soft warning with confirmation; flagged for review; accepted if confirmed |
| EC-M4-03 | Curated food values are corrected by the platform after plans referencing it were issued | Issued plans retain the values in force at issue; new plans use corrected values; correction recorded |
| EC-M4-04 | Practitioner deletes a custom food used in issued plans | Deletion blocked, or food retired while remaining resolvable for historical plans |
| EC-M4-05 | Plan totals fall far outside the client's target | Non-blocking warning; the practitioner's clinical judgement always prevails |
| EC-M4-06 | Practitioner builds a plan for a client whose dietary preference changes later | Existing plan unaffected; filter applies to subsequent editing; conflict surfaced |
| EC-M4-07 | Two practitioners in a clinic edit the same plan concurrently | Last write must not silently discard the other's work; conflict detected and surfaced |
| EC-M4-08 | Plan document generation fails | Practitioner notified with a retry; plan data never lost; failure logged |
| EC-M4-09 | Client is on a Jain diet with multiple exclusions | Dietary classification supports exclusion sets; filtering respects all applicable exclusions |
| EC-M4-10 | Practitioner exceeds a reasonable template count | No hard limit at MVP; monitored; treated as an entitlement candidate later |

### M4.10 Scope summary

| MVP | Phase 2 | Phase 3 |
|---|---|---|
| Curated Indian food database with household measures, vernacular search, custom foods (inline), reusable meals, seed recipes, plan builder with slots, running totals, targets, alternatives, dietary filtering, templates, versioning, professional document output, failed-search logging | Micronutrients, GI/GL, practitioner recipes, custom measures, branding templates, regional language output, multi-week cycling, custom-food promotion | Portion auto-optimisation, cooking-method adjustment, food database API, community-shared templates |

---

## M5 — AI Plan Drafting

### M5.1 Purpose

Produce a credible first-draft diet plan from the client's assessment and the practitioner's own templates, which the practitioner then reviews, edits and approves.

### M5.2 Business value

The single largest time saving in the product, and the clearest differentiator against both the manual workflow and foreign incumbents.

🔒 **The framing is decisive:** this is *"AI that drafts plans in your style,"* not *"AI that replaces your judgement."* Practitioners are professionally and legally accountable for what they prescribe. A product that appears to take that away will be rejected by exactly the practitioners we most want.

### M5.3 Safety model

🔒 **BINDING — non-negotiable.**

| Rule | Requirement |
|---|---|
| **Human approval** | No AI-generated plan may reach a client without explicit practitioner approval. There MUST be no auto-send pathway, and no setting that creates one. |
| **Draft status** | Generated plans MUST exist in an explicit `Draft` state, visually distinct from approved plans. |
| **Attribution** | The practitioner MUST be able to see that a plan originated as an AI draft, and what they changed. |
| **No diagnosis** | The system MUST NOT generate diagnostic conclusions, medical advice, or medication/supplement prescriptions. |
| **Grounding** | Generation MUST be constrained to foods that exist in the food database. The system MUST NOT invent foods or nutritional values. |
| **Escalation** | 🟡 **PROPOSED** — where the assessment indicates a condition requiring medical supervision, the draft MUST carry a prominent advisory for practitioner attention. |

> ⚠️ **RISK — clinical harm.** An unreviewed generated plan reaching a client with chronic kidney disease, type 1 diabetes or a serious eating disorder could cause real harm and existential liability. The review gate is the primary control and MUST NOT be weakened for convenience, at any tier, for any customer.

### M5.4 User stories

| ID | Story |
|---|---|
| US-M5-01 | As Priya, I want a draft plan generated from the client's assessment, so I start from something rather than nothing. |
| US-M5-02 | As Priya, I want drafts to use the foods and structures I normally use, so it sounds like me. |
| US-M5-03 | As Priya, I want to edit anything in the draft before it goes out, so I remain in control. |
| US-M5-04 | As Priya, I want to understand why the draft made its choices, so I can trust or correct it. |
| US-M5-05 | As Priya, I want generation to be fast enough to use during a consultation. |

### M5.5 Functional requirements

| ID | Requirement | Scope |
|---|---|---|
| FR-M5-001 | The system MUST generate a draft plan using the client's assessment, measurements, dietary preferences, stated goal and nutritional target. | MVP |
| FR-M5-002 | Generation MUST be grounded in the food database and the practitioner's own templates and saved meals. | MVP |
| FR-M5-003 | Generated plans MUST enter the `Draft` state and MUST require explicit approval before delivery. | MVP |
| FR-M5-004 | Every element of a draft MUST be editable using the same plan builder as manual plans (M4) — no separate AI-only editing surface. | MVP |
| FR-M5-005 | The system MUST record that a plan originated as an AI draft, with the model and prompt version used. | MVP |
| FR-M5-006 | Generation MUST respect all dietary exclusions and stated allergies absolutely. | MVP |
| FR-M5-007 | The system MUST NOT generate content that constitutes diagnosis, medical advice, or supplement/medication prescription. | MVP |
| FR-M5-008 | Generation MUST complete within the performance budget, with clear progress indication. | MVP |
| FR-M5-009 | Generation MUST be metered per tenant and enforced against plan entitlements. | MVP |
| FR-M5-010 | On generation failure, the practitioner MUST be able to proceed manually with no data loss and no quota consumed. | MVP |
| FR-M5-011 | The system MUST provide a brief rationale for the draft's structure. | MVP |
| FR-M5-012 | Conversational refinement of a draft. | Phase 2 |
| FR-M5-013 | Learning from a practitioner's accumulated edits to improve future drafts. | Phase 2 |
| FR-M5-014 | Draft adjustment informed by logged adherence data. | Phase 3 |

> **Rationale for FR-M5-004** — a separate AI editing surface would duplicate plan-building logic, which is exactly V1's failure mode. One builder, one set of rules.
> **Rationale for FR-M5-006** — an allergy violation is a safety incident, not a quality issue. This MUST be enforced deterministically in code, not left to the model's compliance.

### M5.6 Acceptance criteria

| ID | Criterion |
|---|---|
| AC-M5-001 | A draft plan is generated from a completed assessment within the performance budget. |
| AC-M5-002 | Every food in every generated draft exists in the food database; no invented foods or values appear across a full test corpus. |
| AC-M5-003 | For a vegetarian client with a declared nut allergy, no draft in a full test corpus contains any non-vegetarian food or nut. |
| AC-M5-004 | No pathway exists by which a `Draft` plan is delivered to a client without explicit practitioner approval. |
| AC-M5-005 | A draft is fully editable in the standard plan builder. |
| AC-M5-006 | Generation failure leaves the practitioner able to build manually, with no quota consumed. |
| AC-M5-007 | A practitioner at their generation limit is told the limit and the upgrade path, and can still build manually. |
| AC-M5-008 | Across a review panel, practitioners rate drafts as *"a useful starting point"* or better in a majority of cases. |

### M5.7 Edge cases

| ID | Case | Required behaviour |
|---|---|---|
| EC-M5-01 | Assessment is incomplete | Generation proceeds where sufficient data exists, stating what was assumed; blocked with explanation if critically insufficient |
| EC-M5-02 | Client has an unusual condition combination | Draft generated conservatively with a prominent advisory for practitioner review |
| EC-M5-03 | Model returns malformed or unusable output | Treated as failure (FR-M5-010); no partial draft presented; no quota consumed |
| EC-M5-04 | Model attempts a food not in the database | Rejected deterministically; substituted or omitted; never surfaced as a fabricated entry |
| EC-M5-05 | Model produces content resembling medical advice | Filtered before presentation; incident logged for prompt review |
| EC-M5-06 | Practitioner generates repeatedly to game a target | Metered per generation; quota applies regardless of outcome |
| EC-M5-07 | Provider outage or rate limiting | Clear message; manual path unaffected; no quota consumed |
| EC-M5-08 | Client's assessment indicates a possible eating disorder | 🟡 **PROPOSED** — generation withheld; practitioner advised to review directly. Requires clinical validation (OD-06) |
| EC-M5-09 | Cost per generation exceeds the economic threshold | Monitored per tenant; caps enforced; anomalies alerted |

### M5.8 Scope summary

| MVP | Phase 2 | Phase 3 |
|---|---|---|
| Grounded draft generation from assessment + practitioner templates, mandatory `Draft` state and approval gate, full editability in the standard builder, absolute allergy/dietary enforcement, metering, graceful failure, brief rationale | Conversational refinement, learning from practitioner edits, multi-day generation variety, regional cuisine emphasis | Adherence-informed adjustment, outcome-based improvement, generated client education content |

---

## M6 — Appointments

### M6.1 Purpose

Record when consultations occur, and ensure both parties turn up.

### M6.2 Business value

No-shows cost time that cannot be recovered. Manual reminders cost effort every day. This module is deliberately thin: 🔒 practitioners already use Google Calendar and Zoom competently, and replacing tools that work is not our wedge.

### M6.3 User stories

| ID | Story |
|---|---|
| US-M6-01 | As Priya, I want to book a consultation against a client, so it appears in their history. |
| US-M6-02 | As Priya, I want automatic reminders sent, so I stop sending them manually. |
| US-M6-03 | As Priya, I want my day's appointments visible at a glance. |
| US-M6-04 | As Anjali, I want a reminder with the joining link, so I do not hunt for it. |
| US-M6-05 | As Priya, I want notes recorded against the appointment, so context stays together. |

### M6.4 Functional requirements

| ID | Requirement | Scope |
|---|---|---|
| FR-M6-001 | The system MUST allow an appointment to be created against a client with date, time, duration, type and mode (in-person / video / phone). | MVP |
| FR-M6-002 | The system MUST store a meeting link supplied by the practitioner. | MVP |
| FR-M6-003 | The system MUST provide day, week and list views of appointments. | MVP |
| FR-M6-004 | The system MUST send a confirmation to the client on booking. | MVP |
| FR-M6-005 | The system MUST send reminders at 🟡 **PROPOSED** intervals of 24 hours and 1 hour before, configurable per tenant. | MVP |
| FR-M6-006 | The system MUST support appointment statuses: `Scheduled`, `Completed`, `Cancelled`, `No-show`, `Rescheduled`. | MVP |
| FR-M6-007 | Rescheduling MUST retain the original appointment in history. | MVP |
| FR-M6-008 | Appointments MUST appear on the client timeline. | MVP |
| FR-M6-009 | All appointment times MUST be stored unambiguously and displayed in the practitioner's configured timezone. | MVP |
| FR-M6-010 | Client self-booking against practitioner availability. | Phase 2 |
| FR-M6-011 | Two-way Google Calendar synchronisation. | Phase 2 |
| FR-M6-012 | Recurring appointments and availability rules. | Phase 2 |
| FR-M6-013 | Payment collection at booking; group appointments. | Phase 3 |

> **Rationale for FR-M6-009** — India has a single timezone, so this is trivial at launch. It is specified now because GCC and UK expansion (Phase 2/3 markets) make it a correctness problem, and timezone handling retrofitted into stored data is a notoriously expensive fix.

### M6.5 Acceptance criteria

| ID | Criterion |
|---|---|
| AC-M6-001 | An appointment is created against a client in ≤ 4 interactions. |
| AC-M6-002 | The client receives a confirmation containing date, time and joining link where applicable. |
| AC-M6-003 | Reminders are sent at the configured intervals and are recorded in the message log. |
| AC-M6-004 | Rescheduling produces a new appointment while the original remains visible in history. |
| AC-M6-005 | Marking an appointment `Completed` allows notes to be attached in the same flow. |
| AC-M6-006 | The day view shows all of the day's appointments on one screen without scrolling on a laptop. |

### M6.6 Edge cases

| ID | Case | Required behaviour |
|---|---|---|
| EC-M6-01 | Two appointments booked in the same slot | Overlap warning shown; not blocked — practitioners sometimes double-book deliberately |
| EC-M6-02 | Appointment booked in the past | Permitted with a warning; back-dated record-keeping is a real need |
| EC-M6-03 | Client's mobile is invalid, so reminders fail | Failure surfaced to the practitioner; not silently swallowed |
| EC-M6-04 | Appointment for a `Paused` client | Blocked, with a prompt to reactivate the client first |
| EC-M6-05 | Reminder due while the tenant is suspended | Suppressed; recorded as suppressed with reason |
| EC-M6-06 | Client cancels via WhatsApp reply | 🟡 Two-way WhatsApp is Phase 2; at MVP the practitioner updates status manually |
| EC-M6-07 | Appointment deleted after reminders were sent | Cancellation notice sent to the client; original retained in history |

### M6.7 Scope summary

| MVP | Phase 2 | Phase 3 |
|---|---|---|
| Manual booking, day/week/list views, confirmations, configurable reminders, statuses, reschedule history, practitioner-supplied meeting links, timeline integration | Client self-booking, availability rules, Google Calendar two-way sync, recurring appointments, buffer/travel time | Payment at booking, group appointments, waitlists, multi-practitioner clinic scheduling |

---

## M7 — Client Portal (PWA)

### M7.1 Purpose

Give the client a fast, focused place to see their plan, log their progress, and feel that someone is paying attention.

### M7.2 Business value

Client engagement drives renewal, and renewal is the practitioner's business. 🔒 It is also how the product markets itself: every client who uses a polished portal is exposed to WellnessCRM, and practitioners talk to each other.

### M7.3 Design principles

🔒 **BINDING** — derived from persona P3 and V1's failure (*"client experience was not engaging enough"*).

| Principle | Implication |
|---|---|
| **WhatsApp is the entry point** | Every client journey starts from a WhatsApp message with a deep link. The portal is a destination, never a place they think to visit. |
| **60-second rule** | Every client task MUST be completable in under 60 seconds on a phone. |
| **No password** | Magic-link access (FR-M0-005). |
| **Installable, not install-required** | Fully functional in a mobile browser; installation is an enhancement. |
| **Assume poor network** | Must work on intermittent 4G and be data-frugal. |
| **One job per screen** | Countermeasure to V1's information overload. |

### M7.4 User stories

| ID | Story |
|---|---|
| US-M7-01 | As Anjali, I want to see what I should eat today without scrolling through chat history. |
| US-M7-02 | As Anjali, I want to tick off meals I followed, in one tap. |
| US-M7-03 | As Anjali, I want to log my weight quickly when reminded. |
| US-M7-04 | As Anjali, I want to see whether I am making progress. |
| US-M7-05 | As Anjali, I want my plan available even with poor signal. |
| US-M7-06 | As Anjali, I want to send my dietitian a lab report from my phone. |
| US-M7-07 | As Anjali, I want the app on my home screen, so it feels like a real app. |

### M7.5 Functional requirements

| ID | Requirement | Scope |
|---|---|---|
| FR-M7-001 | Clients MUST access the portal via a magic link without creating a password. | MVP |
| FR-M7-002 | The portal MUST display the current active plan, defaulting to **today's** meals. | MVP |
| FR-M7-003 | The portal MUST display the full plan across days where a multi-day plan is issued. | MVP |
| FR-M7-004 | Clients MUST be able to mark adherence per meal with a single interaction. | MVP |
| FR-M7-005 | Clients MUST be able to record weight and defined measurements. | MVP |
| FR-M7-006 | Clients MUST be able to view their weight trend. | MVP |
| FR-M7-007 | Clients MUST be able to view upcoming appointments with joining links. | MVP |
| FR-M7-008 | Clients MUST be able to complete an assigned assessment. | MVP |
| FR-M7-009 | Clients MUST be able to upload a document. | MVP |
| FR-M7-010 | The portal MUST be installable as a PWA on Android, iOS, Windows and macOS. | MVP |
| FR-M7-011 | The current plan MUST remain viewable offline once loaded. | MVP |
| FR-M7-012 | Actions taken offline MUST be queued and synchronised on reconnection, with clear pending status. | MVP |
| FR-M7-013 | Deep links MUST open directly on the referenced content. | MVP |
| FR-M7-014 | The portal MUST display the practitioner's name and branding where provided. | MVP |
| FR-M7-015 | Clients MUST be able to view and manage their consent choices. | MVP |
| FR-M7-016 | Web push notifications. | Phase 2 |
| FR-M7-017 | Food/meal photo logging; free-text daily notes to the practitioner. | Phase 2 |
| FR-M7-018 | Habit and water tracking; streaks. | Phase 2 |
| FR-M7-019 | Wearable integration; regional language interface. | Phase 3 |

> **Rationale for FR-M7-016 (Phase 2, not MVP)** — iOS Safari web push requires the user to add the site to their home screen first and remains unreliable. In India, **WhatsApp is a more dependable notification channel than web push**, so MVP relies on M8 rather than building a second, weaker notification path.

### M7.6 Acceptance criteria

| ID | Criterion |
|---|---|
| AC-M7-001 | A client opens a WhatsApp plan link and sees today's meals within the performance budget on a mid-range Android over 4G. |
| AC-M7-002 | Logging adherence for a full day takes ≤ 6 interactions total. |
| AC-M7-003 | Logging weight takes ≤ 3 interactions from opening the portal. |
| AC-M7-004 | With the network disabled after first load, the current plan remains fully viewable. |
| AC-M7-005 | Adherence logged offline is visible as pending and synchronises correctly on reconnection. |
| AC-M7-006 | The portal installs to the home screen on Android and iOS and launches without browser chrome. |
| AC-M7-007 | Every client-facing screen is usable one-handed on a 5-inch display without horizontal scrolling. |
| AC-M7-008 | A client views and modifies their consent choices, and the change appears in the consent ledger. |

### M7.7 Edge cases

| ID | Case | Required behaviour |
|---|---|---|
| EC-M7-01 | Magic link expires | Clear message with a self-service re-request; no practitioner involvement needed |
| EC-M7-02 | Client has no active plan | Meaningful empty state, not a blank screen or error |
| EC-M7-03 | Plan is revised while the client is viewing it | Client is informed a newer version exists and can refresh; MUST NOT silently swap content |
| EC-M7-04 | Client logs adherence for a past date | Permitted within a bounded window; recorded with the actual log time |
| EC-M7-05 | Offline queue conflicts with server state on sync | Server state authoritative for plan content; client logs preserved and reconciled; nothing discarded silently |
| EC-M7-06 | Client is moved to `Paused` while holding a valid link | Read-only access to existing content; no new logging; neutral explanatory message |
| EC-M7-07 | Client shares their link with a family member | Single-use, time-limited (EC-M0-01); consumption audited |
| EC-M7-08 | Tenant is suspended | Client sees a neutral message; no practitioner-attributable blame; no data loss |
| EC-M7-09 | Client uses an unsupported legacy browser | Graceful degradation to a readable plan view without offline capability |
| EC-M7-10 | Client's device storage is full | Offline caching fails silently; online functionality unaffected |

### M7.8 Scope summary

| MVP | Phase 2 | Phase 3 |
|---|---|---|
| Magic-link access, today's plan default, full plan view, one-tap adherence, weight/measurement logging, trend view, appointments, assessment completion, document upload, PWA install, offline plan viewing, offline queue with sync, deep links, practitioner branding, consent management | Web push, photo logging, notes to practitioner, habit/water tracking, streaks, plan history | Wearables, regional languages, client-side education content, community features |

---

## M8 — Messaging & Scheduling Engine

### M8.1 Purpose

One engine that sends every outbound message in the product, on time, through the right transport, with a full record of what happened.

### M8.2 Business value

🔒 **This module is the difference between "software I opened once" and "software that runs my practice."** "Follow up consistently" is the fourth step of the core loop, and it is the one practitioners fail at manually.

### M8.3 Design decision — one engine, not three

🔒 **BINDING**

Read the core loop literally and it implies three reminder systems: appointment reminders, client check-in nudges, and lead follow-ups. Built separately, that becomes three schedulers, three template stores, three retry paths, three failure logs — precisely V1's *"business logic duplicated across multiple screens"* failure, in its most costly form.

**They are one capability:** *"send message M to recipient R at time T via transport X, then record what happened."*

| Component | Responsibility |
|---|---|
| **Scheduler** | Decides what is due |
| **Template registry** | Holds every message's content and variables, versioned |
| **Transport layer** | WhatsApp, SMS, email, push — interchangeable behind one interface |
| **Delivery log** | Immutable record of every attempt, status and failure |
| **Retry policy** | One policy, applied uniformly |

Every reminder in the product is a row in this engine. Adding a new message type must require **no new infrastructure.**

### M8.4 WhatsApp as the primary channel

🔒 In India, WhatsApp is the delivery layer, not an integration.

⚠️ **RISK — external dependency with a long lead time.** WhatsApp Business API requires Meta Business Verification (**2–6 weeks, can be rejected on first submission**) and per-template pre-approval. **Verification MUST begin in week 1 of the project, independent of engineering progress.** This is a calendar dependency, not an engineering one.

⚠️ **RISK — cost.** WhatsApp is the dominant variable cost (₹60–90/tenant/month at expected volumes). It MUST be metered per tenant and capped per tier, or a single heavy user can cost more than they pay. Rates require verification against Meta's current India pricing before pricing is finalised (ASM-08).

⚠️ **RISK — policy compliance.** Template messages must be approved and used within their approved category. Misuse risks number restriction or account suspension — which would take the product's primary channel offline. Marketing-style messaging via this channel is out of scope.

### M8.5 User stories

| ID | Story |
|---|---|
| US-M8-01 | As Priya, I want plans delivered to clients on WhatsApp automatically, so I stop attaching PDFs by hand. |
| US-M8-02 | As Priya, I want weekly check-ins sent without me remembering. |
| US-M8-03 | As Priya, I want to know whether a message was actually delivered. |
| US-M8-04 | As Priya, I want to control how often clients are messaged, so the product never embarrasses me. |
| US-M8-05 | As Anjali, I want messages on WhatsApp where I actually read them. |
| US-M8-06 | As Anjali, I want to stop receiving messages if I choose. |

### M8.6 Functional requirements

**Engine**

| ID | Requirement | Scope |
|---|---|---|
| FR-M8-001 | 🔒 The system MUST implement exactly **one** scheduled-message engine used by all modules. No module may implement its own scheduling or sending. | MVP |
| FR-M8-002 | Message content MUST be defined as versioned templates with typed variables. | MVP |
| FR-M8-003 | Every send attempt MUST be recorded with recipient, template, version, transport, timestamp, status and failure reason. | MVP |
| FR-M8-004 | Failed sends MUST be retried under a single defined policy, with a bounded attempt count. | MVP |
| FR-M8-005 | Messages MUST NOT be sent to clients in `Paused`, `Churned` or `Archived` stages (FR-M1-014). | MVP |
| FR-M8-006 | Messages MUST NOT be sent where the recipient has withdrawn the relevant consent. | MVP |
| FR-M8-007 | Messages MUST NOT be sent for a suspended tenant; suppression MUST be recorded with reason. | MVP |
| FR-M8-008 | The system MUST enforce a per-client maximum message frequency. | MVP |
| FR-M8-009 | 🟡 **PROPOSED** — messages MUST NOT be sent outside 08:00–21:00 in the recipient's local time; due messages are deferred to the next permitted window. | MVP |
| FR-M8-010 | The system MUST meter messages per tenant against entitlements. | MVP |
| FR-M8-011 | The practitioner MUST be able to view the message history for a client. | MVP |
| FR-M8-012 | Transport fallback per message type. | Phase 2 |

**Message types at MVP**

| ID | Requirement | Scope |
|---|---|---|
| FR-M8-013 | Plan delivery notification with a deep link to the plan. | MVP |
| FR-M8-014 | Appointment confirmation. | MVP |
| FR-M8-015 | Appointment reminders at configured intervals. | MVP |
| FR-M8-016 | Scheduled client check-in nudge (weight/adherence prompt). | MVP |
| FR-M8-017 | Assessment invitation and reminder. | MVP |
| FR-M8-018 | Lead enquiry acknowledgement. | MVP |
| FR-M8-019 | Practitioner notification of a new lead. | MVP |
| FR-M8-020 | Magic link delivery for portal access. | MVP |
| FR-M8-021 | Re-engagement nudge for inactive clients. | Phase 2 |

**Check-in scheduling**

| ID | Requirement | Scope |
|---|---|---|
| FR-M8-022 | The practitioner MUST be able to configure a recurring check-in schedule per client. | MVP |
| FR-M8-023 | 🟡 **PROPOSED** default: weekly, on the day of week the client became `Active`. | MVP |
| FR-M8-024 | Check-ins MUST be pausable per client without changing the client's lifecycle stage. | MVP |
| FR-M8-025 | Check-ins MUST stop automatically when the client leaves `Active`. | MVP |

**Practitioner control**

| ID | Requirement | Scope |
|---|---|---|
| FR-M8-026 | Practitioners MUST be able to preview any message template as their clients will receive it. | MVP |
| FR-M8-027 | Practitioners MUST be able to disable any non-essential message type tenant-wide. | MVP |
| FR-M8-028 | Practitioners MUST be able to see pending scheduled messages for a client. | MVP |
| FR-M8-029 | Practitioner-editable template wording, within transport policy constraints. | Phase 2 |
| FR-M8-030 | Two-way WhatsApp inbox. | Phase 2 |
| FR-M8-031 | Broadcast messaging to segments. | Phase 3 |

> **Rationale for FR-M8-026/027** — the practitioner's professional reputation is attached to every message we send on their behalf. If they cannot see or control it, they will not trust the automation, and trust in the automation is the entire value of this module.

### M8.7 Acceptance criteria

| ID | Criterion |
|---|---|
| AC-M8-001 | An approved plan triggers a WhatsApp message to the client containing a working deep link, within 60 seconds. |
| AC-M8-002 | A configured weekly check-in fires on schedule for 4 consecutive weeks without practitioner action. |
| AC-M8-003 | Every send attempt across a full Journey J1 and J2 run appears in the delivery log with a final status. |
| AC-M8-004 | A message to a `Paused` client is suppressed and recorded as suppressed with reason. |
| AC-M8-005 | A client who withdraws messaging consent receives no further non-essential messages. |
| AC-M8-006 | A message due at 02:00 is deferred to the permitted window rather than sent or dropped. |
| AC-M8-007 | A transport failure is retried per policy, and terminal failure is visible to the practitioner. |
| AC-M8-008 | Adding a new message type requires only a new template and schedule, with no change to scheduling or transport logic. |
| AC-M8-009 | A practitioner previews every MVP message template as the client will see it. |

### M8.8 Edge cases

| ID | Case | Required behaviour |
|---|---|---|
| EC-M8-01 | Client's number is not on WhatsApp | Detected; recorded; falls back to SMS where available; practitioner informed |
| EC-M8-02 | Client blocks the practitioner's WhatsApp number | Delivery failures recorded; practitioner informed after repeated failure |
| EC-M8-03 | Meta template approval is revoked | Affected message type suspended; practitioner and operator alerted; other types unaffected |
| EC-M8-04 | Tenant exceeds their message quota mid-month | Non-essential messages suspended with a clear upgrade prompt; essential transactional messages (magic links) continue |
| EC-M8-05 | Scheduler is down when messages are due | Messages sent on recovery, with staleness checked; a check-in more than 24 h stale is dropped and logged rather than sent late |
| EC-M8-06 | Duplicate send triggered by a retry | Idempotency MUST prevent a client receiving the same message twice |
| EC-M8-07 | Client replies to an outbound message | 🟡 MVP: replies land in the practitioner's own WhatsApp. Two-way inbox is Phase 2. This MUST be clearly communicated to practitioners |
| EC-M8-08 | Client's number changes | Scheduled messages retarget the new number; history retains the old |
| EC-M8-09 | Same client is messaged by two modules simultaneously | Frequency cap applies across all types; lower-priority message deferred |
| EC-M8-10 | WhatsApp provider outage | Messages queued, not lost; operator alerted; sent on recovery subject to staleness rules |

### M8.9 Scope summary

| MVP | Phase 2 | Phase 3 |
|---|---|---|
| Single scheduling engine, versioned templates, WhatsApp + SMS + email transports, delivery log, retry policy, consent and stage suppression, frequency caps, quiet hours, metering, 8 MVP message types, configurable check-ins, practitioner preview and control | Transport fallback, editable templates, two-way WhatsApp inbox, re-engagement nudges, richer scheduling rules | Broadcast/segments, chatbot flows, campaign tooling |

---

## M9 — Progress & Retention

### M9.1 Purpose

Show whether clients are progressing, and surface those who are disengaging while there is still time to act.

### M9.2 Business value

Retention is the practitioner's revenue and our own. A client who silently disengages churns in month three. **A practitioner who saves one client per month from churning earns back many times the subscription cost** — this is the retention half of the ROI story that lead recovery starts.

### M9.3 User stories

| ID | Story |
|---|---|
| US-M9-01 | As Priya, I want to see which clients have gone quiet, so I can reach out before they cancel. |
| US-M9-02 | As Priya, I want this week's check-in responses in one place, so I review 40 clients in minutes. |
| US-M9-03 | As Priya, I want a client's progress at a glance, so I can open a conversation with evidence. |
| US-M9-04 | As Anjali, I want to see my own progress, so I stay motivated. |
| US-M9-05 | As Priya, I want to know how my practice is doing overall. |

### M9.4 Functional requirements

| ID | Requirement | Scope |
|---|---|---|
| FR-M9-001 | The system MUST provide an at-risk view listing `Active` clients with no logged activity in a defined period. | MVP |
| FR-M9-002 | 🟡 **PROPOSED** — the at-risk threshold is 10 days without adherence logging, measurement or message response, configurable per tenant. | MVP |
| FR-M9-003 | The system MUST provide a consolidated view of check-in responses for a period. | MVP |
| FR-M9-004 | The system MUST display per-client progress against their stated goal, including measurement trend and adherence rate. | MVP |
| FR-M9-005 | Clients MUST see their own progress in the portal (FR-M7-006). | MVP |
| FR-M9-006 | The system MUST provide practice-level counts: active clients, new clients this period, churned this period, leads received, conversion rate. | MVP |
| FR-M9-007 | Adherence MUST be presented as a rate over a period, never as a single judgemental score. | MVP |
| FR-M9-008 | Cohort and outcome analysis; revenue reporting; exportable reports. | Phase 2 |
| FR-M9-009 | Churn prediction; benchmarking. | Phase 3 |

> **Rationale for FR-M9-007** — a single "compliance score" invites clients to feel graded and practitioners to substitute a number for clinical judgement. Both damage the relationship the product exists to support.

### M9.5 Acceptance criteria

| ID | Criterion |
|---|---|
| AC-M9-001 | A client with no activity for the threshold period appears in the at-risk view without any manual tracking. |
| AC-M9-002 | A practitioner reviews a week of check-ins for 40 clients from one screen. |
| AC-M9-003 | Weight trend and adherence rate are visible on the client record without navigating away. |
| AC-M9-004 | Practice-level counts reconcile exactly with the underlying client records. |
| AC-M9-005 | A client sees their own weight trend in the portal within the performance budget. |

### M9.6 Edge cases

| ID | Case | Required behaviour |
|---|---|---|
| EC-M9-01 | New client has no data yet | Excluded from at-risk until a reasonable onboarding window has elapsed |
| EC-M9-02 | Client engages via the practitioner's WhatsApp but not the portal | 🟡 Appears at-risk despite real engagement. MUST be dismissible by the practitioner with an acknowledgement, or the view loses credibility |
| EC-M9-03 | Client's weight increases against a loss goal | Presented factually and neutrally; no negative framing to the client |
| EC-M9-04 | Client logs adherence but never weight | Both tracked independently; at-risk considers either |
| EC-M9-05 | Practitioner has fewer than 5 clients | Practice metrics shown without misleading percentages |
| EC-M9-06 | Client's goal changes mid-engagement | Progress recalculated against the current goal; goal history retained |

### M9.7 Scope summary

| MVP | Phase 2 | Phase 3 |
|---|---|---|
| At-risk client view, consolidated check-in review, per-client progress with trend and adherence rate, client-facing progress, practice-level counts | Cohort analysis, outcome tracking, revenue reporting, exports, configurable dashboards | Churn prediction, anonymised benchmarking, practitioner performance insights |

---

## M10 — Subscription & Entitlements

### M10.1 Purpose

Determine what a tenant is entitled to, enforce it, and collect payment for it.

### M10.2 Business value

Our revenue. 🔒 Entitlement enforcement is structural and cannot be retrofitted; automated collection is operational and can be deferred.

### M10.3 Design decision — build enforcement, defer collection

🔒 **BINDING**

| Capability | MVP | Reasoning |
|---|---|---|
| Plan definitions, limits, metering, enforcement | ✅ **Build** | Every module depends on it; retrofitting means touching every module |
| Automated recurring collection, e-mandates, dunning, self-serve upgrade | ❌ **Defer** | Weeks of work for a problem that does not exist below ~20 customers |

**Collection at MVP:** the practitioner is sent a payment link, pays, and an operator activates their plan. At 20 customers this is minutes of work per month. Building subscription machinery before having subscribers is the classic pre-revenue trap.

⚠️ **RISK — long lead time.** Razorpay merchant KYC and recurring-mandate activation take time and require business documentation. **Begin in week 1**, even though automated collection is Phase 2. Same class of calendar dependency as Meta verification.

### M10.4 Proposed plans

🟡 **PROPOSED — pricing requires market validation (OD-04)**

| Plan | Price/month | Active clients | AI drafts/mo | WhatsApp msgs/mo | Storage |
|---|---|---|---|---|---|
| **Free** | ₹0 | 3 | 5 | 50 | 100 MB |
| **Starter** | ₹799 | 30 | 40 | 600 | 2 GB |
| **Growth** | ₹1,799 | 100 | 150 | 2,000 | 10 GB |
| **Clinic** | ₹3,499 | 300 (3 practitioners) | 400 | 6,000 | 30 GB |

Annual billing at 10 months' price. 🟡 Quota values are proposals pending cost verification (ASM-08).

> **Why a permanent free tier:** every client the practitioner onboards sees our product. In a market where practitioners talk to each other constantly, the client-facing surface is our cheapest acquisition channel. Three active clients is enough to prove value and too few to run a practice on.

### M10.5 Functional requirements

| ID | Requirement | Scope |
|---|---|---|
| FR-M10-001 | The system MUST define plans as configuration, not code, so limits change without a release. | MVP |
| FR-M10-002 | The system MUST track per-tenant consumption of every metered resource. | MVP |
| FR-M10-003 | Limits MUST be enforced at the point of action, stating the limit and the upgrade path. | MVP |
| FR-M10-004 | The system MUST display current usage against limits in the practitioner interface. | MVP |
| FR-M10-005 | The system MUST warn a tenant approaching a limit 🟡 **PROPOSED** at 80% consumption. | MVP |
| FR-M10-006 | Exceeding a limit MUST NOT restrict access to existing data — only new metered actions. | MVP |
| FR-M10-007 | The system MUST support a trial period with full feature access. | MVP |
| FR-M10-008 | Operators MUST be able to assign a plan and set its validity manually. | MVP |
| FR-M10-009 | The system MUST support tenant suspension and reactivation without data loss. | MVP |
| FR-M10-010 | 🟡 **PROPOSED** — a suspended tenant retains read-only access to their own data for 90 days, and export capability, before any purge is considered. | MVP |
| FR-M10-011 | The system MUST issue a GST-compliant invoice for every payment received. | MVP |
| FR-M10-012 | Automated recurring collection via our payment gateway, including UPI mandates. | Phase 2 |
| FR-M10-013 | Self-serve upgrade and downgrade. | Phase 2 |
| FR-M10-014 | Dunning and retry on failed payment. | Phase 2 |
| FR-M10-015 | Annual billing; coupons and referral credit. | Phase 2 |
| FR-M10-016 | Per-practitioner seat billing for clinics; usage-based overage. | Phase 3 |

> ⚠️ **FR-M10-011 is a legal requirement in India, not a feature.** GST invoicing applies from the first rupee collected and requires the correct tax treatment. Needs accountant confirmation of place-of-supply and rate treatment (ASM-09).
> **Rationale for FR-M10-006/010** — holding a practitioner's client data hostage over a lapsed payment is both an ethical failure and, under DPDP, a probable compliance problem. It also guarantees they never return.

### M10.6 Acceptance criteria

| ID | Criterion |
|---|---|
| AC-M10-001 | A tenant at their active-client limit cannot add another `Active` client, and sees the limit and upgrade path. |
| AC-M10-002 | Plan limits are changed via configuration with no code release. |
| AC-M10-003 | Current usage against every metered resource is visible in the practitioner interface. |
| AC-M10-004 | A tenant over their limit retains full read and edit access to existing clients and plans. |
| AC-M10-005 | An operator activates a paid plan manually and the tenant's entitlements update immediately. |
| AC-M10-006 | A suspended tenant retains read-only access and can export their data. |
| AC-M10-007 | A GST-compliant invoice is generated for a recorded payment. |
| AC-M10-008 | AI generation and WhatsApp quotas enforce independently of the active-client limit. |

### M10.7 Edge cases

| ID | Case | Required behaviour |
|---|---|---|
| EC-M10-01 | Tenant downgrades while over the new limit | Existing data fully accessible; no new `Active` clients until compliant; explained clearly |
| EC-M10-02 | Trial ends with clients above the free limit | Read-only above the free allowance; explicit prompt; no deletion |
| EC-M10-03 | Metering is briefly unavailable | Fail safe: reads allowed, new metered actions blocked, incident logged (FR-M0-046) |
| EC-M10-04 | Usage counted for an action that then fails | Quota consumption MUST be reconciled; failed AI generation consumes nothing (FR-M5-010) |
| EC-M10-05 | Tenant pays after suspension | Immediate reactivation with all data intact |
| EC-M10-06 | Clinic plan practitioner count is exceeded | Additional practitioner cannot be added; existing users unaffected |
| EC-M10-07 | Tenant requests full account deletion | DPDP erasure pathway; export offered first; confirmation required; retention limits honoured |
| EC-M10-08 | Payment recorded twice | Detected and reconciled; validity extended rather than double-charged |

### M10.8 Scope summary

| MVP | Phase 2 | Phase 3 |
|---|---|---|
| Configurable plans, metering of 4 resources, point-of-action enforcement, usage display, 80% warnings, trial, manual operator activation, suspension with read-only grace, GST invoicing | Automated recurring collection, UPI e-mandates, self-serve upgrade/downgrade, dunning, annual billing, coupons, referrals | Seat-based clinic billing, usage overage, partner/reseller billing, multi-currency |

---

## M11 — Super Admin & Support Console

### M11.1 Purpose

Let the platform operator diagnose and resolve a practitioner's problem without requesting their password, and without unrestricted access to clinical data.

### M11.2 Business value

There is no support team. 🔒 When a practitioner says *"my client can't see their plan,"* the operator must be able to look immediately. Without this, every support interaction becomes a guessing exercise conducted over WhatsApp — and support quality at launch determines whether early customers stay.

Also proven in V1: Super Admin was on the validated concepts list.

### M11.3 Security model

⚠️ **RISK — this is the highest-privilege, cross-tenant surface in the entire product.** A single compromised operator account could expose every client record on the platform. It must be built defensively from the first commit, not hardened later.

🔒 **BINDING controls**

| Control | Requirement |
|---|---|
| **Separate identity realm** | Operator credentials MUST NOT be practitioner credentials (FR-M0-004) |
| **Mandatory 2FA** | No exceptions, no bypass (FR-M0-009) |
| **Read-only by default** | Mutation requires an explicit, separately-authorised action |
| **Full audit of reads** | Every access to tenant data MUST be logged (FR-M0-032) |
| **Explicit impersonation** | Acting as a practitioner MUST be deliberate, time-boxed, visibly indicated, and audited. There MUST be no silent "log in as user" |
| **Minimum clinical exposure** | Operators MUST be able to diagnose without reading clinical content wherever technically possible |
| **No bulk export** | Operators MUST NOT be able to export data across tenants |

### M11.4 User stories

| ID | Story |
|---|---|
| US-M11-01 | As an operator, I want to find a tenant by practitioner email or phone, so I can respond to a support request quickly. |
| US-M11-02 | As an operator, I want to see a tenant's plan, usage and account state, so I can answer billing questions. |
| US-M11-03 | As an operator, I want to see whether a message was delivered, so I can resolve "my client didn't get it." |
| US-M11-04 | As an operator, I want to activate or extend a plan manually, so I can collect payment before automation exists. |
| US-M11-05 | As an operator, I want to reproduce what a practitioner is seeing, without asking for their password. |
| US-M11-06 | As a practitioner, I want to know when platform staff accessed my account, so I can trust the platform. |

### M11.5 Functional requirements

| ID | Requirement | Scope |
|---|---|---|
| FR-M11-001 | Operators MUST be able to search tenants by practitioner email, mobile or tenant name. | MVP |
| FR-M11-002 | Operators MUST be able to view tenant account state: plan, usage, limits, validity, suspension status, registration date. | MVP |
| FR-M11-003 | Operators MUST be able to view a tenant's aggregate counts (clients, plans, appointments) **without reading client-identifying content**. | MVP |
| FR-M11-004 | Operators MUST be able to view the message delivery log for a tenant, including failure reasons. | MVP |
| FR-M11-005 | Operators MUST be able to assign, extend or change a plan, and suspend or reactivate a tenant. | MVP |
| FR-M11-006 | Operators MUST be able to trigger a password reset for a practitioner without seeing or setting the password. | MVP |
| FR-M11-007 | Every operator action MUST be recorded in the audit log with actor, target tenant, action and timestamp. | MVP |
| FR-M11-008 | Operators MUST NOT be able to alter or delete audit records. | MVP |
| FR-M11-009 | Operators MUST be able to view platform-level health: tenant counts by plan, message failure rates, AI generation failures, error rates. | MVP |
| FR-M11-010 | Operators MUST be able to manage the curated food database (add, correct, retire entries). | MVP |
| FR-M11-011 | Operators MUST be able to view the failed-food-search log to prioritise curation (FR-M4-014). | MVP |
| FR-M11-012 | Time-boxed, audited impersonation of a practitioner, requiring a stated reason. | Phase 2 |
| FR-M11-013 | Practitioner-visible notification when platform staff access their account. | Phase 2 |
| FR-M11-014 | Operator role tiers (support vs engineering vs billing). | Phase 2 |
| FR-M11-015 | In-product support messaging; feature flag management per tenant. | Phase 3 |

> **Why impersonation is Phase 2 (FR-M11-012)** — it is the most dangerous capability in the console. At MVP, aggregate views plus the delivery log resolve most support cases. Building impersonation before the audit infrastructure is proven in production is an unnecessary risk.
> **Why FR-M11-010 is MVP** — the curated food database is the moat (M4.2), and it requires continuous curation from launch. Editing it via direct database access would be unauditable and error-prone.

### M11.6 Acceptance criteria

| ID | Criterion |
|---|---|
| AC-M11-001 | An operator locates a tenant from a practitioner's email in under 30 seconds. |
| AC-M11-002 | An operator diagnoses a failed client message using only the delivery log, without reading clinical content. |
| AC-M11-003 | Every operator action performed during a support scenario appears in the audit log. |
| AC-M11-004 | An operator cannot authenticate to the console without 2FA. |
| AC-M11-005 | A practitioner credential cannot authenticate to the operator console, and vice versa. |
| AC-M11-006 | An operator activates a paid plan and the tenant's entitlements update immediately. |
| AC-M11-007 | An operator adds a curated food and it becomes searchable for all tenants, with the change audited. |
| AC-M11-008 | No operator interface exposes a cross-tenant data export. |

### M11.7 Edge cases

| ID | Case | Required behaviour |
|---|---|---|
| EC-M11-01 | Operator account is compromised | 2FA required; all access audited; operator sessions individually revocable |
| EC-M11-02 | Operator needs clinical detail to resolve a defect | Requires explicit, reason-stated, audited elevation; practitioner notified (Phase 2) |
| EC-M11-03 | Operator corrects a curated food value used in issued plans | Issued plans retain original values (EC-M4-03); correction audited |
| EC-M11-04 | Operator suspends the wrong tenant | Reversible immediately; no data loss; both actions audited |
| EC-M11-05 | Operator is also a practitioner on the platform | Separate credentials mandatory; MUST NOT operate on their own tenant through the console |
| EC-M11-06 | Audit log grows very large | Retention and archival policy required; MUST NOT be truncated silently |

### M11.8 Scope summary

| MVP | Phase 2 | Phase 3 |
|---|---|---|
| Tenant search, account state view, aggregate counts without clinical content, message delivery log, plan assignment and suspension, password reset trigger, full audit, platform health view, curated food database management, failed-search log | Time-boxed audited impersonation, practitioner access notifications, operator role tiers, richer platform analytics | In-product support messaging, per-tenant feature flags, automated anomaly alerting |

---

## 9. Nutrition Assessment — PROPOSED FOR REVIEW

> # 🟡 PROPOSED — DO NOT IMPLEMENT WITHOUT PRACTITIONER VALIDATION
>
> **This entire section is a structured proposal, not a specification.**
>
> I am not a registered dietitian. What follows is assembled from **documented, citable professional frameworks** plus India-specific dimensions that those frameworks do not cover. It is offered so that a practising professional has something concrete to correct, rather than a blank page.
>
> **Every field below requires validation before implementation.** Where I have inferred rather than sourced something, it is marked `[INFERRED]`. Where a framework supports it, it is marked `[SOURCED]`.
>
> **Review outcome required:** for each field — *keep as-is* / *reword* / *make optional* / *remove* / *missing, add this*.

### 9.1 Frameworks this draft is built on

| Framework | Origin | What it contributes |
|---|---|---|
| **ABCD model** | Standard dietetic assessment teaching | The four assessment domains: **A**nthropometric, **B**iochemical, **C**linical, **D**ietary |
| **Nutrition Care Process (ADIME)** | Academy of Nutrition and Dietetics | The workflow: Assessment → Diagnosis → Intervention → Monitoring & Evaluation |
| **24-hour dietary recall** | Standard dietary intake method | Structure for capturing actual recent intake |
| **Food Frequency Questionnaire (FFQ)** | Standard dietary intake method | Structure for capturing habitual patterns |
| **IFCT 2017** | National Institute of Nutrition, Hyderabad | Food composition reference for the food database (M4) |

> ⚠️ **My confidence is high on the *existence and shape* of these frameworks, and lower on the *specific field-level content* below.** Treat structure as a reasonable starting point and content as a first draft.

### 9.2 Design constraints on the assessment

🔒 These derive from Phase 1 decisions and persona requirements, and are not part of the clinical proposal.

| Constraint | Source |
|---|---|
| MUST be versioned data, changeable without a code change (FR-M3-002) | V1 lesson: design evolved during build |
| Clinical sections MUST be individually skippable (FR-M3-006) | Persona P2 (Rahul) is not clinically qualified |
| MUST be completable in stages on a mobile phone (FR-M3-005) | Persona P3 (Anjali), 4G, low patience |
| MUST NOT diagnose or triage (FR-M5-007) | Safety and regulatory posture |
| Output MUST feed the plan builder and AI grounding | M4, M5 dependency |
| MUST support repeat administration for comparison (FR-M3-007) | Progress tracking |

### 9.3 Proposed section structure

| § | Section | Completed by | Required? | Est. time |
|---|---|---|---|---|
| A | Client profile & context | Client | ✅ Required | 1 min |
| B | Goals & motivation | Client | ✅ Required | 2 min |
| C | Anthropometric | Client or practitioner | ✅ Required | 2 min |
| D | Medical & clinical history | Client | ⚠️ Skippable *(section-level)* | 3 min |
| E | Biochemical / lab values | Practitioner | ⚠️ Optional | — |
| F | Dietary pattern & preferences | Client | ✅ Required | 3 min |
| G | Typical day's intake (24h recall) | Client | ✅ Required | 4 min |
| H | Food frequency (habitual) | Client | ⚠️ Optional | 3 min |
| I | Lifestyle, activity & sleep | Client | ✅ Required | 2 min |
| J | Household & practical context | Client | ✅ Required | 2 min |
| K | Behavioural readiness | Client | ⚠️ Optional | 1 min |
| L | Practitioner assessment & notes | Practitioner | ✅ Required | — |

**Total client-facing time: 20–25 minutes if all sections completed.**

> ⚠️ **RISK — this is too long.** 20+ minutes on a phone will cause abandonment (EC-M3-01 mitigates but does not solve it). **OD-07: which sections are genuinely required before a first consultation, and which can be collected later or during the consultation?** My instinct is that A, B, C, F, G, J are the pre-consultation minimum (~14 min) and the rest is practitioner-driven — but this is a clinical workflow judgement, not mine to make.

### 9.4 Section A — Client profile & context

| Field | Type | Notes | Source |
|---|---|---|---|
| Full name | Text | Pre-filled from client record | `[SOURCED]` |
| Age / date of birth | Date | Drives requirement calculations, and minor-consent (FR-M0-028) | `[SOURCED]` |
| Sex | Choice | 🟡 Options and wording need review. Clinically needed for requirement estimation | `[SOURCED]` |
| City / state | Text | Regional cuisine inference; seasonal availability | `[INFERRED]` |
| Occupation | Text | Activity level, meal timing, desk-bound risk | `[SOURCED]` |
| Work pattern | Choice | Day shift / night shift / rotating / work-from-home / irregular | `[INFERRED]` |
| Preferred language | Choice | Communication and future regional output | `[INFERRED]` |
| Marital status | Choice | 🟡 **Flagged for review** — relevant to who cooks and meal autonomy, but may feel intrusive. Recommend optional | `[INFERRED]` |
| Pregnancy / lactation status | Choice | ⚠️ **Clinically significant.** Materially changes requirements and contraindications. Recommend required where applicable | `[SOURCED]` |

### 9.5 Section B — Goals & motivation

| Field | Type | Notes | Source |
|---|---|---|---|
| Primary goal | Choice | 🟡 Proposed: weight loss / weight gain / muscle gain / manage a medical condition / improve energy / improve digestion / sports performance / general wellbeing / other | `[INFERRED]` |
| Goal detail | Text | Free text in the client's own words | `[SOURCED]` |
| Target weight | Number | Optional | `[INFERRED]` |
| Target timeframe | Choice | Sets expectations; flags unrealistic goals for practitioner attention | `[INFERRED]` |
| Previous attempts | Text | What they have tried and what happened | `[SOURCED]` |
| Prior diet plans followed | Text | Including any current diet | `[SOURCED]` |
| What has stopped you before | Text | 🟡 High-value for behavioural strategy | `[INFERRED]` |
| Motivation level | Scale | 🟡 Recommend keeping — feeds readiness assessment (§K) | `[INFERRED]` |

### 9.6 Section C — Anthropometric `[SOURCED — ABCD model]`

| Field | Type | Notes |
|---|---|---|
| Height | Number | cm, or ft/in normalised on entry (FR-M3-015) |
| Current weight | Number | kg |
| Waist circumference | Number | 🟡 Clinically important for central obesity in Indian populations; needs guidance on how a client measures it correctly at home |
| Hip circumference | Number | Optional; enables waist-hip ratio |
| Highest ever weight | Number | Optional; weight history context |
| Lowest adult weight | Number | Optional |
| Recent weight change | Choice + number | ⚠️ Unintentional loss is a clinical red flag |
| Body fat % | Number | Practitioner-entered only; device-dependent |

**Derived by the system (FR-M3-012):** BMI, waist-hip ratio.

> ⚠️ **OD-08 — BMI thresholds.** Indian guidelines commonly use **lower** BMI cut-offs for overweight and obesity than WHO international standards, reflecting higher metabolic risk at lower body mass in South Asian populations. **I am confident this distinction exists; I am not confident of the exact current values.** The thresholds we display must be confirmed against a current authoritative Indian clinical source before implementation, and the source cited in-product. Displaying wrong thresholds in a clinical tool is a serious defect.

### 9.7 Section D — Medical & clinical history `[SOURCED — ABCD model]`

⚠️ Section-level skippable (FR-M3-006). Rahul (P2) must be able to bypass this entirely.

| Field | Type | Notes |
|---|---|---|
| Diagnosed conditions | Multi-select + other | 🟡 Proposed India-weighted list below |
| Condition details | Text | Duration, current management |
| Current medications | Repeatable text | ⚠️ Name and dose only. **The system MUST NOT interpret, advise on, or interact-check medications** |
| Current supplements | Repeatable text | Common and clinically relevant in this context |
| Known allergies | Multi-select + text | ⚠️ **SAFETY-CRITICAL.** Must flow to M4 filtering and M5 absolute exclusion (FR-M5-006) |
| Food intolerances | Multi-select + text | Distinct from allergy; e.g. lactose, gluten |
| Digestive symptoms | Multi-select | 🟡 Proposed: bloating / acidity / constipation / loose stools / gas / nausea / none |
| Surgical history | Text | Optional; bariatric and GI surgery are materially relevant |
| Family history | Multi-select | Diabetes, hypertension, cardiac, thyroid, obesity |
| Menstrual history | Group | Where applicable. ⚠️ PCOS is highly prevalent in this client population |
| Physician under care | Text | Optional; supports referral and coordination |

**Proposed condition list — 🟡 India-weighted, needs practitioner review:**
Type 2 diabetes · Prediabetes · Type 1 diabetes · Hypertension · Hypothyroidism · Hyperthyroidism · PCOS/PCOD · Dyslipidaemia · Fatty liver (NAFLD) · Anaemia · Vitamin D deficiency · Vitamin B12 deficiency · IBS · Acid reflux/GERD · Coeliac disease · Chronic kidney disease · Cardiac disease · Gout · Osteoporosis/osteopenia · Cancer (current or history) · Eating disorder (current or history) · None

> ⚠️ **Two entries need explicit clinical policy, not just review:**
> - **Chronic kidney disease** — protein and electrolyte restriction is specialist territory. Should AI drafting be withheld entirely for these clients? (OD-06)
> - **Eating disorder** — EC-M5-08 proposes withholding AI generation. A dietetic professional must confirm the correct product behaviour here. This is a genuine safety question, and I do not want to guess at it.

### 9.8 Section E — Biochemical / lab values `[SOURCED — ABCD model]`

Practitioner-entered, optional. Values may also arrive as an uploaded document (FR-M3-024).

🟡 **Proposed panel — requires validation of which markers matter enough to structure:**

| Group | Markers |
|---|---|
| Glycaemic | Fasting glucose, post-prandial glucose, HbA1c, fasting insulin |
| Lipid | Total cholesterol, LDL, HDL, triglycerides |
| Thyroid | TSH, T3, T4 |
| Haematology | Haemoglobin, ferritin |
| Vitamins | Vitamin D, Vitamin B12 |
| Renal | Creatinine, urea, eGFR |
| Hepatic | SGOT/AST, SGPT/ALT |
| Other | Uric acid, CRP |

Each entry: value, unit, reference range, test date.

> **OD-09** — should these be structured fields at MVP, or is a document upload plus practitioner notes sufficient? Structuring enables trend charts and AI grounding; it is also significant scope. My recommendation: **structure a small subset** (HbA1c, TSH, Hb, Vitamin D, B12) and leave the rest to documents at MVP.
> 🔒 The system MUST NOT interpret lab values or suggest diagnoses (FR-M5-007). Reference ranges are displayed for practitioner convenience only.

### 9.9 Section F — Dietary pattern & preferences `[SOURCED — ABCD model]` 🇮🇳

**This section is where foreign products fail. Highest-value section for review.**

| Field | Type | Notes |
|---|---|---|
| Dietary classification | Choice | 🟡 Vegetarian / Eggetarian / Non-vegetarian / Vegan / Jain / Other. Must map to M4 filtering (FR-M4-011) |
| Non-veg frequency & types | Group | Where applicable — chicken, fish, mutton, egg; and how often |
| Onion & garlic | Choice | ⚠️ Jain and some observant Hindu clients exclude these. Must reach plan filtering |
| Root vegetables | Choice | Jain exclusion |
| Religious fasting observed | Multi-select | 🟡 Proposed: Navratri · Ekadashi · Shravan Mondays · Karva Chauth · Ramadan · Jain fasts · weekly personal fast · none. ⚠️ **A plan that ignores fasting days is immediately useless** |
| Fasting day eating pattern | Text | What is permitted on those days |
| Regional cuisine | Choice | 🟡 North / South / East / West / Maharashtrian / Bengali / Gujarati / Punjabi / South Indian / Mixed — needs practitioner input on the right granularity |
| Staple grain | Choice | Wheat / rice / millets / mixed — drives plan structure |
| Cooking oil used | Multi-select | Regionally significant; a routine intervention point |
| Foods disliked | Text | Adherence-critical |
| Foods loved | Text | 🟡 Adherence lever — include what they must not lose |
| Eating out frequency | Choice | Per week |
| Food delivery frequency | Choice | 🟡 Increasingly significant in urban India |
| Alcohol | Group | Frequency and type |
| Tobacco / paan | Choice | 🟡 Flagged — clinically relevant, may feel intrusive. Recommend optional |
| Tea / coffee | Group | ⚠️ Cups per day, **sugar per cup**, milk type. A very common intervention point |
| Water intake | Number | Glasses or litres per day |

### 9.10 Section G — Typical day's intake `[SOURCED — 24-hour recall]`

Structured by the meal slots in FR-M4-025 so intake maps directly onto plan structure.

For each of **Early Morning · Breakfast · Mid-Morning · Lunch · Evening Snack · Dinner · Bedtime**:

| Field | Type |
|---|---|
| Usual time | Time |
| What is typically eaten | Text, with optional food-database linking |
| Approximate quantity | Text, in household measures |
| Skipped? | Boolean |

Plus: weekday vs weekend difference · late-night eating · meal skipping frequency · who decides what is cooked.

> 🟡 **OD-10 — free text or structured food entry?** Free text is far easier for the client and likely to be completed. Structured entry (linked to the food database) enables baseline nutritional analysis and much better AI grounding. **Recommendation: free text at MVP**, with the practitioner optionally structuring it during consultation. Forcing a client to search a food database for 7 meal slots on a phone will cause abandonment.

### 9.11 Section H — Food frequency `[SOURCED — FFQ]`

Optional. Habitual intake frequency (daily / 4–6× week / 2–3× week / weekly / occasionally / never) across 🟡 proposed groups: cereals & millets · pulses & legumes · milk & dairy · vegetables (green leafy, other) · fruits · non-vegetarian items · nuts & seeds · fried snacks · sweets & desserts · packaged/processed snacks · aerated drinks · fruit juices.

> **OD-11** — is an FFQ realistic in a self-service digital assessment, or is it something practitioners do conversationally? It is well-established in clinical practice, but 12 groups × frequency on a phone is heavy. Recommend **optional and practitioner-triggered**, not part of the default client flow.

### 9.12 Section I — Lifestyle, activity & sleep `[SOURCED]`

| Field | Type | Notes |
|---|---|---|
| Activity level | Choice | 🟡 Sedentary / lightly active / moderately active / very active — needs definitions clients can self-assess accurately |
| Structured exercise | Group | Type, frequency, duration, intensity |
| Daily step count | Number | Optional |
| Occupation activity | Choice | Desk-bound / standing / physically demanding |
| Sleep duration | Number | Hours |
| Sleep quality | Choice | |
| Sleep timing | Group | Bedtime and wake time — relevant to meal timing |
| Stress level | Scale | 🟡 Self-reported |
| Screen time before bed | Choice | 🟡 **Flagged** — may be beyond nutrition scope. Recommend removing unless practitioners use it |

### 9.13 Section J — Household & practical context 🇮🇳

**🟡 Entirely `[INFERRED]` — no Western framework covers this, and I believe it is decisive for adherence in India. This section most needs practitioner validation.**

| Field | Type | Why it matters |
|---|---|---|
| Who cooks at home | Choice | ⚠️ **Possibly the single most important adherence field.** If the client's mother or a cook prepares food, a plan requiring separate meals will fail |
| Household size | Number | Separate cooking feasibility |
| Family type | Choice | Nuclear / joint — meal autonomy differs sharply |
| Can you eat differently from the family | Choice | Directly determines plan realism |
| Kitchen help available | Choice | Full-time cook / part-time / none |
| Meals eaten at home vs outside | Group | |
| Carries tiffin to work | Boolean | ⚠️ Tiffin/dabba culture shapes lunch planning entirely |
| Access to a refrigerator | Boolean | 🟡 Meal-prep feasibility |
| Cooking skill/willingness | Choice | |
| Time available to cook | Choice | |
| Monthly food budget sensitivity | Choice | 🟡 **Flagged** — important for realistic planning, but potentially sensitive. Recommend optional with neutral wording |
| Travel frequency | Choice | |

### 9.14 Section K — Behavioural readiness

Optional. 🟡 Loosely `[INFERRED]` from behaviour-change practice; the specific framing needs validation.

Readiness to change (scale) · confidence in following a plan (scale) · preferred accountability frequency · previous adherence self-assessment · emotional eating triggers · biggest anticipated obstacle.

> **OD-12** — do practitioners actually use structured readiness assessment, or is this academic? Remove if it is not used in real practice; I would rather cut it than pad the assessment.

### 9.15 Section L — Practitioner assessment & notes

Practitioner-only, after review. `[SOURCED — ADIME]`

| Field | Type | Notes |
|---|---|---|
| Nutritional diagnosis / problem statement | Text | ADIME "Diagnosis" step. 🟡 Free text at MVP; standardised terminology is out of scope |
| Estimated energy requirement | Number | 🟡 See OD-13 |
| Macronutrient targets | Group | Protein / carbohydrate / fat — feeds FR-M4-028 |
| Key intervention priorities | Text | |
| Contraindications & cautions | Text | ⚠️ Must be visible to AI drafting as constraints |
| Referral needed | Boolean + text | Non-diagnostic flag for practitioner follow-through |
| Review interval | Choice | Drives check-in scheduling (FR-M8-022) |

> **OD-13 — energy requirement calculation.** Several standard predictive equations exist (Mifflin-St Jeor, Harris-Benedict, and ICMR reference standards for Indian populations). **I do not know which is standard practice among Indian dietitians, and I will not pick one.** Options: (a) practitioner enters manually, (b) system offers a named equation with the practitioner able to override, (c) system offers several and the practitioner chooses. **Recommendation: (b) or (c), never (a) alone** — but the equation and its citation must come from you.

### 9.16 What I need from your review

| Priority | Question | Ref |
|---|---|---|
| 🔴 **Critical** | Which sections are genuinely required *before* a first consultation? The full assessment is too long. | OD-07 |
| 🔴 **Critical** | Correct BMI thresholds for Indian populations, with a citable source. | OD-08 |
| 🔴 **Critical** | Product behaviour for CKD and eating-disorder clients — withhold AI drafting, or warn? | OD-06 |
| 🔴 **Critical** | Which energy-requirement equation, and is it practitioner-overridable? | OD-13 |
| 🟠 High | Is §J (household context) as decisive for adherence as I believe? | — |
| 🟠 High | Is the fasting list (§F) complete and correctly framed? | — |
| 🟠 High | Free text or structured entry for the 24-hour recall? | OD-10 |
| 🟠 High | Structured lab fields at MVP, or documents only? | OD-09 |
| 🟡 Medium | Is the FFQ (§H) realistic digitally, or conversational only? | OD-11 |
| 🟡 Medium | Is behavioural readiness (§K) used in real practice? | OD-12 |
| 🟡 Medium | Which flagged-as-intrusive fields should be cut (marital status, tobacco, budget, screen time)? | — |
| 🟡 Medium | Is the condition list correctly weighted for Indian practice? | — |

**Review format that would help most:** go section by section and mark each field *keep / reword / make optional / remove*, then tell me what is missing. Missing fields matter more than wrong ones — I can only propose what I know to look for.

---

## 10. Non-Functional Requirements

### 10.1 Performance

🟡 Budgets are proposals calibrated to the target environment: **mid-range Android phone on Indian 4G**, not a laptop on office broadband.

| ID | Requirement | Target | Applies to |
|---|---|---|---|
| NFR-001 | Practitioner page load (first meaningful content) | ≤ 2.0 s | Practitioner app |
| NFR-002 | Client portal first load | ≤ 2.5 s on 4G | Client PWA |
| NFR-003 | Client portal repeat load (cached) | ≤ 1.0 s | Client PWA |
| NFR-004 | Food search results | ≤ 300 ms | M4 |
| NFR-005 | Client search results | ≤ 300 ms | M1 |
| NFR-006 | Client timeline (20 most recent events) | ≤ 800 ms | M1 |
| NFR-007 | Plan document generation | ≤ 5 s | M4 |
| NFR-008 | AI draft generation | ≤ 30 s, with progress indication | M5 |
| NFR-009 | Message dispatch after trigger | ≤ 60 s | M8 |
| NFR-010 | Any interactive action feedback | ≤ 100 ms | All |

> **Rationale for NFR-004** — food search is the most repeated interaction in the product. A practitioner building a 7-slot plan may search 20+ times. At 1 s per search the plan builder feels broken regardless of every other optimisation.
> **Rationale for NFR-008** — 30 s is slow by web standards but acceptable for generation *if* progress is visible. It must not block the practitioner from working manually meanwhile (FR-M5-010).

### 10.2 Click budgets

🔒 Countermeasure to V1's *"too many clicks for common workflows."* These are **acceptance criteria, not aspirations** (see AC references).

| ID | Workflow | Budget | Ref |
|---|---|---|---|
| NFR-011 | Create a client (name + mobile) | ≤ 3 interactions | AC-M1-001 |
| NFR-012 | Change a client's lifecycle stage | ≤ 2 interactions | FR-M1-016 |
| NFR-013 | Create a plan from a template | ≤ 3 interactions | AC-M4-007 |
| NFR-014 | Add a food to a plan slot | ≤ 3 interactions | M4 |
| NFR-015 | Create a custom food inline | ≤ 5 interactions | FR-M4-012 |
| NFR-016 | Approve and send a plan | ≤ 2 interactions | M5, M8 |
| NFR-017 | Book an appointment | ≤ 4 interactions | AC-M6-001 |
| NFR-018 | Client logs a full day's adherence | ≤ 6 interactions | AC-M7-002 |
| NFR-019 | Client logs weight | ≤ 3 interactions | AC-M7-003 |

### 10.3 Availability & reliability

| ID | Requirement | Target |
|---|---|---|
| NFR-020 | Monthly uptime for practitioner and client applications | 🟡 99.5% (≈3.6 h/month) — realistic for a solo-operated platform; MUST NOT be publicly promised higher |
| NFR-021 | Recovery Point Objective (maximum acceptable data loss) | ≤ 24 hours |
| NFR-022 | Recovery Time Objective (maximum acceptable downtime) | ≤ 8 hours |
| NFR-023 | Automated daily backups, retained ≥ 30 days | Mandatory |
| NFR-024 | Restore procedure MUST be **tested** before launch, not merely configured | Mandatory |
| NFR-025 | Scheduled message delivery MUST survive an application restart without loss or duplication | Mandatory |
| NFR-026 | Planned maintenance MUST be announced ≥ 24 h ahead and scheduled outside 08:00–21:00 IST | Mandatory |

> ⚠️ **NFR-024 is the one people skip.** An untested backup is not a backup. For a solo operator with no ops team, an unverified restore path is the single most likely cause of catastrophic, unrecoverable failure.

### 10.4 Security

| ID | Requirement |
|---|---|
| NFR-027 | All data in transit MUST be encrypted using current TLS. |
| NFR-028 | All data at rest MUST be encrypted. |
| NFR-029 | Passwords MUST be stored using a current password-hashing algorithm, never reversible encryption. |
| NFR-030 | Tenant isolation MUST be enforced below the application layer (FR-M0-011). |
| NFR-031 | 🔒 The browser MUST NOT query the database directly. All data access MUST pass through the application's own service layer. |
| NFR-032 | Authorization MUST be decided in exactly one place (FR-M0-015). |
| NFR-033 | 🔒 Clinical data, client identifiers and credentials MUST NOT appear in application logs, error reports, traces or analytics. |
| NFR-034 | Secrets MUST NOT be committed to source control. |
| NFR-035 | File access MUST be authorized on every request; unguessable URLs alone are insufficient (FR-M0-038). |
| NFR-036 | Uploads MUST be constrained by type and size and MUST NOT be executable. |
| NFR-037 | All inputs MUST be validated server-side regardless of client-side validation. |
| NFR-038 | Database access MUST use parameterised queries exclusively. |
| NFR-039 | Authentication and authorization failures MUST be rate-limited. |
| NFR-040 | Dependencies MUST be pinned to exact versions and monitored for known vulnerabilities. |
| NFR-041 | Operator console access MUST require 2FA (FR-M0-009). |
| NFR-042 | Session tokens MUST be invalidated on logout and on password change. |
| NFR-043 | Error messages MUST NOT reveal whether an account exists (EC-M0-08). |

> **NFR-031 is the single most important line in this section.** V1's authentication and permission unmaintainability came from having two legitimate data paths — the browser talking directly to the database for some operations, and the application layer for others. That produced two authorization systems that drifted apart. One path, one decision point.
> **NFR-033 matters more here than in most products.** Clinical data in a log file is a data breach that no amount of database encryption prevents, and log aggregation tools are frequently the least protected part of a stack.

### 10.5 Data protection & compliance (DPDP Act 2023)

| ID | Requirement |
|---|---|
| NFR-044 | The system MUST operate on the principle of collecting only data necessary for a stated purpose. |
| NFR-045 | Every purpose for which data is processed MUST have a recorded consent basis (FR-M0-022). |
| NFR-046 | Consent MUST be withdrawable as easily as it is given (FR-M0-024). |
| NFR-047 | The consent ledger MUST be append-only (FR-M0-023). |
| NFR-048 | The system MUST support data access, correction and erasure requests (FR-M0-026, 027). |
| NFR-049 | Personal data MUST have a defined retention period, and MUST be purged when it expires. |
| NFR-050 | 🟡 **PROPOSED** — clients under 18 require verifiable guardian consent, and MUST NOT be subject to behavioural tracking or targeted messaging (FR-M0-028). |
| NFR-051 | The system MUST be able to produce, for any client, a complete record of what data is held and on what consent basis. |
| NFR-052 | Third-party processors MUST be documented, with the data shared and the purpose recorded. |
| NFR-053 | A breach notification procedure MUST exist before launch, covering notification to the Data Protection Board and to affected individuals. |
| NFR-054 | 🔒 Regional compliance rules MUST be expressed as configuration, not embedded in business logic (FR-M0-014). |

> ⚠️ **RISK — my compliance knowledge has a currency limit.** DPDP's implementing rules and Significant Data Fiduciary thresholds were still being operationalised as of my knowledge cutoff. **An Indian privacy lawyer MUST review the consent notice, the retention policy, the minor-consent mechanism and the breach procedure before launch.** I have architected for the Act's principles, which are stable; I cannot certify compliance with its current operational detail. See ASM-10.

### 10.6 Usability & accessibility

| ID | Requirement |
|---|---|
| NFR-055 | 🔒 The client portal MUST be designed mobile-first; the practitioner application MUST be fully usable on a tablet. |
| NFR-056 | 🔒 A single design system MUST exist before feature interfaces are built. No feature may introduce bespoke one-off components. |
| NFR-057 | 🔒 Navigation MUST derive from one declared information architecture, so navigation, breadcrumbs and routes cannot drift apart. |
| NFR-058 | 🔒 Every screen MUST have one primary purpose, with secondary detail progressively disclosed. |
| NFR-059 | Interactive targets MUST be large enough for reliable one-handed thumb use on a phone. |
| NFR-060 | Text MUST meet accepted contrast standards and MUST respect the device's text-size setting. |
| NFR-061 | All interactive elements MUST be keyboard reachable and operable. |
| NFR-062 | Form inputs MUST have programmatically associated labels. |
| NFR-063 | Error messages MUST state what went wrong and what to do next, in plain language, never an error code alone. |
| NFR-064 | Every list and data view MUST have a designed empty state that explains what to do next. |
| NFR-065 | Destructive actions MUST require confirmation and MUST state what will be lost. |
| NFR-066 | Client-facing interfaces MUST be comprehensible to a non-technical adult with no onboarding. |
| NFR-067 | 🎯 **Target: WCAG 2.1 Level AA.** Full conformance cannot be verified without manual testing with assistive technology and expert accessibility review — treat as a design target with known verification limits, not a claim. |

> **NFR-056/057/058 are direct countermeasures** to V1's *"no unified design system," "navigation became inconsistent"* and *"screens became overloaded."* They are sequencing constraints, not style preferences: the design system exists in Phase 6, before feature interfaces in Phase 9.

### 10.7 Maintainability

🔒 These derive from V1's architectural failures and the solo-developer constraint. They are binding.

| ID | Requirement |
|---|---|
| NFR-068 | 🔒 Business logic MUST NOT reside in user interface components. Domain rules live in a service layer; interfaces call services and render. |
| NFR-069 | 🔒 Any domain rule appearing in two places is a defect, not a duplication to tidy later. |
| NFR-070 | 🔒 Modules MUST communicate only through published interfaces. No module may read another module's storage directly. |
| NFR-071 | 🔒 Each module MUST have one clearly stated responsibility, documented. |
| NFR-072 | 🔒 There MUST be exactly one implementation of each of: authorization decisions, portion conversion, nutritional totalling, message scheduling and dispatch, entitlement checks, audit writing, tenant resolution. |
| NFR-073 | Every module MUST have automated tests covering its acceptance criteria. |
| NFR-074 | Every module MUST document its purpose, public interface and dependencies. |
| NFR-075 | The system MUST run identically in development and production configurations. |
| NFR-076 | Database schema changes MUST be applied through versioned, reversible migrations. |
| NFR-077 | 🔒 No microservices, event buses, container orchestration or distributed systems unless a specific demonstrated need is documented and accepted. |
| NFR-078 | Every dependency MUST be justified: a solo developer must be able to debug it, and it must be actively maintained. |
| NFR-079 | Type definitions MUST be shared between backend and frontend by generation from a single source, not maintained in parallel by hand. |

> **NFR-072 is the concrete form of "no duplicate business logic."** It names the specific places V1 would have duplicated. Each of these has exactly one home, and any second implementation is a bug regardless of how convenient it seemed.

### 10.8 Observability

| ID | Requirement |
|---|---|
| NFR-080 | Unhandled errors MUST be captured with enough context to diagnose, and MUST exclude clinical data and identifiers (NFR-033). |
| NFR-081 | Message delivery failures MUST be visible without database access (FR-M11-004). |
| NFR-082 | AI generation failures and latency MUST be monitored. |
| NFR-083 | Per-tenant consumption of metered resources MUST be observable (FR-M11-009). |
| NFR-084 | External service outages (WhatsApp, payment gateway, AI provider) MUST be detected and surfaced to the operator. |
| NFR-085 | 🟡 **PROPOSED** — the operator MUST be alerted on: message failure rate above threshold, AI failure rate above threshold, restore/backup failure, and anomalous per-tenant cost. |
| NFR-086 | Health checks MUST cover the database, storage and each external dependency. |

> **Rationale for NFR-085** — a solo operator cannot watch dashboards. Alerting on the four things that either cost money or break the product silently is the minimum viable operations posture.

### 10.9 Cost constraints

🔒 From Phase 1: fixed infrastructure under ₹5,000/month for the first 6 months.

| ID | Requirement |
|---|---|
| NFR-087 | Fixed infrastructure cost MUST remain under ₹5,000/month at up to 🟡 200 tenants. |
| NFR-088 | Per-tenant variable cost (WhatsApp, AI, gateway fees) MUST be attributable per tenant. |
| NFR-089 | Every variable-cost resource MUST be capped by entitlement, so no tenant can cost more than they pay. |
| NFR-090 | AI generation MUST cache and reuse where semantically safe, to contain cost. |
| NFR-091 | Cost per tenant MUST be reportable, so pricing can be validated against reality. |

> ⚠️ **The ₹5,000 ceiling covers fixed infrastructure only.** Variable per-tenant costs scale with revenue and sit outside it — at 200 practitioners, WhatsApp alone is ₹12,000–18,000/month against roughly ₹3.6 lakh revenue. NFR-089 is what prevents a single heavy user from becoming unprofitable.

### 10.10 Scalability

🔒 Target is 50–200 practitioners at 18 months. **Deliberately not designed for more.**

| ID | Requirement |
|---|---|
| NFR-092 | The system MUST support 200 tenants and 20,000 client records on a single database instance without architectural change. |
| NFR-093 | 🔒 No sharding, read replicas, caching tier or queue infrastructure beyond the simplest thing that works. Revisit at 10× scale. |
| NFR-094 | Scaling limits and the trigger points for revisiting them MUST be documented, so growth is not a surprise. |
| NFR-095 | Background work (message dispatch, document generation, AI calls) MUST be separable from request handling without re-architecture. |

> **Rationale for NFR-093** — designing for 10,000 tenants when 200 is the target is the most common way a solo developer never ships. The correct engineering decision at this scale is to keep it boring, document the limits, and buy the right to change later by keeping module boundaries clean.

### 10.11 Localisation

| ID | Requirement |
|---|---|
| NFR-096 | All user-facing text MUST be externalised from code from the first commit, even though only English ships at launch. |
| NFR-097 | Dates, numbers and currency MUST be formatted for the Indian locale. |
| NFR-098 | Currency MUST be stored with an explicit currency code, never as a bare number. |
| NFR-099 | All timestamps MUST be stored unambiguously with timezone, and displayed in the user's timezone (FR-M6-009). |
| NFR-100 | Mobile numbers MUST be stored in international format. |
| NFR-101 | The system MUST support Unicode throughout, including Indic scripts in names and free text. |

> **Rationale for NFR-096/098/099** — India is a single timezone with a single currency, so none of this matters at launch. All three are catastrophically expensive to retrofit once real data exists, and the roadmap has GCC and UK in Phase 2 and 3. This is the cheapest insurance in the document.

---

## 11. Success Metrics

### 11.1 What "working" means

| Metric | Definition | Target |
|---|---|---|
| **Activation** | Practitioner creates a client AND issues a plan within 7 days of signup | 🟡 40% of signups |
| **Time to first plan** | Signup → first plan delivered | 🟡 ≤ 48 hours (median) |
| **Core loop completion** | Practitioners completing all six loop steps within 30 days | 🟡 60% of activated |
| **Weekly active practitioners** | Practitioners issuing a plan or logging a consultation in a week | 🟡 70% of paying |
| **Client engagement** | Clients logging adherence or weight at least weekly | 🟡 50% of active clients |
| **Plan creation time** | Median time to build a plan from a template | 🟡 ≤ 5 min |
| **AI draft acceptance** | Drafts approved with edits (rather than discarded) | 🟡 70% |
| **Food search success** | Searches returning a used result | 🟡 90% |
| **Paid conversion** | Trial/free → paying | 🟡 25% |
| **Monthly churn** | Paying practitioners lost per month | 🟡 < 5% |

> All targets are 🟡 **proposals without a baseline.** They are set so that the product has a defined notion of failure. **Revisit after 20 paying customers with real data** — at that point these become measurements rather than guesses.

### 11.2 The single metric that matters most

**Time to first delivered plan.**

It is the only metric that proves the core promise. A practitioner who delivers a plan through WellnessCRM in their first session has experienced the time saving; one who has not, never will. Every other metric follows from it, and if it is bad, nothing else can compensate.

### 11.3 Leading indicators of failure

| Signal | Interpretation | Response |
|---|---|---|
| High food-search failure rate | Food database does not cover real practice | Curate against the failed-search log (FR-M4-014) — this is why that log is MVP |
| AI drafts discarded rather than edited | Drafts are not credible | Fix grounding and prompt; do not add features |
| Plans built from blank rather than templates | Template value not understood, or templates hard to create | Onboarding and interface problem |
| Clients not opening plan links | WhatsApp delivery or portal experience failing | Highest-severity signal — the engagement loop is broken |
| Practitioners exporting PDFs but not using the portal | We are a document generator, not a platform | Re-examine the client-side value proposition |
| Signup without a first client | Onboarding failure | Empty state and first-run experience |

### 11.4 Anti-metrics — deliberately not optimised

| Not optimising | Why |
|---|---|
| Total foods in the database | Coverage of *used* foods matters; total count is vanity |
| Number of features shipped | Directly opposed to the solo-developer constraint |
| Time spent in the product | We are selling time back, not engagement |
| Messages sent | Each one costs money and risks annoying clients |
| AI generations performed | A generation that gets discarded is a cost, not a success |

---

## 12. Out of Scope

### 12.1 Out of scope for V2 entirely

🔒 Decided in Phase 1. Reopening any of these requires an explicit decision, not a pull request.

| Excluded | Reason |
|---|---|
| Workout and exercise programming | Different buyer and data model (Trainerize's category) |
| Sales pipeline automation and marketing suite | Our buyer has 5–15 enquiries a month (HubSpot's category) |
| Insurance claims, superbills, coding | Irrelevant in India's cash-pay market |
| Embedded video calling and recording | Practitioners already use Zoom, Meet or WhatsApp calls |
| In-app chat between practitioner and client | WhatsApp is the channel; a second inbox splits the relationship |
| Native iOS/Android applications | PWA covers the need until PMF is proven |
| Ayurveda-specific clinical models | Genuinely different domain, not a renamed assessment |
| Group programs, cohorts, batch scheduling | 1:1 is the proven wedge |
| E-commerce, supplement sales, marketplace | Different business entirely |
| Practitioner directory or lead marketplace | Two-sided marketplace is a different company |
| Wearable and device integration | Phase 3 at the earliest |
| Public API and webhooks | Phase 3; no launch customer needs it |
| Multi-region infrastructure | India-only at launch |
| Custom report builder | Fixed views suffice at this scale |
| White-label or reseller capability | No demand established |
| Full HIPAA compliance program | US is Phase 4 of the market roadmap |

### 12.2 Explicitly deferred, with the reason

| Deferred to | Item | Why not now |
|---|---|---|
| Phase 2 | Automated recurring collection | Manual collection is minutes/month below 20 customers |
| Phase 2 | Kanban pipeline board | Leads and clients share one record; a view is cheap to add later |
| Phase 2 | Two-way WhatsApp inbox | Replies land in the practitioner's own WhatsApp at MVP |
| Phase 2 | Google Calendar sync | Practitioners can maintain both briefly |
| Phase 2 | Client self-booking | Manual booking works at 40 clients |
| Phase 2 | Web push notifications | WhatsApp is more reliable in India |
| Phase 2 | Practitioner recipe builder | Seed recipes plus reusable meals cover most need |
| Phase 2 | Micronutrient data | Macros are sufficient for the majority of plans |
| Phase 2 | Operator impersonation | Most dangerous capability; wait until audit is proven in production |
| Phase 3 | Multi-practitioner clinic interface | Data model supports it; the interface can wait |
| Phase 3 | Regional language interface | English is workable for the launch persona |

### 12.3 Never in scope

| Never | Reason |
|---|---|
| Auto-sending AI-generated plans without practitioner review | Clinical safety. No tier, no setting, no customer request changes this |
| Diagnosing conditions or interpreting lab values | We are not a medical device and will not behave like one |
| Prescribing or advising on medication | Outside practitioner and platform scope |
| Selling or sharing client health data | Ethical and legal absolute |
| Reading clinical data for product analytics | Aggregate, non-identifying metrics only |
| Holding client data hostage over unpaid subscriptions | Ethically wrong and a probable DPDP violation |

---

## 13. Assumptions Register

Every assumption that could invalidate part of this PRD. 🔴 = must be validated before or during implementation.

| ID | Assumption | Impact if wrong | Validation |
|---|---|---|---|
| 🔴 **ASM-01** | Indian nutritionists will pay ₹799–1,799/month for this | Pricing and unit economics collapse | Interview 10 practitioners; test willingness to pay before launch |
| 🔴 **ASM-02** | Diet plan creation is genuinely the biggest time sink | The wedge is wrong; we built the wrong module first | Observe 5 practitioners building a plan; time it |
| 🔴 **ASM-03** | WhatsApp Business API is approvable and affordable at our volume | The entire delivery model fails | Begin Meta verification week 1; verify current India rates |
| 🔴 **ASM-04** | Indian BMI thresholds differ from WHO standards and we can source current values | Clinically incorrect output in a clinical tool | Practitioner + authoritative Indian clinical source (OD-08) |
| 🔴 **ASM-05** | The proposed assessment reflects real practice | Practitioners reject it as academic; AI grounding is poor | §9 review (this document's primary open question) |
| 🔴 **ASM-06** | IFCT 2017 is usable as our composition base, with acceptable licensing | Food database — the moat — has no reliable base | Confirm licensing and completeness for our food list |
| 🔴 **ASM-07** | Clients will use a PWA rather than needing a native app | Engagement loop fails; retention story collapses | Measure portal open rate from WhatsApp links with first cohort |
| 🔴 **ASM-08** | Current WhatsApp and AI per-unit costs support the proposed quotas | Margins are wrong; quotas unaffordable | Verify against current provider pricing before finalising plans |
| 🟠 ASM-09 | GST treatment for SaaS to Indian practitioners is straightforward | Invoicing non-compliant from the first rupee | Accountant confirmation (place of supply, rate) |
| 🔴 **ASM-10** | DPDP obligations as designed here match current operational rules | Compliance exposure at launch | Indian privacy lawyer review before launch |
| 🟠 ASM-11 | Practitioners will trust AI drafting if they retain approval | The differentiator is unusable | Demo to 5 practitioners; observe reaction |
| 🟠 ASM-12 | Stage-based active-client counting is understood and accepted | Billing disputes and confusion | Explain to 5 practitioners; check comprehension |
| 🟠 ASM-13 | A curated seed food set can cover the majority of real plans | Practitioners hit gaps immediately and leave | Build 10 real plans against the seed set before launch |
| 🟠 ASM-14 | 4–6 months solo is achievable for this MVP scope | Timeline slips; motivation and runway suffer | Phase 7/8 estimation against this scope |
| 🟡 ASM-15 | Household context (§9.13) materially affects adherence | An entire assessment section is noise | Practitioner review |
| 🟡 ASM-16 | Practitioners will build and reuse templates | The compounding switching cost never accumulates | Measure template creation in first cohort |
| 🟡 ASM-17 | Magic-link access is acceptable to clients and practitioners | Client access friction, or perceived insecurity | First cohort feedback |
| 🟡 ASM-18 | 99.5% uptime is acceptable to practitioners | Trust damage from outages | Observe reaction to first incident |
| 🟡 ASM-19 | Practitioners accept that MVP replies land in their own WhatsApp | Perceived as incomplete | Communicate explicitly during onboarding |
| 🟡 ASM-20 | Zoom/Meet link-out is sufficient rather than embedded video | Competitive gap in demos | Track how often it is requested |

### 13.1 The four assumptions to validate first

If ASM-01, ASM-02, ASM-03 or ASM-05 is wrong, **significant parts of this PRD are wrong.** They are cheap to test with conversations and expensive to discover in month five.

**Recommendation: validate all four before Phase 3 begins.** Ten practitioner conversations would materially de-risk this document, and would cost days rather than months.

---

## 14. Open Decisions

Decisions I need from you. Each has my recommendation; none is blocking Phase 3 unless marked.

| ID | Decision | My recommendation | Blocks |
|---|---|---|---|
| **OD-01** | Are the 7 lifecycle stages right? Is `Contacted` meaningful? | Validate with practitioners; I suspect `Contacted` may be noise | Phase 4 |
| **OD-02** | Can practitioners customise the assessment at MVP? | **No.** Largest complexity multiplier in M3; hurts AI grounding | Phase 4 |
| **OD-03** | Composition of the food database seed set, and katori reference values | Practitioner-led; prioritise prepared dishes over raw ingredients | Phase 4 |
| **OD-04** | Final pricing and quota values | Validate ASM-01 first; do not launch on my modelled numbers alone | Phase 8 |
| **OD-05** | Verifiable parental consent mechanism for under-18 clients | Needs legal advice — ⚠️ **launch blocker if unresolved** | Launch |
| **OD-06** | Product behaviour for CKD and eating-disorder clients | Withhold AI drafting; practitioner-only. **Needs clinical confirmation** | Phase 5 |
| **OD-07** | Which assessment sections are required pre-consultation? | A, B, C, F, G, J (~14 min); rest practitioner-driven | Phase 6 |
| **OD-08** | Indian BMI thresholds and their citable source | Practitioner + authoritative source; cite in-product | Phase 4 |
| **OD-09** | Structured lab fields at MVP, or documents only? | Structure a small subset (HbA1c, TSH, Hb, Vit D, B12) | Phase 4 |
| **OD-10** | 24-hour recall: free text or structured food entry? | **Free text** at MVP; practitioner structures during consultation | Phase 4 |
| **OD-11** | Is a digital FFQ realistic? | Optional and practitioner-triggered, not in the default client flow | Phase 6 |
| **OD-12** | Is behavioural readiness used in real practice? | Cut it unless practitioners confirm they use it | Phase 6 |
| **OD-13** | Which energy-requirement equation? | Named equation with practitioner override; **the choice must come from you** | Phase 4 |
| **OD-14** | Which flagged-as-intrusive assessment fields to cut? | Cut screen time; make marital status, tobacco and budget optional | Phase 6 |

---

## Appendix A — Traceability: V1 failures → countermeasures

Every V1 failure and where this PRD addresses it.

| V1 failure | Countermeasure | Where |
|---|---|---|
| Design evolved while building | 11-phase gate; architecture before features | Process; NFR-071 |
| Modules became tightly coupled | Modules communicate only via published interfaces | NFR-070; M8.3 |
| **Business logic duplicated across screens** | Logic in services, never in UI; single implementation of 7 named concerns | **NFR-068, 069, 072** |
| Auth, routing, permissions unmaintainable | One authorization decision point; browser never queries the database | FR-M0-015; **NFR-031, 032** |
| No unified design system | Design system precedes feature interfaces | NFR-056 |
| Navigation inconsistent | One declared information architecture drives navigation | NFR-057 |
| Too many clicks | Click budgets as acceptance criteria | NFR-011–019 |
| Actions hard to discover | One primary purpose per screen; progressive disclosure | NFR-058 |
| Client experience not engaging | PWA design principles; WhatsApp-driven entry; 60-second rule | M7.3 |
| Screens overloaded | One primary purpose per screen | NFR-058 |

## Appendix B — Long-lead external dependencies

⏳ **Start in week 1, independent of engineering.** Each of these can silently consume a month.

| Dependency | Lead time | Risk | Blocks |
|---|---|---|---|
| **Meta Business Verification** | 2–6 weeks, can be rejected | 🔴 High | All of M8 — the delivery layer |
| **WhatsApp template approval** | Days–weeks each, per template | 🟠 Medium | Specific message types |
| **Razorpay merchant KYC** | Days–weeks | 🟠 Medium | Any payment collection |
| **IFCT 2017 licensing confirmation** | Unknown | 🔴 High | The food database moat |
| **Indian privacy lawyer review** | Weeks to schedule | 🔴 High | Launch (ASM-10, OD-05) |
| **Practitioner design partners** | Ongoing | 🔴 High | ASM-01, 02, 05; the §9 review |
| **Accountant — GST treatment** | Days | 🟡 Low | FR-M10-011 |

## Appendix C — Glossary

| Term | Meaning |
|---|---|
| **Active client** | A client whose lifecycle stage is `Active`. The billing unit (M1.5) |
| **Tenant** | One practitioner or clinic account. The isolation boundary |
| **Practitioner** | The paying user — dietitian, nutritionist or coach |
| **Client** | The practitioner's client. A user, never the buyer |
| **Operator** | Platform staff (initially the founder) using the Super Admin console |
| **Plan** | A diet plan issued to a specific client |
| **Template** | A reusable plan structure owned by a practitioner |
| **Meal** | A reusable named set of foods with portions |
| **Household measure** | Katori, cup, roti, glass, spoon — mapped to grams per food |
| **Curated food** | Platform-maintained food entry, visible to all tenants |
| **Custom food** | Practitioner-created entry, private to their tenant |
| **Draft** | An AI-generated plan awaiting practitioner approval. Cannot be delivered |
| **Consent ledger** | Append-only record of every consent grant, change and withdrawal |
| **DPDP** | Digital Personal Data Protection Act, 2023 (India) |
| **PHI** | Protected Health Information (US/HIPAA term; used here for clinical data generally) |
| **IFCT** | Indian Food Composition Tables, 2017 (NIN Hyderabad) |
| **ADIME** | Assessment, Diagnosis, Intervention, Monitoring, Evaluation |
| **ABCD** | Anthropometric, Biochemical, Clinical, Dietary |
| **PWA** | Progressive Web App |

---

**END OF DOCUMENT**

*Phase 2 of 11 complete. Awaiting review before Phase 3 — Architecture.*
