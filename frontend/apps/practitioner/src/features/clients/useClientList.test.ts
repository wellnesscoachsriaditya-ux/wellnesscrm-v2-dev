/**
 * `useClientList` — FR-M1-021/022, EC-M1-04, NFR-005.
 *
 * 🔒 These tests exist for the failures that are **silent**. A broken filter is
 * obvious the moment you look at the screen; a cursor carried across a filter
 * change quietly drops clients from a list that still looks complete, and a
 * failed handover that reports nothing looks exactly like one that worked.
 *
 * ⚠️ Fake timers throughout, because the search box is debounced. Real timers
 * would make every search assertion a race with a 250 ms wall clock.
 */

import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@wellnesscrm/api-client'
import { useClientList } from './useClientList'
import type { ClientListItem, ClientPage } from './discoveryApi'

vi.mock('./discoveryApi', () => ({
  fetchClients: vi.fn(),
  fetchSortOptions: vi.fn(),
  reassignClients: vi.fn(),
}))

const api = await import('./discoveryApi')
const fetchClients = vi.mocked(api.fetchClients)
const fetchSortOptions = vi.mocked(api.fetchSortOptions)
const reassignClients = vi.mocked(api.reassignClients)

/**
 * ⚠️ Every field spelled out and **no `as` cast**. A cast here would let the
 * fixture drift from the generated schema silently, which is the one failure
 * these tests cannot catch by construction — the suite would keep passing
 * against a shape the server no longer sends.
 */
function client(id: string): ClientListItem {
  return {
    id,
    full_name: `Client ${id}`,
    stage: 'active',
    email: null,
    mobile: null,
    city: null,
    dietary_class: null,
    tags: [],
    owner_user_id: 'u-1',
    owner_name: null,
    archived_at: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  }
}

function page(ids: string[], next: string | null = null, total?: number): ClientPage {
  return {
    items: ids.map(client),
    page: { has_more: next !== null, next_cursor: next, total: total ?? null },
  }
}

beforeEach(() => {
  vi.useFakeTimers()
  fetchClients.mockResolvedValue(page(['a', 'b'], null, 2))
  fetchSortOptions.mockResolvedValue([{ value: 'recent_activity', label: 'Recent activity' }])
  reassignClients.mockResolvedValue({ moved: 2, requested: 2 })
})

afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

/** Let the mounted hook's first fetch settle. */
async function settle() {
  await act(async () => {
    await vi.runOnlyPendingTimersAsync()
  })
}

describe('the first load', () => {
  it('asks for a total, and reports it once counted', async () => {
    const { result } = renderHook(() => useClientList())
    await settle()

    expect(result.current.clients).toHaveLength(2)
    expect(result.current.total).toBe(2)
    expect(result.current.loading).toBe(false)
    // 🔒 NFR-005: the COUNT is paid once, on the first page only.
    expect(fetchClients.mock.calls[0]?.[0]).toMatchObject({ includeTotal: true })
  })

  it('leaves the total null when the server does not send one', async () => {
    // ⚠️ null is "not counted", which must not render as "0 clients".
    fetchClients.mockResolvedValue(page(['a'], null))
    const { result } = renderHook(() => useClientList())
    await settle()

    expect(result.current.total).toBeNull()
  })
})

describe('the debounced search', () => {
  it('sends one request per pause, not one per keystroke', async () => {
    const { result } = renderHook(() => useClientList())
    await settle()
    fetchClients.mockClear()

    // Four keystrokes inside one debounce window.
    act(() => {
      result.current.setSearch('a')
      result.current.setSearch('as')
      result.current.setSearch('ash')
      result.current.setSearch('asha')
    })
    expect(fetchClients).not.toHaveBeenCalled()

    await settle()
    expect(fetchClients).toHaveBeenCalledTimes(1)
    expect(fetchClients.mock.calls[0]?.[0]).toMatchObject({ search: 'asha' })
  })

  it('omits the search key entirely when the box is empty', async () => {
    const { result } = renderHook(() => useClientList())
    await settle()
    fetchClients.mockClear()

    act(() => result.current.setSearch('asha'))
    await settle()
    act(() => result.current.setSearch(''))
    await settle()

    // 🔒 Absent, not `search: ''` — an empty string is a filter that matches
    // nothing on some backends, and "" is not what "no search" means.
    expect(fetchClients.mock.calls.at(-1)?.[0]).not.toHaveProperty('search')
  })
})

