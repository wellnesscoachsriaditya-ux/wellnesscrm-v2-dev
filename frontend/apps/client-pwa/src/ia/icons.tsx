import type { ReactNode } from 'react'

/**
 * Bottom-bar icons for the client PWA.
 *
 * 🔒 Required, not decorative. `MobileShell` types `icon` as mandatory because
 * a bottom bar of four text labels is unreadable at thumb size, and the IA is
 * validated with `requireNavIcons` so a missing one fails at module load.
 *
 * 24px rather than the practitioner app's 20px: these are the primary touch
 * targets on a phone (NFR-059).
 */
function icon(path: ReactNode): ReactNode {
  return (
    <svg
      viewBox="0 0 24 24"
      width="24"
      height="24"
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

export const todayIcon = icon(
  <>
    <rect x="3.5" y="5" width="17" height="16" rx="2" />
    <path d="M3.5 10h17M8 3v4M16 3v4" />
    <path d="m9.5 15 1.75 1.75L15 13.5" />
  </>,
)

export const progressIcon = icon(
  <>
    <path d="M4 19V5M4 19h16" />
    <path d="m7.5 15 3.5-4 3 2.5 4.5-6" />
  </>,
)

export const messagesIcon = icon(
  <>
    <path d="M20.5 12a8.5 8.5 0 0 1-12.4 7.5L3.5 21l1.5-4.4A8.5 8.5 0 1 1 20.5 12Z" />
  </>,
)

export const meIcon = icon(
  <>
    <circle cx="12" cy="8.5" r="3.5" />
    <path d="M4.5 20.5a7.5 7.5 0 0 1 15 0" />
  </>,
)
