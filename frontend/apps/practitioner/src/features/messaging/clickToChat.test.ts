/**
 * Click-to-chat link building — the manual WhatsApp path.
 *
 * 🔒 Two properties carry real consequences and are pinned hardest: a number is
 * never guessed (a wrong guess opens a conversation with a stranger), and the
 * body is URL-encoded (an unencoded `&` truncates a plan message at the
 * client's handset, and the practitioner sends half a sentence without
 * noticing).
 */

import { describe, expect, it } from 'vitest'
import { MAX_PREFILL_LENGTH, toWhatsAppNumber, whatsAppChatLink } from './clickToChat'

describe('the number', () => {
  it('drops the plus, because wa.me takes the bare international number', () => {
    expect(toWhatsAppNumber('+919876543210')).toBe('919876543210')
  })

  it('tolerates separators a paste introduces', () => {
    expect(toWhatsAppNumber('+91 98765-43210')).toBe('919876543210')
    expect(toWhatsAppNumber('+91 (98765) 43210')).toBe('919876543210')
  })

  it('refuses a number with no country code rather than assuming one', () => {
    // 🔒 The decision this file exists to protect. `normalise_mobile` adds +91
    // when the client is *created*; guessing again here would be a second,
    // weaker copy of that rule — and when it guessed wrong it would open a
    // conversation with someone else entirely.
    expect(toWhatsAppNumber('9876543210')).toBeNull()
    expect(toWhatsAppNumber('09876543210')).toBeNull()
  })

  it('refuses anything that is not E.164', () => {
    expect(toWhatsAppNumber('+0123456789')).toBeNull() // leading zero country
    expect(toWhatsAppNumber('+91987')).toBeNull() // too short
    expect(toWhatsAppNumber('+9198765432109876')).toBeNull() // too long
    expect(toWhatsAppNumber('not a number')).toBeNull()
  })

  it('treats an absent number as absent', () => {
    expect(toWhatsAppNumber(null)).toBeNull()
    expect(toWhatsAppNumber(undefined)).toBeNull()
    expect(toWhatsAppNumber('')).toBeNull()
  })
})

describe('the link', () => {
  it('opens the conversation when there is nothing to prefill', () => {
    expect(whatsAppChatLink('+919876543210')).toEqual({
      href: 'https://wa.me/919876543210',
      reason: null,
    })
  })

  it('url-encodes the prefilled message', () => {
    const { href } = whatsAppChatLink('+919876543210', 'Hi Anjali & team: plan #2 is ready')

    expect(href).toBe(
      'https://wa.me/919876543210?text=Hi%20Anjali%20%26%20team%3A%20plan%20%232%20is%20ready',
    )
    // ⚠️ The two characters that would silently end the query value early.
    expect(href).not.toContain('&team')
    expect(href).not.toContain('#2')
  })

  it('encodes newlines, which a rendered template body carries', () => {
    const { href } = whatsAppChatLink('+919876543210', 'Line one\nLine two')
    expect(href).toContain('%0ALine%20two')
  })

  it('ignores a message that is only whitespace', () => {
    expect(whatsAppChatLink('+919876543210', '   ').href).toBe('https://wa.me/919876543210')
  })

  it('truncates at WhatsApp’s own limit rather than letting the client do it silently', () => {
    const { href } = whatsAppChatLink('+919876543210', 'a'.repeat(MAX_PREFILL_LENGTH + 50))
    expect(href).toBe(`https://wa.me/919876543210?text=${'a'.repeat(MAX_PREFILL_LENGTH)}`)
  })

  it('explains a missing number, and what to do about it', () => {
    // NFR-063 — what went wrong *and* the fix.
    const { href, reason } = whatsAppChatLink(null)
    expect(href).toBeNull()
    expect(reason).toMatch(/no mobile number on file/i)
    expect(reason).toMatch(/add one/i)
  })

  it('explains a malformed number differently from a missing one', () => {
    const { href, reason } = whatsAppChatLink('9876543210')
    expect(href).toBeNull()
    expect(reason).toMatch(/country code/i)
  })

  it('never produces a link to a host other than wa.me', () => {
    // 🔒 The href is rendered into an anchor. A number field that could steer
    // the host would be an open redirect the practitioner clicks themselves.
    for (const candidate of ['+919876543210', '+14155552671', '+442071838750']) {
      expect(whatsAppChatLink(candidate).href).toMatch(/^https:\/\/wa\.me\/\d+/)
    }
  })
})
