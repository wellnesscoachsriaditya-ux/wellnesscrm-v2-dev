import { cx } from '../utils/cx'
import styles from './Spinner.module.css'

export interface SpinnerProps {
  size?: 'sm' | 'md' | 'lg'
  /**
   * Accessible label. Defaults to "Loading".
   *
   * 🔒 A spinner with no accessible name is invisible to a screen reader — the
   * user is told nothing while they wait, which is indistinguishable from a
   * broken page. Where possible prefer a skeleton with real structure; a
   * spinner is the fallback when the shape of what is loading is unknown.
   */
  label?: string
  className?: string
}

/** Spinner — indeterminate progress. */
export function Spinner({ size = 'md', label = 'Loading', className }: SpinnerProps) {
  return (
    <span className={cx(styles.wrapper, styles[size], className)} role="status">
      <svg className={styles.svg} viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <circle
          className={styles.track}
          cx="12"
          cy="12"
          r="10"
          stroke="currentColor"
          strokeWidth="3"
        />
        <circle
          className={styles.indicator}
          cx="12"
          cy="12"
          r="10"
          stroke="currentColor"
          strokeWidth="3"
          strokeLinecap="round"
        />
      </svg>
      <span className={styles.srOnly}>{label}</span>
    </span>
  )
}
