/**
 * The client's message panel — FR-M8-011, FR-M8-028, AC-M8-006/007.
 *
 * 🔒 What these pin is the *wording*, because the wording is where this panel
 * can mislead: a queued message is not a sent one, a deferral is not a delay
 * nobody explained, and a failure a practitioner cannot see is a failure they
 * will not act on.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ClientMessagesPanel } from './ClientMessagesPanel'
import type { MessageHistoryView, PendingMessageView } from './ClientMessagesPanel'

const SENT: MessageHistoryView = {
  id: 'dispatch-1',
  templateCode: 'plan_delivered',
  transport: 'whatsapp',
  status: 'delivered',
  recipientAddress: '+919876543210',
  attemptNumber: 1,
  failureReason: null,
  createdAt: '2026-08-14T09:00:00Z',
}

const FAILED: MessageHistoryView = {
  ...SENT,
  id: 'dispatch-2',
  status: 'failed',
  attemptNumber: 3,
  failureReason: 'The provider rejected the message (HTTP 400).',
}

const PENDING: PendingMessageView = {
  id: 'scheduled-1',
  templateCode: 'checkin_nudge',
  scheduledFor: '2026-08-21T03:30:00Z',
  deferredFrom: null,
  preview: 'Hi Anjali Rao, time for your check-in.',
}

function renderPanel(overrides: Partial<Parameters<typeof ClientMessagesPanel>[0]> = {}) {
  const onLoadMore = vi.fn()
  const onCancel = vi.fn()

  render(
    <ClientMessagesPanel
      history={[SENT]}
      pending={[PENDING]}
      onLoadMore={onLoadMore}
      onCancel={onCancel}
      {...overrides}
    />,
  )

  return { onLoadMore, onCancel }
}

describe('the delivery log', () => {
  it('names the message type in the practitioner’s language, not the wire code', () => {
    // ⚠️ `checkin_nudge` on a practitioner's screen is jargon leaking out of the
    // database.
    renderPanel()
    expect(screen.getByText('Plan delivered')).toBeInTheDocument()
    expect(screen.queryByText('plan_delivered')).not.toBeInTheDocument()
  })

  it('shows failures with the provider’s reason', () => {
    // 🔒 AC-M8-007 — terminal failure is visible to the practitioner, and the
    // reason is what makes it actionable.
    renderPanel({ history: [FAILED] })
    expect(screen.getByText('Failed')).toBeInTheDocument()
    expect(screen.getByText(/provider rejected/i)).toBeInTheDocument()
  })

  it('mentions the attempt number only when it is not the first', () => {
    // ⚠️ "attempt 1" on every row is noise; "attempt 3" is the thing worth
    // noticing.
    renderPanel({ history: [SENT] })
    expect(screen.queryByText(/attempt/i)).not.toBeInTheDocument()

    renderPanel({ history: [FAILED] })
    expect(screen.getByText(/attempt 3/i)).toBeInTheDocument()
  })

  it('says nothing has been sent rather than showing an empty list', () => {
    renderPanel({ history: [] })
    expect(screen.getByText(/no messages yet/i)).toBeInTheDocument()
  })

  it('offers more only when there is more', () => {
    renderPanel({ hasMore: false })
    expect(screen.queryByRole('button', { name: /load more/i })).not.toBeInTheDocument()
  })
})

describe('the scheduled queue', () => {
  it('says a message is queued, never that it has been sent', () => {
    // 🔒 DB §11.5 — suppression happens at dispatch, against live state. A panel
    // promising delivery would be promising something the engine deliberately
    // does not.
    renderPanel()
    expect(screen.getByText(/queued for/i)).toBeInTheDocument()
  })

  it('shows what the message will say', () => {
    // 🔒 FR-M8-028 — "one message is scheduled" is not something a practitioner
    // can act on.
    renderPanel()
    expect(screen.getByText('Hi Anjali Rao, time for your check-in.')).toBeInTheDocument()
  })

  it('explains a quiet-hours deferral rather than leaving the time unexplained', () => {
    // 🔒 AC-M8-006 — the message moved; it was not dropped, and it was not late.
    renderPanel({ pending: [{ ...PENDING, deferredFrom: '2026-08-20T20:30:00Z' }] })
    expect(screen.getByText(/moved out of quiet hours/i)).toBeInTheDocument()
  })

  it('cancels the message it names', async () => {
    const { onCancel } = renderPanel()
    await userEvent.click(screen.getByRole('button', { name: /cancel/i }))
    expect(onCancel).toHaveBeenCalledWith('scheduled-1')
  })

  it('says the queue is empty in words', () => {
    renderPanel({ pending: [] })
    expect(screen.getByText(/nothing is queued/i)).toBeInTheDocument()
  })

  it('reports a load failure without blanking the other section', () => {
    // ⚠️ The two halves fail apart: a practitioner who came for the history
    // should still see it when the queue is unavailable.
    renderPanel({ pendingError: 'Scheduled messages could not be loaded.' })
    expect(screen.getByRole('alert')).toHaveTextContent(/could not be loaded/i)
    expect(screen.getByText('Plan delivered')).toBeInTheDocument()
  })
})
