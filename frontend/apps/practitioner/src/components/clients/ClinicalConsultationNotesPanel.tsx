/**
 * Consultation notes panel — FR-M3-018…021, AC-M3-006.
 *
 * 🔒 Renders and reports. Every action is a callback and every fact a prop,
 * which is what R8 enforces for anything under `components/`.
 *
 * 🔒 **The edit control appears only for the author** (FR-M3-020). The API
 * refuses anyone else — including the tenant owner — so showing the button
 * universally would teach practitioners the rule by refusing them. `canEdit` is
 * computed from ids the screen already holds, not from a second request.
 *
 * ⚠️ **Completely isolated from `ClientNotesPanel`** (S2 Slice B). S2's notes
 * are general scratchpads; these are clinical records tied to a consultation
 * date (FR-M3-018) and they never mix.
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
  Input,
  Spinner,
  Textarea,
} from '@wellnesscrm/design-system'

export interface ConsultationNoteView {
  id: string
  noteDate: string
  body: string
  authorUserId: string
  createdAt: string
  updatedAt: string
}

export interface ClinicalConsultationNotesPanelProps {
  notes: readonly ConsultationNoteView[]
  /** The signed-in practitioner, for the authorship rule. */
  currentUserId: string
  /** 🔒 The tenant owner may remove any note, but edit none but their own. */
  isOwner: boolean
  error?: string | null
  busy?: boolean
  loading?: boolean
  onAdd: (noteDate: string, body: string) => void
  onEdit: (noteId: string, body: string) => void
  onArchive: (noteId: string) => void
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    })
  } catch {
    return iso
  }
}

export function ClinicalConsultationNotesPanel({
  notes,
  currentUserId,
  isOwner,
  error = null,
  busy = false,
  loading = false,
  onAdd,
  onEdit,
  onArchive,
}: ClinicalConsultationNotesPanelProps) {
  const [showAddForm, setShowAddForm] = useState(false)
  const [draftDate, setDraftDate] = useState(() => new Date().toISOString().split('T')[0])
  const [draftBody, setDraftBody] = useState('')
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editDraft, setEditDraft] = useState('')
  const [archivingId, setArchivingId] = useState<string | null>(null)

  function submitNew() {
    if (!draftDate || draftDate.trim() === '' || draftBody.trim() === '') return
    onAdd(draftDate, draftBody)
    setDraftDate(new Date().toISOString().split('T')[0])
    setDraftBody('')
    setShowAddForm(false)
  }

  function startEdit(note: ConsultationNoteView) {
    setEditingId(note.id)
    setEditDraft(note.body)
  }

  function submitEdit() {
    if (editingId && editDraft.trim() !== '') {
      onEdit(editingId, editDraft)
    }
    setEditingId(null)
    setEditDraft('')
  }

  return (
    <Card>
      <CardHeader 
        title="Consultation notes" 
        description="Clinical notes from sessions. Strictly practitioner-only." 
        actions={
          <Button
            size="sm"
            variant="secondary"
            onClick={() => setShowAddForm(!showAddForm)}
            disabled={busy}
          >
            {showAddForm ? 'Cancel' : 'New note'}
          </Button>
        }
      />
      <CardBody>
        {error !== null && <p role="alert" style={{ color: 'var(--ds-colour-negative)' }}>{error}</p>}
        {loading && <Spinner label="Loading consultation notes…" />}

        {showAddForm && (
          <div style={{ padding: '1rem', marginBottom: '1.5rem', borderRadius: 'var(--ds-radius-md, 8px)', background: 'var(--ds-colour-surface-secondary, #f7f7f8)' }}>
            <FormField label="Consultation date">
              <Input
                id="note-date"
                type="date"
                value={draftDate}
                onChange={(e) => setDraftDate(e.target.value)}
                disabled={busy}
              />
            </FormField>
            <div style={{ marginTop: '1rem' }}>
              <FormField label="Note">
                <Textarea
                  id="note-body"
                  value={draftBody}
                  onChange={(e) => setDraftBody(e.target.value)}
                  disabled={busy}
                  rows={4}
                />
              </FormField>
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: '1rem' }}>
              <Button onClick={submitNew} disabled={busy || draftBody.trim() === '' || !draftDate || draftDate.trim() === ''}>
                Save note
              </Button>
            </div>
          </div>
        )}

        {!loading && error === null && notes.length === 0 && !showAddForm && (
          <EmptyState
            title="No consultation notes"
            description="Record a note after a session with this client."
          />
        )}
        {notes.length > 0 && (
          <ul style={{ listStyle: 'none', padding: 0, margin: 0, display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
            {notes.map((note) => {
              const isAuthor = note.authorUserId === currentUserId
              const isEditing = editingId === note.id

              return (
                <li key={note.id} style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <strong style={{ fontSize: '1rem' }}>Consultation on {formatDate(note.noteDate)}</strong>
                    <div style={{ display: 'flex', gap: '0.5rem' }}>
                      {isAuthor && !isEditing && (
                        <Button size="sm" variant="secondary" onClick={() => startEdit(note)} disabled={busy}>
                          Edit
                        </Button>
                      )}
                      {(isAuthor || isOwner) && !isEditing && (
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => setArchivingId(note.id)}
                          disabled={busy}
                        >
                          Archive
                        </Button>
                      )}
                    </div>
                  </div>

                  {isEditing ? (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
                      <Textarea
                        id={`edit-note-${note.id}`}
                        value={editDraft}
                        onChange={(e) => setEditDraft(e.target.value)}
                        disabled={busy}
                        rows={4}
                      />
                      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '0.5rem' }}>
                        <Button size="sm" variant="secondary" onClick={() => setEditingId(null)} disabled={busy}>
                          Cancel
                        </Button>
                        <Button size="sm" onClick={submitEdit} disabled={busy || editDraft.trim() === ''}>
                          Save changes
                        </Button>
                      </div>
                    </div>
                  ) : (
                    <div style={{ whiteSpace: 'pre-wrap', lineHeight: '1.5' }}>
                      {note.body}
                    </div>
                  )}

                  <div style={{ fontSize: '0.875rem', opacity: 0.7 }}>
                    Written {formatDate(note.createdAt)}
                    {note.updatedAt !== note.createdAt && ' · Edited'}
                  </div>

                  {archivingId === note.id && (
                    <ConfirmDialog
                      open={true}
                      title="Archive consultation note?"
                      consequence="This note will be hidden. Archiving is the disposition; notes cannot be deleted."
                      confirmLabel="Archive note"
                      onConfirm={() => {
                        onArchive(note.id)
                        setArchivingId(null)
                      }}
                      onCancel={() => setArchivingId(null)}
                      busy={busy}
                    />
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </CardBody>
    </Card>
  )
}
