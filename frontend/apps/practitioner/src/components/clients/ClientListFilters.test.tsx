/**
 * Search, filters and sort — FR-M1-021, FR-M1-022.
 *
 * 🔒 These tests pin the decisions a future edit is most likely to undo: that
 * `archived` is not offered as a stage, that the tag rule is stated rather than
 * inferred, and that controls the server did not supply are absent rather than
 * empty.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ClientListFilters } from './ClientListFilters'

const TAGS = [
  { id: 'tag-pcos', name: 'PCOS' },
  { id: 'tag-natal', name: 'Post-natal' },
]

const SORTS = [
  { value: 'recent_activity', label: 'Recent activity' },
  { value: 'name', label: 'Name' },
]

function renderFilters(overrides: Partial<Parameters<typeof ClientListFilters>[0]> = {}) {
  const handlers = {
    onSearchChange: vi.fn(),
    onToggleStage: vi.fn(),
    onToggleTag: vi.fn(),
    onArchivedChange: vi.fn(),
    onSortChange: vi.fn(),
    onClearFilters: vi.fn(),
  }

  const view = render(
    <ClientListFilters
      search=""
      stages={[]}
      tagIds={[]}
      archived="exclude"
      sort="recent_activity"
      availableTags={TAGS}
      sortOptions={SORTS}
      total={null}
      isFiltered={false}
      {...handlers}
      {...overrides}
    />,
  )

  return { ...handlers, container: view.container }
}

describe('search', () => {
  it('gives the search box a real label, not a placeholder', () => {
    // 🔒 NFR-062. A placeholder vanishes on the first keystroke and is not an
    // accessible name, so a screen-reader user returning to a half-typed field
    // would hear only what they had typed.
    renderFilters()
    expect(screen.getByLabelText('Search clients')).toBeInTheDocument()
  })

  it('reports each keystroke, leaving the debounce to the hook', async () => {
    const { onSearchChange } = renderFilters()

    await userEvent.type(screen.getByLabelText('Search clients'), 'ka')
    expect(onSearchChange).toHaveBeenCalledTimes(2)
    expect(onSearchChange).toHaveBeenLastCalledWith('a')
  })
})

describe('the match count', () => {
  it('stays absent until something has been counted', () => {
    // ⚠️ `null` is "not counted yet", which is not "0 clients" — showing a zero
    // during the first load would read as an empty practice.
    //
    // 🔒 Asserted on the live region rather than on text matching /client/: the
    // label and the tag hint both contain that word, so a text query would pass
    // while the count was on screen.
    const { container } = renderFilters({ total: null })
    expect(container.querySelector('[aria-live]')).toBeNull()
  })

  it('announces the count politely, and reads correctly at one', () => {
    // ⚠️ `polite`, not `assertive`. The count changes on every keystroke, and
    // interrupting a screen-reader user mid-word to read an intermediate result
    // would make the search box unusable.
    renderFilters({ total: 1 })
    const count = screen.getByText('1 client')
    expect(count).toHaveAttribute('aria-live', 'polite')
  })
})

describe('stage filters', () => {
  it('announces which stages are active', () => {
    // 🔒 Asserted through `STAGE_LABEL`'s wording, which is the practitioner's
    // vocabulary ("New enquiry"), not the database's ("lead").
    renderFilters({ stages: ['active'] })

    expect(
      screen.getByRole('button', { name: 'Active client', pressed: true }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'New enquiry', pressed: false }),
    ).toBeInTheDocument()
  })

  it('does not offer archived as a stage', () => {
    // 🔒 Archived is reached through the separate control (FR-M1-014). Offering
    // it here too would let a practitioner build "archived stage, excluding
    // archived" and get an empty list with no explanation.
    renderFilters()

    const stageGroup = screen.getByRole('group', { name: 'Filter by stage' })
    expect(stageGroup).not.toHaveTextContent(/archived/i)
  })

  it('reports the stage toggled', async () => {
    const { onToggleStage } = renderFilters()

    await userEvent.click(screen.getByRole('button', { name: 'Paused' }))
    expect(onToggleStage).toHaveBeenCalledWith('paused')
  })
})

describe('tag filters', () => {
  it('states that tags combine with AND', () => {
    // 🔒 Both readings are plausible from the buttons alone, and a filter whose
    // logic you infer from result counts is one that silently misleads.
    renderFilters()
    expect(screen.getByText(/every tag selected/i)).toBeInTheDocument()
  })

  it('is absent entirely when the practice has no tags', () => {
    renderFilters({ availableTags: [] })
    expect(screen.queryByRole('group', { name: 'Filter by tag' })).not.toBeInTheDocument()
  })
})

describe('controls the server did not supply', () => {
  it('hides sort rather than showing an empty dropdown', () => {
    // ⚠️ If `sort-options` failed, the list is still readable in its default
    // order. An empty dropdown would suggest sorting is broken, not unavailable.
    renderFilters({ sortOptions: [] })
    expect(screen.queryByLabelText('Sort by')).not.toBeInTheDocument()
  })

  it('offers the three archived views by name', async () => {
    const { onArchivedChange } = renderFilters()

    await userEvent.selectOptions(screen.getByLabelText('Show'), 'only')
    expect(onArchivedChange).toHaveBeenCalledWith('only')
  })
})

describe('clearing', () => {
  it('offers a way out only once something is narrowing the list', async () => {
    const { onClearFilters } = renderFilters({ isFiltered: true })

    await userEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(onClearFilters).toHaveBeenCalled()
  })

  it('is hidden when nothing is filtered', () => {
    renderFilters({ isFiltered: false })
    expect(screen.queryByRole('button', { name: 'Clear filters' })).not.toBeInTheDocument()
  })
})
