/**
 * The enquiry form's state — FR-M2-001, US-M2-01, EC-M2-07.
 *
 * 🔒 The API layer (Arch §4.4). The screen renders what this returns and calls
 * what it exposes; it never fetches.
 *
 * 🔒 **The share URL is the server's, not assembled here** (Principle 3). A
 * frontend building it from a slug would have to know the public URL shape, so
 * changing that shape — a custom domain, a shorter path — would become a
 * coordinated release instead of a server-side edit.
 *
 * ⚠️ **Pausing is optimistic, and reverts on failure.** The toggle is the one
 * control here and a round trip before the badge moves feels broken; but a badge
 * left switched after a refusal would tell the practitioner their form is off
 * while it is still collecting enquiries, which is the more expensive lie.
 */

import { useCallback, useEffect, useState } from 'react'
import { ApiError } from '@wellnesscrm/api-client'
import { fetchEnquiryForms, updateEnquiryForm, type EnquiryForm } from './enquiriesApi'

export interface EnquiryFormState {
  form: EnquiryForm | null
  /** 🔒 The complete public URL, server-supplied. `null` until the form loads. */
  shareUrl: string | null
  loading: boolean
  /** True while the pause/resume toggle is in flight. */
  busy: boolean
  error: string | null
  setActive: (isActive: boolean) => Promise<boolean>
}

function messageOf(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? `${cause.message} ${cause.action}` : fallback
}

export function useEnquiryForm(): EnquiryFormState {
  const [form, setForm] = useState<EnquiryForm | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()

    fetchEnquiryForms(controller.signal)
      .then((forms) => {
        if (controller.signal.aborted) return
        // ⚠️ The API returns a list because FR-M2-012 makes several per tenant a
        // Phase 2 feature. At MVP there is exactly one, so taking the first is
        // correct — but `?? null` matters: a tenant whose form has not been
        // created yet returns an empty list rather than an error.
        setForm(forms[0] ?? null)
        setError(null)
        setLoading(false)
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return
        // ⚠️ Not fatal to the screen. The enquiry *list* is the point of the
        // page; losing the share panel costs a link the practitioner can still
        // find elsewhere, so the error is held here rather than replacing the
        // whole view.
        setError(messageOf(cause, 'Your enquiry form could not be loaded.'))
        setLoading(false)
      })

    return () => controller.abort()
  }, [])

  return {
    form,
    shareUrl: form?.share_url ?? null,
    loading,
    busy,
    error,
    /**
     * Pause or resume the public form — EC-M2-07's practitioner-side half.
     *
     * 🔒 **Failures are state, not exceptions.** Returns `false` and sets
     * `error` rather than rejecting: a rejected promise from a JSX handler is an
     * unhandled rejection nobody sees, and "is my form still collecting
     * enquiries?" must have an answer on screen.
     */
    setActive: useCallback(
      async (isActive: boolean) => {
        if (form === null) return false

        const previous = form
        // Optimistic — see the module note on why, and why it reverts.
        setForm({ ...form, is_active: isActive })
        setBusy(true)
        setError(null)
        try {
          const updated = await updateEnquiryForm(form.id, { is_active: isActive })
          setForm(updated)
          return true
        } catch (cause: unknown) {
          setForm(previous)
          setError(
            messageOf(
              cause,
              isActive ? 'Your form could not be reactivated.' : 'Your form could not be paused.',
            ),
          )
          return false
        } finally {
          setBusy(false)
        }
      },
      [form],
    ),
  }
}
