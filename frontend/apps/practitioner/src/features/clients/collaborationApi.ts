/**
 * Collaboration — notes, tags and shared access (S2 Slice C).
 *
 * 🔒 **Arch §4.4 / NFR-068 — the layer allowed to talk to the API.** R8 fails
 * the build if anything under `components/` imports this or calls `fetch`.
 *
 * ⚠️ **A 404 from any of these can mean "not yours".** API §5.4 makes an
 * unassigned client indistinguishable from a missing one, deliberately — a 403
 * would confirm the client exists and let a colleague's caseload be enumerated.
 * So the UI must not say "that client was deleted"; it says it could not be
 * found, which is the honest reading of both cases.
 */

import { createApiClient } from '@wellnesscrm/api-client'
import type { components } from '@wellnesscrm/api-client'

export type Note = components['schemas']['NoteResponse']
export type Tag = components['schemas']['TagResponse']
export type Grant = components['schemas']['GrantResponse']
export type TagColour = components['schemas']['TagColour']

const api = createApiClient()

// ─── Notes (FR-M1-007, FR-M3-020) ────────────────────────────────────────

export async function fetchNotes(clientId: string, signal?: AbortSignal): Promise<Note[]> {
  return api.request('get', '/api/v1/app/clients/{client_id}/notes', {
    path: { client_id: clientId },
    ...(signal ? { signal } : {}),
  })
}

export async function addNote(clientId: string, body: string): Promise<Note> {
  return api.request('post', '/api/v1/app/clients/{client_id}/notes', {
    path: { client_id: clientId },
    body: { body },
  })
}

/**
 * Rewrite a note — FR-M3-020.
 *
 * 🔒 Author only. The API refuses anyone else, including the tenant owner, so
 * the UI hides the control rather than letting a practitioner discover the rule
 * by being refused.
 */
export async function editNote(clientId: string, noteId: string, body: string): Promise<Note> {
  return api.request('patch', '/api/v1/app/clients/{client_id}/notes/{note_id}', {
    path: { client_id: clientId, note_id: noteId },
    body: { body },
  })
}

/** Take a note out of the thread. A soft delete — nothing is destroyed. */
export async function removeNote(clientId: string, noteId: string): Promise<Note> {
  return api.request('delete', '/api/v1/app/clients/{client_id}/notes/{note_id}', {
    path: { client_id: clientId, note_id: noteId },
  })
}

// ─── Tags (FR-M1-008) ────────────────────────────────────────────────────

/** The tenant's whole vocabulary — the picker's source. */
export async function fetchTags(signal?: AbortSignal): Promise<Tag[]> {
  return api.request('get', '/api/v1/app/tags', { ...(signal ? { signal } : {}) })
}

export async function fetchClientTags(clientId: string, signal?: AbortSignal): Promise<Tag[]> {
  return api.request('get', '/api/v1/app/clients/{client_id}/tags', {
    path: { client_id: clientId },
    ...(signal ? { signal } : {}),
  })
}

export async function createTag(name: string, colour: TagColour): Promise<Tag> {
  return api.request('post', '/api/v1/app/tags', { body: { name, colour } })
}

/** Apply a tag — `PUT`, and idempotent, so a fast double-click is harmless. */
export async function attachTag(clientId: string, tagId: string): Promise<void> {
  await api.request('put', '/api/v1/app/clients/{client_id}/tags/{tag_id}', {
    path: { client_id: clientId, tag_id: tagId },
  })
}

export async function detachTag(clientId: string, tagId: string): Promise<void> {
  await api.request('delete', '/api/v1/app/clients/{client_id}/tags/{tag_id}', {
    path: { client_id: clientId, tag_id: tagId },
  })
}

// ─── Shared access (EC-M0-04, EC-M1-04) ──────────────────────────────────

export async function fetchGrants(clientId: string, signal?: AbortSignal): Promise<Grant[]> {
  return api.request('get', '/api/v1/app/clients/{client_id}/access', {
    path: { client_id: clientId },
    ...(signal ? { signal } : {}),
  })
}

/**
 * Share a client with a colleague — EC-M0-04.
 *
 * 🔒 Owner-only; a practitioner gets a 403 with a message naming the owner as
 * the person to ask.
 */
export async function grantAccess(clientId: string, userId: string): Promise<Grant> {
  return api.request('post', '/api/v1/app/clients/{client_id}/access', {
    path: { client_id: clientId },
    body: { user_id: userId },
  })
}

export async function revokeAccess(clientId: string, userId: string): Promise<Grant> {
  return api.request('delete', '/api/v1/app/clients/{client_id}/access/{user_id}', {
    path: { client_id: clientId, user_id: userId },
  })
}

/**
 * Move a client to a different owning practitioner — EC-M1-04.
 *
 * ⚠️ **One client at a time.** EC-M1-04 also describes reassigning a departing
 * practitioner's whole caseload; that needs a selection UI over the client list,
 * which Slice E owns. Returns nothing — the caller re-reads the client, because
 * the owner change also changes who the grant list is meaningful about.
 */
export async function reassignOwner(clientId: string, ownerUserId: string): Promise<void> {
  await api.request('post', '/api/v1/app/clients/{client_id}/owner', {
    path: { client_id: clientId },
    body: { owner_user_id: ownerUserId },
  })
}
