/**
 * Search, filters and sort for the client list — FR-M1-021, FR-M1-022.
 *
 * 🔒 Renders and reports (Arch §4.4). Every value is a prop and every change a
 * callback; R8 fails the build if this file reaches the API. Debouncing lives in
 * `useClientList` — a component that owns a timer is a component that has state
 * the screen cannot reason about.
 *
 * 🔒 **NFR-062 — every control is labelled.** The search box is a `FormField`
 * rather than a placeholder-only input: a placeholder disappears on first
 * keystroke and is not an accessible name, so a screen-reader user who tabs back
 * to a half-typed field would hear only its contents.
 */

import { Badge, Button, FormField, Input, Select } from '@wellnesscrm/design-system'
import { STAGE_LABEL, type ArchivedView, type StageValue } from './stages'

export interface TagFilterOption {
  id: string
  name: string
}

export interface SortFilterOption {
  value: string
  label: string
}

export interface ClientListFiltersProps {
  search: string
  stages: readonly string[]
  tagIds: readonly string[]
  archived: ArchivedView
  sort: string
  availableTags: readonly TagFilterOption[]
  sortOptions: readonly SortFilterOption[]
  /** How many match — rendered only once counted. */
  total: number | null
  isFiltered: boolean
  onSearchChange: (value: string) => void
  onToggleStage: (stage: string) => void
  onToggleTag: (tagId: string) => void
  onArchivedChange: (value: ArchivedView) => void
  onSortChange: (value: string) => void
  onClearFilters: () => void
}

/**
 * 🔒 The stages offered as filters, in funnel order.
 *
 * ⚠️ `archived` is absent by design — archived clients are reached through the
 * separate `archived` control (FR-M1-014), not by picking a stage. Offering it
 * in both places would let a practitioner build the contradictory query
 * "archived stage, excluding archived" and get an empty list with no
 * explanation.
 */
const FILTERABLE_STAGES: readonly StageValue[] = [
  'lead',
  'contacted',
  'consultation_scheduled',
  'active',
  'paused',
  'churned',
]

/** 🔒 FR-M1-014's three states, worded as a practitioner would ask for them. */
const ARCHIVED_CHOICES: readonly { value: ArchivedView; label: string }[] = [
  { value: 'exclude', label: 'Active clients' },
  { value: 'include', label: 'Active and archived' },
  { value: 'only', label: 'Archived only' },
]

export function ClientListFilters({
  search,
  stages,
  tagIds,
  archived,
  sort,
  availableTags,
  sortOptions,
  total,
  isFiltered,
  onSearchChange,
  onToggleStage,
  onToggleTag,
  onArchivedChange,
  onSortChange,
  onClearFilters,
}: ClientListFiltersProps) {
  return (
    <div>
      {/* 🔒 FR-M1-021. `type="search"` rather than `text`: mobile keyboards
        * offer a search key, and the browser's own clear button appears — one
        * fewer control for us to label. */}
      <FormField
        label="Search clients"
        hint="Name, email, or the last digits of a mobile number."
      >
        <Input
          type="search"
          value={search}
          onChange={(event) => onSearchChange(event.target.value)}
          autoComplete="off"
        />
      </FormField>

      {/* ⚠️ `aria-live="polite"` and not `assertive`. The count changes on every
        * keystroke while typing; interrupting a screen-reader user mid-word to
        * announce an intermediate result would make the field unusable. */}
      {total !== null && (
        <p aria-live="polite">
          {total === 1 ? '1 client' : `${total} clients`}
        </p>
      )}

      {/* 🔒 FR-M1-022. Toggle buttons rather than a multi-select, matching the
        * timeline's filters: the whole set is small and visible, and
        * `aria-pressed` announces state without opening a menu. */}
      <div role="group" aria-label="Filter by stage">
        {FILTERABLE_STAGES.map((stage) => {
          const active = stages.includes(stage)
          return (
            <Button
              key={stage}
              variant={active ? 'primary' : 'secondary'}
              aria-pressed={active}
              onClick={() => onToggleStage(stage)}
            >
              {STAGE_LABEL[stage]}
            </Button>
          )
        })}
      </div>

      {availableTags.length > 0 && (
        // 🔒 Tags AND together, which the hint states outright. A practitioner
        // picking "diabetic" and "post-natal" almost always means both, but the
        // opposite reading is just as plausible from the UI alone — and a filter
        // whose logic you have to infer from result counts is a filter that
        // silently misleads.
        <div role="group" aria-label="Filter by tag">
          <p id="tag-filter-hint">Clients must have every tag selected.</p>
          {availableTags.map((tag) => {
            const active = tagIds.includes(tag.id)
            return (
              <Button
                key={tag.id}
                variant={active ? 'primary' : 'secondary'}
                aria-pressed={active}
                aria-describedby="tag-filter-hint"
                onClick={() => onToggleTag(tag.id)}
              >
                {tag.name}
                {active && <Badge tone="neutral">✓</Badge>}
              </Button>
            )
          })}
        </div>
      )}

      <FormField label="Show">
        <Select
          value={archived}
          onChange={(event) => onArchivedChange(event.target.value as ArchivedView)}
        >
          {ARCHIVED_CHOICES.map((choice) => (
            <option key={choice.value} value={choice.value}>
              {choice.label}
            </option>
          ))}
        </Select>
      </FormField>

      {/* ⚠️ Rendered only when the server supplied the vocabulary. If
        * `sort-options` failed, the list is still readable in its default order,
        * and an empty dropdown would suggest sorting is broken rather than
        * unavailable. */}
      {sortOptions.length > 0 && (
        <FormField label="Sort by">
          <Select value={sort} onChange={(event) => onSortChange(event.target.value)}>
            {sortOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
        </FormField>
      )}

      {isFiltered && (
        <Button variant="ghost" onClick={onClearFilters}>
          Clear filters
        </Button>
      )}
    </div>
  )
}
