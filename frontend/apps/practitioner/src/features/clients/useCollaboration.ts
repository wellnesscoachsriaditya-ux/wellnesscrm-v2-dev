/**
 * Notes and tags on the client detail screen — the state behind them.
 *
 * 🔒 The API layer (Arch §4.4). Components render what this returns and call
 * what it exposes; they never fetch.
 *
 * ⚠️ **Notes and tags load together but fail apart.** One request failing must
 * not blank the other: a practitioner who can see the note thread should still
 * see it when the tag list is briefly unavailable. That is why there are two
 * error slots rather than one.
 */

import { useCallback, useEffect, useState } from 'react'
import { ApiError } from '@wellnesscrm/api-client'
import {
  addNote,
  attachTag,
  createTag,
  detachTag,
  editNote,
  fetchClientTags,
  fetchGrants,
  fetchNotes,
  fetchTags,
  grantAccess,
  reassignOwner,
  removeNote,
  revokeAccess,
  type Grant,
  type Note,
  type Tag,
  type TagColour,
} from './collaborationApi'

/** Which error slot a mutation reports into. */
type Slot = 'notes' | 'tags' | 'access'

export interface CollaborationState {
  notes: Note[]
  /** Every tag in the tenant — the picker's options. */
  allTags: Tag[]
  /** The tags this client carries. */
  clientTags: Tag[]
  /** Live and revoked grants on this client — EC-M0-04. */
  grants: Grant[]
  loading: boolean
  notesError: string | null
  tagsError: string | null
  accessError: string | null
  busy: boolean
  addNote: (body: string) => Promise<void>
  editNote: (noteId: string, body: string) => Promise<void>
  removeNote: (noteId: string) => Promise<void>
  toggleTag: (tagId: string, attached: boolean) => Promise<void>
  createAndAttachTag: (name: string, colour: TagColour) => Promise<void>
  grant: (userId: string) => Promise<void>
  revoke: (userId: string) => Promise<void>
  /**
   * Hand the client to a different practitioner — EC-M1-04.
   *
   * ⚠️ Takes an `onReassigned` callback rather than re-reading the client
   * itself. The owner lives on the client record, which `useClientDetail` owns;
   * this hook re-reading it would give the screen two sources for one field.
   */
  reassign: (userId: string) => Promise<void>
}

function messageOf(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? `${cause.message} ${cause.action}` : fallback
}

