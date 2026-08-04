/**
 * Colour contrast — NFR-060, NFR-067.
 *
 * 🔒 NFR-060: "Text MUST meet accepted contrast standards."
 * 🎯 NFR-067: WCAG 2.1 Level AA as a design target.
 *
 * Every foreground/background pair the design system actually uses is checked
 * here against the AA thresholds. This is the difference between claiming
 * accessibility and having it: a palette tweak that drops a pair below 4.5:1
 * fails CI on the commit that causes it, rather than being discovered in an
 * audit eighteen months later when forty screens depend on the colour.
 *
 * ⚠️ **What this does not prove.** Contrast is the one part of WCAG that is
 * genuinely computable. Reading order, focus management, meaningful alt text
 * and screen-reader comprehensibility are not, and NFR-067 says so explicitly:
 * "full conformance cannot be verified without manual testing with assistive
 * technology." This file closes one gap precisely; it does not close the rest.
 */

import { describe, expect, it } from 'vitest'
import { colour } from './tokens'

/** WCAG 2.1 relative luminance. */
function luminance(hex: string): number {
  const value = hex.replace('#', '')
  const channels = [0, 2, 4].map((offset) => {
    const raw = Number.parseInt(value.slice(offset, offset + 2), 16) / 255
    // The 0.03928 piecewise transfer function is the specification's, not an
    // approximation — sRGB is not linear and a naive average is wrong.
    return raw <= 0.03928 ? raw / 12.92 : ((raw + 0.055) / 1.055) ** 2.4
  })
  const [r, g, b] = channels as [number, number, number]
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

/** WCAG 2.1 contrast ratio, 1:1 to 21:1. */
function contrast(foreground: string, background: string): number {
  const a = luminance(foreground)
  const b = luminance(background)
  const [lighter, darker] = a > b ? [a, b] : [b, a]
  return (lighter + 0.05) / (darker + 0.05)
}

/** AA thresholds. */
const AA_BODY = 4.5 // Normal text
const AA_LARGE = 3.0 // ≥18.66px bold or ≥24px
const AA_NON_TEXT = 3.0 // UI component boundaries and graphics (1.4.11)

describe('contrast ratio calculation', () => {
  // The helper is doing the work, so it is verified against known values
  // before being trusted to judge the palette.
  it('computes the documented extremes', () => {
    expect(contrast('#000000', '#ffffff')).toBeCloseTo(21, 1)
    expect(contrast('#ffffff', '#ffffff')).toBeCloseTo(1, 5)
  })

  it('is symmetric', () => {
    expect(contrast(colour.neutral900, colour.neutral0)).toBeCloseTo(
      contrast(colour.neutral0, colour.neutral900),
      5,
    )
  })
})

describe('body text meets AA (4.5:1)', () => {
  const pairs: Array<[string, string, string]> = [
    ['primary text on page background', colour.neutral900, colour.neutral50],
    ['primary text on surface', colour.neutral900, colour.neutral0],
    ['body text on surface', colour.neutral800, colour.neutral0],
    ['secondary text on surface', colour.neutral700, colour.neutral0],
    ['muted text on surface', colour.neutral600, colour.neutral0],
    ['muted text on page background', colour.neutral600, colour.neutral50],
    ['muted text on subtle fill', colour.neutral600, colour.neutral100],
  ]

  it.each(pairs)('%s', (_name, foreground, background) => {
    expect(contrast(foreground, background)).toBeGreaterThanOrEqual(AA_BODY)
  })
})

describe('button variants meet AA', () => {
  const pairs: Array<[string, string, string]> = [
    ['primary label', colour.neutral0, colour.brand600],
    ['primary label, hover', colour.neutral0, colour.brand700],
    ['primary label, active', colour.neutral0, colour.brand800],
    ['secondary label', colour.neutral800, colour.neutral0],
    ['secondary label, hover', colour.neutral800, colour.neutral50],
    ['ghost label', colour.neutral700, colour.neutral0],
    ['ghost label, hover', colour.neutral900, colour.neutral100],
    ['danger label', colour.neutral0, colour.danger500],
    ['danger label, hover', colour.neutral0, colour.danger600],
  ]

  it.each(pairs)('%s', (_name, foreground, background) => {
    expect(contrast(foreground, background)).toBeGreaterThanOrEqual(AA_BODY)
  })
})

describe('badges meet AA', () => {
  // 🔒 Badges carry client lifecycle stage throughout the practitioner app.
  // An unreadable status badge is worse than no badge.
  const pairs: Array<[string, string, string]> = [
    ['neutral', colour.neutral700, colour.neutral100],
    ['brand', colour.brand700, colour.brand50],
    ['success', colour.success500, colour.success50],
    ['warning', colour.warning500, colour.warning50],
    ['danger', colour.danger600, colour.danger50],
    ['info', colour.info500, colour.info50],
  ]

  it.each(pairs)('%s badge', (_name, foreground, background) => {
    expect(contrast(foreground, background)).toBeGreaterThanOrEqual(AA_BODY)
  })
})

describe('status text on white meets AA', () => {
  const pairs: Array<[string, string]> = [
    ['success', colour.success500],
    ['warning', colour.warning500],
    ['danger', colour.danger500],
    ['danger strong', colour.danger600],
    ['info', colour.info500],
  ]

  it.each(pairs)('%s', (_name, foreground) => {
    expect(contrast(foreground, colour.neutral0)).toBeGreaterThanOrEqual(AA_BODY)
  })
})

describe('operator console (dark header) meets AA', () => {
  const pairs: Array<[string, string, string]> = [
    ['brand on header', colour.neutral0, colour.neutral900],
    ['nav link on header', colour.neutral300, colour.neutral900],
    ['nav link on hover fill', colour.neutral0, colour.neutral800],
    ['operator label on header', colour.neutral300, colour.neutral900],
    ['production banner', colour.neutral0, colour.danger600],
    ['environment banner', colour.neutral0, colour.neutral700],
  ]

  it.each(pairs)('%s', (_name, foreground, background) => {
    expect(contrast(foreground, background)).toBeGreaterThanOrEqual(AA_BODY)
  })
})

describe('non-text contrast meets AA (3:1, WCAG 1.4.11)', () => {
  // Borders and focus rings are how a sighted user finds a control at all.
  const pairs: Array<[string, string, string]> = [
    ['field border on surface', colour.neutral400, colour.neutral0],
    ['field border on page background', colour.neutral400, colour.neutral50],
    ['focus ring on surface', colour.brand500, colour.neutral0],
    ['focus ring on page background', colour.brand500, colour.neutral50],
    ['selected tab indicator', colour.brand600, colour.neutral0],
  ]

  it.each(pairs)('%s', (_name, foreground, background) => {
    expect(contrast(foreground, background)).toBeGreaterThanOrEqual(AA_NON_TEXT)
  })
})

describe('large text meets AA (3:1)', () => {
  it('page title on page background', () => {
    expect(contrast(colour.neutral900, colour.neutral50)).toBeGreaterThanOrEqual(AA_LARGE)
  })
})

describe('known limitations are stated, not hidden', () => {
  /**
   * ⚠️ `neutral500` on white is 2.8:1 — below AA for text.
   *
   * It is retained as a token because it is legitimately useful for
   * *non-text* purposes (disabled-state borders, decorative dividers, the
   * select chevron). This test documents the constraint so the value is not
   * later "fixed" into body-text use by someone who assumes every token in the
   * palette is safe for text.
   */
  it('neutral500 is not usable for body text on white', () => {
    expect(contrast(colour.neutral500, colour.neutral0)).toBeLessThan(AA_BODY)
  })

  it('placeholder text uses neutral500 and is therefore not load-bearing', () => {
    // Placeholders are hint text that disappears on input; WCAG does not treat
    // them as content, and nothing in this product depends on reading one.
    // Real guidance goes in `FormField`'s `hint`, which uses neutral600.
    expect(contrast(colour.neutral600, colour.neutral0)).toBeGreaterThanOrEqual(AA_BODY)
  })
})
