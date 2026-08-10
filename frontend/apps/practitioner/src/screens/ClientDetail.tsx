/**
 * Client detail — profile and lifecycle (S2 Slice B).
 *
 * 🔒 The screen composes; the hook fetches; the components render. That split is
 * Arch §4.4 and is enforced by `check_boundaries.py` R8, which fails the build
 * if anything under `components/` imports the API client.
 *
 * ⏳ Timeline, notes, tags, measurements and plans land in Slices C–F. The
 * headings are not stubbed out here — an empty "Timeline" panel reads as a bug,
 * whereas its absence reads as a screen that has not been built yet.
 */

import { ErrorState, PageHeader, Spinner } from '@wellnesscrm/design-system'
import { useIaLocation } from '@wellnesscrm/ia'
import { ClientAccessPanel } from '../components/clients/ClientAccessPanel'
import type { GrantView } from '../components/clients/ClientAccessPanel'
import { ClientLifecyclePanel } from '../components/clients/ClientLifecyclePanel'
import { ClientNotesPanel } from '../components/clients/ClientNotesPanel'
import type { NoteView } from '../components/clients/ClientNotesPanel'
import { ClientSummary } from '../components/clients/ClientSummary'
import { ClientTagsPanel } from '../components/clients/ClientTagsPanel'
import type { TagView } from '../components/clients/ClientTagsPanel'
import { ClientTimelinePanel } from '../components/clients/ClientTimelinePanel'
import type { TimelineEntryView } from '../components/clients/ClientTimelinePanel'
import { EntitlementNotice } from '../components/clients/EntitlementNotice'
import type { StageValue } from '../components/clients/stages'
import { useClientDetail } from '../features/clients/useClientDetail'
import { useCollaboration } from '../features/clients/useCollaboration'
import { useTimeline } from '../features/clients/useTimeline'
import type { Grant, Note, Tag } from '../features/clients/collaborationApi'
import type { TimelineEntry, TimelineEventType } from '../features/clients/timelineApi'
import type { SelectableStage } from '../features/clients/api'
import { useCurrentSession } from '../features/session/useCurrentSession'

/**
 * Project the wire shapes onto what the components render.
 *
 * 🔒 The mapping lives here rather than in the components, so a field rename in
 * the API is a compile error in one file instead of a silent `undefined` in
 * three. The components' props are named for what they mean on screen; the wire
 * types are named for the contract.
 */
function toNoteView(note: Note): NoteView {
  return {
    id: note.id,
    body: note.body,
    authorUserId: note.author_user_id,
    createdAt: note.created_at,
    updatedAt: note.updated_at,
  }
}

function toTagView(tag: Tag): TagView {
  return { id: tag.id, name: tag.name, colour: tag.colour }
}

function toGrantView(grant: Grant): GrantView {
  return {
    userId: grant.user_id,
    grantedByUserId: grant.granted_by_user_id,
    grantedAt: grant.granted_at,
    revokedAt: grant.revoked_at,
    isLive: grant.is_live,
  }
}

function toTimelineView(entry: TimelineEntry): TimelineEntryView {
  return {
    id: entry.id,
    eventType: entry.event_type,
    occurredAt: entry.occurred_at,
    summary: entry.summary,
    actorType: entry.actor_type,
    actorId: entry.actor_id,
  }
}

/**
 * 🔒 The stages the dropdown offers, in funnel order.
 *
 * ⚠️ `archived` is absent, and the type is what enforces it:
 * `SelectableStage` comes from the generated request body, whose union the API
 * defines without it (FR-M1-010 — archiving is a separate action). A stage
 * removed server-side becomes a compile error here rather than a dead option.
 */
const SELECTABLE_STAGES: readonly SelectableStage[] = [
  'lead',
  'contacted',
  'consultation_scheduled',
  'active',
  'paused',
  'churned',
]

