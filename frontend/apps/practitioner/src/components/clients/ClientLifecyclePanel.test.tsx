/**
 * The lifecycle panel — what a practitioner can actually do, and what they are
 * told before they do it (S2 Slice B).
 *
 * 🔒 The panel is a pure component, so these tests need no network and no
 * router: props in, callbacks out. That is the property Arch §4.4 buys, and
 * these tests are what spend it.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ClientLifecyclePanel } from './ClientLifecyclePanel'
import type { StageValue } from './stages'

const SELECTABLE: readonly StageValue[] = [
  'lead',
  'contacted',
  'consultation_scheduled',
  'active',
  'paused',
  'churned',
]

function renderPanel(overrides: Partial<Parameters<typeof ClientLifecyclePanel>[0]> = {}) {
  const onChangeStage = vi.fn()
  const onArchive = vi.fn()
  const onRestore = vi.fn()

  render(
    <ClientLifecyclePanel
      stage="lead"
      isArchived={false}
      fullName="Asha Menon"
      selectableStages={SELECTABLE}
      onChangeStage={onChangeStage}
      onArchive={onArchive}
      onRestore={onRestore}
      {...overrides}
    />,
  )

  return { onChangeStage, onArchive, onRestore }
}

describe('stage changes', () => {
  it('never offers `archived` as a stage', async () => {
    // 🔒 FR-M1-010 — archiving is a soft delete with its own action, and the
    // API refuses `archived` as a transition target. Offering it in the
    // dropdown would be an invitation to a guaranteed error.
    renderPanel()
    await userEvent.click(screen.getByLabelText('Move to stage'))

    expect(screen.queryByRole('option', { name: /archived/i })).not.toBeInTheDocument()
  })

  it('does not offer the stage the client is already in', () => {
    // A no-op transition cannot be recorded — `client_stage_history` refuses a
    // row whose from_stage equals its to_stage — so the API rejects it.
    renderPanel({ stage: 'active' })

    expect(screen.queryByRole('option', { name: 'Active client' })).not.toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Paused' })).toBeInTheDocument()
  })

  it('reaches a stage change in two interactions', async () => {
    // 🔒 NFR-012 / FR-M1-016 — "≤ 2 interactions from the client detail view."
    // Choosing the stage and confirming is the whole interaction budget, which
    // is why an ordinary move has no confirmation dialog.
    const { onChangeStage } = renderPanel()

    await userEvent.selectOptions(screen.getByLabelText('Move to stage'), 'active')
    await userEvent.click(screen.getByRole('button', { name: 'Update stage' }))

    expect(onChangeStage).toHaveBeenCalledWith('active', '')
  })

  it('passes the reason through to the history row', async () => {
    const { onChangeStage } = renderPanel()

    await userEvent.selectOptions(screen.getByLabelText('Move to stage'), 'churned')
    await userEvent.type(screen.getByLabelText('Reason (optional)'), 'Moved abroad')
    await userEvent.click(screen.getByRole('button', { name: 'Update stage' }))

    expect(onChangeStage).toHaveBeenCalledWith('churned', 'Moved abroad')
  })

  it('states the billing consequence of the stage being chosen', async () => {
    // 🔒 M1.5 — the stage is a billing boundary that "MUST be predictable
    // without the practitioner thinking about it". A dropdown that silently
    // starts charging is the surprise that requirement exists to prevent.
    renderPanel()
    await userEvent.selectOptions(screen.getByLabelText('Move to stage'), 'active')

    expect(screen.getByText(/counts towards your plan/i)).toBeInTheDocument()
  })

  it('cannot change the stage of an archived client', () => {
    // FR-M1-010 — the API refuses it, so the control is absent rather than
    // present-and-failing.
    renderPanel({ isArchived: true })

    expect(screen.queryByLabelText('Move to stage')).not.toBeInTheDocument()
  })
})

describe('archive and restore', () => {
  it('confirms before archiving and says what happens', async () => {
    // 🔒 NFR-065 — a destructive-looking action must state what is lost. It
    // must also say what is *not* lost: a practitioner who thinks archiving
    // deletes data will instead leave a churned client `active`, and go on
    // paying for them.
    const { onArchive } = renderPanel({ stage: 'active' })

    await userEvent.click(screen.getByRole('button', { name: 'Archive client' }))

    expect(screen.getByText(/frees one active-client slot/i)).toBeInTheDocument()
    expect(screen.getByText(/nothing is deleted/i)).toBeInTheDocument()
    expect(onArchive).not.toHaveBeenCalled()
  })

  it('archives only after the confirmation is accepted', async () => {
    const { onArchive } = renderPanel()

    await userEvent.click(screen.getByRole('button', { name: 'Archive client' }))
    const confirm = screen.getAllByRole('button', { name: 'Archive client' })
    // The dialog's confirm button is the last one rendered.
    await userEvent.click(confirm[confirm.length - 1] as HTMLElement)

    expect(onArchive).toHaveBeenCalledTimes(1)
  })

  it('offers restore instead of archive when the client is archived', async () => {
    const { onRestore } = renderPanel({ isArchived: true })

    expect(screen.queryByRole('button', { name: 'Archive client' })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Restore client' }))

    expect(onRestore).toHaveBeenCalledTimes(1)
  })

  it('shows the archived state alongside the preserved stage', () => {
    // 🔒 Archiving does not change the stage (DB §5.2) — that is what lets a
    // restore return the client to what they were. The badge shows both so the
    // practitioner can see what they will get back.
    renderPanel({ stage: 'active', isArchived: true })

    expect(screen.getByText('Active client · archived')).toBeInTheDocument()
  })
})
