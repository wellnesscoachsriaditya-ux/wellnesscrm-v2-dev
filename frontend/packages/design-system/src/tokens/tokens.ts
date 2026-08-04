/**
 * Design tokens — ADR-03.
 *
 * 🔒 This file and `tokens.css` are the ONLY place raw colour, spacing, type
 * and radius values exist. Everything else references them. The ESLint rule in
 * `frontend/eslint.config.js` fails the build on a raw hex or rgb() literal in
 * a feature app, which is what makes "only place" true rather than aspirational.
 *
 * V1 had no design system and its UI drifted (NFR-056). The countermeasure is
 * not a style guide document — it is this file plus a lint rule.
 *
 * ## Why CSS custom properties rather than JS objects
 *
 * Tokens are emitted as CSS variables and consumed through `var(--wc-*)`. A
 * JS-object theme would mean every themed value passes through React, making
 * runtime theme switching a re-render of the tree. CSS variables cascade, so a
 * theme change is one attribute on `<html>` — and they work in plain CSS files,
 * which is where most styling actually lives.
 *
 * The TypeScript exports below mirror the CSS for the cases that genuinely need
 * a token in JS (canvas, inline SVG, chart libraries). They are not a second
 * source of truth: `tokens.test.ts` asserts the two agree.
 */

// ─── Colour ──────────────────────────────────────────────────────────────
//
// A restrained palette. This is a clinical tool used for hours a day, not a
// marketing site; saturated colour is reserved for meaning (status, danger),
// never decoration.
//
// ⚠️ Contrast: every foreground/background pair used for text is checked
// against WCAG 2.1 AA (4.5:1 body, 3:1 large text and UI boundaries) by
// `contrast.test.ts`. NFR-060 requires it and NFR-067 targets AA overall.
// The test is the enforcement — a palette tweak that breaks contrast fails CI.

export const colour = {
  // Brand — a muted teal. Clinical, calm, not the saturated wellness green
  // every competitor uses.
  brand50: '#f0f7f6',
  brand100: '#d9ebe9',
  brand200: '#b4d7d3',
  brand300: '#84bcb6',
  brand400: '#559b95',
  brand500: '#38807a',
  brand600: '#2b6763',
  brand700: '#255351',
  brand800: '#214442',
  brand900: '#1e3938',

  // Neutrals carry most of the interface.
  neutral0: '#ffffff',
  neutral50: '#f8f9fa',
  neutral100: '#f1f3f5',
  neutral200: '#e9ecef',
  neutral300: '#dee2e6',
  // 🔒 neutral400 is the field-border colour. It must clear 3:1 against both
  // white and the page background (WCAG 1.4.11) — the border is the only thing
  // that tells a sighted user where an input begins. The conventional #ced4da
  // measures 1.5:1 and fails outright, which is why so many "clean" interfaces
  // have inputs that are invisible to anyone with low vision.
  neutral400: '#888f97',
  neutral500: '#adb5bd',
  // neutral600 carries secondary and muted body text, so it must clear 4.5:1
  // against neutral100 (the darkest surface it appears on), not just white.
  neutral600: '#63696f',
  neutral700: '#495057',
  neutral800: '#343a40',
  neutral900: '#212529',
  neutral950: '#121416',

  // Status. 🔒 Never the only signal — NFR-067/WCAG 1.4.1: colour must not be
  // the sole carrier of meaning, so every status component pairs colour with
  // an icon or text label.
  //
  // ⚠️ These are darker than the usual palette values because they are used as
  // *text* on tinted backgrounds in `Badge`, where 4.5:1 applies. Amber is the
  // hardest: a "readable-looking" #b7791f is only 3.6:1 on white and fails.
  success500: '#276749',
  success50: '#f0fff4',
  warning500: '#8a5a06',
  warning50: '#fffbeb',
  danger500: '#c53030',
  danger600: '#9b2c2c',
  danger50: '#fff5f5',
  info500: '#2b6cb0',
  info50: '#ebf8ff',
} as const

// ─── Type ────────────────────────────────────────────────────────────────
//
// 🔒 NFR-060 — "MUST respect the device's text-size setting."
//
// Sizes are in `rem`, never `px`. A px font size ignores the user's browser
// text-size preference entirely, which is a hard accessibility failure and the
// single most common way an interface becomes unusable for someone who needs
// larger text. This is why the scale below has no px values in it.

