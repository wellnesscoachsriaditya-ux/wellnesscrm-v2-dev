/**
 * The enquiry list — FR-M2-011, AC-M2-005, EC-M2-02.
 *
 * 🔒 What these pin is the *queue's* job: surfacing what has waited too long, and
 * letting the practitioner clear it. The failure mode US-M2-03 names is silent —
 * a forgotten enquiry looks exactly like an enquiry that never arrived — so the
 * ageing warning and the answered state are the two things that must be visible
 * and correctly attributed.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { EnquiryTable } from './EnquiryTable'
import type { EnquiryRowView } from './EnquiryTable'

const WAITING: EnquiryRowView = {
  id: 'sub-1',
  clientId: 'client-1',
  name: 'Asha Menon',
  contact: '+919876543210',
  goal: 'Lose 8kg before my sister’s wedding',
  source: 'instagram',
  ageHours: 50,
  isAgeing: true,
  isAnswered: false,
  isRepeatEnquiry: false,
  stage: 'lead',
  ownerName: 'Priya',
}

function renderTable(overrides: Partial<Parameters<typeof EnquiryTable>[0]> = {}) {
  const onOpenClient = vi.fn()
  const onRespond = vi.fn()
  const onLoadMore = vi.fn()

  render(
    <EnquiryTable
      enquiries={[WAITING]}
      view="needs-response"
      onOpenClient={onOpenClient}
      onRespond={onRespond}
      onLoadMore={onLoadMore}
      {...overrides}
    />,
  )

  return { onOpenClient, onRespond, onLoadMore }
}

describe('the waiting queue', () => {
  it('shows how long an enquiry has waited', () => {
    // 🔒 AC-M2-005 — "visible in a single view with their age". The number is
    // server-computed; this pins that it reaches the screen in words a
    // practitioner reads rather than as a raw float.
    renderTable()
    expect(screen.getByText('2 days ago')).toBeInTheDocument()
  })

  it('offers the action that clears it from the queue', async () => {
    const user = userEvent.setup()
    const { onRespond } = renderTable()

    await user.click(screen.getByRole('button', { name: 'Mark responded' }))

    expect(onRespond).toHaveBeenCalledWith('sub-1')
  })

  it('replaces the action with a state once answered', () => {
    // ⚠️ Not a disabled button. An answered enquiry has nothing left to do, and
    // a permanently disabled control reads as a broken feature rather than a
    // completed one.
    renderTable({ enquiries: [{ ...WAITING, isAnswered: true }] })

    expect(screen.queryByRole('button', { name: 'Mark responded' })).not.toBeInTheDocument()
    expect(screen.getByText('Responded')).toBeInTheDocument()
  })

  it('marks a repeat enquiry so the practitioner knows the history', () => {
    // 🔒 EC-M2-02's deliberate asymmetry: the *practitioner* is told the
    // prospect has enquired before; the submitter never is. This is the visible
    // half — the invisible half is asserted on the backend, where the public
    // response cannot express it.
    renderTable({ enquiries: [{ ...WAITING, isRepeatEnquiry: true }] })

    expect(screen.getByText('Enquired before')).toBeInTheDocument()
  })

  it('does not offer a dead link when the client has been erased', () => {
    // ⚠️ FR-M0-027 sets `client_id` to null while the submission survives as
    // consent evidence. The name still renders — the row is still evidence —
    // but there is nothing to navigate to.
    renderTable({ enquiries: [{ ...WAITING, clientId: null }] })

    expect(screen.queryByRole('button', { name: 'Asha Menon' })).not.toBeInTheDocument()
    expect(screen.getByText('Asha Menon')).toBeInTheDocument()
  })
})

describe('empty states', () => {
  it('treats an empty queue as success, not absence', () => {
    // 🔒 NFR-064. An empty *queue* means every enquiry has been answered, which
    // is the good outcome. Showing "share your form to get leads" here would
    // tell a practitioner who just cleared their backlog that they have none.
    renderTable({ enquiries: [], view: 'needs-response' })

    expect(screen.getByText('Nothing waiting')).toBeInTheDocument()
  })

  it('treats an empty archive as a prompt to share the form', () => {
    // The opposite reading of the same zero: no enquiry has ever arrived, so the
    // useful next action is the one that fixes that.
    renderTable({ enquiries: [], view: 'all' })

    expect(screen.getByText('No enquiries yet')).toBeInTheDocument()
  })
})