describe('filters', () => {
  it('applies a stage immediately, without waiting for the debounce', async () => {
    const { result } = renderHook(() => useClientList())
    await settle()
    fetchClients.mockClear()

    // 🔒 Ticking a filter is one deliberate act; only typing is debounced.
    //
    // ⚠️ `act` is synchronous here because the setter is: awaiting its `void`
    // return is what `@typescript-eslint/await-thenable` rejects. The `settle()`
    // that follows is not decoration — it flushes the *fetch* the setter kicked
    // off, and without it React warns about a state update outside `act`.
    act(() => {
      result.current.toggleStage('lead')
    })
    await settle()

    expect(fetchClients).toHaveBeenCalledTimes(1)
    expect(fetchClients.mock.calls[0]?.[0]).toMatchObject({ stages: ['lead'] })
  })

  it('toggles a stage off again', async () => {
    const { result } = renderHook(() => useClientList())
    await settle()

    act(() => result.current.toggleStage('lead'))
    act(() => result.current.toggleStage('lead'))
    await settle()

    expect(result.current.filters.stages).toEqual([])
  })

  it('counts search, stages, tags and archived as filtering — but not sort', async () => {
    const { result } = renderHook(() => useClientList())
    await settle()
    expect(result.current.isFiltered).toBe(false)

    // ⚠️ Reordering removes nobody, so an empty list under a non-default sort is
    // still "you have no clients" rather than "nothing matches" — which is the
    // difference between offering "Add client" and offering "Clear filters".
    act(() => result.current.setSort('name_asc'))
    await settle()
    expect(result.current.isFiltered).toBe(false)

    act(() => result.current.toggleTag('t-1'))
    await settle()
    expect(result.current.isFiltered).toBe(true)
  })

  it('clears back to the default view', async () => {
    const { result } = renderHook(() => useClientList())
    await settle()

    act(() => result.current.toggleStage('lead'))
    act(() => result.current.setArchived('only'))
    act(() => result.current.clearFilters())
    await settle()

    expect(result.current.filters).toMatchObject({ stages: [], archived: 'exclude' })
    expect(result.current.isFiltered).toBe(false)
  })
})

describe('paging', () => {
  it('appends the next page instead of replacing the list', async () => {
    fetchClients.mockResolvedValue(page(['a', 'b'], 'cursor-1', 2))
    const { result } = renderHook(() => useClientList())
    await settle()
    expect(result.current.hasMore).toBe(true)

    fetchClients.mockResolvedValue(page(['c'], null))
    await act(async () => {
      await result.current.loadMore()
    })

    expect(result.current.clients.map((entry) => entry.id)).toEqual(['a', 'b', 'c'])
    expect(result.current.hasMore).toBe(false)
    // 🔒 No second COUNT — the total does not change as you page.
    expect(fetchClients.mock.calls[1]?.[0]).not.toHaveProperty('includeTotal')
  })

  it('does nothing when there is no next page', async () => {
    const { result } = renderHook(() => useClientList())
    await settle()
    fetchClients.mockClear()

    await act(async () => {
      await result.current.loadMore()
    })

    expect(fetchClients).not.toHaveBeenCalled()
  })

  it('discards a page that arrives after the filters moved', async () => {
    // ⚠️ The regression this guards is invisible: splicing a page from the old
    // query into the new result set shows clients the current filter excludes,
    // or drops ones it includes, and the list still looks complete either way.
    fetchClients.mockResolvedValue(page(['a'], 'cursor-1', 1))
    const { result } = renderHook(() => useClientList())
    await settle()

    // The next call — "load more" — hangs until we release it.
    let release: (value: ClientPage) => void = () => {}
    fetchClients.mockReturnValueOnce(
      new Promise<ClientPage>((resolve) => {
        release = resolve
      }),
    )
    // ⚠️ Queued *before* the toggle, so the filter change's own read returns a
    // different result set with no cursor. Leaving the old response in place
    // would restore `cursor-1` and the guard would legitimately allow the
    // append — the test would fail while the hook was behaving correctly.
    fetchClients.mockResolvedValue(page(['z'], null, 1))

    let pending: Promise<void> = Promise.resolve()
    act(() => {
      pending = result.current.loadMore()
    })
    act(() => {
      result.current.toggleStage('lead')
    })

    await act(async () => {
      release(page(['stale'], null))
      await pending
    })

    expect(result.current.clients.map((entry) => entry.id)).toEqual(['z'])
  })
})

