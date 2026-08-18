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
import { useNavigate } from 'react-router-dom'
import { ClientAccessPanel, type GrantView } from '../components/clients/ClientAccessPanel'
import { ClientLifecyclePanel } from '../components/clients/ClientLifecyclePanel'
import { ClientNotesPanel, type NoteView } from '../components/clients/ClientNotesPanel'
import { ClientSummary } from '../components/clients/ClientSummary'
import { ClientTagsPanel, type TagView } from '../components/clients/ClientTagsPanel'
import { ClientTimelinePanel, type TimelineEntryView } from '../components/clients/ClientTimelinePanel'
import {
  ClientMessagesPanel,
  type MessageHistoryView,
  type PendingMessageView,
} from '../components/clients/ClientMessagesPanel'
import {
  ClientCheckinPanel,
  type CheckinFrequencyValue,
  type CheckinScheduleView,
} from '../components/clients/ClientCheckinPanel'
import { ClientWhatsAppPanel } from '../components/clients/ClientWhatsAppPanel'
import { EntitlementNotice } from '../components/clients/EntitlementNotice'
import {
  ClinicalAssessmentPanel,
  type AssessmentView,
} from '../components/clients/ClinicalAssessmentPanel'
import {
  ClinicalMeasurementPanel,
  type MeasurementView,
} from '../components/clients/ClinicalMeasurementPanel'
import {
  ClinicalConsultationNotesPanel,
  type ConsultationNoteView,
} from '../components/clients/ClinicalConsultationNotesPanel'
import {
  ClinicalDocumentsPanel,
  type ClientDocumentView,
} from '../components/clients/ClinicalDocumentsPanel'
import { useClientDetail } from '../features/clients/useClientDetail'
import { useCollaboration } from '../features/clients/useCollaboration'
import { useTimeline } from '../features/clients/useTimeline'
import { useClientMessaging } from '../features/messaging/useClientMessaging'
import { useClickToChat } from '../features/messaging/useClickToChat'
import {
  useAssessments,
  useMeasurements,
  useConsultationNotes,
  useDocuments,
} from '../features/clients/useClinical'
import { useCurrentSession } from '../features/session/useCurrentSession'
import { ClientPlansPanel } from '../components/nutrition/ClientPlansPanel'
import { useClientPlans } from '../features/nutrition/useClientPlans'
import type { Grant, Note, Tag } from '../features/clients/collaborationApi'
import type { TimelineEntry, TimelineEventType } from '../features/clients/timelineApi'
import type {
  CheckinSchedule,
  Dispatch,
  PendingMessage,
} from '../features/messaging/messagingApi'
import type {
  AssessmentSummary,
  MeasurementResponse,
  ConsultationNoteResponse,
  ClientDocumentResponse,
} from '../features/clients/clinicalApi'
import type { SelectableStage } from '../features/clients/api'
import type { StageValue } from '../components/clients/stages'

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
 * A delivery attempt, as the panel renders it — FR-M8-011.
 *
 * ⚠️ `failure_reason` is the provider's own text, shown to the practitioner and
 * never to the client.
 */
function toMessageView(dispatch: Dispatch): MessageHistoryView {
  return {
    id: dispatch.id,
    templateCode: dispatch.template_code,
    transport: dispatch.transport,
    status: dispatch.status,
    recipientAddress: dispatch.recipient_address,
    attemptNumber: dispatch.attempt_number,
    failureReason: dispatch.failure_reason,
    createdAt: dispatch.created_at,
  }
}

function toPendingView(message: PendingMessage): PendingMessageView {
  return {
    id: message.id,
    templateCode: message.template_code,
    scheduledFor: message.scheduled_for,
    deferredFrom: message.deferred_from,
    preview: message.preview,
  }
}

function toCheckinView(schedule: CheckinSchedule | null): CheckinScheduleView | null {
  if (schedule === null) return null
  return {
    frequency: schedule.frequency as CheckinFrequencyValue,
    dayOfWeek: schedule.day_of_week,
    timeOfDay: schedule.time_of_day,
    isPaused: schedule.is_paused,
    nextDueOn: schedule.next_due_on,
  }
}

function toAssessmentView(a: AssessmentSummary): AssessmentView {
  return {
    id: a.id,
    definitionCode: a.definition_code,
    definitionVersion: a.definition_version,
    status: a.status,
    completedBy: a.completed_by ?? null,
    startedAt: a.started_at,
    completedAt: a.completed_at ?? null,
  }
}

function toMeasurementView(m: MeasurementResponse): MeasurementView {
  return {
    id: m.id,
    measuredOn: m.measured_on,
    weightKg: m.weight_kg !== null && m.weight_kg !== undefined ? String(m.weight_kg) : null,
    heightCm: m.height_cm !== null && m.height_cm !== undefined ? String(m.height_cm) : null,
    waistCm: m.waist_cm !== null && m.waist_cm !== undefined ? String(m.waist_cm) : null,
    hipCm: m.hip_cm !== null && m.hip_cm !== undefined ? String(m.hip_cm) : null,
    bodyFatPct: m.body_fat_pct !== null && m.body_fat_pct !== undefined ? String(m.body_fat_pct) : null,
    bmi: m.bmi !== null && m.bmi !== undefined ? String(m.bmi) : null,
    waistHipRatio: m.waist_hip_ratio !== null && m.waist_hip_ratio !== undefined ? String(m.waist_hip_ratio) : null,
    source: m.source,
    isFlaggedImplausible: m.is_flagged_implausible ?? false,
    notes: m.notes ?? null,
    createdAt: m.measured_on, // using measured_on for createdAt if not available
  }
}

