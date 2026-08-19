/**
 * The public enquiry form's API layer — S2 Slice H, API §11.1/§11.2.
 *
 * 🔒 **The only unauthenticated surface in this app** (Arch §15.3). Both calls
 * are anonymous by design: a prospect has no account, and getting one is what
 * the form exists to start (FR-M2-002).
 *
 * 🔒 **Nothing here is scoped by anything the browser chose.** The tenant is
 * named by the slug in the URL and re-resolved server-side; there is no tenant
 * id to send, and `PublicFormResponse` deliberately does not carry one. See
 * `adopt_tenant_scope` in the backend for why that distinction is the whole
 * safety argument.
 *
 * ⚠️ **A 202 says nothing about what happened.** The response is identical for a
 * new prospect, a returning one, and a submission dropped as spam (EC-M2-02,
 * EC-M2-03). Nothing in this module may branch on it, and there is nothing in
 * the payload to branch on.
 */

import { createApiClient } from '@wellnesscrm/api-client'
import type { components } from '@wellnesscrm/api-client'

export type PublicForm = components['schemas']['PublicFormResponse']
export type PublicConsentNotice = components['schemas']['PublicConsentNotice']
export type EnquirySubmitRequest = components['schemas']['EnquirySubmitRequest']
export type EnquirySubmitResponse = components['schemas']['EnquirySubmitResponse']

const api = createApiClient()

/**
 * The form definition and the consent notice to display — API §11.1.
 *
 * 🔒 The notice **body** comes back with it, not just an identifier: DPDP
 * requires consent against text the person actually saw (NFR-051), so the words
 * on screen and the version recorded in the ledger must come from one response.
 *
 * ⚠️ 404 for an unknown slug, a paused form, or a suspended tenant — one neutral
 * refusal for all three (EC-M2-07). The caller must not try to tell them apart.
 */
export async function fetchPublicForm(
  tenantSlug: string,
  signal?: AbortSignal,
): Promise<PublicForm> {
  return api.request('get', '/api/v1/public/forms/{tenant_slug}', {
    path: { tenant_slug: tenantSlug },
    ...(signal ? { signal } : {}),
  })
}

/**
 * Submit an enquiry — API §11.2, FR-M2-005.
 *
 * 🔒 `consent_notice_id` is the notice the prospect was *shown*, echoed from
 * `fetchPublicForm`. The server refuses it if the notice has since moved on
 * rather than recording agreement to text nobody displayed (EC-M2-04).
 */
export async function submitEnquiry(
  tenantSlug: string,
  body: EnquirySubmitRequest,
): Promise<EnquirySubmitResponse> {
  return api.request('post', '/api/v1/public/forms/{tenant_slug}/submit', {
    path: { tenant_slug: tenantSlug },
    body,
  })
}
