import { forwardRef } from 'react'
import type { SelectHTMLAttributes } from 'react'
import { cx } from '../utils/cx'
import styles from './Field.module.css'

export interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  invalid?: boolean
}

/**
 * Select — a native dropdown.
 *
 * ⚠️ Deliberately the native `<select>`, not a custom listbox. A custom one
 * would let us style the option list, at the cost of reimplementing keyboard
 * navigation, type-ahead, screen-reader semantics and mobile behaviour — all of
 * which the native element already does correctly, and all of which are easy to
 * get subtly wrong. On Android and iOS the native picker is also markedly
 * better than any div-based imitation.
 *
 * When a searchable multi-select is genuinely needed (food search in S3), it
 * will be a separate `Combobox` primitive built on a real listbox pattern —
 * not a widened version of this.
 */
export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { invalid, className, children, ...props },
  ref,
) {
  return (
    <select
      ref={ref}
      aria-invalid={invalid || undefined}
      className={cx(styles.field, styles.select, className)}
      {...props}
    >
      {children}
    </select>
  )
})
