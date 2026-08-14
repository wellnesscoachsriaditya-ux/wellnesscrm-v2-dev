/**
 * The messaging engine — PRD M8, S5.
 *
 * 🔒 **Arch §4.4 / NFR-068 — the layer allowed to talk to the API.** R8 fails
 * the build if anything under `components/` imports this or calls `fetch`.
 *
 * ⚠️ **A 404 on a client-bound route means "not yours" as often as "not
 * found"** (API §5.4). An unassigned client is indistinguishable from a missing
 * one, so the UI must not claim the client was deleted.
 *
 * ⚠️ **Nothing here sends a message.** `queueMessage` creates intent; the
 * engine decides at dispatch whether it goes out (FR-M8-001). A UI that said
 * "sent" on a 200 would be claiming something the server never promised — see
 * the wording in `ClientMessagesPanel`.
 */

import { createApiClient } from '@wellnesscrm/api-client'
import type { components } from '@wellnesscrm/api-client'

export type Dispatch = components['schemas']['DispatchResponse']
export type MessageHistory = components['schemas']['MessageHistoryResponse']
export type PendingMessage = components['schemas']['PendingMessageResponse']
export type ScheduledMessage = components['schemas']['ScheduledMessageResponse']
export type MessageTemplate = components['schemas']['TemplateResponse']
export type MessagePreview = components['schemas']['PreviewResponse']
export type MessagePreference = components['schemas']['PreferenceResponse']
export type CheckinSchedule = components['schemas']['CheckinScheduleResponse']
export type CheckinFrequency = components['schemas']['CheckinFrequency']

const api = createApiClient()

/**
 * One page of a client's delivery log — FR-M8-011, newest first.
 *
 * 🔒 Every *attempt*, including failures and retries. A history that showed only
 * successes would hide exactly the case AC-M8-007 exists for.
 */
export async function fetchMessageHistory(
  clientId: string,
  options: { cursor?: string } = {},
  signal?: AbortSignal,
): Promise<MessageHistory> {
  return api.request('get', '/api/v1/app/clients/{client_id}/messages', {
    path: { client_id: clientId },
    ...(options.cursor !== undefined ? { query: { cursor: options.cursor } } : {}),
    ...(signal ? { signal } : {}),
  })
}

/** What is queued but not yet sent — FR-M8-028. */
export async function fetchPendingMessages(
  clientId: string,
  signal?: AbortSignal,
): Promise<PendingMessage[]> {
  return api.request('get', '/api/v1/app/clients/{client_id}/messages/pending', {
    path: { client_id: clientId },
    ...(signal ? { signal } : {}),
  })
}

/**
 * Queue a message for a client.
 *
 * ⚠️ The recipient's name and the portal link are filled in server-side and
 * cannot be passed here — a caller-supplied `client_name` would let one client's
 * name reach another under the practitioner's own name.
 */
export async function queueMessage(
  clientId: string,
  templateCode: string,
  variables: Record<string, string> = {},
): Promise<ScheduledMessage> {
  return api.request('post', '/api/v1/app/clients/{client_id}/messages', {
    path: { client_id: clientId },
    body: { template_code: templateCode, variables },
  })
}

/**
 * Cancel a message that has not gone out — FR-M8-028.
 *
 * ⚠️ There is no "unsend". The API refuses anything already dispatched, and the
 * UI must not offer the action once the state has moved on.
 */
export async function cancelScheduledMessage(
  scheduledMessageId: string,
): Promise<ScheduledMessage> {
  return api.request('post', '/api/v1/app/messaging/scheduled/{scheduled_message_id}/cancel', {
    path: { scheduled_message_id: scheduledMessageId },
  })
}

/** Every message type this practice can send — FR-M8-026. */
export async function fetchTemplates(signal?: AbortSignal): Promise<MessageTemplate[]> {
  return api.request('get', '/api/v1/app/messaging/templates', {
    ...(signal ? { signal } : {}),
  })
}

/**
 * A template rendered as the client will receive it — FR-M8-026.
 *
 * 🔒 Rendered by the server, with the same function the dispatch path uses. A
 * preview assembled in the browser would be a preview of something else, which
 * is precisely the reassurance this requirement is for.
 */
export async function fetchPreview(
  code: string,
  options: { clientId?: string } = {},
  signal?: AbortSignal,
): Promise<MessagePreview> {
  return api.request('get', '/api/v1/app/messaging/templates/{code}/preview', {
    path: { code },
    ...(options.clientId !== undefined ? { query: { client_id: options.clientId } } : {}),
    ...(signal ? { signal } : {}),
  })
}

/** The practice-wide message settings — FR-M8-027. */
export async function fetchPreferences(signal?: AbortSignal): Promise<MessagePreference[]> {
  return api.request('get', '/api/v1/app/messaging/preferences', {
    ...(signal ? { signal } : {}),
  })
}

export interface PreferenceUpdate {
  template_code?: string | null
  is_enabled?: boolean | null
  quiet_hours_start?: string | null
  quiet_hours_end?: string | null
  max_messages_per_week?: number | null
}

/**
 * Change a practice-wide setting — FR-M8-027.
 *
 * ⚠️ Omitted fields are left alone rather than cleared, so a form that submits
 * one section does not silently reset another.
 */
export async function updatePreference(update: PreferenceUpdate): Promise<MessagePreference> {
  return api.request('put', '/api/v1/app/messaging/preferences', { body: update })
}

/** A client's own overrides, and the practice defaults they layer on. */
export async function fetchClientPreferences(
  clientId: string,
  signal?: AbortSignal,
): Promise<MessagePreference[]> {
  return api.request('get', '/api/v1/app/clients/{client_id}/message-preferences', {
    path: { client_id: clientId },
    ...(signal ? { signal } : {}),
  })
}

export async function updateClientPreference(
  clientId: string,
  update: PreferenceUpdate,
): Promise<MessagePreference> {
  return api.request('put', '/api/v1/app/clients/{client_id}/message-preferences', {
    path: { client_id: clientId },
    body: update,
  })
}

/** Recent delivery failures across the practice — AC-M8-007. */
export async function fetchFailures(signal?: AbortSignal): Promise<Dispatch[]> {
  return api.request('get', '/api/v1/app/messaging/failures', {
    ...(signal ? { signal } : {}),
  })
}

/** A client's check-in cadence — FR-M8-022. `null` when none is configured. */
export async function fetchCheckinSchedule(
  clientId: string,
  signal?: AbortSignal,
): Promise<CheckinSchedule | null> {
  return api.request('get', '/api/v1/app/clients/{client_id}/checkin-schedule', {
    path: { client_id: clientId },
    ...(signal ? { signal } : {}),
  })
}

export interface CheckinUpdate {
  frequency: CheckinFrequency
  day_of_week?: number | null
  time_of_day?: string
  is_paused?: boolean
}

/**
 * Configure or pause a client's check-ins — FR-M8-022/024.
 *
 * 🔒 Pausing does **not** change the client's lifecycle stage, and the UI must
 * not imply that it does: a paused check-in is a messaging decision.
 */
export async function updateCheckinSchedule(
  clientId: string,
  update: CheckinUpdate,
): Promise<CheckinSchedule> {
  return api.request('put', '/api/v1/app/clients/{client_id}/checkin-schedule', {
    path: { client_id: clientId },
    body: update,
  })
}
