/**
 * The public enquiry form's state — S2 Slice H.
 *
 * 🔒 The API layer (Arch §4.4). The screen renders fields and calls `submit`;
 * this owns both requests and turns a failure into something renderable.
 *
 * 🔒 **Two of FR-M2-008's three spam signals are produced here**, and neither is
 * a judgement:
 *
 * * `company` is the honeypot — a field hidden from humans, so any value in it
 *   is automation rather than evidence of it.
 * * `elapsed_seconds` is measured from when the form became usable to when it
 *   was submitted.
 *
 * ⚠️ **Both are reported, never acted on.** The score and the threshold live in
 * `kernel.leads`, server-side. A browser that decided "this looks like spam" and
 * declined to send would be a business rule in a component (NFR-068) — and one
 * an actual bot would simply skip.
 *
 * ⏳ `captcha_token` is left unset: no provider is wired (FR-M2-008 is satisfied
 * by the honeypot, timing and rate limiting at MVP). The field is in the request
 * type, so wiring one later is a change here and not a contract change.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, ApiTransportError } from '@wellnesscrm/api-client'
import {
  fetchPublicForm,
  submitEnquiry,
  type PublicForm,
  type EnquirySubmitRequest,
} from './publicFormApi'

export interface EnquiryDraft {
  fullName: string
  mobile: string
  email: string
  primaryGoal: string
  /** The honeypot. 🔒 Hidden from humans; a value here means automation. */
  company: string
  consentGranted: boolean
}

export const EMPTY_DRAFT: EnquiryDraft = {
  fullName: '',
  mobile: '',
  email: '',
  primaryGoal: '',
  company: '',
  consentGranted: false,
}

export interface PublicEnquiryState {
  form: PublicForm | null
  loading: boolean
  /** 🔒 True when the form is unavailable for any reason — EC-M2-07's one 404. */
  unavailable: boolean
  /** A load failure that is *not* a 404 — a network drop, a 500. Retryable. */
  loadError: string | null
  submitting: boolean
  /** Set once the 202 arrives. The form is replaced by the acknowledgement. */
  acknowledgement: string | null
  formError: string | null
  fieldErrors: Record<string, string | undefined>
  submit: (draft: EnquiryDraft) => Promise<void>
  retry: () => void
}

export function usePublicEnquiry(tenantSlug: string, source: string | null): PublicEnquiryState {
  const [form, setForm] = useState<PublicForm | null>(null)
  const [loading, setLoading] = useState(true)
  const [unavailable, setUnavailable] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [acknowledgement, setAcknowledgement] = useState<string | null>(null)
  const [formError, setFormError] = useState<string | null>(null)
  const [fieldErrors, setFieldErrors] = useState<Record<string, string | undefined>>({})
  const [attempt, setAttempt] = useState(0)

  /**
   * When the form became fillable, for the timing signal.
   *
   * ⚠️ A ref, not state: it must not trigger a render, and it must survive one.
   * Set when the form *loads* rather than when the component mounts — the
   * interval that matters is how long the prospect spent with the fields in
   * front of them, and on a slow connection those differ by seconds.
   */
  const readyAt = useRef<number | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setUnavailable(false)
    setLoadError(null)

    fetchPublicForm(tenantSlug, controller.signal)
      .then((loaded) => {
        if (controller.signal.aborted) return
        setForm(loaded)
        readyAt.current = Date.now()
        setLoading(false)
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return
        setForm(null)
        // 🔒 EC-M2-07 — unknown slug, paused form and suspended tenant are one
        // 404 with one neutral message. This branch must not try to distinguish
        // them, because the server deliberately did not.
        if (cause instanceof ApiError && cause.status === 404) {
          setUnavailable(true)
        } else {
          setLoadError(
            cause instanceof ApiError
              ? `${cause.message} ${cause.action}`
              : 'This form could not be loaded. Check your connection and try again.',
          )
        }
        setLoading(false)
      })

    return () => controller.abort()
  }, [tenantSlug, attempt])

  const submit = useCallback(
    async (draft: EnquiryDraft) => {
      if (form === null) return

      setSubmitting(true)
      setFormError(null)
      setFieldErrors({})

      // 🔒 EC-M2-01 — caught before the request so the prospect is corrected
      // inline rather than by a round trip. The server validates the same rule;
      // this is about the message arriving next to the field, not about trust.
      if (draft.mobile.trim() === '' && draft.email.trim() === '') {
        setFieldErrors({ mobile: 'Add a mobile number so your dietitian can reply.' })
        setSubmitting(false)
        return
      }

      // 🔒 FR-M2-004 / EC-M2-04 — consent is a precondition, not a field. The
      // server refuses with a 403 and creates nothing; refusing here as well
      // means the prospect is told which box to tick.
      if (!draft.consentGranted) {
        setFieldErrors({ consent: 'Please agree to the privacy notice before sending.' })
        setSubmitting(false)
        return
      }

      const body: EnquirySubmitRequest = {
        full_name: draft.fullName.trim(),
        primary_goal: draft.primaryGoal.trim(),
        consent_granted: true,
        consent_notice_id: form.consent.notice_id,
        // Empty strings are omitted rather than sent: `""` is a value the API
        // would try to validate, and the error would name a field the prospect
        // deliberately left blank.
        ...(draft.mobile.trim() !== '' ? { mobile: draft.mobile.trim() } : {}),
        ...(draft.email.trim() !== '' ? { email: draft.email.trim() } : {}),
        // 🔒 FR-M2-009 — attribution from the link the prospect followed. Sent
        // verbatim; `normalise_source` server-side decides what it means, so a
        // channel spelled two ways still lands in one bucket.
        ...(source !== null ? { source } : {}),
        ...(draft.company.trim() !== '' ? { company: draft.company } : {}),
        ...(readyAt.current !== null
          ? { elapsed_seconds: (Date.now() - readyAt.current) / 1000 }
          : {}),
      }

      try {
        const result = await submitEnquiry(tenantSlug, body)
        // 🔒 EC-M2-02 — whatever happened server-side, this is all there is to
        // show. `message` cannot vary on whether the mobile matched an existing
        // client, and nothing here may infer that it did.
        setAcknowledgement(result.message)
      } catch (cause: unknown) {
        if (cause instanceof ApiError) {
          const fields = cause.fieldErrors
          if (fields.length > 0) {
            setFieldErrors(Object.fromEntries(fields.map((field) => [field.field, field.message])))
          } else {
            setFormError(`${cause.message} ${cause.action}`)
          }
        } else if (cause instanceof ApiTransportError) {
          setFormError('Your enquiry could not be sent. Check your connection and try again.')
        } else {
          setFormError('Your enquiry could not be sent. Please try again.')
        }
      } finally {
        setSubmitting(false)
      }
    },
    [form, source, tenantSlug],
  )

  return {
    form,
    loading,
    unavailable,
    loadError,
    submitting,
    acknowledgement,
    formError,
    fieldErrors,
    submit,
    retry: useCallback(() => setAttempt((value) => value + 1), []),
  }
}
