/**
 * The client timeline's state — FR-M1-018, FR-M1-019.
 *
 * 🔒 The API layer (Arch §4.4). The panel renders what this returns and calls
 * what it exposes; it never fetches.
 *
 * ⚠️ **Paging appends; filtering replaces.** They look like one operation and
 * are not: "load more" must keep what is on screen, while changing a filter must
 * discard it, because the cursor from the old filter names a position in a
 * result set that no longer exists.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '@wellnesscrm/api-client'
import {
  fetchTimeline,
  fetchTimelineFilters,
  type TimelineEntry,
  type TimelineEventType,
  type TimelineFilter,
} from './timelineApi'

export interface TimelineState {
  entries: TimelineEntry[]
  /** The event types worth offering — decided by the server, not hardcoded. */
  filters: TimelineFilter[]
  /** Which are currently ticked. Empty means everything. */
  selected: readonly TimelineEventType[]
  loading: boolean
  /** True while a "load more" is in flight, so the button can say so. */
  loadingMore: boolean
  error: string | null
  hasMore: boolean
  loadMore: () => Promise<void>
  toggleFilter: (eventType: TimelineEventType) => void
  clearFilters: () => void
}

function messageOf(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? `${cause.message} ${cause.action}` : fallback
}

export function useTimeline(clientId: string): TimelineState {
  const [entries, setEntries] = useState<TimelineEntry[]>([])
  const [filters, setFilters] = useState<TimelineFilter[]>([])
  const [selected, setSelected] = useState<readonly TimelineEventType[]>([])
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [cursor, setCursor] = useState<string | null>(null)

  /**
   * 🔒 The cursor `loadMore` will use, held in a ref as well as in state.
   *
   * ⚠️ Not redundant. `loadMore` is a `useCallback` the panel holds across
   * renders; closing over the state value would capture the cursor as it was
   * when the callback was created, so a second "load more" would re-request the
   * same page. The ref is read at call time.
   */
  const cursorRef = useRef<string | null>(null)

  // The filter list is per-system, not per-page, so it loads once.
  useEffect(() => {
    const controller = new AbortController()
    fetchTimelineFilters(clientId, controller.signal)
      .then(setFilters)
      .catch(() => {
        // ⚠️ Swallowed. Losing the filter list costs a practitioner the ability
        // to narrow a timeline they can still read in full — degrading to "no
        // filters offered" is better than replacing the timeline with an error.
      })
    return () => controller.abort()
  }, [clientId])

  // 🔒 Re-runs when the filter selection changes, and replaces rather than
  // appends. `selected` is in the dependency list, which is what makes ticking
  // a filter re-query from the first page.
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)

    fetchTimeline(clientId, { eventTypes: selected }, controller.signal)
      .then((page) => {
        if (controller.signal.aborted) return
        setEntries(page.items)
        setCursor(page.page.next_cursor ?? null)
        cursorRef.current = page.page.next_cursor ?? null
        setError(null)
        setLoading(false)
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return
        setError(messageOf(cause, 'The timeline could not be loaded.'))
        setLoading(false)
      })

    return () => controller.abort()
  }, [clientId, selected])

  const loadMore = useCallback(async () => {
    const from = cursorRef.current
    if (from === null) return

    setLoadingMore(true)
    try {
      const page = await fetchTimeline(clientId, { cursor: from, eventTypes: selected })
      // ⚠️ Appended, and the guard matters: a filter change between the request
      // and its response would have reset the list, and appending a stale page
      // to it would show entries the current filter excludes.
      if (cursorRef.current !== from) return
      setEntries((current) => [...current, ...page.items])
      setCursor(page.page.next_cursor ?? null)
      cursorRef.current = page.page.next_cursor ?? null
      setError(null)
    } catch (cause: unknown) {
      setError(messageOf(cause, 'More timeline entries could not be loaded.'))
    } finally {
      setLoadingMore(false)
    }
  }, [clientId, selected])

  const toggleFilter = useCallback((eventType: TimelineEventType) => {
    setSelected((current) =>
      current.includes(eventType)
        ? current.filter((value) => value !== eventType)
        : [...current, eventType],
    )
  }, [])

  const clearFilters = useCallback(() => setSelected([]), [])

  return {
    entries,
    filters,
    selected,
    loading,
    loadingMore,
    error,
    hasMore: cursor !== null,
    loadMore,
    toggleFilter,
    clearFilters,
  }
}
