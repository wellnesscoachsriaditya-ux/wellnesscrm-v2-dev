/**
 * Enquiries — the lead workflow's API layer (S2 Slice F).
 *
 * 🔒 **Arch §4.4 / NFR-068 — the layer allowed to talk to the API.** R8 fails the
 * build if anything under `components/` imports this or calls `fetch`.
 *
 * 🔒 **Scoping is the server's job, not a query parameter.** A practitioner sees
 * enquiries for clients they own or are assigned to; an owner sees the tenant
 * (AC-M1-006, FR-M0-017). That is a WHERE clause inside `list_enquiries` — there
 * is no `?mine=true` to send, and adding one would imply the client could ask
 * for more.
 *
 * ⚠️ **`age_hours` and `is_ageing` are server-computed** (Principle 3). The
 * browser renders them and never derives them: a client-side calculation would
 * disagree across a timezone or a clock skew, and the number deciding which
 * prospect gets called next would differ per device.
 */

import { createApiClient } from '@wellnesscrm/api-client'
import type { components } from '@wellnesscrm/api-client'

export type EnquiryListItem = components['schemas']['EnquiryListItemResponse']
export type EnquiryPage = components['schemas']['EnquiryListResponse']
export type EnquiryForm = components['schemas']['EnquiryFormResponse']

/**
 * 🔒 The channel vocabulary the server will accept — FR-M2-009.
 *
 * Taken from the generated schema rather than hand-listed, so a channel added or
 * renamed server-side becomes a compile error here instead of a filter that
 * silently matches nothing.
 */
export type LeadSource = components['schemas']['LeadSource']

const api = createApiClient()

export interface EnquiryListQuery {
  /** FR-M2-009 attribution — which channel produced the enquiry. */
  source?: LeadSource
  cursor?: string
  limit?: number
  /** 🔒 Off by default — a COUNT on every load is the expensive half. */
  includeTotal?: boolean
}

/**
 * Every enquiry, newest first — API §7.2.
 *
 * ⚠️ The archive, not the work queue. This answers "did she ever contact us?";
 * `fetchNeedsResponse` answers "who is waiting?" and orders the opposite way.
 */
export async function fetchEnquiries(
  query: EnquiryListQuery = {},
  signal?: AbortSignal,
): Promise<EnquiryPage> {
  return api.request('get', '/api/v1/app/enquiries', {
    query: {
      ...(query.source ? { source: query.source } : {}),
      ...(query.cursor ? { cursor: query.cursor } : {}),
      ...(query.limit !== undefined ? { limit: query.limit } : {}),
      ...(query.includeTotal ? { include_total: true } : {}),
    },
    ...(signal ? { signal } : {}),
  })
}

/**
 * 🔒 The work queue — FR-M2-011, AC-M2-005. **Oldest first.**
 *
 * US-M2-03 is "so none are forgotten", and M2.2 prices a forgotten enquiry at
 * ₹2,500–4,000/month of recurring revenue. The ordering is the feature: the
 * oldest unanswered enquiry is the most urgent one, and a queue that buried it
 * under today's arrivals would be the failure this view exists to prevent.
 */
export async function fetchNeedsResponse(
  query: Omit<EnquiryListQuery, 'source'> = {},
  signal?: AbortSignal,
): Promise<EnquiryPage> {
  return api.request('get', '/api/v1/app/enquiries/needs-response', {
    query: {
      ...(query.cursor ? { cursor: query.cursor } : {}),
      ...(query.limit !== undefined ? { limit: query.limit } : {}),
      ...(query.includeTotal ? { include_total: true } : {}),
    },
    ...(signal ? { signal } : {}),
  })
}

/**
 * Clear an enquiry from the needs-response queue — FR-M2-011.
 *
 * 🔒 Idempotent server-side: a second call keeps the original responder and
 * timestamp, so a double tap cannot rewrite who handled it.
 */
export async function markResponded(submissionId: string): Promise<void> {
  await api.request('post', '/api/v1/app/enquiries/{submission_id}/respond', {
    path: { submission_id: submissionId },
  })
}

/**
 * The tenant's enquiry form — API §7.2, FR-M2-001.
 *
 * ⚠️ Returns a list though MVP has exactly one. FR-M2-012 makes several per
 * tenant a Phase 2 feature, and a list now means the type does not change when
 * the second form arrives.
 */
export async function fetchEnquiryForms(signal?: AbortSignal): Promise<EnquiryForm[]> {
  return api.request('get', '/api/v1/app/enquiry-forms', {
    ...(signal ? { signal } : {}),
  })
}

export interface EnquiryFormPatch {
  title?: string
  /** ⚠️ `null` clears the introduction; omitting the key leaves it alone. */
  intro_text?: string | null
  is_active?: boolean
}

/** Edit the form's heading, introduction, or whether it accepts enquiries. */
export async function updateEnquiryForm(
  formId: string,
  patch: EnquiryFormPatch,
): Promise<EnquiryForm> {
  return api.request('patch', '/api/v1/app/enquiry-forms/{form_id}', {
    path: { form_id: formId },
    body: patch,
  })
}
