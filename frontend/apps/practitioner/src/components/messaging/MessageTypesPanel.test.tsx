/**
 * The practice's message types — FR-M8-026, FR-M8-027, EC-M8-03.
 *
 * 🔒 Two properties are pinned here because both are ways this screen could lie:
 * an essential message type must not appear switchable, and a preview must say
 * when its values are illustrative.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MessageTypesPanel } from './MessageTypesPanel'
import type { MessageTypeView } from './MessageTypesPanel'

const CHECKIN: MessageTypeView = {
  code: 'checkin_nudge',
  label: 'Check-in',
  description: 'Your recurring nudge.',
  isEssential: false,
  canDisable: true,
  isEnabled: true,
  providerStatus: 'approved',
  transport: 'whatsapp',
}

const MAGIC_LINK: MessageTypeView = {
  code: 'magic_link',
  label: 'Portal link',
  description: 'The secure link a client uses to open their portal.',
  isEssential: true,
  canDisable: false,
  isEnabled: true,
  providerStatus: 'approved',
  transport: 'whatsapp',
}

function renderPanel(overrides: Partial<Parameters<typeof MessageTypesPanel>[0]> = {}) {
  const onPreview = vi.fn()
  const onClosePreview = vi.fn()
  const onToggle = vi.fn()

  render(
    <MessageTypesPanel
      types={[CHECKIN, MAGIC_LINK]}
      preview={null}
      previewing={null}
      onPreview={onPreview}
      onClosePreview={onClosePreview}
      onToggle={onToggle}
      {...overrides}
    />,
  )

  return { onPreview, onClosePreview, onToggle }
}

describe('turning message types off', () => {
  it('reports the new state, not the current one', async () => {
    const { onToggle } = renderPanel()

    await userEvent.click(screen.getByRole('button', { name: 'Turn off' }))

    expect(onToggle).toHaveBeenCalledWith('checkin_nudge', false)
  })

  it('announces the toggle state to a screen reader', () => {
    // NFR-062 — `aria-pressed` carries the state; the label alone would not.
    renderPanel({ types: [{ ...CHECKIN, isEnabled: false }] })
    expect(screen.getByRole('button', { name: 'Turn on', pressed: true })).toBeInTheDocument()
  })

  it('offers no switch at all for an essential type', () => {
    // 🔒 FR-M8-027 says "any *non-essential*". A control that looked available
    // and then refused would teach a practitioner that this screen lies.
    renderPanel()
    expect(screen.getByText(/always on — this carries portal access/i)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /turn (on|off)/i })).toHaveLength(1)
  })
})

describe('provider approval', () => {
  it('flags an unapproved WhatsApp template on that type alone', () => {
    // 🔒 EC-M8-03 — one revoked template pauses its own message type. A
    // practice-wide banner would be untrue and would send a practitioner looking
    // in the wrong place.
    renderPanel({ types: [{ ...CHECKIN, providerStatus: 'pending' }, MAGIC_LINK] })

    expect(screen.getAllByText(/awaiting whatsapp approval/i)).toHaveLength(1)
  })
})

describe('preview', () => {
  it('asks for the preview by template code', async () => {
    const { onPreview } = renderPanel()

    await userEvent.click(screen.getAllByRole('button', { name: 'Preview' })[0]!)

    expect(onPreview).toHaveBeenCalledWith('checkin_nudge')
  })

  it('says when the values are examples', () => {
    // ⚠️ Without this a practitioner could reasonably believe they were looking
    // at a real client's message.
    renderPanel({
      previewing: 'checkin_nudge',
      preview: {
        templateCode: 'checkin_nudge',
        body: 'Hi Priya Sharma, time for your check-in.',
        isSample: true,
      },
    })

    expect(screen.getByText(/example values are shown/i)).toBeInTheDocument()
    expect(screen.getByText(/time for your check-in/i)).toBeInTheDocument()
  })

  it('does not caveat a real client’s preview', () => {
    renderPanel({
      previewing: 'checkin_nudge',
      preview: {
        templateCode: 'checkin_nudge',
        body: 'Hi Anjali Rao, time for your check-in.',
        isSample: false,
      },
    })

    expect(screen.queryByText(/example values/i)).not.toBeInTheDocument()
  })
})
