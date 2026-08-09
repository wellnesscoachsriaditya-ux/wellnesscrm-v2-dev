/**
 * The client list — search, filters and cursor pagination (S2 Slice E).
 *
 * 🔒 **Arch §4.4 / NFR-068 — the layer allowed to talk to the API.** R8 fails
 * the build if anything under `components/` imports this or calls `fetch`.
 *
 * 🔒 **Scoping is the server's job, not a query parameter.** A practitioner sees
 * their own and their assigned clients; an owner sees the tenant (AC-M1-006,
 * FR-M0-017). That decision is a WHERE clause inside `list_clients` — there is
 * no `?mine=true` to send and adding one would imply the client could ask for
 * more.
 *
 * ⚠️ **The cursor is opaque and must be passed back verbatim.** It encodes
 * `(sort_value, client_id)` — the id half is what stops clients sharing an
 * `updated_at` from straddling a page boundary, which a bulk reassignment makes
 * routine rather than theoretical.
 */

import { createApiClient } from '@wellnesscrm/api-client'
import type { components } from '@wellnesscrm/api-client'

export type ClientListItem = components['schemas']['ClientListItemResponse']
export type ClientPage = components['schemas']['ClientListResponse']
export type ClientTagSummary = components['schemas']['ClientTagSummary']
export type ArchivedFilter = components['schemas']['ArchivedFilter']
export type ClientSortOption = components['schemas']['ClientSortOption']

/**
 * 🔒 The orderings the server will accept — API §6.3's allowlist.
 *
 * Taken from the generated schema rather than hand-listed: every value is
 * index-backed (migration 0013), and a sort added or renamed server-side becomes
 * a compile error here instead of a dropdown option that silently falls back.
 */
export type ClientSort = components['schemas']['ClientSort']
const api = createApiClient()

/** What narrows the list. Every field optional — the empty object is "everyone". */
export interface ClientListQuery {
  /** Free text: name, email, or the last digits of a mobile (FR-M1-021). */
  search?: string
  /** OR within the field — API §6.2. */
  stages?: readonly string[]
  /** 🔒 AND across tags: picking a second tag must *narrow* the caseload. */
  tagIds?: readonly string[]
  ownerUserIds?: readonly string[]
  archived?: ArchivedFilter
  /** `name`, `recent_activity` or `created`; prefix `-` for descending. */
  sort?: string
  cursor?: string
  limit?: number
  /** 🔒 Off by default — a COUNT on every keystroke is what makes search slow. */
  includeTotal?: boolean
}

/**
 * One page of the client list — FR-M1-021/022, NFR-005 (≤300 ms).
 *
 * ⚠️ Empty arrays are omitted rather than sent. A `stage=` with no value would
 * be "match nothing" to a query builder and "match everything" to a reader; not
 * sending the key at all is unambiguous, and matches what the backend treats as
 * an absent filter.
 */
export async function fetchClients(
  query: ClientListQuery = {},
  signal?: AbortSignal,
): Promise<ClientPage> {
  return api.request('get', '/api/v1/app/clients', {
    query: {
      ...(query.search ? { q: query.search } : {}),
      ...(query.stages && query.stages.length > 0 ? { stage: query.stages } : {}),
      ...(query.tagIds && query.tagIds.length > 0 ? { tag_id: query.tagIds } : {}),
      ...(query.ownerUserIds && query.ownerUserIds.length > 0
        ? { owner_user_id: query.ownerUserIds }
        : {}),
      ...(query.archived ? { archived: query.archived } : {}),
      ...(query.sort ? { sort: query.sort } : {}),
      ...(query.cursor ? { cursor: query.cursor } : {}),
      ...(query.limit !== undefined ? { limit: query.limit } : {}),
      ...(query.includeTotal ? { include_total: true } : {}),
    },
    ...(signal ? { signal } : {}),
  })
}

/**
 * The orderings to offer, with their labels — API §6.3.
 *
 * 🔒 Fetched rather than hardcoded, for the same reason the timeline's filters
 * are: the server decides which sorts are index-backed, so the dropdown cannot
 * offer an ordering the database would have to sort in memory.
 */
export async function fetchSortOptions(signal?: AbortSignal): Promise<ClientSortOption[]> {
  return api.request('get', '/api/v1/app/clients/sort-options', {
    ...(signal ? { signal } : {}),
  })
}

/**
 * Hand several clients to one practitioner — EC-M1-04.
 *
 * 🔒 Owner-only, and **all or nothing**: one transaction, so a failure partway
 * rolls the whole batch back. A half-completed handover would split a caseload
 * between two practitioners with no record of the intent.
 *
 * ⚠️ `moved` can be lower than the number submitted — clients already owned by
 * the target are skipped, which in an overlapping selection is correct rather
 * than an error. The caller should report what moved, not treat it as failure.
 */
export async function reassignClients(
  clientIds: readonly string[],
  ownerUserId: string,
): Promise<components['schemas']['BulkReassignResponse']> {
  return api.request('post', '/api/v1/app/clients/reassign', {
    body: { client_ids: [...clientIds], owner_user_id: ownerUserId },
  })
}
