import { cloneElement, useId } from 'react'
import type { ReactElement, ReactNode } from 'react'
import { cx } from '../utils/cx'
import styles from './FormField.module.css'

export interface FormFieldProps {
  /** The visible label. */
  label: ReactNode
  /**
   * The control. A single element — `Input`, `Select`, `Textarea` or any
   * component forwarding a ref and accepting `id` / `aria-describedby`.
   *
   * ⚠️ The prop types include `| undefined` explicitly because the workspace
   * sets `exactOptionalPropertyTypes`. Under that flag `foo?: string` and
   * `foo: string | undefined` are different types, and `cloneElement` passes
   * `undefined` for the attributes that do not apply — omitting the union makes
   * this a compile error rather than an accidental `aria-describedby="undefined"`.
   */
  children: ReactElement<{
    id?: string | undefined
    'aria-describedby'?: string | undefined
    'aria-invalid'?: boolean | undefined
    'aria-errormessage'?: string | undefined
    required?: boolean | undefined
  }>
  /** Guidance shown under the label, before the control is used. */
  hint?: ReactNode
  /**
   * Error message.
   *
   * 🔒 NFR-063 — "state what went wrong AND what to do next, in plain
   * language, never an error code alone." "Invalid input" fails this;
   * "Enter a mobile number with 10 digits" passes it.
   */
  error?: ReactNode
  required?: boolean
  className?: string
}

/**
 * FormField — label, control, hint and error, wired together.
 *
 * 🔒 NFR-062 — "form inputs MUST have programmatically associated labels."
 *
 * This component exists so that association is automatic rather than
 * remembered. It generates one id, points the `<label>`'s `htmlFor` at the
 * control, and injects `id` and `aria-describedby` into the child by cloning
 * it. A developer cannot use `FormField` and end up with an unlabelled input,
 * an orphaned hint, or an error a screen reader never reads out — which is
 * exactly what happens when each screen wires this by hand across forty forms.
 *
 * ⚠️ The `cloneElement` is deliberate. The alternatives are a render-prop
 * (verbose at every call site, so it gets bypassed) or context (invisible
 * coupling, and breaks when the control is wrapped). Cloning keeps the call
 * site to one line, which is what determines whether it actually gets used.
 */
export function FormField({
  label,
  children,
  hint,
  error,
  required = false,
  className,
}: FormFieldProps) {
  const id = useId()
  const hintId = hint ? `${id}-hint` : undefined
  const errorId = error ? `${id}-error` : undefined

  // Both, when both exist: a screen reader reads the hint and the error.
  const describedBy = [hintId, errorId].filter(Boolean).join(' ') || undefined

  const control = cloneElement(children, {
    id,
    'aria-describedby': describedBy,
    'aria-invalid': error ? true : undefined,
    'aria-errormessage': errorId,
    required: required || children.props.required,
  })

  return (
    <div className={cx(styles.field, className)}>
      <label htmlFor={id} className={styles.label}>
        {label}
        {required && (
          <>
            {/* 🔒 The asterisk is decorative — a screen reader announcing
             * "asterisk" conveys nothing. The word "required" is what carries
             * the meaning, and `required` on the control carries it natively. */}
            <span aria-hidden="true" className={styles.asterisk}>
              *
            </span>
            <span className={styles.srOnly}>(required)</span>
          </>
        )}
      </label>

      {hint && (
        <p id={hintId} className={styles.hint}>
          {hint}
        </p>
      )}

      {control}

      {error && (
        // `role="alert"` announces the error when it appears, without the user
        // having to navigate back to the field to discover it.
        <p id={errorId} role="alert" className={styles.error}>
          {error}
        </p>
      )}
    </div>
  )
}
