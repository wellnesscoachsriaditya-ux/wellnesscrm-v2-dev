/**
 * The timeline panel — FR-M1-018, FR-M1-019.
 *
 * 🔒 The assertions that matter here are about *reading a history*: that the
 * order is announced, that the two empty states say different things, and that
 * an automated action is never rendered as though a colleague performed it.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ClientTimelinePanel } from './ClientTimelinePanel'
import type { TimelineEntryView, TimelineFilterView } from './ClientTimelinePanel'

const ENTRIES: TimelineEntryView[] = [
  {
    id: 'evt-3',
    eventType: 'note_added',
    occurredAt: '2026-08-09T10:00:00Z',
    summary: 'Note added',
    actorType: 'practitioner',
    actorId: 'user-priya',
  },
  {
    id: 'evt-2',
    eventType: 'stage_changed',
    occurredAt: '2026-08-08T09:00:00Z',
    summary: 'New enquiry → Contacted',
    actorType: 'practitioner',
    actorId: 'user-priya',
  },
  {
    id: 'evt-1',
    eventType: 'client_archived',
    occurredAt: '2026-08-07T08:00:00Z',
    summary: 'Client archived',
    actorType: 'system',
    actorId: null,
  },
]

const FILTERS: TimelineFilterView[] = [
  { eventType: 'stage_changed', label: 'Stage changes' },
  { eventType: 'note_added', label: 'Notes' },
]

function renderPanel(overrides: Partial<Parameters<typeof ClientTimelinePanel>[0]> = {}) {
  const onLoadMore = vi.fn()
  const onToggleFilter = vi.fn()
  const onClearFilters = vi.fn()

  const { container } = render(
    <ClientTimelinePanel
      entries={ENTRIES}
      filters={FILTERS}
      selected={[]}
      onLoadMore={onLoadMore}
      onToggleFilter={onToggleFilter}
      onClearFilters={onClearFilters}
      {...overrides}
    />,
  )

  return { onLoadMore, onToggleFilter, onClearFilters, container }
}

describe('reading the timeline', () => {
  it('renders entries as an ordered list, because the order is the meaning', () => {
    // 🔒 An `ol`, not a `ul`: a screen reader announcing "list of 3" without
    // order would lose what a timeline is for.
    renderPanel()

    const list = screen.getByRole('list', { name: 'Timeline' })
    expect(list.tagName).toBe('OL')
    expect(within(list).getAllByRole('listitem')).toHaveLength(3)
  })

  it('keeps the order the server sent, newest first', () => {
    // 🔒 Principle 3 — the client renders, never derives. Re-sorting here is how
    // the screen comes to disagree with the cursor the server paginates by.
    renderPanel()

    const items = within(screen.getByRole('list', { name: 'Timeline' })).getAllByRole('listitem')
    expect(items[0]).toHaveTextContent('Note added')
    expect(items[2]).toHaveTextContent('Client archived')
  })

  it('names an automated action as automatic, not as a person', () => {
    // 🔒 The misattribution `actor_type` exists to prevent. An archive performed
    // by a retention rule must not read as though a colleague did it and forgot
    // to say so.
    renderPanel()

    const archived = within(screen.getByRole('list', { name: 'Timeline' }))
      .getAllByRole('listitem')[2]
    expect(archived).toHaveTextContent('Automatic')
  })

  it('renders a machine-readable timestamp for every entry', () => {
    // A `<time datetime>` is what lets a screen reader and a future relative
    // formatter both read the instant, rather than reparsing display text.
    const { container } = renderPanel()
    expect(container.querySelectorAll('time[datetime]')).toHaveLength(3)
  })
})

describe('empty states — NFR-064', () => {
  it('distinguishes an empty timeline from an over-filtered one', () => {
    // 🔒 Two different problems. "No matches" is a filter the practitioner can
    // clear; "nothing yet" is a client nothing has happened to. One message for
    // both would send them looking for a bug in the wrong place.
    renderPanel({ entries: [], selected: [] })
    expect(screen.getByText(/nothing has happened yet/i)).toBeInTheDocument()
  })

  it('says so when filters exclude everything', () => {
    renderPanel({ entries: [], selected: ['note_added'] })
    expect(screen.getByText(/nothing matches those filters/i)).toBeInTheDocument()
  })
})

describe('filtering — FR-M1-019', () => {
  it('announces which filters are applied', () => {
    // `aria-pressed` — the pressed state is the information, and colour alone
    // would fail WCAG 1.4.1.
    renderPanel({ selected: ['note_added'] })

    expect(screen.getByRole('button', { name: 'Notes', pressed: true })).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Stage changes', pressed: false }),
    ).toBeInTheDocument()
  })

  it('reports the type when a filter is toggled', async () => {
    const { onToggleFilter } = renderPanel()

    await userEvent.click(screen.getByRole('button', { name: 'Notes' }))

    expect(onToggleFilter).toHaveBeenCalledWith('note_added')
  })

  it('offers a way back to everything only while filtered', async () => {
    const { onClearFilters } = renderPanel({ selected: ['note_added'] })

    await userEvent.click(screen.getByRole('button', { name: 'Show everything' }))
    expect(onClearFilters).toHaveBeenCalled()
  })

  it('hides the clear control when nothing is filtered', () => {
    renderPanel({ selected: [] })
    expect(screen.queryByRole('button', { name: 'Show everything' })).not.toBeInTheDocument()
  })

  it('offers no filter bar when the server reports no producible types', () => {
    // ⚠️ The filter list is server-driven precisely so a build that can produce
    // nothing offers nothing, rather than filters that always return empty.
    renderPanel({ filters: [] })
    expect(screen.queryByLabelText('Filter timeline')).not.toBeInTheDocument()
  })
})

describe('paging — ADR-A05', () => {
  it('offers more only when the server said there is more', () => {
    renderPanel({ hasMore: true })
    expect(screen.getByRole('button', { name: 'Load older entries' })).toBeInTheDocument()
  })

  it('does not offer to load past the end', () => {
    // `has_more` false is what stops the UI asking forever at the bottom of a
    // short history.
    renderPanel({ hasMore: false })
    expect(screen.queryByRole('button', { name: 'Load older entries' })).not.toBeInTheDocument()
  })

  it('asks for the next page when pressed', async () => {
    const { onLoadMore } = renderPanel({ hasMore: true })

    await userEvent.click(screen.getByRole('button', { name: 'Load older entries' }))

    expect(onLoadMore).toHaveBeenCalled()
  })
})

describe('loading and failure', () => {
  it('shows a spinner instead of an empty state while loading', () => {
    // ⚠️ An empty state during the first load reads as "this client has no
    // history", which is a lie that resolves itself a moment later.
    renderPanel({ entries: [], loading: true })

    expect(screen.getByText(/loading timeline/i)).toBeInTheDocument()
    expect(screen.queryByText(/nothing has happened yet/i)).not.toBeInTheDocument()
  })

  it('surfaces a failure without hiding the entries already read', () => {
    // A failed "load more" must not blank the history the practitioner is
    // reading.
    renderPanel({ error: 'More timeline entries could not be loaded.' })

    expect(screen.getByRole('alert')).toHaveTextContent('could not be loaded')
    expect(screen.getByRole('list', { name: 'Timeline' })).toBeInTheDocument()
  })
})