function toConsultationNoteView(n: ConsultationNoteResponse): ConsultationNoteView {
  return {
    id: n.id,
    noteDate: n.note_date,
    body: n.body,
    authorUserId: n.author_user_id,
    createdAt: n.created_at,
    updatedAt: n.updated_at,
  }
}

function toClientDocumentView(d: ClientDocumentResponse): ClientDocumentView {
  return {
    id: d.id,
    fileId: d.file_id,
    documentType: d.document_type,
    documentDate: d.document_date ?? null,
    uploadedBy: d.uploaded_by as 'practitioner' | 'client' | 'system',
    description: d.description ?? null,
    createdAt: d.created_at,
    archivedAt: d.archived_at ?? null,
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
  const messaging = useClientMessaging(clientId)
  const whatsApp = useClickToChat(client?.mobile ?? null)
  
  const assessmentsData = useAssessments(clientId)
  const measurementsData = useMeasurements(clientId)
  const consultationNotesData = useConsultationNotes(clientId)
  const documentsData = useDocuments(clientId)
  
  const { session } = useCurrentSession()
  const navigate = useNavigate()
  const plans = useClientPlans(clientId)

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

      <ClinicalAssessmentPanel
        assessments={assessmentsData.assessments.map(toAssessmentView)}
        loading={assessmentsData.loading}
        error={assessmentsData.error}
      />

      <ClinicalMeasurementPanel
        measurements={measurementsData.measurements.map(toMeasurementView)}
        loading={measurementsData.loading}
        error={measurementsData.error}
        busy={measurementsData.busy}
        onAdd={(body) => void measurementsData.addMeasurement(body)}
      />

      <ClinicalConsultationNotesPanel
        notes={consultationNotesData.notes.map(toConsultationNoteView)}
        currentUserId={session?.user_id ?? ''}
        isOwner={session?.role === 'owner'}
        loading={consultationNotesData.loading}
        error={consultationNotesData.error}
        busy={consultationNotesData.busy}
        onAdd={(date, body) => void consultationNotesData.addNote(date, body)}
        onEdit={(id, body) => void consultationNotesData.editNote(id, body)}
        onArchive={(id) => void consultationNotesData.archiveNote(id)}
      />

      <ClinicalDocumentsPanel
        documents={documentsData.documents.map(toClientDocumentView)}
        loading={documentsData.loading}
        error={documentsData.error}
      />

      {/* 🔒 M4 — the Plans entry point from Client 360. The panel lists this
        * client's plans and creates new ones; opening one navigates to the
        * builder at `/plans/:planId`. The 402 refusal at the plan's client
        * ceiling surfaces as `createError` in the API's own words. */}
      <ClientPlansPanel
        plans={plans.plans.map((summary) => ({
          planId: summary.plan.id,
          title: summary.plan.title,
          state: summary.version?.state ?? null,
          versionNumber: summary.version?.version_number ?? null,
        }))}
        loading={plans.loading}
        error={plans.error}
        creating={plans.creating}
        createError={plans.createError}
        onCreate={(title) => {
          void plans.create({ title }).then((created) => {
            if (created !== null) navigate(`/plans/${created.planId}`)
          })
        }}
        onOpen={(planId) => navigate(`/plans/${planId}`)}
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
      {/* 🔒 M8 — what was sent and what is queued (FR-M8-011, FR-M8-028), and
        * the check-in cadence behind most of it (FR-M8-022).
        * ⚠️ Above the timeline: the timeline says a message was sent, this says
        * what it was and whether it arrived. */}
      <ClientCheckinPanel
        schedule={toCheckinView(messaging.checkin)}
        loading={messaging.loading}
        error={messaging.checkinError}
        busy={messaging.busy}
        onSave={(update) =>
          void messaging.saveCheckin({
            frequency: update.frequency,
            day_of_week: update.dayOfWeek,
            is_paused: update.isPaused,
          })
        }
      />

      {/* 🔒 The *manual* WhatsApp channel — the practitioner's own number, no
        * Meta credentials, nothing recorded. Placed above the engine's own panel
        * so the distinction is read in that order: this is what you send, that
        * is what WellnessCRM sent. */}
      <ClientWhatsAppPanel
        clientName={client.full_name}
        message={whatsApp.message}
        href={whatsApp.href}
        reason={whatsApp.reason}
        onMessageChange={whatsApp.setMessage}
      />

      <ClientMessagesPanel
        whatsAppLinkFor={whatsApp.linkFor}
        history={messaging.history.map(toMessageView)}
        pending={messaging.pending.map(toPendingView)}
        loading={messaging.loading}
        loadingMore={messaging.loadingMore}
        hasMore={messaging.hasMore}
        historyError={messaging.historyError}
        pendingError={messaging.pendingError}
        busy={messaging.busy}
        onLoadMore={() => void messaging.loadMore()}
        onCancel={(id) => void messaging.cancel(id)}
      />

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
