/**
 * The client list's state — FR-M1-021/022, NFR-005.
 *
 * 🔒 The API layer (Arch §4.4). The screen renders what this returns and calls
 * what it exposes; it never fetches.
 *
 * ⚠️ **Paging appends; every other change replaces.** They look like one
 * operation and are not: "load more" must keep what is on screen, while changing
 * a filter, a sort or the search box must discard it — the cursor from the old
 * query names a position in a result set that no longer exists, and appending to
 * it would show clients the current filter excludes.
 *
 * 🔒 **The search box is debounced, the filters are not.** FR-M1-021 wants
 * results as the practitioner types, which means a request per keystroke unless
 * something intervenes; ticking a checkbox is one deliberate act and should feel
 * immediate.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ApiError } from '@wellnesscrm/api-client'
import {
  fetchClients,
  fetchSortOptions,
  reassignClients,
  type ArchivedFilter,
  type ClientListItem,
  type ClientSortOption,
} from './discoveryApi'

/**
 * 🔒 How long to wait after a keystroke before searching.
 *
 * ⚠️ A real trade-off, not a round number. Too short and a practitioner typing
 * "asha" fires four searches, three of which are discarded — the load NFR-005's
 * 300 ms budget is measured against. Too long and the list feels broken. 250 ms
 * is below the ~400 ms at which a pause reads as lag, and long enough that
 * ordinary typing produces one request per word.
 */
const SEARCH_DEBOUNCE_MS = 250

export interface ClientListFilters {
  search: string
  stages: readonly string[]
  tagIds: readonly string[]
  archived: ArchivedFilter
  sort: string
}

/** 🔒 The default view: everyone active, most recently touched first. */
export const DEFAULT_FILTERS: ClientListFilters = {
  search: '',
  stages: [],
  tagIds: [],
  archived: 'exclude',
  sort: 'recent_activity',
}

export interface ClientListState {
  clients: ClientListItem[]
  sortOptions: ClientSortOption[]
  filters: ClientListFilters
  /** How many match the current filters — only present once counted. */
  total: number | null
  loading: boolean
  /** True while a "load more" is in flight, so the button can say so. */
  loadingMore: boolean
  error: string | null
  requestId: string | null
  hasMore: boolean
  /** 🔒 True when anything narrows the list — drives the "no matches" wording. */
  isFiltered: boolean
  /** True while a bulk reassignment is in flight — EC-M1-04. */
  reassigning: boolean
  /**
   * 🔒 A failed reassignment, kept apart from `error`.
   *
   * ⚠️ The list is still on screen and still correct when a handover fails, so
   * replacing it with an error state would discard a working view over a failed
   * action. This renders beside the dialog instead.
   */
  reassignError: string | null
  setSearch: (value: string) => void
  toggleStage: (stage: string) => void
  toggleTag: (tagId: string) => void
  setArchived: (value: ArchivedFilter) => void
  setSort: (value: string) => void
  clearFilters: () => void
  loadMore: () => Promise<void>
  reassign: (clientIds: readonly string[], ownerUserId: string) => Promise<number | null>
  refresh: () => void
}

function messageOf(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? `${cause.message} ${cause.action}` : fallback
}

