/**
 * The note thread on the client detail screen — FR-M1-007, FR-M3-020.
 *
 * 🔒 Renders and reports. Every action is a callback and every fact a prop,
 * which is what R8 enforces for anything under `components/`.
 *
 * 🔒 **The edit control appears only for the author** (FR-M3-020). The API
 * refuses anyone else — including the tenant owner — so showing the button
 * universally would teach practitioners the rule by refusing them. `canEdit` is
 * computed from ids the screen already holds, not from a second request.
 */

import { useState } from 'react'
import {
  Button,
  Card,
  CardBody,
  CardHeader,
  ConfirmDialog,
  EmptyState,
  FormField,
  Spinner,
  Textarea,
} from '@wellnesscrm/design-system'

export interface NoteView {
  id: string
  body: string
  authorUserId: string
  createdAt: string
  updatedAt: string
}

export interface ClientNotesPanelProps {
  notes: readonly NoteView[]
  /** The signed-in practitioner, for the authorship rule. */
  currentUserId: string
  /** 🔒 The tenant owner may remove any note, but edit none but their own. */
  isOwner: boolean
  error?: string | null
  busy?: boolean
  /**
   * ⚠️ **Distinct from `busy`.** `busy` is a mutation in flight; this is the
   * first read still arriving. Without it an empty `notes` array renders "No
   * notes yet" during the load, which tells the practitioner a client has no
   * history at the exact moment their history is being fetched.
   */
  loading?: boolean
  onAdd: (body: string) => void
  onEdit: (noteId: string, body: string) => void
  onRemove: (noteId: string) => void
}

export function ClientNotesPanel({
  notes,
  currentUserId,
  isOwner,
  error = null,
  busy = false,
  loading = false,
  onAdd,
  onEdit,
  onRemove,
}: ClientNotesPanelProps) {
  const [draft, setDraft] = useState('')
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editDraft, setEditDraft] = useState('')
  const [removingId, setRemovingId] = useState<string | null>(null)

  function submitNew() {
    if (draft.trim() === '') return
    onAdd(draft)
    setDraft('')
  }

  return (
    <Card>
      <CardHeader title="Notes" description="Only your practice can see these." />
      <CardBody>
        {error !== null && <p role="alert">{error}</p>}

        <FormField label="Add a note">
          <Textarea
            value={draft}
            rows={3}
            maxLength={5000}
            disabled={busy}
            onChange={(event) => setDraft(event.target.value)}
          />
        </FormField>
        <Button onClick={submitNew} disabled={draft.trim() === ''} loading={busy}>
          Add note
        </Button>

        {loading ? (
          <Spinner label="Loading notes…" />
        ) : notes.length === 0 ? (
          <EmptyState
            title="No notes yet"
            description="Notes are private to your practice — the client never sees them."
          />
        ) : (
          // 🔒 FR-M1-007 — a list, newest first, as the server ordered it. The
          // component does not sort: ordering is the server's decision and a
          // second opinion here is a way for the two to disagree.
          <ol aria-label="Note thread">
            {notes.map((note) => {
              const canEdit = note.authorUserId === currentUserId
              const canRemove = canEdit || isOwner
              const isEditing = editingId === note.id

              return (
                <li key={note.id}>
                  {isEditing ? (
                    <>
                      <FormField label="Edit note">
                        <Textarea
                          value={editDraft}
                          rows={3}
                          maxLength={5000}
                          disabled={busy}
                          onChange={(event) => setEditDraft(event.target.value)}
                        />
                      </FormField>
                      <Button
                        onClick={() => {
                          onEdit(note.id, editDraft)
                          setEditingId(null)
                        }}
                        loading={busy}
                      >
                        Save
                      </Button>
                      <Button variant="secondary" onClick={() => setEditingId(null)} disabled={busy}>
                        Cancel
                      </Button>
                    </>
                  ) : (
                    <>
                      <p>{note.body}</p>
                      <p>
                        <time dateTime={note.createdAt}>
                          {new Date(note.createdAt).toLocaleString()}
                        </time>
                        {/* Edited notes say so. Without it a practitioner
                          * reading a colleague's note cannot tell whether it is
                          * what was originally written. */}
                        {note.updatedAt !== note.createdAt && <span> · edited</span>}
                      </p>

                      {canEdit && (
                        <Button
                          variant="ghost"
                          disabled={busy}
                          onClick={() => {
                            setEditingId(note.id)
                            setEditDraft(note.body)
                          }}
                        >
                          Edit
                        </Button>
                      )}
                      {canRemove && (
                        <Button variant="ghost" disabled={busy} onClick={() => setRemovingId(note.id)}>
                          Remove
                        </Button>
                      )}
                    </>
                  )}
                </li>
              )
            })}
          </ol>
        )}
      </CardBody>

      {/* 🔒 NFR-065 — and the consequence is the reassuring half. A practitioner
        * who thinks "Remove" destroys the record will avoid it and leave a
        * mistaken note standing. */}
      <ConfirmDialog
        open={removingId !== null}
        onCancel={() => setRemovingId(null)}
        onConfirm={() => {
          if (removingId !== null) onRemove(removingId)
          setRemovingId(null)
        }}
        title="Remove this note?"
        consequence="It disappears from the thread. Nothing is deleted, and your practice's record of it is kept."
        confirmLabel="Remove note"
        tone="primary"
        busy={busy}
      />
    </Card>
  )
}
