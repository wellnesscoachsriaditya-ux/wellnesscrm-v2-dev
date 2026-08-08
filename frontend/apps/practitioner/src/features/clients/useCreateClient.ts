/**
 * Creating a client — the submit, and what comes back when it fails.
 *
 * 🔒 The API layer (Arch §4.4). The screen renders fields and calls `submit`;
 * this owns the request and turns a failure into something renderable.
 *
 * 🔒 **Field errors are bound back to their fields** (API §5.3, NFR-063). The
 * envelope's `details.fields` names the offending input, and dropping that
 * detail into one banner is what makes a validation message unactionable — the
 * practitioner is told the mobile is wrong without being shown which box.
 */

import { useCallback, useState } from 'react'
import { ApiError } from '@wellnesscrm/api-client'
import { createClient, type Client } from './api'

export interface CreateClientInput {
  fullName: string
  mobile: string
  email: string
}

export interface CreateClientState {
  submit: (input: CreateClientInput) => Promise<Client | null>
  busy: boolean
  /** Keyed by field name, as the envelope reports them. */
  fieldErrors: Record<string, string | undefined>
  formError: string | null
}

export function useCreateClient(): CreateClientState {
  const [busy, setBusy] = useState(false)
  const [fieldErrors, setFieldErrors] = useState<Record<string, string | undefined>>({})
  const [formError, setFormError] = useState<string | null>(null)

  const submit = useCallback(async (input: CreateClientInput): Promise<Client | null> => {
    setBusy(true)
    setFieldErrors({})
    setFormError(null)

    // 🔒 FR-M1-004 checked here as well as server-side, because the server's
    // refusal cannot name a field for this rule — "at least one of two" is not
    // a property of either one alone.
    if (input.mobile.trim() === '' && input.email.trim() === '') {
      setFormError('Add a mobile number or an email address so you can reach them.')
      setBusy(false)
      return null
    }

    try {
      return await createClient({
        full_name: input.fullName,
        // Empty strings are omitted rather than sent: `""` is a value the API
        // would try to validate as a mobile number, and the error it returned
        // would be about a field the practitioner deliberately left blank.
        ...(input.mobile.trim() !== '' ? { mobile: input.mobile.trim() } : {}),
        ...(input.email.trim() !== '' ? { email: input.email.trim() } : {}),
        stage: 'lead',
      })
    } catch (cause: unknown) {
      if (cause instanceof ApiError) {
        const fields = cause.fieldErrors
        if (fields.length > 0) {
          setFieldErrors(
            Object.fromEntries(fields.map((field) => [field.field, field.message])),
          )
        } else {
          setFormError(`${cause.message} ${cause.action}`)
        }
      } else {
        setFormError('That client could not be created. Check your connection and try again.')
      }
      return null
    } finally {
      setBusy(false)
    }
  }, [])

  return { submit, busy, fieldErrors, formError }
}