export function useClientList(): ClientListState {
  const [clients, setClients] = useState<ClientListItem[]>([])
  const [sortOptions, setSortOptions] = useState<ClientSortOption[]>([])
  const [filters, setFilters] = useState<ClientListFilters>(DEFAULT_FILTERS)
  const [total, setTotal] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [requestId, setRequestId] = useState<string | null>(null)
  const [cursor, setCursor] = useState<string | null>(null)
  const [reassigning, setReassigning] = useState(false)
  const [reassignError, setReassignError] = useState<string | null>(null)

  /**
   * 🔒 The search text actually sent, trailing the box by `SEARCH_DEBOUNCE_MS`.
   *
   * ⚠️ Held separately from `filters.search` rather than debouncing the setter.
   * The input must re-render on every keystroke or it feels dead; only the
   * *query* waits. Conflating them is what produces a laggy text field.
   */
  const [debouncedSearch, setDebouncedSearch] = useState('')

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
    const timer = setTimeout(() => setDebouncedSearch(filters.search), SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [filters.search])

  // The sort vocabulary is per-system, not per-query, so it loads once.
  useEffect(() => {
    const controller = new AbortController()
    fetchSortOptions(controller.signal)
      .then(setSortOptions)
      .catch(() => {
        // ⚠️ Swallowed. Losing the sort list costs a practitioner the ability to
        // reorder a list they can still read — degrading to "no sort offered" is
        // better than replacing the list with an error.
      })
    return () => controller.abort()
  }, [])

  /**
   * 🔒 Re-runs whenever the query changes, and **replaces** rather than appends.
   *
   * ⚠️ The dependency list is the query, spread field by field rather than
   * passed as an object: `filters` is a new reference on every render, so
   * depending on it directly would re-fetch in a loop.
   */
  const { stages, tagIds, archived, sort } = filters
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)

    fetchClients(
      {
        ...(debouncedSearch ? { search: debouncedSearch } : {}),
        stages,
        tagIds,
        archived,
        sort,
        // 🔒 Asked for only on the first page. The count does not change as the
        // practitioner pages through, and a COUNT per page would pay NFR-005's
        // most expensive query repeatedly for an answer already on screen.
        includeTotal: true,
      },
      controller.signal,
    )
      .then((page) => {
        if (controller.signal.aborted) return
        setClients(page.items)
        setTotal(page.page.total ?? null)
        setCursor(page.page.next_cursor ?? null)
        cursorRef.current = page.page.next_cursor ?? null
        setError(null)
        setRequestId(null)
        setLoading(false)
      })
      .catch((cause: unknown) => {
        // An aborted request is a newer keystroke, not a failure — reporting it
        // would flash an error under the practitioner's cursor as they type.
        if (controller.signal.aborted) return
        setError(messageOf(cause, 'Your clients could not be loaded.'))
        setRequestId(cause instanceof ApiError ? cause.requestId : null)
        setLoading(false)
      })

    return () => controller.abort()
  }, [debouncedSearch, stages, tagIds, archived, sort, reloadToken])

  const loadMore = useCallback(async () => {
    const from = cursorRef.current
    if (from === null) return

    setLoadingMore(true)
    try {
      const page = await fetchClients({
        ...(debouncedSearch ? { search: debouncedSearch } : {}),
        stages,
        tagIds,
        archived,
        sort,
        cursor: from,
      })
      // ⚠️ Appended, and the guard matters: a filter change between the request
      // and its response would have reset the list, and appending a stale page
      // to it would show clients the current filter excludes.
      if (cursorRef.current !== from) return
      setClients((current) => [...current, ...page.items])
      setCursor(page.page.next_cursor ?? null)
      cursorRef.current = page.page.next_cursor ?? null
      setError(null)
    } catch (cause: unknown) {
      setError(messageOf(cause, 'More clients could not be loaded.'))
    } finally {
      setLoadingMore(false)
    }
  }, [debouncedSearch, stages, tagIds, archived, sort])

  /**
   * 🔒 Every filter setter goes through here, and each **resets the cursor**.
   *
   * ⚠️ This is the single most important line in the hook. A cursor kept across
   * a filter change names a position in the *previous* result set, so the next
   * "load more" would splice one query's page into another's — the failure mode
   * is silently missing clients, which looks like a practice that never had them.
   */
  const update = useCallback((change: Partial<ClientListFilters>) => {
    cursorRef.current = null
    setFilters((current) => ({ ...current, ...change }))
  }, [])

  const toggle = useCallback(
    (key: 'stages' | 'tagIds', value: string) => {
      setFilters((current) => {
        cursorRef.current = null
        const selected = current[key]
        return {
          ...current,
          [key]: selected.includes(value)
            ? selected.filter((entry) => entry !== value)
            : [...selected, value],
        }
      })
    },
    [],
  )

  return {
    clients,
    sortOptions,
    filters,
    total,
    loading,
    loadingMore,
    error,
    requestId,
    hasMore: cursor !== null,
    reassigning,
    reassignError,
    // ⚠️ The sort is deliberately not counted as a filter. Reordering does not
    // remove anybody, so an empty list under a non-default sort is still "you
    // have no clients" rather than "nothing matches".
    isFiltered: useMemo(
      () =>
        filters.search !== '' ||
        filters.stages.length > 0 ||
        filters.tagIds.length > 0 ||
        filters.archived !== DEFAULT_FILTERS.archived,
      [filters],
    ),
    setSearch: useCallback((value: string) => update({ search: value }), [update]),
    toggleStage: useCallback((stage: string) => toggle('stages', stage), [toggle]),
    toggleTag: useCallback((tagId: string) => toggle('tagIds', tagId), [toggle]),
    setArchived: useCallback((value: ArchivedFilter) => update({ archived: value }), [update]),
    setSort: useCallback((value: string) => update({ sort: value }), [update]),
    clearFilters: useCallback(() => {
      cursorRef.current = null
      setFilters(DEFAULT_FILTERS)
    }, []),
    loadMore,
    /**
     * Hand several clients to another practitioner — EC-M1-04.
     *
     * 🔒 Re-reads the list afterwards rather than patching it locally. A
     * reassignment can remove clients from *this* practitioner's view entirely
     * (they may no longer be visible under AC-M1-006), and deciding that on the
     * client would mean reimplementing the scoping rule in the browser.
     *
     * 🔒 **Failures are state, not exceptions.** Returns `null` and sets
     * `reassignError` rather than rejecting — a rejected promise from a JSX
     * handler is an unhandled rejection the practitioner never sees, and the
     * handover is exactly the operation where "did that work?" must have an
     * answer on screen.
     *
     * ⚠️ The list is **not** re-read on failure. EC-M1-04 is all-or-nothing, so
     * a refusal means nothing moved and what is on screen is still correct.
     */
    reassign: useCallback(
      async (clientIds: readonly string[], ownerUserId: string) => {
        setReassigning(true)
        setReassignError(null)
        try {
          const result = await reassignClients(clientIds, ownerUserId)
          cursorRef.current = null
          setReloadToken((token) => token + 1)
          return result.moved
        } catch (cause: unknown) {
          setReassignError(messageOf(cause, 'Those clients could not be reassigned.'))
          return null
        } finally {
          setReassigning(false)
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
