/**
 * The client timeline — FR-M1-018, FR-M1-019.
 *
 * 🔒 **Arch §4.4 / NFR-068 — the layer allowed to talk to the API.** R8 fails
 * the build if anything under `components/` imports this or calls `fetch`.
 *
 * ⚠️ **A 404 here means "not yours" as often as "not found".** API §5.4 makes an
 * unassigned client indistinguishable from a missing one, so the UI must not
 * claim the client was deleted.
 */

import { createApiClient } from '@wellnesscrm/api-client'
import type { components } from '@wellnesscrm/api-client'

export type TimelineEntry = components['schemas']['TimelineEntryResponse']
export type TimelineEventType = components['schemas']['TimelineEventType']
export type TimelineFilter = components['schemas']['TimelineFilterResponse']
export type TimelinePage = components['schemas']['TimelineResponse']

const api = createApiClient()

/**
 * One page of a client's timeline — newest first.
 *
 * 🔒 Cursor-paginated (ADR-A05). The cursor is opaque and must be passed back
 * verbatim: it encodes `(occurred_at, id)`, and the id half is what keeps events
 * sharing a timestamp from straddling a page boundary.
 */
export async function fetchTimeline(
  clientId: string,
  options: { cursor?: string; eventTypes?: readonly TimelineEventType[] } = {},
  signal?: AbortSignal,
): Promise<TimelinePage> {
  return api.request('get', '/api/v1/app/clients/{client_id}/timeline', {
    path: { client_id: clientId },
    query: {
      ...(options.cursor !== undefined ? { cursor: options.cursor } : {}),
      // ⚠️ An array becomes repeated keys, which is API §6.2's OR-within-a-field
      // encoding and what the backend's `list[TimelineEventType]` expects.
      // Omitted entirely when empty: "no filters ticked" must mean everything,
      // not nothing.
      ...(options.eventTypes && options.eventTypes.length > 0
        ? { event_type: options.eventTypes }
        : {}),
    },
    ...(signal ? { signal } : {}),
  })
}

/**
 * The event types worth offering as filters — FR-M1-019.
 *
 * 🔒 Fetched rather than hardcoded. The enum carries S3–S6 values nothing can
 * produce yet; the server decides which are real, so the filter list grows as
 * producers land without a frontend change.
 */
export async function fetchTimelineFilters(
  clientId: string,
  signal?: AbortSignal,
): Promise<TimelineFilter[]> {
  return api.request('get', '/api/v1/app/clients/{client_id}/timeline/filters', {
    path: { client_id: clientId },
    ...(signal ? { signal } : {}),
  })
}
