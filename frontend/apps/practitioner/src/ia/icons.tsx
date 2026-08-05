import type { ReactNode } from 'react'

/**
 * Navigation icons for the practitioner IA.
 *
 * ⚠️ Hand-written SVG rather than an icon package. NFR-078 asks whether a solo
 * developer can justify each dependency; an icon library ships thousands of
 * glyphs to deliver eight, and the bundle budget (NFR-002) is measured on what
 * we ship, not on what we import.
 *
 * `aria-hidden` throughout: every icon here sits beside its own text label, so
 * announcing it would duplicate the label.
 */
function icon(path: ReactNode): ReactNode {
  return (
    <svg
      viewBox="0 0 24 24"
      width="20"
      height="20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {path}
    </svg>
  )
}

export const dashboardIcon = icon(
  <>
    <rect x="3" y="3" width="7" height="9" rx="1" />
    <rect x="14" y="3" width="7" height="5" rx="1" />
    <rect x="14" y="12" width="7" height="9" rx="1" />
    <rect x="3" y="16" width="7" height="5" rx="1" />
  </>,
)

export const clientsIcon = icon(
  <>
    <circle cx="9" cy="8" r="3.25" />
    <path d="M2.5 20a6.5 6.5 0 0 1 13 0" />
    <path d="M16 5.5a3 3 0 0 1 0 5.75M17.5 20a6.6 6.6 0 0 0-2-4.7" />
  </>,
)

export const leadsIcon = icon(
  <>
    <path d="M4 6h16v12H4z" />
    <path d="m4 7 8 6 8-6" />
  </>,
)

export const plansIcon = icon(
  <>
    <path d="M6 3h9l4 4v14H6z" />
    <path d="M14 3v5h5" />
    <path d="M9.5 13h5M9.5 16.5h3.5" />
  </>,
)

export const appointmentsIcon = icon(
  <>
    <rect x="3.5" y="5" width="17" height="16" rx="2" />
    <path d="M3.5 10h17M8 3v4M16 3v4" />
  </>,
)

export const messagesIcon = icon(
  <>
    <path d="M20.5 12a8.5 8.5 0 0 1-12.4 7.5L3.5 21l1.5-4.4A8.5 8.5 0 1 1 20.5 12Z" />
  </>,
)

export const settingsIcon = icon(
  <>
    <circle cx="12" cy="12" r="3" />
    <path d="M12 2.5v3M12 18.5v3M4.2 7l2.6 1.5M17.2 15.5l2.6 1.5M4.2 17l2.6-1.5M17.2 8.5l2.6-1.5" />
  </>,
)
