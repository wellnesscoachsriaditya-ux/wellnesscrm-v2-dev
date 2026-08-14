/**
 * Quiet hours, the weekly cap and delivery failures — FR-M8-008/009, AC-M8-007.
 *
 * 🔒 The assertion that matters most is a word: "held", not "blocked". A
 * practitioner who believed quiet hours discarded messages would widen the
 * window to be safe, and their clients would be woken at 02:00.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MessagingControlsPanel } from './MessagingControlsPanel'
import type { DeliveryFailureView } from './MessagingControlsPanel'

const FAILURE: DeliveryFailureView = {
  id: 'dispatch-1',
  templateLabel: 'Plan delivered',
  recipientAddress: '+919876543210',
  failureReason: 'The provider rejected the message (HTTP 400).',
  createdAt: '2026-08-14T09:00:00Z',
}

function renderPanel(overrides: Partial<Parameters<typeof MessagingControlsPanel>[0]> = {}) {
  const onSave = vi.fn()

  render(
    <MessagingControlsPanel
      quietHours={{ start: '21:00', end: '08:00', maxPerWeek: null }}
      failures={[]}
      onSave={onSave}
      {...overrides}
    />,
  )

  return { onSave }
}

describe('quiet hours', () => {
  it('says messages are held, not dropped', () => {
    // 🔒 AC-M8-006 — deferred to the next permitted time, never discarded.
    renderPanel()
    expect(screen.getByText(/held and sent at the next permitted time/i)).toBeInTheDocument()
  })

  it('shows the window the engine will actually use when none is set', () => {
    // ⚠️ An empty field would ask the practitioner to guess at a default the
    // product already has (21:00–08:00).
    renderPanel()
    expect(screen.getByLabelText(/quiet hours start/i)).toHaveValue('21:00')
    expect(screen.getByLabelText(/quiet hours end/i)).toHaveValue('08:00')
  })

  it('saves the window and the cap together', async () => {
    const { onSave } = renderPanel()

    await userEvent.clear(screen.getByLabelText(/most messages per client/i))
    await userEvent.type(screen.getByLabelText(/most messages per client/i), '4')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(onSave).toHaveBeenCalledWith({ start: '21:00', end: '08:00', maxPerWeek: 4 })
  })

  it('treats a blank cap as "use the default"', async () => {
    const { onSave } = renderPanel({
      quietHours: { start: '22:00', end: '07:00', maxPerWeek: 5 },
    })

    await userEvent.clear(screen.getByLabelText(/most messages per client/i))
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(onSave).toHaveBeenCalledWith({ start: '22:00', end: '07:00', maxPerWeek: null })
  })

  it('says the cap counts every message type', () => {
    // 🔒 EC-M8-09 — three features each sending "only two" messages is six
    // messages to the client.
    renderPanel()
    expect(screen.getByText(/counted across every message type/i)).toBeInTheDocument()
  })
})

describe('delivery failures', () => {
  it('shows the reason a message did not arrive', () => {
    renderPanel({ failures: [FAILURE] })

    expect(screen.getByText('Plan delivered')).toBeInTheDocument()
    expect(screen.getByText(/provider rejected/i)).toBeInTheDocument()
  })

  it('says nothing has failed rather than showing an empty list', () => {
    // NFR-064 — and it also tells a practitioner what this panel is for before
    // there is anything in it.
    renderPanel()
    expect(screen.getByText(/nothing has failed/i)).toBeInTheDocument()
  })
})
