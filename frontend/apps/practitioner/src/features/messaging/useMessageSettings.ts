/**
 * The practice-wide messaging screen — templates, controls and failures.
 *
 * 🔒 The API layer (Arch §4.4).
 *
 * ⚠️ **The preview is fetched, never rendered locally.** FR-M8-026 promises the
 * practitioner sees what their client will receive; a body assembled in the
 * browser would be a promise about different code than the one that sends.
 */

import { useCallback, useEffect, useState } from 'react'
import { ApiError } from '@wellnesscrm/api-client'
import {
  fetchFailures,
  fetchPreferences,
  fetchPreview,
  fetchTemplates,
  updatePreference,
  type Dispatch,
  type MessagePreference,
  type MessagePreview,
  type MessageTemplate,
  type PreferenceUpdate,
} from './messagingApi'

export interface MessageSettingsState {
  templates: MessageTemplate[]
  preferences: MessagePreference[]
  failures: Dispatch[]
  preview: MessagePreview | null
  previewing: string | null
  loading: boolean
  error: string | null
  settingsError: string | null
  busy: boolean
  showPreview: (code: string) => Promise<void>
  closePreview: () => void
  save: (update: PreferenceUpdate) => Promise<void>
}

function messageOf(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? `${cause.message} ${cause.action}` : fallback
}

export function useMessageSettings(): MessageSettingsState {
  const [templates, setTemplates] = useState<MessageTemplate[]>([])
  const [preferences, setPreferences] = useState<MessagePreference[]>([])
  const [failures, setFailures] = useState<Dispatch[]>([])
  const [preview, setPreview] = useState<MessagePreview | null>(null)
  const [previewing, setPreviewing] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [settingsError, setSettingsError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)

    void Promise.allSettled([
      fetchTemplates(controller.signal),
      fetchPreferences(controller.signal),
      fetchFailures(controller.signal),
    ]).then(([templateResult, preferenceResult, failureResult]) => {
      if (controller.signal.aborted) return

      if (templateResult.status === 'fulfilled') {
        setTemplates(templateResult.value)
        setError(null)
      } else {
        setError(messageOf(templateResult.reason, 'Message types could not be loaded.'))
      }

      if (preferenceResult.status === 'fulfilled') {
        setPreferences(preferenceResult.value)
      }

      // ⚠️ A failed *failures* request is not surfaced as a page error. It is a
      // secondary panel, and blanking the settings a practitioner came to change
      // because a report could not load would be the wrong trade.
      if (failureResult.status === 'fulfilled') {
        setFailures(failureResult.value)
      }

      setLoading(false)
    })

    return () => controller.abort()
  }, [])

  const showPreview = useCallback(async (code: string) => {
    setPreviewing(code)
    try {
      setPreview(await fetchPreview(code))
    } catch (cause) {
      setPreviewing(null)
      setSettingsError(messageOf(cause, 'That preview could not be loaded.'))
    }
  }, [])

  const closePreview = useCallback(() => {
    setPreviewing(null)
    setPreview(null)
  }, [])

  const save = useCallback(async (update: PreferenceUpdate) => {
    setBusy(true)
    try {
      await updatePreference(update)
      // 🔒 Re-read rather than patch: precedence between the four preference
      // scopes is the server's rule, and a local edit is how a screen comes to
      // disagree with what the engine will actually do.
      setPreferences(await fetchPreferences())
      setSettingsError(null)
    } catch (cause) {
      // ⚠️ The most likely failure here is deliberate: turning off an essential
      // message type is refused with a sentence, and the practitioner needs to
      // read it (FR-M8-027).
      setSettingsError(messageOf(cause, 'That setting could not be saved.'))
    } finally {
      setBusy(false)
    }
  }, [])

  return {
    templates,
    preferences,
    failures,
    preview,
    previewing,
    loading,
    error,
    settingsError,
    busy,
    showPreview,
    closePreview,
    save,
  }
}