export function ClientDetail() {
  const { params, breadcrumbs } = useIaLocation()
  const clientId = params.clientId ?? ''
  const {
    client,
    loading,
    error,
    requestId,
    refusal,
    busy,
    changeClientStage,
    archive,
    restore,
    dismissRefusal,
    refresh,
  } = useClientDetail(clientId)
  // 🔒 `refresh` is passed because reassigning the owner changes a field on the
  // client record, which this hook does not own. See `useClientDetail.refresh`.
  const collaboration = useCollaboration(clientId, refresh)
  const timeline = useTimeline(clientId)
  const { session } = useCurrentSession()

  if (loading) {
    return (
      <>
        <PageHeader title="Client" breadcrumbs={breadcrumbs} />
        <Spinner label="Loading client…" />
      </>
    )
  }

  if (error !== null || client === null) {
    return (
      <>
        <PageHeader title="Client" breadcrumbs={breadcrumbs} />
        <ErrorState
          title={error ?? 'That client could not be loaded'}
          whatToDoNext="Check the link, or go back and search for them by name."
          {...(requestId !== null ? { reference: requestId } : {})}
        />
      </>
    )
  }

  const isArchived = client.archived_at !== null

  return (
    <>
      <PageHeader title={client.full_name} breadcrumbs={breadcrumbs} />

      {/* 🔒 FR-M0-045 — rendered above the controls, so the refusal is the first
       * thing read after the action that caused it. */}
      {refusal !== null && (
        <EntitlementNotice
          message={refusal.message}
          action={refusal.action}
          limit={refusal.limit}
          used={refusal.used}
          planCode={refusal.planCode}
          upgradeTo={refusal.upgradeTo}
          onDismiss={dismissRefusal}
        />
      )}

      <ClientSummary
        fullName={client.full_name}
        mobile={client.mobile}
        email={client.email}
        city={client.city}
        isMinor={client.is_minor}
        activatedAt={client.activated_at}
      />

      <ClientLifecyclePanel
        stage={client.stage as StageValue}
        isArchived={isArchived}
        fullName={client.full_name}
        selectableStages={SELECTABLE_STAGES}
        onChangeStage={(toStage, reason) => {
          void changeClientStage(toStage as SelectableStage, reason)
        }}
        onArchive={() => void archive()}
        onRestore={() => void restore()}
        busy={busy}
      />

      {/* 🔒 FR-M1-008. Rendered before notes because tags are how a practitioner
        * orients themselves before reading — the label answers "who is this"
        * faster than the thread does. */}
      <ClientTagsPanel
        allTags={collaboration.allTags.map(toTagView)}
        clientTagIds={collaboration.clientTags.map((tag) => tag.id)}
        error={collaboration.tagsError}
        busy={collaboration.busy}
        loading={collaboration.loading}
        onToggle={(tagId, attached) => void collaboration.toggleTag(tagId, attached)}
        onCreate={(name, colour) => void collaboration.createAndAttachTag(name, colour)}
      />

      {/* 🔒 FR-M1-007 / FR-M3-020. `currentUserId` is what makes the edit
        * control appear only for a note's own author; without a session the
        * fallback shows none, which is the safe direction. */}
      <ClientNotesPanel
        notes={collaboration.notes.map(toNoteView)}
        currentUserId={session?.user_id ?? ''}
        isOwner={session?.role === 'owner'}
        error={collaboration.notesError}
        busy={collaboration.busy}
        loading={collaboration.loading}
        onAdd={(body) => void collaboration.addNote(body)}
        onEdit={(noteId, body) => void collaboration.editNote(noteId, body)}
        onRemove={(noteId) => void collaboration.removeNote(noteId)}
      />

      {/* 🔒 EC-M0-04 / FR-M0-017. Rendered last: it is the panel a practitioner
        * reaches for least often, and `canManage` is owner-only, so for most
        * users it is a read-only statement of who else can see this client.
        * Without a session the fallback is no controls, which is the safe
        * direction — the API would refuse them anyway. */}
      <ClientAccessPanel
        ownerUserId={client.owner_user_id}
        grants={collaboration.grants.map(toGrantView)}
        canManage={session?.role === 'owner'}
        error={collaboration.accessError}
        busy={collaboration.busy}
        loading={collaboration.loading}
        onGrant={(userId) => void collaboration.grant(userId)}
        onRevoke={(userId) => void collaboration.revoke(userId)}
        onReassign={(userId) => void collaboration.reassign(userId)}
      />

      {/* 🔒 FR-M1-018 — the unified history, last on the screen because it is
        * the longest panel and the one a practitioner scrolls to deliberately.
        * ⚠️ It does not re-read when a note or tag changes: the timeline is
        * written by a transactional subscriber, so the entry exists the moment
        * the mutation commits, but this hook holds a page fetched earlier. A
        * practitioner sees it on their next load — acceptable for a history
        * panel, and cheaper than invalidating on every mutation. */}
      <ClientTimelinePanel
        entries={timeline.entries.map(toTimelineView)}
        filters={timeline.filters.map((filter) => ({
          eventType: filter.event_type,
          label: filter.label,
        }))}
        selected={timeline.selected}
        loading={timeline.loading}
        loadingMore={timeline.loadingMore}
        error={timeline.error}
        hasMore={timeline.hasMore}
        onLoadMore={() => void timeline.loadMore()}
        onToggleFilter={(eventType) => timeline.toggleFilter(eventType as TimelineEventType)}
        onClearFilters={timeline.clearFilters}
      />
    </>
  )
}
