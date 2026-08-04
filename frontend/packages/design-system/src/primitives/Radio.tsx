import { forwardRef, useId } from 'react'
import type { InputHTMLAttributes, ReactNode } from 'react'
import { cx } from '../utils/cx'
import styles from './Field.module.css'

export interface RadioProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type'> {
  label: ReactNode
  description?: ReactNode
}

/**
 * Radio — one option within a group.
 *
 * ⚠️ A lone radio is almost always a mistake: a single radio cannot be
 * deselected, so a yes/no question wants a `Checkbox` or two radios. Use
 * `RadioGroup` below, which supplies the `fieldset`/`legend` that makes the
 * group's purpose available to a screen reader (WCAG 1.3.1, NFR-062).
 */
export const Radio = forwardRef<HTMLInputElement, RadioProps>(function Radio(
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
        type="radio"
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

export interface RadioGroupProps {
  /** The group's question. Rendered as a `<legend>`. */
  legend: ReactNode
  /** Shared `name` — this is what makes the radios mutually exclusive. */
  name: string
  children: ReactNode
  /** Error message. Presence marks the group invalid. */
  error?: ReactNode
  className?: string
}

/**
 * RadioGroup — a `fieldset` wrapper that names the question.
 *
 * 🔒 Without a `fieldset`/`legend`, a screen reader announces "Male, radio
 * button, 1 of 3" with no indication of what is being asked. The legend is the
 * question; the labels are the answers.
 */
export function RadioGroup({
  legend,
  name,
  children,
  error,
  className,
}: RadioGroupProps) {
  const errorId = useId()

  return (
    <fieldset
      className={cx(styles.group, className)}
      aria-invalid={error ? true : undefined}
      aria-errormessage={error ? errorId : undefined}
      data-radio-group-name={name}
    >
      <legend className={styles.legend}>{legend}</legend>
      {children}
      {error && (
        <p id={errorId} className={styles.groupError} role="alert">
          {error}
        </p>
      )}
    </fieldset>
  )
}
