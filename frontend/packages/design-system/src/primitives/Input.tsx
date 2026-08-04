import { forwardRef } from 'react'
import type { InputHTMLAttributes } from 'react'
import { cx } from '../utils/cx'
import styles from './Field.module.css'

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  /** Marks the field invalid, wiring `aria-invalid` for assistive technology. */
  invalid?: boolean
}

/**
 * Input — a single-line text field.
 *
 * 🔒 NFR-062 — "form inputs MUST have programmatically associated labels."
 * This primitive deliberately does **not** render its own label. Pairing an
 * input with a label, a hint and an error message is `FormField`'s job, and
 * routing every field through it is what makes the association automatic
 * rather than a thing each screen remembers. A bare `Input` is for the rare
 * case where the label lives elsewhere (a table filter, a search box with a
 * visible icon) — those must pass `aria-label` themselves.
 *
 * Forwards its ref so `FormField` and form libraries can focus it.
 */
export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { invalid, className, type = 'text', ...props },
  ref,
) {
  return (
    <input
      ref={ref}
      type={type}
      aria-invalid={invalid || undefined}
      className={cx(styles.field, styles.input, className)}
      {...props}
    />
  )
})
