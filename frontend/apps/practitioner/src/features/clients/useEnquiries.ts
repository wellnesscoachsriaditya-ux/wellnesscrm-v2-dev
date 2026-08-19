/**
 * The lead workflow's state — FR-M2-010, FR-M2-011, US-M2-03.
 *
 * 🔒 The API layer (Arch §4.4). The screen renders what this returns and calls
 * what it exposes; it never fetches.
 *
 * 🔒 **Two lists, one hook, and the mode is what differs.** "Needs response" and
 * "all enquiries" are the same rows under a different filter *and a different
 * ordering* — oldest-first for the queue, newest-first for the archive. Keeping
 * them in one hook is what stops the two drifting into inconsistent paging or
 * scoping; keeping the *ordering* on the server is what stops the browser
 * deciding which prospect is most urgent.
 *
 * ⚠️ **Answering an enquiry re-reads the list rather than patching it locally.**
 * Clearing a row from the queue changes what the next page's cursor means, and
 * splicing a local edit into a cursor-paginated result set is how rows go
 * missing. The re-read costs one request on an action taken a handful of times
 * a day.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '@wellnesscrm/api-client'
import {
  fetchEnquiries,
  fetchNeedsResponse,
  markResponded,
  type EnquiryListItem,
} from './enquiriesApi'

/** Which question the list is answering. */
export type EnquiryView = 'needs-response' | 'all'

export interface EnquiryListState {
  enquiries: EnquiryListItem[]
  view: EnquiryView
  /** How many match — only present once counted. */
  total: number | null
  loading: boolean
  loadingMore: boolean
  error: string | null
  requestId: string | null
  hasMore: boolean
  /** The submission currently being marked answered, so its row can say so. */
  respondingId: string | null
  /**
   * 🔒 A failed "mark answered", kept apart from `error`.
   *
   * ⚠️ The list is still on screen and still correct when the action fails, so
   * replacing it with an error state would discard a working view over one
   * failed click.
   */
  respondError: string | null
  setView: (view: EnquiryView) => void
  loadMore: () => Promise<void>
  respond: (submissionId: string) => Promise<boolean>
  refresh: () => void
}

function messageOf(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? `${cause.message} ${cause.action}` : fallback
}

export function useEnquiries(initialView: EnquiryView = 'needs-response'): EnquiryListState {
  const [enquiries, setEnquiries] = useState<EnquiryListItem[]>([])
  const [view, setViewState] = useState<EnquiryView>(initialView)
  const [total, setTotal] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [requestId, setRequestId] = useState<string | null>(null)
  const [cursor, setCursor] = useState<string | null>(null)
  const [respondingId, setRespondingId] = useState<string | null>(null)
  const [respondError, setRespondError] = useState<string | null>(null)

  /**
   * 🔒 The cursor `loadMore` will use, held in a ref as well as in state.
   *
   * ⚠️ Not redundant. `loadMore` is a `useCallback` the screen holds across
   * renders; closing over the state value would capture the cursor as it was
   * when the callback was created, so a second "load more" would re-request the
   * same page.
   */
  const cursorRef = useRef<string | null>(null)

  /** Bumped to force a re-read without changing what is being asked for. */
  const [reloadToken, setReloadToken] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)

    const request =
      view === 'needs-response'
        ? fetchNeedsResponse({ includeTotal: true }, controller.signal)
        : fetchEnquiries({ includeTotal: true }, controller.signal)

    request
      .then((page) => {
        if (controller.signal.aborted) return
        setEnquiries(page.items)
        setTotal(page.page.total ?? null)
        setCursor(page.page.next_cursor ?? null)
        cursorRef.current = page.page.next_cursor ?? null
        setError(null)
        setRequestId(null)
        setLoading(false)
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return
        setError(messageOf(cause, 'Your enquiries could not be loaded.'))
        setRequestId(cause instanceof ApiError ? cause.requestId : null)
        setLoading(false)
      })

    return () => controller.abort()
  }, [view, reloadToken])

  const loadMore = useCallback(async () => {
    const from = cursorRef.current
    if (from === null) return

    setLoadingMore(true)
    try {
      const page =
        view === 'needs-response'
          ? await fetchNeedsResponse({ cursor: from })
          : await fetchEnquiries({ cursor: from })

      // ⚠️ The guard matters: switching view between the request and its
      // response would have reset the list, and appending a stale page to it
      // would mix the queue's rows into the archive's ordering.
      if (cursorRef.current !== from) return
      setEnquiries((current) => [...current, ...page.items])
      setCursor(page.page.next_cursor ?? null)
      cursorRef.current = page.page.next_cursor ?? null
      setError(null)
    } catch (cause: unknown) {
      setError(messageOf(cause, 'More enquiries could not be loaded.'))
    } finally {
      setLoadingMore(false)
    }
  }, [view])

  /**
   * 🔒 Switching view **resets the cursor**.
   *
   * ⚠️ The two views are ordered oppositely, so a cursor from one names a
   * position that does not exist in the other. Carrying it across would splice
   * one ordering's page into the other's — the failure mode is silently missing
   * enquiries, which looks like a practice that never received them.
   */
  const setView = useCallback((next: EnquiryView) => {
    cursorRef.current = null
    setRespondError(null)
    setViewState(next)
  }, [])

  return {
    enquiries,
    view,
    total,
    loading,
    loadingMore,
    error,
    requestId,
    hasMore: cursor !== null,
    respondingId,
    respondError,
    setView,
    loadMore,
    /**
     * Mark an enquiry answered — FR-M2-011.
     *
     * 🔒 **Failures are state, not exceptions.** Returns `false` and sets
     * `respondError` rather than rejecting — a rejected promise from a JSX
     * handler is an unhandled rejection the practitioner never sees, and "did
     * that work?" must have an answer on screen.
     */
    respond: useCallback(
      async (submissionId: string) => {
        setRespondingId(submissionId)
        setRespondError(null)
        try {
          await markResponded(submissionId)
          cursorRef.current = null
          setReloadToken((token) => token + 1)
          return true
        } catch (cause: unknown) {
          setRespondError(messageOf(cause, 'That enquiry could not be updated.'))
          return false
        } finally {
          setRespondingId(null)
        }
      },
      [],
    ),
    refresh: useCallback(() => {
      cursorRef.current = null
      setReloadToken((token) => token + 1)
    }, []),
  }
}
