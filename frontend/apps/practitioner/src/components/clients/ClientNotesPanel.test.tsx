/**
 * The note thread — who can do what to whose notes (FR-M1-007, FR-M3-020).
 *
 * 🔒 The interesting assertions are about *absent* controls. The API refuses a
 * non-author's edit regardless, so these tests are about not teaching the rule
 * by refusal — a button that always fails is worse than no button.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ClientNotesPanel } from './ClientNotesPanel'
import type { NoteView } from './ClientNotesPanel'

const ME = 'user-me'
const COLLEAGUE = 'user-colleague'

const MY_NOTE: NoteView = {
  id: 'note-mine',
  body: 'I wrote this',
  authorUserId: ME,
  createdAt: '2026-08-01T10:00:00Z',
  updatedAt: '2026-08-01T10:00:00Z',
}

const THEIR_NOTE: NoteView = {
  id: 'note-theirs',
  body: 'A colleague wrote this',
  authorUserId: COLLEAGUE,
  createdAt: '2026-08-02T10:00:00Z',
  updatedAt: '2026-08-02T10:00:00Z',
}

function renderPanel(overrides: Partial<Parameters<typeof ClientNotesPanel>[0]> = {}) {
  const onAdd = vi.fn()
  const onEdit = vi.fn()
  const onRemove = vi.fn()

  render(
    <ClientNotesPanel
      notes={[THEIR_NOTE, MY_NOTE]}
      currentUserId={ME}
      isOwner={false}
      onAdd={onAdd}
      onEdit={onEdit}
      onRemove={onRemove}
      {...overrides}
    />,
  )

  return { onAdd, onEdit, onRemove }
}

describe('writing notes', () => {
  it('adds a note and clears the draft', async () => {
    const { onAdd } = renderPanel({ notes: [] })

    await userEvent.type(screen.getByLabelText('Add a note'), 'First consultation')
    await userEvent.click(screen.getByRole('button', { name: 'Add note' }))

    expect(onAdd).toHaveBeenCalledWith('First consultation')
    expect(screen.getByLabelText('Add a note')).toHaveValue('')
  })

  it('will not submit an empty note', async () => {
    const { onAdd } = renderPanel({ notes: [] })

    await userEvent.click(screen.getByRole('button', { name: 'Add note' }))

    expect(onAdd).not.toHaveBeenCalled()
  })

  it('says so when there are no notes yet', () => {
    // NFR-064 — every list has an empty state. A blank panel reads as broken.
    renderPanel({ notes: [] })
    expect(screen.getByText('No notes yet.')).toBeInTheDocument()
  })
})

describe('editing — FR-M3-020', () => {
  it('offers Edit only on the practitioner’s own note', () => {
    // 🔒 The API refuses a non-author's edit, so showing the button on a
    // colleague's note would teach the rule by refusing them.
    renderPanel()

    const items = screen.getAllByRole('listitem')
    const mine = items.find((item) => item.textContent?.includes('I wrote this'))
    const theirs = items.find((item) => item.textContent?.includes('A colleague wrote this'))

    expect(mine?.querySelector('button')).toHaveTextContent('Edit')
    expect(theirs?.textContent).not.toContain('Edit')
  })

  it('does not let even the owner edit a colleague’s note', () => {
    // 🔒 The case that makes FR-M3-020 mean something. An owner rewriting a
    // note would put words in a practitioner's mouth under their name.
    renderPanel({ isOwner: true })

    const theirs = screen
      .getAllByRole('listitem')
      .find((item) => item.textContent?.includes('A colleague wrote this'))

    expect(theirs?.textContent).not.toContain('Edit')
  })

  it('lets the owner remove a colleague’s note', () => {
    // The counterpart: wider than editing, because the owner is accountable for
    // what the practice records and needs a remedy that is not a rewrite.
    renderPanel({ isOwner: true })

    const theirs = screen
      .getAllByRole('listitem')
      .find((item) => item.textContent?.includes('A colleague wrote this'))

    expect(theirs?.textContent).toContain('Remove')
  })

  it('does not let a plain practitioner remove a colleague’s note', () => {
    renderPanel({ isOwner: false })

    const theirs = screen
      .getAllByRole('listitem')
      .find((item) => item.textContent?.includes('A colleague wrote this'))

    expect(theirs?.textContent).not.toContain('Remove')
  })

  it('saves an edit through the callback', async () => {
    const { onEdit } = renderPanel({ notes: [MY_NOTE] })

    await userEvent.click(screen.getByRole('button', { name: 'Edit' }))
    const field = screen.getByLabelText('Edit note')
    await userEvent.clear(field)
    await userEvent.type(field, 'Corrected wording')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(onEdit).toHaveBeenCalledWith('note-mine', 'Corrected wording')
  })
})

describe('removing', () => {
  it('confirms and states that nothing is deleted', async () => {
    // 🔒 NFR-065 — and the reassuring half matters: a practitioner who thinks
    // Remove destroys the record will leave a mistaken note standing.
    const { onRemove } = renderPanel({ notes: [MY_NOTE] })

    await userEvent.click(screen.getByRole('button', { name: 'Remove' }))

    expect(screen.getByText(/nothing is deleted/i)).toBeInTheDocument()
    expect(onRemove).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: 'Remove note' }))
    expect(onRemove).toHaveBeenCalledWith('note-mine')
  })
})

describe('provenance', () => {
  it('marks an edited note as edited', () => {
    // Without it, a practitioner reading a colleague's note cannot tell whether
    // it is what was originally written.
    renderPanel({
      notes: [{ ...MY_NOTE, updatedAt: '2026-08-03T09:00:00Z' }],
    })
    expect(screen.getByText(/edited/)).toBeInTheDocument()
  })

  it('renders a failure without losing the thread', () => {
    renderPanel({ error: 'Notes could not be loaded.' })
    expect(screen.getByRole('alert')).toHaveTextContent('Notes could not be loaded.')
  })
})
