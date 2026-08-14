/**
 * The manual WhatsApp panel.
 *
 * 🔒 The assertions that matter are about *honesty*, not mechanics: this panel
 * must never imply WellnessCRM sent anything. A practitioner who believed a
 * hand-sent message was tracked would stop checking whether their client
 * replied — and the delivery log would be silently incomplete.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ClientWhatsAppPanel, WhatsAppFallbackLink } from './ClientWhatsAppPanel'

/**
 * ⚠️ **The links here are literals, not built by the real rule** — a test file
 * beside a component is a component file to `check_boundaries.py` R8, and
 * importing `features/messaging/clickToChat` here fails the build.
 *
 * That is the right constraint and not merely a workaround: the panel's job is
 * to render whatever `href` it is handed, and asserting that against a literal
 * is a truer unit test than re-deriving it. The *rule* — encoding, refusal, the
 * refusal to guess a country code — is pinned exhaustively in
 * `features/messaging/clickToChat.test.ts`.
 */
const HREF = 'https://wa.me/919876543210'

function renderPanel(overrides: Partial<Parameters<typeof ClientWhatsAppPanel>[0]> = {}) {
  const onMessageChange = vi.fn()

  render(
    <ClientWhatsAppPanel
      clientName="Anjali Rao"
      message=""
      href={HREF}
      reason={null}
      onMessageChange={onMessageChange}
      {...overrides}
    />,
  )

  return { onMessageChange }
}

describe('opening the conversation', () => {
  it('links to the client’s own WhatsApp conversation', () => {
    renderPanel()

    const link = screen.getByRole('link', { name: /open whatsapp with anjali rao/i })
    expect(link).toHaveAttribute('href', HREF)
  })

  it('opens in a new tab without handing it window.opener', () => {
    // 🔒 `noopener` is the one that matters: without it the opened page can
    // navigate this one.
    renderPanel()

    const link = screen.getByRole('link', { name: /open whatsapp/i })
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'))
  })

  it('renders the prefilled link it was given', () => {
    const prefilled = `${HREF}?text=Hi%20%26%20welcome`
    renderPanel({ message: 'Hi & welcome', href: prefilled })

    expect(screen.getByRole('link', { name: /open whatsapp/i })).toHaveAttribute(
      'href',
      prefilled,
    )
  })

  it('reports what the practitioner typed, leaving the state to the hook', async () => {
    const { onMessageChange } = renderPanel()

    await userEvent.type(screen.getByLabelText('Message'), 'Hi')

    expect(onMessageChange).toHaveBeenCalled()
  })

  it('offers no link when the client has no number, and says why', () => {
    // EC-M1-08 — a client with only an email is legitimate. NFR-063 — the
    // message states the fix.
    renderPanel({ href: null, reason: 'This client has no mobile number on file. Add one.' })

    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent(/no mobile number on file/i)
  })

  it('refuses a number with no country code rather than guessing one', () => {
    renderPanel({
      href: null,
      reason: 'This number is not in international format. Enter it with the country code.',
    })

    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent(/country code/i)
  })
})

describe('what it promises', () => {
  it('says the practitioner sends it, not WellnessCRM', () => {
    // 🔒 No dispatch row is written, and this sentence is why that is not a gap.
    renderPanel()
    expect(screen.getByText(/you press send/i)).toBeInTheDocument()
  })

  it('says these messages are absent from the message history', () => {
    renderPanel()
    expect(screen.getByText(/not in the message history/i)).toBeInTheDocument()
  })
})

describe('the fallback beside a failed message', () => {
  it('carries the message that failed, so it need not be retyped', () => {
    const failed = `${HREF}?text=Hi%20Anjali%2C%20your%20plan%20is%20ready`
    render(<WhatsAppFallbackLink href={failed} />)

    const link = screen.getByRole('link', { name: /send this by hand/i })
    expect(link).toHaveAttribute('href', failed)
  })

  it('renders nothing at all when the client is unreachable', () => {
    // ⚠️ Not a disabled link: an action that opens an empty conversation is
    // worse than no action.
    const { container } = render(<WhatsAppFallbackLink href={null} />)
    expect(container).toBeEmptyDOMElement()
  })
})
