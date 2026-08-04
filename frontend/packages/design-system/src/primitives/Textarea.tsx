import { forwardRef } from 'react'
import type { TextareaHTMLAttributes } from 'react'
import { cx } from '../utils/cx'
import styles from './Field.module.css'

export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  invalid?: boolean
}

/**
 * Textarea — multi-line text.
 *
 * Used for consultation notes and plan instructions, which are frequently long.
 * `resize: vertical` is left enabled deliberately: a practitioner writing a
 * long note should be able to make the box bigger, and disabling resize is a
 * common, gratuitous usability loss.
 */
export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { invalid, className, rows = 4, ...props },
  ref,
) {
  return (
    <textarea
      ref={ref}
      rows={rows}
      aria-invalid={invalid || undefined}
      className={cx(styles.field, styles.textarea, className)}
      {...props}
    />
  )
})
