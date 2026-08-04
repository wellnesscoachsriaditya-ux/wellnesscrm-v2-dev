import { useCallback, useEffect, useId, useRef } from 'react'
import type { ReactNode } from 'react'
import { cx } from '../utils/cx'
import styles from './Modal.module.css'

export interface ModalProps {
  /** Whether the modal is rendered. */
  open: boolean
  /** Called on Escape, backdrop click, or close button. */
  onClose: () => void
  /** The accessible name. Rendered as the heading. */
  title: ReactNode
  children: ReactNode
  /** Action buttons, right-aligned in the footer. */
  footer?: ReactNode
  /** `sm` for confirmations, `md` default, `lg` for forms. */
  size?: 'sm' | 'md' | 'lg'
  /**
   * Suppresses backdrop-click and Escape dismissal.
   *
   * ⚠️ Use only where dismissing would lose work. A modal the user cannot
   * escape is a trap, and Escape is the behaviour every user expects.
   */
  dismissible?: boolean
  className?: string
}

/** Elements that can receive keyboard focus. Used for the focus trap. */
const FOCUSABLE_PARTS = [
  'a[href]',
  'button:not(:disabled)',
  'input:not(:disabled)',
  'select:not(:disabled)',
  'textarea:not(:disabled)',
  '[tabindex]:not([tabindex="-1"])',
] as const

const FOCUSABLE = FOCUSABLE_PARTS.join(', ')

/**
 * Scope a focusable-element query to one region.
 *
 * ⚠️ Each part must be prefixed individually. `` `${scope} ${FOCUSABLE}` ``
 * looks right and is wrong: a comma-separated selector list binds the
 * descendant combinator to the *first* entry only, so the remaining five
 * match document-wide and the scope silently does nothing.
 */
function scopedFocusable(scope: string): string {
  return FOCUSABLE_PARTS.map((part) => `${scope} ${part}`).join(', ')
}

/**
 * Whether an element is actually visible, and so worth focusing.
 *
 * ⚠️ The obvious check — `element.offsetParent !== null` — works in a browser
 * and is useless under test: jsdom has no layout engine, so `offsetParent` is
 * always `null`. Using it silently emptied the focusable list, which made the
 * trap block *every* Tab instead of only the wrapping one. The bug was caught
 * by the trap test, not by clicking around, which is the argument for having it.
 */
function isVisible(element: HTMLElement): boolean {
  if (typeof element.checkVisibility === 'function') {
    return element.checkVisibility()
  }
  const style = getComputedStyle(element)
  return style.display !== 'none' && style.visibility !== 'hidden'
}

/**
 * Modal — a focus-trapping dialog.
 *
 * This is the most accessibility-critical primitive in the system, because a
 * dialog done naively is actively hostile: a screen-reader user is left in the
 * page behind it with no idea it opened, and a keyboard user tabs into content
 * they cannot see. Four things are therefore non-negotiable, and all four are
 * implemented here once so that no screen has to remember them:
 *
 * 1. **`role="dialog"` + `aria-modal`** — announces the context change.
 * 2. **Focus moves in on open, and back on close.** Losing your place in the
 *    page after closing a dialog is disorienting; returning focus to the
 *    trigger is what makes it feel like nothing moved.
 * 3. **Tab is trapped** (WCAG 2.1.2, "no keyboard trap" — the modal is the
 *    exception that requires containment while it is open).
 * 4. **Escape closes**, and the body does not scroll behind it.
 *
 * 🔒 NFR-061 — keyboard reachable and operable.
 */
export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  size = 'md',
  dismissible = true,
  className,
}: ModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  // Where focus was before we opened, so it can be restored.
  const previouslyFocused = useRef<HTMLElement | null>(null)
  // Unique per instance: a hard-coded id would break the accessible name of
  // both dialogs if one ever opened above another.
  const titleId = useId()

  const handleKeyDown = useCallback(
    (event: KeyboardEvent) => {
      if (event.key === 'Escape' && dismissible) {
        event.stopPropagation()
        onClose()
        return
      }

      if (event.key !== 'Tab') return

      const dialog = dialogRef.current
      if (!dialog) return

      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(isVisible)
      if (focusable.length === 0) {
        // Nothing focusable inside: keep focus on the dialog rather than
        // letting Tab escape into the page behind.
        event.preventDefault()
        return
      }

      const first = focusable[0]!
      const last = focusable[focusable.length - 1]!

      // Wrap at both ends. Without this, Tab from the last element lands in
      // the browser chrome and the next Tab is inside the hidden page.
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    },
    [dismissible, onClose],
  )

  useEffect(() => {
    if (!open) return

    previouslyFocused.current = document.activeElement as HTMLElement | null

    // 🔒 Focus order is deliberate: body → footer → close button → dialog.
    //
    // Not simply "first focusable in DOM order", which would land on the close
    // button every time, because it is rendered first in the header. Focusing
    // the X is almost never what the user wants: in a form dialog they want the
    // first field, and in a confirmation they want the safe default action.
    //
    // This ordering is what makes `ConfirmDialog` focus Cancel rather than the
    // destructive button — its actions live in the footer and its body is just
    // text, so the footer's first control wins.
    const dialog = dialogRef.current
    const firstIn = (region: string) =>
      dialog?.querySelector<HTMLElement>(scopedFocusable(`[data-modal-region="${region}"]`))

    const target =
      firstIn('body') ??
      firstIn('footer') ??
      dialog?.querySelector<HTMLElement>('[data-modal-close]') ??
      dialog ??
      null
    target?.focus()

    document.addEventListener('keydown', handleKeyDown, true)

    // Scroll lock. Compensating for the scrollbar width prevents the layout
    // shift that otherwise makes the whole page jump when a modal opens.
    const { overflow, paddingRight } = document.body.style
    const scrollbar = window.innerWidth - document.documentElement.clientWidth
    document.body.style.overflow = 'hidden'
    if (scrollbar > 0) document.body.style.paddingRight = `${scrollbar}px`

    return () => {
      document.removeEventListener('keydown', handleKeyDown, true)
      document.body.style.overflow = overflow
      document.body.style.paddingRight = paddingRight
      // Restore focus so the user is back where they left off.
      previouslyFocused.current?.focus()
    }
  }, [open, handleKeyDown])

  if (!open) return null

  return (
    <div
      className={styles.backdrop}
      // A backdrop click is a dismissal gesture, but only when the click both
      // started and ended on the backdrop — dragging a text selection out of
      // the dialog should not close it.
      onMouseDown={(event) => {
        if (dismissible && event.target === event.currentTarget) onClose()
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={cx(styles.dialog, styles[size], className)}
      >
        <div className={styles.header}>
          <h2 id={titleId} className={styles.title}>
            {title}
          </h2>
          {dismissible && (
            <button
              type="button"
              data-modal-close=""
              onClick={onClose}
              className={styles.close}
              aria-label="Close dialog"
            >
              <svg viewBox="0 0 24 24" width="20" height="20" fill="none" aria-hidden="true">
                <path
                  d="M18 6 6 18M6 6l12 12"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                />
              </svg>
            </button>
          )}
        </div>

        <div data-modal-region="body" className={styles.body}>
          {children}
        </div>

        {footer && (
          <div data-modal-region="footer" className={styles.footer}>
            {footer}
          </div>
        )}
      </div>
    </div>
  )
}
