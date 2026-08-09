/**
 * The client list — FR-M1-021, FR-M1-022, EC-M1-04.
 *
 * 🔒 The screen composes; the hook fetches; the components render. That split is
 * Arch §4.4 and is enforced by `check_boundaries.py` R8, which fails the build if
 * anything under `components/` imports the API client.
 *
 * ⚠️ **This is the screen the app opens on**, and the one where a wrong answer is
 * invisible: a filter that silently drops somebody looks exactly like a practice
 * that does not have them. The scoping that decides who appears is a WHERE clause
 * on the server (AC-M1-006) — there is nothing here to get wrong, and that is
 * deliberate.
 */

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Button, ErrorState, PageHeader } from '@wellnesscrm/design-system'
import { useIaLocation } from '@wellnesscrm/ia'
import { ClientListFilters } from '../components/clients/ClientListFilters'
import { ClientListTable } from '../components/clients/ClientListTable'
import type { ClientRowView } from '../components/clients/ClientListTable'
import type { StageValue } from '../components/clients/stages'
import { ReassignClientsDialog } from '../components/clients/ReassignClientsDialog'
import { useClientList } from '../features/clients/useClientList'
import { useTagVocabulary } from '../features/clients/useTagVocabulary'
import { useCurrentSession } from '../features/session/useCurrentSession'
import type { ClientListItem } from '../features/clients/discoveryApi'

/**
 * Project the wire shape onto what the table renders.
 *
 * 🔒 The mapping lives here rather than in the component, so a field rename in
 * the API is a compile error in one file instead of a silent `undefined` in the
 * table. The component's props are named for what they mean on screen; the wire
 * type is named for the contract.
 */
function toRowView(item: ClientListItem): ClientRowView {
  return {
    id: item.id,
    fullName: item.full_name,
    mobile: item.mobile,
    email: item.email,
    city: item.city,
    stage: item.stage as StageValue,
    isArchived: item.archived_at !== null,
    ownerName: item.owner_name,
    tags: item.tags.map((tag) => ({ id: tag.id, name: tag.name })),
  }
}

export function ClientList() {
  const { breadcrumbs } = useIaLocation()
  const navigate = useNavigate()
  const list = useClientList()
  const { tags } = useTagVocabulary()
  const { session } = useCurrentSession()

  const [selectedIds, setSelectedIds] = useState<readonly string[]>([])
  const [reassigning, setReassigning] = useState(false)

  /**
   * 🔒 EC-M1-04 is owner-only, matching `client.bulk_reassign`.
   *
   * ⚠️ Without a session the answer is "no", which is the safe direction — the
   * API would refuse the call anyway, and a checkbox column that is always
   * refused reads as a broken feature rather than one the practitioner lacks.
   */
  const canReassign = session?.role === 'owner'

  if (list.error !== null) {
    return (
      <>
        <PageHeader title="Clients" breadcrumbs={breadcrumbs} />
        <ErrorState
          title="Your clients could not be loaded"
          whatToDoNext="Check your connection and try again."
          {...(list.requestId !== null ? { reference: list.requestId } : {})}
        />
      </>
    )
  }

  return (
    <>
      <PageHeader
        title="Clients"
        breadcrumbs={breadcrumbs}
        actions={
          <Button variant="primary" onClick={() => navigate('/clients/new')}>
            Add client
          </Button>
        }
      />

      <ClientListFilters
        search={list.filters.search}
        stages={list.filters.stages}
        tagIds={list.filters.tagIds}
        archived={list.filters.archived}
        sort={list.filters.sort}
        availableTags={tags}
        sortOptions={list.sortOptions}
        total={list.total}
        isFiltered={list.isFiltered}
        onSearchChange={list.setSearch}
        onToggleStage={list.toggleStage}
        onToggleTag={list.toggleTag}
        onArchivedChange={list.setArchived}
        onSortChange={list.setSort}
        onClearFilters={list.clearFilters}
      />

      {/* 🔒 EC-M1-04. Rendered above the table so the count and the action sit
        * beside the selection they describe, and only when something is
        * selected — a permanently visible "Reassign 0 clients" is noise. */}
      {canReassign && selectedIds.length > 0 && (
        <div role="status">
          <span>
            {selectedIds.length === 1
              ? '1 client selected'
              : `${selectedIds.length} clients selected`}
          </span>
          <Button variant="secondary" onClick={() => setReassigning(true)}>
            Reassign to a colleague
          </Button>
          <Button variant="ghost" onClick={() => setSelectedIds([])}>
            Clear selection
          </Button>
        </div>
      )}

      <ClientListTable
        clients={list.clients.map(toRowView)}
        loading={list.loading}
        loadingMore={list.loadingMore}
        hasMore={list.hasMore}
        isFiltered={list.isFiltered}
        {...(canReassign
          ? { selectedIds, onSelectionChange: setSelectedIds }
          : {})}
        onOpenClient={(clientId) => navigate(`/clients/${clientId}`)}
        onLoadMore={() => void list.loadMore()}
        onClearFilters={list.clearFilters}
      />

      <ReassignClientsDialog
        open={reassigning}
        count={selectedIds.length}
        busy={list.reassigning}
        error={list.reassignError}
        onCancel={() => setReassigning(false)}
        onConfirm={(ownerUserId) => {
          // ⚠️ `void` because JSX handlers must not be given a promise — an
          // unhandled rejection here would be invisible. `reassign` resolves to
          // `null` instead of throwing, so the outcome arrives as state.
          void list.reassign(selectedIds, ownerUserId).then((moved) => {
            // 🔒 The dialog stays open and selection intact when `moved` is
            // null. EC-M1-04 moved nothing, so the practitioner can correct the
            // id and retry rather than hunt for all of them again.
            if (moved === null) return
            setSelectedIds([])
            setReassigning(false)
          })
        }}
      />
    </>
  )
}
