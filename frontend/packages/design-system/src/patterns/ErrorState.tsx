import type { ReactNode } from 'react'
import { cx } from '../utils/cx'
import styles from './ErrorState.module.css'

export interface ErrorStateProps {
  /**
   * What went wrong, in plain language.
   *
   * 🔒 NFR-063 — "never an error code alone." "We couldn't load your clients",
   * not "Error 500" and not "Something went wrong."
   */
  title: ReactNode
  /**
   * 🔒 NFR-063 — "and what to do next."
   *
   * Required. An error that does not tell the user their next move leaves them
   * with a dead end and a support ticket. "Check your connection and try
   * again. If this keeps happening, contact support."
   */
  whatToDoNext: ReactNode
  /** The retry action. Present wherever retrying is meaningful. */
  action?: ReactNode
  /**
   * A support reference.
   *
   * 🔒 NFR-033 — an identifier only. Never a stack trace, a database message
   * or anything derived from clinical data: this string is read aloud over the
   * phone and pasted into emails.
   */
  reference?: string
  size?: 'sm' | 'md'
  className?: string
}

/**
 * ErrorState — a failure the user needs to act on.
 *
 * 🔒 NFR-063: "Error messages MUST state what went wrong and what to do next,
 * in plain language, never an error code alone."
 *
 * The prop is named `whatToDoNext` rather than `description` for the same
 * reason `ConfirmDialog.consequence` is: the name is the requirement, and a
 * required prop is harder to skip than a guideline in a document.
 *
 * ⚠️ Distinct from `EmptyState`. "No clients yet" is a normal, expected state
 * with a happy next step; "We couldn't load your clients" is a failure. Showing
 * an empty state for a failed request is actively misleading — the practitioner
 * concludes their data is gone.
 */
export function ErrorState({
  title,
  whatToDoNext,
  action,
  reference,
  size = 'md',
  className,
}: ErrorStateProps) {
  return (
    // 🔒 `role="alert"` so the failure is announced when it replaces the
    // content, rather than silently swapping in for a sighted-only audience.
    <div role="alert" className={cx(styles.error, styles[size], className)}>
      <div className={styles.icon} aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none">
          <path
            d="M12 9v4m0 4h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </div>

      <p className={styles.title}>{title}</p>
      <p className={styles.next}>{whatToDoNext}</p>

      {action && <div className={styles.actions}>{action}</div>}

      {reference && (
        <p className={styles.reference}>
          Reference: <code className={styles.code}>{reference}</code>
        </p>
      )}
    </div>
  )
}
