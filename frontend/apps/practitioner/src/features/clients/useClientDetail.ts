/**
 * The client detail screen's state — loading, mutating, and the 402.
 *
 * 🔒 **This is the layer allowed to call the API** (Arch §4.4, NFR-068). The
 * screen and its components receive data and callbacks; they never fetch.
 *
 * 🔒 **The entitlement refusal is state, not an exception to swallow.** A 402 is
 * an expected outcome of activating a client (FR-M1-002) — the practitioner has
 * done nothing wrong, they are simply at their plan's limit. Treating it as a
 * generic failure would lose `limit`, `used`, `plan_code` and `upgrade_to`, and
 * with them the entire ability to explain the refusal (FR-M0-045).
 */

import { useCallback, useEffect, useState } from 'react'
import { ApiError } from '@wellnesscrm/api-client'
import {
  archiveClient,
  changeStage,
  fetchClient,
  restoreClient,
  type Client,
  type SelectableStage,
} from './api'

/** The shape the refusal notice renders from — read straight off the envelope. */
export interface EntitlementRefusal {
  message: string
  action: string
  limit?: number | undefined
  used?: number | undefined
  planCode?: string | undefined
  upgradeTo?: string | undefined
}

export interface ClientDetailState {
  client: Client | null
  loading: boolean
  /** A failure that is not an entitlement limit — rendered as an error state. */
  error: string | null
  /**
   * The failed request's id, for the error state's support reference.
   *
   * 🔒 NFR-033 — an identifier only. It is what makes a support conversation
   * tractable, and it is safe to display precisely because it carries nothing
   * derived from the record.
   */
  requestId: string | null
  /** 🔒 The 402, kept apart from `error` so it can be explained rather than reported. */
  refusal: EntitlementRefusal | null
  busy: boolean
  changeClientStage: (toStage: SelectableStage, reason: string) => Promise<void>
  archive: () => Promise<void>
  restore: () => Promise<void>
  dismissRefusal: () => void
}

function toRefusal(error: ApiError): EntitlementRefusal {
  const details = error.details ?? {}
  return {
    message: error.message,
    action: error.action,
    // ⚠️ Narrowed rather than cast. `details` is `Record<string, unknown>` on
    // the envelope, and a wrong type here would render "Using undefined of
    // undefined" — a worse message than showing none of the numbers.
    limit: typeof details.limit === 'number' ? details.limit : undefined,
    used: typeof details.used === 'number' ? details.used : undefined,
    planCode: typeof details.plan_code === 'string' ? details.plan_code : undefined,
    upgradeTo: typeof details.upgrade_to === 'string' ? details.upgrade_to : undefined,
  }
}

export function useClientDetail(clientId: string): ClientDetailState {
  const [client, setClient] = useState<Client | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [requestId, setRequestId] = useState<string | null>(null)
  const [refusal, setRefusal] = useState<EntitlementRefusal | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    setRequestId(null)

    fetchClient(clientId, controller.signal)
      .then((loaded) => {
        setClient(loaded)
        setLoading(false)
      })
      .catch((cause: unknown) => {
        // An aborted request is a navigation, not a failure — reporting it would
        // flash an error as the user leaves the screen.
        if (controller.signal.aborted) return
        setError(cause instanceof ApiError ? cause.message : 'That client could not be loaded.')
        setRequestId(cause instanceof ApiError ? cause.requestId : null)
        setLoading(false)
      })

    return () => controller.abort()
  }, [clientId])

  /**
   * Run one lifecycle mutation and fold its result into state.
   *
   * 🔒 The server's response replaces the local client wholesale. The stage,
   * `activated_at`, `archived_at` and `updated_at` are all decided server-side
   * (Principle 3 — the client renders, never derives), and patching them locally
   * is how a screen comes to disagree with the database.
   */
  const run = useCallback(async (mutate: () => Promise<Client>) => {
    setBusy(true)
    setRefusal(null)
    setError(null)
    setRequestId(null)
    try {
      setClient(await mutate())
    } catch (cause: unknown) {
      if (cause instanceof ApiError && cause.type === 'entitlement_exceeded') {
        setRefusal(toRefusal(cause))
      } else if (cause instanceof ApiError) {
        setError(cause.message)
        setRequestId(cause.requestId)
      } else {
        setError('That change could not be saved.')
      }
    } finally {
      setBusy(false)
    }
  }, [])

  return {
    client,
    loading,
    error,
    requestId,
    refusal,
    busy,
    changeClientStage: useCallback(
      (toStage: SelectableStage, reason: string) =>
        run(() => changeStage(clientId, toStage, reason)),
      [clientId, run],
    ),
    archive: useCallback(() => run(() => archiveClient(clientId)), [clientId, run]),
    restore: useCallback(() => run(() => restoreClient(clientId)), [clientId, run]),
    dismissRefusal: useCallback(() => setRefusal(null), []),
  }
}
