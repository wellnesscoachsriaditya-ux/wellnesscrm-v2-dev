/**
 * Token parity — ADR-03.
 *
 * 🔒 Tokens exist twice: as CSS custom properties (what components actually
 * use) and as TypeScript constants (for canvas, inline SVG and charts). Two
 * declarations of the same value is exactly the duplication NFR-069 calls a
 * defect — unless they are proven identical, which is what this file does.
 *
 * Without this test, a designer updating `--wc-brand-600` in the CSS would
 * leave `colour.brand600` stale, and the two would diverge silently until a
 * chart rendered in last season's brand colour.
 */

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { colour, duration, fontSize, radius, space, touchTarget } from './tokens'

// Vite rewrites `import.meta.url` to an http URL under the test transform, so
// the path is resolved from the project root instead.
const css = readFileSync(join(process.cwd(), 'src/tokens/tokens.css'), 'utf8')

/** Read a custom property's value out of `tokens.css`. */
function cssVar(name: string): string | undefined {
  // Only the `:root` block — the `prefers-reduced-motion` override
  // deliberately redeclares the duration tokens with different values.
  const root = css.slice(css.indexOf(':root'), css.indexOf('@media'))
  const match = new RegExp(`${name}:\\s*([^;]+);`).exec(root)
  return match?.[1]?.trim()
}

describe('CSS and TypeScript tokens agree', () => {
  it.each([
    ['--wc-brand-500', colour.brand500],
    ['--wc-brand-600', colour.brand600],
    ['--wc-brand-700', colour.brand700],
    ['--wc-neutral-0', colour.neutral0],
    ['--wc-neutral-500', colour.neutral500],
    ['--wc-neutral-900', colour.neutral900],
    ['--wc-success-500', colour.success500],
    ['--wc-warning-500', colour.warning500],
    ['--wc-danger-500', colour.danger500],
    ['--wc-danger-600', colour.danger600],
    ['--wc-info-500', colour.info500],
  ])('%s', (name, expected) => {
    expect(cssVar(name)).toBe(expected)
  })

  it.each([
    ['--wc-space-1', space[1]],
    ['--wc-space-4', space[4]],
    ['--wc-space-8', space[8]],
    ['--wc-space-16', space[16]],
  ])('%s', (name, expected) => {
    expect(cssVar(name)).toBe(expected)
  })

  it.each([
    ['--wc-font-size-xs', fontSize.xs],
    ['--wc-font-size-base', fontSize.base],
    ['--wc-font-size-2xl', fontSize['2xl']],
  ])('%s', (name, expected) => {
    expect(cssVar(name)).toBe(expected)
  })

  it.each([
    ['--wc-radius-md', radius.md],
    ['--wc-radius-full', radius.full],
  ])('%s', (name, expected) => {
    expect(cssVar(name)).toBe(expected)
  })

  it.each([
    ['--wc-touch-target-min', touchTarget.minimum],
    ['--wc-touch-target-comfortable', touchTarget.comfortable],
  ])('%s', (name, expected) => {
    expect(cssVar(name)).toBe(expected)
  })

  it.each([
    ['--wc-duration-fast', duration.fast],
    ['--wc-duration-normal', duration.normal],
  ])('%s', (name, expected) => {
    expect(cssVar(name)).toBe(expected)
  })
})

describe('accessibility invariants baked into the tokens', () => {
  it('touch targets meet the 44px WCAG 2.5.5 minimum', () => {
    // 🔒 NFR-059. A token below this would silently undersize every control
    // in the system.
    expect(Number.parseInt(touchTarget.minimum, 10)).toBeGreaterThanOrEqual(44)
    expect(Number.parseInt(touchTarget.comfortable, 10)).toBeGreaterThanOrEqual(
      Number.parseInt(touchTarget.minimum, 10),
    )
  })

  it('font sizes are relative, so the device text-size setting is honoured', () => {
    // 🔒 NFR-060 — "MUST respect the device's text-size setting." A px font
    // size ignores the user's browser preference outright.
    for (const [name, value] of Object.entries(fontSize)) {
      expect(value, `fontSize.${name} must be in rem, not px`).toMatch(/rem$/)
    }
  })

  it('spacing is relative, so layout scales with text', () => {
    for (const [name, value] of Object.entries(space)) {
      if (value === '0') continue
      expect(value, `space.${name} must be in rem`).toMatch(/rem$/)
    }
  })

  it('motion is disabled under prefers-reduced-motion', () => {
    // 🔒 The override block must zero every duration token. A single missed
    // token is a component that still animates for a user who asked it not to.
    const override = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'))
    for (const name of ['fast', 'normal', 'slow'] as const) {
      expect(override).toContain(`--wc-duration-${name}: 0ms`)
    }
  })
})