export function useCollaboration(
  clientId: string,
  onOwnerChanged?: () => void | Promise<void>,
): CollaborationState {
  const [notes, setNotes] = useState<Note[]>([])
  const [allTags, setAllTags] = useState<Tag[]>([])
  const [clientTags, setClientTags] = useState<Tag[]>([])
  const [grants, setGrants] = useState<Grant[]>([])
  const [loading, setLoading] = useState(true)
  const [notesError, setNotesError] = useState<string | null>(null)
  const [tagsError, setTagsError] = useState<string | null>(null)
  const [accessError, setAccessError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)

    // ⚠️ `allSettled`, not `all`. With `all` a failing tag request would reject
    // the whole load and blank the note thread the practitioner came to read.
    void Promise.allSettled([
      fetchNotes(clientId, controller.signal),
      fetchTags(controller.signal),
      fetchClientTags(clientId, controller.signal),
      fetchGrants(clientId, controller.signal),
    ]).then(([noteResult, tagResult, clientTagResult, grantResult]) => {
      if (controller.signal.aborted) return

      if (noteResult.status === 'fulfilled') {
        setNotes(noteResult.value)
        setNotesError(null)
      } else {
        setNotesError(messageOf(noteResult.reason, 'Notes could not be loaded.'))
      }

      if (tagResult.status === 'fulfilled' && clientTagResult.status === 'fulfilled') {
        setAllTags(tagResult.value)
        setClientTags(clientTagResult.value)
        setTagsError(null)
      } else {
        // ⚠️ `reason` is `any` on the built-in type. Narrowed to `unknown` here
        // so `messageOf` does the instanceof check rather than trusting it.
        const reason: unknown =
          tagResult.status === 'rejected'
            ? tagResult.reason
            : (clientTagResult as PromiseRejectedResult).reason
        setTagsError(messageOf(reason, 'Tags could not be loaded.'))
      }

      if (grantResult.status === 'fulfilled') {
        setGrants(grantResult.value)
        setAccessError(null)
      } else {
        setAccessError(messageOf(grantResult.reason, 'Access could not be loaded.'))
      }

      setLoading(false)
    })

    return () => controller.abort()
  }, [clientId])

  /**
   * Run a mutation, then re-read what it changed.
   *
   * 🔒 Re-reads rather than patching local state. Principle 3 — the client
   * renders, never derives: the server decides ordering, timestamps and which
   * tags survive an archive, and a local patch is how a screen comes to disagree
   * with the database.
   */
  const run = useCallback(
    async (mutate: () => Promise<void>, refresh: () => Promise<void>, slot: Slot) => {
      const setError =
        slot === 'notes' ? setNotesError : slot === 'tags' ? setTagsError : setAccessError
      setBusy(true)
      try {
        await mutate()
        await refresh()
        setError(null)
      } catch (cause: unknown) {
        setError(messageOf(cause, 'That change could not be saved.'))
      } finally {
        setBusy(false)
      }
    },
    [],
  )

  const refreshNotes = useCallback(async () => {
    setNotes(await fetchNotes(clientId))
  }, [clientId])

  const refreshTags = useCallback(async () => {
    const [tenantTags, applied] = await Promise.all([fetchTags(), fetchClientTags(clientId)])
    setAllTags(tenantTags)
    setClientTags(applied)
  }, [clientId])

  const refreshGrants = useCallback(async () => {
    setGrants(await fetchGrants(clientId))
  }, [clientId])

  return {
    notes,
    allTags,
    clientTags,
    grants,
    loading,
    notesError,
    tagsError,
    accessError,
    busy,
    addNote: useCallback(
      (body: string) => run(() => addNote(clientId, body).then(() => undefined), refreshNotes, 'notes'),
      [clientId, refreshNotes, run],
    ),
    editNote: useCallback(
      (noteId: string, body: string) =>
        run(() => editNote(clientId, noteId, body).then(() => undefined), refreshNotes, 'notes'),
      [clientId, refreshNotes, run],
    ),
    removeNote: useCallback(
      (noteId: string) =>
        run(() => removeNote(clientId, noteId).then(() => undefined), refreshNotes, 'notes'),
      [clientId, refreshNotes, run],
    ),
    toggleTag: useCallback(
      (tagId: string, attached: boolean) =>
        run(
          () => (attached ? detachTag(clientId, tagId) : attachTag(clientId, tagId)),
          refreshTags,
          'tags',
        ),
      [clientId, refreshTags, run],
    ),
    createAndAttachTag: useCallback(
      (name: string, colour: TagColour) =>
        run(
          async () => {
            // 🔒 Create then apply, in that order and in two requests. The API
            // keeps the vocabulary and its application separate (DB §5.4)
            // because a tag outlives any client that carries it.
            const tag = await createTag(name, colour)
            await attachTag(clientId, tag.id)
          },
          refreshTags,
          'tags',
        ),
      [clientId, refreshTags, run],
    ),
    grant: useCallback(
      (userId: string) =>
        run(() => grantAccess(clientId, userId).then(() => undefined), refreshGrants, 'access'),
      [clientId, refreshGrants, run],
    ),
    revoke: useCallback(
      (userId: string) =>
        run(() => revokeAccess(clientId, userId).then(() => undefined), refreshGrants, 'access'),
      [clientId, refreshGrants, run],
    ),
    reassign: useCallback(
      (userId: string) =>
        run(
          () => reassignOwner(clientId, userId),
          async () => {
            // 🔒 Both, because the owner is a field on the *client* while the
            // grants are a separate read, and the panel renders them together.
            // A new owner who already held a grant keeps it — `reassign_owner`
            // leaves it deliberately (EC-M1-04: it records a decision somebody
            // made) — so the list legitimately shows a grant to the owner, and
            // only a re-read of both keeps that pair consistent.
            await refreshGrants()
            await onOwnerChanged?.()
          },
          'access',
        ),
      [clientId, onOwnerChanged, refreshGrants, run],
    ),
  }
}
