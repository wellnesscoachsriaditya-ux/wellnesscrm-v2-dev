/**
 * Client lifecycle — the data access behind the stage, archive and restore
 * controls (S2 Slice B).
 *
 * 🔒 **Arch §4.4 / NFR-068 — this is the layer allowed to talk to the API.**
 * `check_boundaries.py` R8 fails the build if anything under `components/`
 * imports `@wellnesscrm/api-client` or calls `fetch`. Components render what
 * they are given; hooks fetch and decide.
 *
 * ⚠️ 🔒 **No `If-Match` on any of these, matching the API.** A precondition
 * protects against a *lost update* — two people editing one field where the
 * second save silently discards the first. A concurrent stage change loses
 * nothing: both transitions land in `client_stage_history` with their actor and
 * timestamp, and the final stage is one a practitioner actually asked for.
 * Requiring a token here would add a failed request and a reload to the
 * two-interaction budget NFR-012 sets, to prevent a loss that does not occur.
 */

import { createApiClient } from '@wellnesscrm/api-client'
import type { components } from '@wellnesscrm/api-client'

export type Client = components['schemas']['ClientResponse']
export type ClientStage = Client['stage']

/**
 * The stages a practitioner may move a client *to*.
 *
 * 🔒 Derived from the generated request type, not hand-listed. `archived` is
 * absent from the API's `to_stage` union because archiving is a separate action
 * (FR-M1-010), and taking the list from the contract means a stage added or
 * removed on the server cannot leave a stale option in a dropdown — it becomes a
 * compile error here instead.
 */
export type SelectableStage =
  components['schemas']['StageChangeRequest']['to_stage']

const api = createApiClient()

export async function fetchClient(clientId: string, signal?: AbortSignal): Promise<Client> {
  return api.request('get', '/api/v1/app/clients/{client_id}', {
    path: { client_id: clientId },
    ...(signal ? { signal } : {}),
  })
}

export async function createClient(body: {
  full_name: string
  mobile?: string | null
  email?: string | null
  stage: SelectableStage
  city?: string | null
}): Promise<Client> {
  return api.request('post', '/api/v1/app/clients', { body })
}

/**
 * Move a client between lifecycle stages — ADR-A06.
 *
 * 🔒 Throws `ApiError` with `type: 'entitlement_exceeded'` (402) when the move
 * enters `active` at the plan's ceiling (FR-M1-002). The envelope carries the
 * limit, the usage, the plan and the upgrade path, so the caller renders the
 * refusal without a second request.
 */
export async function changeStage(
  clientId: string,
  toStage: SelectableStage,
  reason?: string,
): Promise<Client> {
  return api.request('post', '/api/v1/app/clients/{client_id}/stage', {
    path: { client_id: clientId },
    body: { to_stage: toStage, ...(reason ? { reason } : {}) },
  })
}

/** Soft-delete — FR-M1-010. Frees the entitlement slot if they were active. */
export async function archiveClient(clientId: string): Promise<Client> {
  return api.request('post', '/api/v1/app/clients/{client_id}/archive', {
    path: { client_id: clientId },
  })
}

/**
 * Un-archive — EC-M1-02, returning them to the stage they were archived at.
 *
 * 🔒 Can also throw a 402: archiving an `active` client frees a slot, so
 * restoring takes one back (EC-M1-06).
 */
export async function restoreClient(clientId: string): Promise<Client> {
  return api.request('post', '/api/v1/app/clients/{client_id}/restore', {
    path: { client_id: clientId },
  })
}