export const fontSize = {
  xs: '0.75rem',
  sm: '0.875rem',
  base: '1rem',
  lg: '1.125rem',
  xl: '1.25rem',
  '2xl': '1.5rem',
  '3xl': '1.875rem',
  '4xl': '2.25rem',
} as const

export const fontWeight = {
  regular: '400',
  medium: '500',
  semibold: '600',
  bold: '700',
} as const

export const lineHeight = {
  tight: '1.25',
  snug: '1.375',
  normal: '1.5',
  relaxed: '1.625',
} as const

export const fontFamily = {
  // System stack: no webfont download on the critical path (NFR-002, ≤2.5s on
  // 4G), and correct rendering of Devanagari and other Indic scripts, which
  // matters for Indian practitioner and client names.
  sans: "system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, 'Noto Sans', sans-serif",
  mono: "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace",
} as const

// ─── Spacing ─────────────────────────────────────────────────────────────
//
// A 4px base grid, expressed in rem. Every margin, padding and gap in the
// system is one of these — arbitrary spacing is what makes an interface look
// assembled by different people.

export const space = {
  0: '0',
  1: '0.25rem',
  2: '0.5rem',
  3: '0.75rem',
  4: '1rem',
  5: '1.25rem',
  6: '1.5rem',
  8: '2rem',
  10: '2.5rem',
  12: '3rem',
  16: '4rem',
  20: '5rem',
} as const

// ─── Sizing ──────────────────────────────────────────────────────────────

export const radius = {
  none: '0',
  sm: '0.25rem',
  md: '0.375rem',
  lg: '0.5rem',
  xl: '0.75rem',
  full: '9999px',
} as const

export const shadow = {
  sm: '0 1px 2px 0 rgb(0 0 0 / 0.05)',
  md: '0 2px 4px -1px rgb(0 0 0 / 0.08), 0 1px 2px -1px rgb(0 0 0 / 0.04)',
  lg: '0 8px 16px -4px rgb(0 0 0 / 0.10), 0 2px 4px -2px rgb(0 0 0 / 0.05)',
  // Modals sit above everything; a heavier shadow is what separates them.
  xl: '0 20px 32px -8px rgb(0 0 0 / 0.16), 0 4px 8px -4px rgb(0 0 0 / 0.06)',
} as const

/**
 * Minimum interactive target sizes.
 *
 * 🔒 NFR-059 — "large enough for reliable one-handed thumb use on a phone."
 *
 * 44px is the WCAG 2.5.5 / iOS HIG figure and the floor for anything a client
 * taps in the PWA. `comfortable` (48px) is the default for primary actions,
 * because the client app is used one-handed, often while cooking or shopping.
 *
 * ⚠️ In px deliberately — unlike type, a touch target is a physical finger
 * size and must not shrink when the user reduces their text size.
 */
export const touchTarget = {
  minimum: '44px',
  comfortable: '48px',
} as const

/** Stacking order. Centralised because z-index conflicts are otherwise endless. */
export const zIndex = {
  base: '0',
  dropdown: '1000',
  sticky: '1100',
  overlay: '1200',
  modal: '1300',
  toast: '1400',
} as const

/**
 * Motion.
 *
 * ⚠️ Every animation must be wrapped in `@media (prefers-reduced-motion: no-preference)`.
 * Vestibular disorders make unwanted motion genuinely painful, and the system
 * setting is the user telling us directly. `tokens.css` enforces this globally
 * by zeroing durations under `prefers-reduced-motion: reduce`.
 */
export const duration = {
  instant: '0ms',
  fast: '120ms',
  normal: '200ms',
  slow: '320ms',
} as const

export const easing = {
  standard: 'cubic-bezier(0.2, 0, 0.2, 1)',
  decelerate: 'cubic-bezier(0, 0, 0.2, 1)',
  accelerate: 'cubic-bezier(0.4, 0, 1, 1)',
} as const

/** Breakpoints. Mobile-first: these are `min-width`. NFR-055. */
export const breakpoint = {
  sm: '480px',
  md: '768px',
  lg: '1024px',
  xl: '1280px',
} as const

export const tokens = {
  colour,
  fontSize,
  fontWeight,
  lineHeight,
  fontFamily,
  space,
  radius,
  shadow,
  touchTarget,
  zIndex,
  duration,
  easing,
  breakpoint,
} as const

export type Tokens = typeof tokens
