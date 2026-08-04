import { forwardRef, useId } from 'react'
import type { InputHTMLAttributes, ReactNode } from 'react'
import { cx } from '../utils/cx'
import styles from './Field.module.css'

export interface CheckboxProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type'> {
  /** The visible label. Required — an unlabelled checkbox is unusable. */
  label: ReactNode
  /** Optional clarification below the label. */
  description?: ReactNode
}

/**
 * Checkbox — a labelled boolean.
 *
 * 🔒 NFR-062 — the label is a required prop, not optional, and is rendered
 * inside the `<label>` element wrapping the input. The association is therefore
 * structural: there is no way to use this component and end up with an
 * unlabelled checkbox, which is the failure `aria-label`-by-convention allows.
 *
 * 🔒 NFR-059 — the whole label row is the hit target, not the 20px box. On a
 * phone, tapping a 20px checkbox reliably is not realistic.
 */
export const Checkbox = forwardRef<HTMLInputElement, CheckboxProps>(function Checkbox(
  { label, description, className, id, ...props },
  ref,
) {
  const generatedId = useId()
  const inputId = id ?? generatedId
  const descriptionId = description ? `${inputId}-description` : undefined

  return (
    <label className={cx(styles.control, className)} htmlFor={inputId}>
      <input
        ref={ref}
        id={inputId}
        type="checkbox"
        aria-describedby={descriptionId}
        className={styles.controlInput}
        {...props}
      />
      <span className={styles.controlLabel}>
        {label}
        {description && (
          <span id={descriptionId} className={styles.controlDescription}>
            {description}
          </span>
        )}
      </span>
    </label>
  )
})
