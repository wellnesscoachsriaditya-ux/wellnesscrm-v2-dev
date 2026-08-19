/**
 * The client timeline — FR-M1-018, FR-M1-019.
 *
 * 🔒 Renders and reports (Arch §4.4). Every entry is a prop and every action a
 * callback; R8 fails the build if anything here reaches the API.
 *
 * 🔒 **Summaries are non-clinical labels**, guaranteed server-side by
 * `kernel.timeline.summarise` (DB §5.6). This component renders them verbatim
 * and never interpolates a client's own data into one — the timeline is the
 * most-viewed screen and the easiest place for a value to leak into a screenshot
 * or a support ticket.
 *
 * ⚠️ **`source_record_id` is not a link.** It may point at a retired tag or a
 * revoked grant, and resolving it would mean this component knowing about six
 * modules' routes. Deep links land with the screens that own those records.
 */

import { Badge, Button, Card, CardBody, CardHeader, EmptyState, Spinner } from '@wellnesscrm/design-system'

export interface TimelineEntryView {
  id: string
  eventType: string
  occurredAt: string
  summary: string
  actorType: 'practitioner' | 'client' | 'system'
  actorId: string | null
}

export interface TimelineFilterView {
  eventType: string
  label: string
}

export interface ClientTimelinePanelProps {
  entries: readonly TimelineEntryView[]
  filters: readonly TimelineFilterView[]
  selected: readonly string[]
  loading?: boolean
  loadingMore?: boolean
  error?: string | null
  hasMore?: boolean
  onLoadMore: () => void
  onToggleFilter: (eventType: string) => void
  onClearFilters: () => void
}

/**
 * How an entry's actor reads.
 *
 * 🔒 "System" is stated rather than left blank. An automated archive with no
 * attribution reads as though a colleague did it and forgot to say so, which is
 * exactly the misattribution `actor_type` exists to prevent.
 *
 * ⚠️ A practitioner is rendered as an id, not a name. `TimelineEntryResponse`
 * carries identifiers only (NFR-033) and there is no team endpoint to resolve
 * them yet — the same gap the access panel has.
 */
function actorLabel(entry: TimelineEntryView): string {
  if (entry.actorType === 'system') return 'Automatic'
  if (entry.actorType === 'client') return 'Client'
  return entry.actorId ?? 'A practitioner'
}

export function ClientTimelinePanel({
  entries,
  filters,
  selected,
  loading = false,
  loadingMore = false,
  error = null,
  hasMore = false,
  onLoadMore,
  onToggleFilter,
  onClearFilters,
}: ClientTimelinePanelProps) {
  const isFiltered = selected.length > 0

  return (
    <Card>
      <CardHeader title="Timeline" description="Everything that has happened with this client." />
      <CardBody>
        {error !== null && <p role="alert">{error}</p>}

        {filters.length > 0 && (
          // 🔒 FR-M1-019. Toggle buttons rather than a multi-select: the whole
          // set is small and visible, and `aria-pressed` announces state on
          // every activation without opening a menu.
          <div aria-label="Filter timeline">
            {filters.map((filter) => {
              const active = selected.includes(filter.eventType)
              return (
                <Button
                  key={filter.eventType}
                  variant={active ? 'primary' : 'secondary'}
                  aria-pressed={active}
                  onClick={() => onToggleFilter(filter.eventType)}
                >
                  {filter.label}
                </Button>
              )
            })}
            {isFiltered && (
              <Button variant="ghost" onClick={onClearFilters}>
                Show everything
              </Button>
            )}
          </div>
        )}

        {loading ? (
          <Spinner label="Loading timeline…" />
        ) : entries.length === 0 ? (
          // 🔒 NFR-064 — and the two empty states say different things. "No
          // matches" after filtering is a filter problem the practitioner can
          // fix; an empty timeline is a client nothing has happened to yet.
          isFiltered ? (
            <EmptyState
              title="Nothing matches those filters"
              description="Try a different event type, or show everything."
            />
          ) : (
            <EmptyState
              title="Nothing has happened yet"
              description="Stage changes, notes and tags will appear here as you work with this client."
            />
          )
        ) : (
          <>
            {/* 🔒 An ordered list: the timeline's order is its meaning, and a
              * screen reader announcing "list of 20" without order would lose
              * that. Newest first, as the server ordered it — the component
              * does not sort. */}
            <ol aria-label="Timeline">
              {entries.map((entry) => (
                <li key={entry.id}>
                  <Badge tone="neutral">{entry.summary}</Badge>
                  <time dateTime={entry.occurredAt}>
                    {new Date(entry.occurredAt).toLocaleString()}
                  </time>
                  <span>{actorLabel(entry)}</span>
                </li>
              ))}
            </ol>

            {hasMore && (
              <Button variant="secondary" onClick={onLoadMore} loading={loadingMore}>
                Load older entries
              </Button>
            )}
          </>
        )}
      </CardBody>
    </Card>
  )
}
