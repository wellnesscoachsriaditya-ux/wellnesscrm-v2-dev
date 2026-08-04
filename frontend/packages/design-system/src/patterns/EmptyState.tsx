import type { ReactNode } from 'react'
import { cx } from '../utils/cx'
import styles from './EmptyState.module.css'

export interface EmptyStateProps {
  /**
   * What is not here — stated plainly. "No clients yet", not "No data".
   */
  title: ReactNode
  /**
   * 🔒 NFR-064 — "explains what to do next."
   *
   * Required, not optional. An empty state that only says "Nothing here" is
   * the failure this component exists to prevent: the user is told there is
   * nothing, and left with no idea whether that is a bug, a filter, or simply
   * a thing they have not done yet.
   */
  description: ReactNode
  /** The action that resolves the emptiness — usually "Add the first X". */
  action?: ReactNode
  /** A secondary route out, e.g. "Clear filters". */
  secondaryAction?: ReactNode
  /** Decorative illustration or icon. */
  icon?: ReactNode
  /** `sm` inside a card or a tab panel; `md` for a full page. */
  size?: 'sm' | 'md'
  className?: string
}

/**
 * EmptyState — what a list shows when it has nothing.
 *
 * 🔒 NFR-064: "Every list and data view MUST have a designed empty state that
 * explains what to do next."
 *
 * ⚠️ **Why this is built in S0, before any screen exists.** NFR-064 applies to
 * roughly forty screens. As a component it costs nothing to satisfy — one
 * import. As a per-screen discipline it would be forty individual acts of
 * remembering, under deadline, and it would not happen. The Implementation
 * Plan calls this out explicitly for exactly that reason.
 *
 * Three distinct situations, all of which look like "no rows" and none of which
 * mean the same thing to the user:
 *
 * * **Nothing yet** — offer the action that creates the first one.
 * * **Nothing matching a filter** — offer to clear the filter, or the search
 *   looks broken.
 * * **Nothing because of an error** — that is `ErrorState`, not this.
 */
export function EmptyState({
  title,
  description,
  action,
  secondaryAction,
  icon,
  size = 'md',
  className,
}: EmptyStateProps) {
  return (
    <div className={cx(styles.empty, styles[size], className)}>
      {icon && (
        <div className={styles.icon} aria-hidden="true">
          {icon}
        </div>
      )}
      <p className={styles.title}>{title}</p>
      <p className={styles.description}>{description}</p>
      {(action || secondaryAction) && (
        <div className={styles.actions}>
          {action}
          {secondaryAction}
        </div>
      )}
    </div>
  )
}
