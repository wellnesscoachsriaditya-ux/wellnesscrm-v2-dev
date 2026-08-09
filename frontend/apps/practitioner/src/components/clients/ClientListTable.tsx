/**
 * The client list itself — FR-M1-021, EC-M1-04.
 *
 * 🔒 Renders and reports (Arch §4.4). R8 fails the build if this reaches the API.
 *
 * 🔒 **A real `<table>`, not a grid of divs.** The list is tabular data a
 * practitioner scans by column, and a table gives row/column announcements,
 * `scope`d headers and native keyboard navigation for free — all of which NFR-062
 * requires and a div grid has to reimplement.
 *
 * ⚠️ **Selection exists only when `onSelectionChange` is passed.** Bulk
 * reassignment is owner-only (EC-M1-04), so a practitioner without the right
 * sees no checkbox column at all rather than a disabled one — a control that is
 * always there but never usable reads as a broken feature.
 */

import {
  Badge,
  Checkbox,
  EmptyState,
  Spinner,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeaderCell,
  TableRow,
  Button,
} from '@wellnesscrm/design-system'
import { STAGE_LABEL, STAGE_TONE, type StageValue } from './stages'

export interface ClientRowView {
  id: string
  fullName: string
  mobile: string | null
  email: string | null
  city: string | null
  stage: StageValue
  isArchived: boolean
  ownerName: string | null
  tags: readonly { id: string; name: string }[]
}

export interface ClientListTableProps {
  clients: readonly ClientRowView[]
  loading?: boolean
  loadingMore?: boolean
  hasMore?: boolean
  /** True when a filter narrows the list — changes what "empty" means. */
  isFiltered?: boolean
  selectedIds?: readonly string[]
  onSelectionChange?: (ids: readonly string[]) => void
  onOpenClient: (clientId: string) => void
  onLoadMore: () => void
  onClearFilters: () => void
}

/**
 * How a client's contact column reads.
 *
 * ⚠️ Mobile is preferred over email, and the reason is the market: for an Indian
 * dietitian the phone number *is* the client's identity — it is what WhatsApp is
 * keyed on and what they would search by. Email is the fallback, and "No contact"
 * is stated rather than left blank so an incomplete record is visibly incomplete
 * rather than looking like a rendering fault.
 */
function contactOf(client: ClientRowView): string {
  return client.mobile ?? client.email ?? 'No contact'
}

export function ClientListTable({
  clients,
  loading = false,
  loadingMore = false,
  hasMore = false,
  isFiltered = false,
  selectedIds = [],
  onSelectionChange,
  onOpenClient,
  onLoadMore,
  onClearFilters,
}: ClientListTableProps) {
  const selectable = onSelectionChange !== undefined
  const allSelected = clients.length > 0 && selectedIds.length === clients.length

  if (loading) return <Spinner label="Loading clients…" />

  if (clients.length === 0) {
    // 🔒 NFR-064, and the two states say different things. "No matches" is a
    // filter the practitioner can undo; an empty practice needs the action that
    // fixes it. Showing "Add your first client" to someone whose filter simply
    // excluded everyone would be actively misleading.
    return isFiltered ? (
      <EmptyState
        title="No clients match those filters"
        description="Try a different stage or tag, or clear the filters to see everyone."
        action={
          <Button variant="secondary" onClick={onClearFilters}>
            Clear filters
          </Button>
        }
      />
    ) : (
      <EmptyState
        title="No clients yet"
        description="Add your first client, or share your enquiry form to start collecting leads."
      />
    )
  }

  return (
    <>
      <Table caption="Your clients" captionHidden>
        <TableHead>
          <TableRow>
            {selectable && (
              <TableHeaderCell>
                {/* 🔒 Selects the loaded rows, and says so. With cursor paging
                  * "all" cannot mean the whole result set — the browser has not
                  * seen it. A checkbox that claims to select 400 clients while
                  * reassigning 50 is the kind of quiet mismatch EC-M1-04's
                  * all-or-nothing transaction exists to avoid. */}
                <Checkbox
                  label={`Select all ${clients.length} loaded`}
                  checked={allSelected}
                  onChange={(event) =>
                    onSelectionChange(event.target.checked ? clients.map((c) => c.id) : [])
                  }
                />
              </TableHeaderCell>
            )}
            <TableHeaderCell>Name</TableHeaderCell>
            <TableHeaderCell>Contact</TableHeaderCell>
            <TableHeaderCell>Stage</TableHeaderCell>
            <TableHeaderCell>Tags</TableHeaderCell>
            <TableHeaderCell>Owner</TableHeaderCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {clients.map((client) => (
            // ⚠️ `muted` on archived rows: under `archived=include` they sit
            // beside active clients, and only the badge would distinguish them.
            <TableRow key={client.id} muted={client.isArchived}>
              {selectable && (
                <TableCell>
                  <Checkbox
                    label={`Select ${client.fullName}`}
                    checked={selectedIds.includes(client.id)}
                    onChange={(event) =>
                      onSelectionChange(
                        event.target.checked
                          ? [...selectedIds, client.id]
                          : selectedIds.filter((id) => id !== client.id),
                      )
                    }
                  />
                </TableCell>
              )}
              <TableCell>
                {/* 🔒 A button, not a whole clickable row. A row-level handler
                  * has no accessible name and cannot be tabbed to, and it fights
                  * the selection checkbox inside it — clicking to tick would
                  * also navigate away. */}
                <Button variant="ghost" onClick={() => onOpenClient(client.id)}>
                  {client.fullName}
                </Button>
              </TableCell>
              <TableCell>{contactOf(client)}</TableCell>
              <TableCell>
                <Badge tone={STAGE_TONE[client.stage]}>{STAGE_LABEL[client.stage]}</Badge>
                {client.isArchived && <Badge tone="neutral">Archived</Badge>}
              </TableCell>
              <TableCell>
                {client.tags.length === 0
                  ? '—'
                  : client.tags.map((tag) => (
                      <Badge key={tag.id} tone="neutral">
                        {tag.name}
                      </Badge>
                    ))}
              </TableCell>
              {/* ⚠️ The owner's name when the server resolved it, the em dash
                * when it did not. `owner_name` is null for a user outside this
                * practitioner's visibility — rendering the raw uuid would leak
                * an identifier for no benefit. */}
              <TableCell>{client.ownerName ?? '—'}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>

      {hasMore && (
        <Button variant="secondary" onClick={onLoadMore} loading={loadingMore}>
          Load more clients
        </Button>
      )}
    </>
  )
}
