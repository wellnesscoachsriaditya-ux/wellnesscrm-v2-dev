/**
 * A client's messages — the state behind the panels on the detail screen.
 *
 * 🔒 The API layer (Arch §4.4). Components render what this returns and call
 * what it exposes; they never fetch.
 *
 * ⚠️ **The three sections load together but fail apart.** A practitioner who
 * came to read the delivery log should still see it when the check-in schedule
 * is briefly unavailable — which is why there are separate error slots rather
 * than one.
 */

import { useCallback, useEffect, useState } from 'react'
import { ApiError } from '@wellnesscrm/api-client'
import {
  cancelScheduledMessage,
  fetchCheckinSchedule,
  fetchMessageHistory,
  fetchPendingMessages,
  updateCheckinSchedule,
  type CheckinSchedule,
  type CheckinUpdate,
  type Dispatch,
  type PendingMessage,
} from './messagingApi'

export interface ClientMessagingState {
  history: Dispatch[]
  pending: PendingMessage[]
  checkin: CheckinSchedule | null
  loading: boolean
  loadingMore: boolean
  hasMore: boolean
  historyError: string | null
  pendingError: string | null
  checkinError: string | null
  busy: boolean
  loadMore: () => Promise<void>
  cancel: (scheduledMessageId: string) => Promise<void>
  saveCheckin: (update: CheckinUpdate) => Promise<void>
}

function messageOf(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? `${cause.message} ${cause.action}` : fallback
}

export function useClientMessaging(clientId: string): ClientMessagingState {
  const [history, setHistory] = useState<Dispatch[]>([])
  const [pending, setPending] = useState<PendingMessage[]>([])
  const [checkin, setCheckin] = useState<CheckinSchedule | null>(null)
  const [cursor, setCursor] = useState<string | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [historyError, setHistoryError] = useState<string | null>(null)
  const [pendingError, setPendingError] = useState<string | null>(null)
  const [checkinError, setCheckinError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)

    // ⚠️ `allSettled`, not `all`: one failing request must not blank the others.
    void Promise.allSettled([
      fetchMessageHistory(clientId, {}, controller.signal),
      fetchPendingMessages(clientId, controller.signal),
      fetchCheckinSchedule(clientId, controller.signal),
    ]).then(([historyResult, pendingResult, checkinResult]) => {
      if (controller.signal.aborted) return

      if (historyResult.status === 'fulfilled') {
        setHistory(historyResult.value.items)
        setCursor(historyResult.value.page.next_cursor ?? null)
        setHasMore(historyResult.value.page.has_more)
        setHistoryError(null)
      } else {
        setHistoryError(messageOf(historyResult.reason, 'Message history could not be loaded.'))
      }

      if (pendingResult.status === 'fulfilled') {
        setPending(pendingResult.value)
        setPendingError(null)
      } else {
        setPendingError(messageOf(pendingResult.reason, 'Scheduled messages could not be loaded.'))
      }

      if (checkinResult.status === 'fulfilled') {
        setCheckin(checkinResult.value)
        setCheckinError(null)
      } else {
        setCheckinError(messageOf(checkinResult.reason, 'The check-in schedule could not be loaded.'))
      }

      setLoading(false)
    })

    return () => controller.abort()
  }, [clientId])

  const loadMore = useCallback(async () => {
    if (cursor === null) return
    setLoadingMore(true)
    try {
      const page = await fetchMessageHistory(clientId, { cursor })
      // 🔒 Appended, not merged by id. The cursor encodes `(created_at, id)`, so
      // the server has already guaranteed no overlap; de-duplicating here would
      // hide a paging bug rather than surface it.
      setHistory((current) => [...current, ...page.items])
      setCursor(page.page.next_cursor ?? null)
      setHasMore(page.page.has_more)
      setHistoryError(null)
    } catch (cause) {
      setHistoryError(messageOf(cause, 'More messages could not be loaded.'))
    } finally {
      setLoadingMore(false)
    }
  }, [clientId, cursor])

  const cancel = useCallback(
    async (scheduledMessageId: string) => {
      setBusy(true)
      try {
        await cancelScheduledMessage(scheduledMessageId)
        // 🔒 Re-read rather than patch. The server decides whether a message was
        // still cancellable, and a local removal would show a practitioner a
        // message as cancelled that had in fact just gone out.
        setPending(await fetchPendingMessages(clientId))
        setPendingError(null)
      } catch (cause) {
        setPendingError(messageOf(cause, 'That message could not be cancelled.'))
      } finally {
        setBusy(false)
      }
    },
    [clientId],
  )

  const saveCheckin = useCallback(
    async (update: CheckinUpdate) => {
      setBusy(true)
      try {
        setCheckin(await updateCheckinSchedule(clientId, update))
        // ⚠️ Pausing cancels what was already queued, so the pending list is
        // stale the moment the schedule changes.
        setPending(await fetchPendingMessages(clientId))
        setCheckinError(null)
      } catch (cause) {
        setCheckinError(messageOf(cause, 'The check-in schedule could not be saved.'))
      } finally {
        setBusy(false)
      }
    },
    [clientId],
  )

  return {
    history,
    pending,
    checkin,
    loading,
    loadingMore,
    hasMore,
    historyError,
    pendingError,
    checkinError,
    busy,
    loadMore,
    cancel,
    saveCheckin,
  }
}
