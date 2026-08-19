/**
 * The check-in cadence — FR-M8-022…024.
 *
 * 🔒 The load-bearing assertion here is a sentence, not a control: pausing
 * check-ins must not read as pausing the *client*. A practitioner who believed
 * otherwise would reach for the stage transition instead, which is metered and
 * visible to the client.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ClientCheckinPanel } from './ClientCheckinPanel'
import type { CheckinScheduleView } from './ClientCheckinPanel'

const WEEKLY: CheckinScheduleView = {
  frequency: 'weekly',
  dayOfWeek: 1,
  timeOfDay: '09:00:00',
  isPaused: false,
  nextDueOn: '2026-08-24',
}

function renderPanel(overrides: Partial<Parameters<typeof ClientCheckinPanel>[0]> = {}) {
  const onSave = vi.fn()
  render(<ClientCheckinPanel schedule={WEEKLY} onSave={onSave} {...overrides} />)
  return { onSave }
}

describe('configuring the cadence', () => {
  it('saves the frequency and day the practitioner chose', async () => {
    const { onSave } = renderPanel()

    await userEvent.selectOptions(screen.getByLabelText('Day'), '3')
    await userEvent.click(screen.getByRole('button', { name: /save schedule/i }))

    expect(onSave).toHaveBeenCalledWith({
      frequency: 'weekly',
      dayOfWeek: 3,
      isPaused: false,
    })
  })

  it('hides the day for a cadence that ignores it', async () => {
    // ⚠️ "Every two weeks, but on a Tuesday" is either 14 days or 21. The API
    // ignores the day for longer cadences, and a control that silently did
    // nothing would be worse than its absence.
    renderPanel()
    await userEvent.selectOptions(screen.getByLabelText('How often'), 'fortnightly')

    expect(screen.queryByLabelText('Day')).not.toBeInTheDocument()
  })

  it('shows when the next check-in falls', () => {
    renderPanel()
    expect(screen.getByText(/next check-in: 2026-08-24/i)).toBeInTheDocument()
  })
})

describe('pausing', () => {
  it('states that pausing does not change the client’s stage', () => {
    // 🔒 FR-M8-024, in words. This is the whole reason the pause lives here
    // rather than being expressed as a lifecycle transition.
    renderPanel()
    expect(screen.getByText(/does not change the client’s stage/i)).toBeInTheDocument()
  })

  it('warns that pausing cancels what is already queued', () => {
    // 🔒 Otherwise a practitioner discovers it from a message that still
    // arrived — or, worse, does not, and concludes the pause was ignored.
    renderPanel()
    expect(screen.getByText(/cancels any already queued/i)).toBeInTheDocument()
  })

  it('offers to resume when paused, and says the client is unaffected', () => {
    renderPanel({ schedule: { ...WEEKLY, isPaused: true } })

    expect(screen.getByRole('button', { name: /resume check-ins/i })).toBeInTheDocument()
    expect(screen.getByText(/the client stays active/i)).toBeInTheDocument()
  })

  it('sends the inverted pause state', async () => {
    const { onSave } = renderPanel()

    await userEvent.click(screen.getByRole('button', { name: /pause check-ins/i }))

    expect(onSave).toHaveBeenCalledWith({ frequency: 'weekly', dayOfWeek: 1, isPaused: true })
  })
})

describe('a client with no schedule', () => {
  it('offers a weekly default rather than an empty form', () => {
    // 🟡 FR-M8-023 — weekly is the proposed default, and a form with nothing
    // selected asks the practitioner a question the product already answered.
    renderPanel({ schedule: null })
    expect(screen.getByLabelText('How often')).toHaveValue('weekly')
  })
})