describe('failures', () => {
  it('reports a failed load with its request id', async () => {
    fetchClients.mockRejectedValue(
      new ApiError(503, {
        type: 'internal_error',
        message: 'Your clients could not be loaded.',
        action: 'Try again shortly.',
        request_id: 'req-77',
      }),
    )
    const { result } = renderHook(() => useClientList())
    await settle()

    // ⚠️ Asserted directly rather than through `waitFor`, which polls on real
    // timers and would hang out the test's budget under `useFakeTimers`.
    expect(result.current.error).toContain('could not be loaded')
    // 🔒 The request id is surfaced so a practitioner can quote it to support.
    expect(result.current.requestId).toBe('req-77')
    expect(result.current.loading).toBe(false)
  })

  it('keeps the list usable when the sort vocabulary fails', async () => {
    // ⚠️ Degrade, don't fail: losing the sort dropdown costs an ordering, while
    // failing the screen costs the practitioner their client list.
    fetchSortOptions.mockRejectedValue(new Error('nope'))
    const { result } = renderHook(() => useClientList())
    await settle()

    expect(result.current.sortOptions).toEqual([])
    expect(result.current.clients).toHaveLength(2)
    expect(result.current.error).toBeNull()
  })
})

describe('reassignment — EC-M1-04', () => {
  it('re-reads the list rather than patching it locally', async () => {
    const { result } = renderHook(() => useClientList())
    await settle()
    fetchClients.mockClear()

    let moved: number | null = null
    await act(async () => {
      moved = await result.current.reassign(['a', 'b'], 'u-2')
    })

    expect(moved).toBe(2)
    expect(reassignClients).toHaveBeenCalledWith(['a', 'b'], 'u-2')
    // 🔒 A handover can remove clients from this practitioner's own view under
    // AC-M1-006, so the server decides what is still visible — not the browser.
    expect(fetchClients).toHaveBeenCalledTimes(1)
    expect(result.current.reassignError).toBeNull()
  })

  it('surfaces a refusal as state and leaves the list untouched', async () => {
    reassignClients.mockRejectedValue(
      new ApiError(403, {
        type: 'forbidden',
        message: 'Only the practice owner can reassign clients.',
        action: 'Ask an owner to move them.',
        request_id: 'req-9',
      }),
    )
    const { result } = renderHook(() => useClientList())
    await settle()
    fetchClients.mockClear()

    let moved: number | null = 1
    await act(async () => {
      moved = await result.current.reassign(['a'], 'u-2')
    })

    // 🔒 Returns null instead of rejecting: a rejected promise from a JSX
    // handler is an unhandled rejection the practitioner never sees.
    expect(moved).toBeNull()
    expect(result.current.reassignError).toContain('Only the practice owner')
    // ⚠️ All-or-nothing means nothing moved, so what is on screen is still
    // correct — re-reading would cost a request to redraw the same list.
    expect(fetchClients).not.toHaveBeenCalled()
    // The list itself is untouched; only the dialog reports the problem.
    expect(result.current.error).toBeNull()
    expect(result.current.clients).toHaveLength(2)
  })

  it('clears a previous error when a later attempt is made', async () => {
    reassignClients.mockRejectedValueOnce(new Error('flaky'))
    const { result } = renderHook(() => useClientList())
    await settle()

    await act(async () => {
      await result.current.reassign(['a'], 'u-2')
    })
    expect(result.current.reassignError).not.toBeNull()

    await act(async () => {
      await result.current.reassign(['a'], 'u-2')
    })
    // ⚠️ A stale error beside a successful retry reads as a second failure.
    expect(result.current.reassignError).toBeNull()
  })
})
