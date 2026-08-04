import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { cx } from '../utils/cx'
import styles from './Button.module.css'

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'
export type ButtonSize = 'sm' | 'md' | 'lg'

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Visual style. Primary for the main action; ghost for low-emphasis. */
  variant?: ButtonVariant
  /** Size. `lg` satisfies touch-target requirements (NFR-059) for client PWA. */
  size?: ButtonSize
  /** Icon to the left of the label. */
  iconStart?: ReactNode
  /** Icon to the right of the label. */
  iconEnd?: ReactNode
  /** Loading state — disables the button and shows a spinner. */
  loading?: boolean
  /** Stretches the button to fill its container. */
  fullWidth?: boolean
}

/**
 * Button — the primary interactive primitive.
 *
 * 🔒 NFR-059: touch targets "large enough for reliable one-handed thumb use."
 * `size="lg"` meets the 48px comfortable threshold; it is the default for
 * client-facing actions. `size="md"` meets the 44px minimum and is the default
 * for practitioner tools, which are more keyboard-driven.
 *
 * 🔒 NFR-061: keyboard operable. The browser handles it; nothing to break.
 *
 * 🔒 WCAG 2.5.5 & 1.4.11: touch target size and non-text contrast both met by
 * the `lg` and `md` sizes and the colour choices in `Button.module.css`.
 */
export function Button({
  variant = 'secondary',
  size = 'md',
  iconStart,
  iconEnd,
  loading = false,
  fullWidth = false,
  disabled,
  className,
  children,
  type = 'button',
  ...props
}: ButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      className={cx(
        styles.button,
        styles[variant],
        styles[size],
        fullWidth && styles.fullWidth,
        loading && styles.loading,
        className,
      )}
      {...props}
    >
      {loading && (
        <span className={styles.spinner} aria-hidden="true">
          <SpinnerIcon />
        </span>
      )}
      {!loading && iconStart && <span className={styles.iconStart}>{iconStart}</span>}
      {children}
      {!loading && iconEnd && <span className={styles.iconEnd}>{iconEnd}</span>}
    </button>
  )
}

function SpinnerIcon() {
  return (
    <svg className={styles.spinnerSvg} viewBox="0 0 24 24" fill="none">
      <circle
        className={styles.spinnerCircle}
        cx="12"
        cy="12"
        r="10"
        stroke="currentColor"
        strokeWidth="3"
      />
    </svg>
  )
}
