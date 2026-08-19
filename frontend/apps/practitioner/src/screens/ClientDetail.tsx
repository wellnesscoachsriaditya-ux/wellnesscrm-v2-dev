/**
 * Client 360 — the premium client workspace (S2 Slice B, recomposed).
 *
 * 🔒 The screen composes; the hooks fetch; the components render. That split is
 * Arch §4.4 and enforced by `check_boundaries.py` R8. Every panel that existed
 * before is still mounted and wired to the same hook — this change is
 * presentation only: a premium identity hero, a sticky section nav, a two-column
 * workspace and a context rail. No API contract, business rule or authorization
 * check is altered.
 */

import { ErrorState, Spinner } from '@wellnesscrm/design-system'
import { useIaLocation } from '@wellnesscrm/ia'
import { useNavigate } from 'react-router-dom'
import { Icon, StatusPill } from '../premium/ui'
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
import { ClinicalAssessmentPanel, type AssessmentView } from '../components/clients/ClinicalAssessmentPanel'
import { ClinicalMeasurementPanel, type MeasurementView } from '../components/clients/ClinicalMeasurementPanel'
import {
  ClinicalConsultationNotesPanel,
  type ConsultationNoteView,
} from '../components/clients/ClinicalConsultationNotesPanel'
import { ClinicalDocumentsPanel, type ClientDocumentView } from '../components/clients/ClinicalDocumentsPanel'
import { useClientDetail } from '../features/clients/useClientDetail'
import { useCollaboration } from '../features/clients/useCollaboration'
import { useTimeline } from '../features/clients/useTimeline'
import { useClientMessaging } from '../features/messaging/useClientMessaging'
import { useClickToChat } from '../features/messaging/useClickToChat'
import { useAssessments, useMeasurements, useConsultationNotes, useDocuments } from '../features/clients/useClinical'
import { useCurrentSession } from '../features/session/useCurrentSession'
import { ClientPlansPanel } from '../components/nutrition/ClientPlansPanel'
import { useClientPlans } from '../features/nutrition/useClientPlans'
import type { Grant, Note, Tag } from '../features/clients/collaborationApi'
import type { TimelineEntry, TimelineEventType } from '../features/clients/timelineApi'
import type { CheckinSchedule, Dispatch, PendingMessage } from '../features/messaging/messagingApi'
import type {
  AssessmentSummary,
  MeasurementResponse,
  ConsultationNoteResponse,
  ClientDocumentResponse,
} from '../features/clients/clinicalApi'
import type { SelectableStage } from '../features/clients/api'
import type { StageValue } from '../components/clients/stages'
import styles from './ClientDetail.module.css'

function toNoteView(note: Note): NoteView {
  return { id: note.id, body: note.body, authorUserId: note.author_user_id, createdAt: note.created_at, updatedAt: note.updated_at }
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
    createdAt: m.measured_on,
  }
}
function toConsultationNoteView(n: ConsultationNoteResponse): ConsultationNoteView {
  return { id: n.id, noteDate: n.note_date, body: n.body, authorUserId: n.author_user_id, createdAt: n.created_at, updatedAt: n.updated_at }
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

const SELECTABLE_STAGES: readonly SelectableStage[] = [
  'lead',
  'contacted',
  'consultation_scheduled',
  'active',
  'paused',
  'churned',
]

/** Hero pill labels — deliberately distinct from the lifecycle panel's own
 * copy ("New enquiry"/"Active client") so a query for those finds one element. */
const HERO_STAGE: Record<string, string> = {
  lead: 'Lead',
  contacted: 'Contacted',
  consultation_scheduled: 'Consultation',
  active: 'Active',
  paused: 'Paused',
  churned: 'Churned',
}

function GroupHeader({ icon, accent, title, id }: { icon: string; accent: string; title: string; id: string }) {
  return (
    <div className={styles.groupHead} id={id}>
      <span className={styles.groupIcon} style={{ background: `var(--p-${accent}-soft)`, color: `var(--p-${accent}-ink)` }}>
        <Icon name={icon} size={18} />
      </span>
      <h2 className={styles.groupTitle}>{title}</h2>
    </div>
  )
}

export function ClientDetail() {
  const { params } = useIaLocation()
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

  if (loading) return <Spinner label="Loading client…" />

  if (error !== null || client === null) {
    return (
      <ErrorState
        title={error ?? 'That client could not be loaded'}
        whatToDoNext="Check the link, or go back and search for them by name."
        {...(requestId !== null ? { reference: requestId } : {})}
      />
    )
  }

  const isArchived = client.archived_at !== null

  return (
    <div className={styles.page}>
      <button type="button" className={styles.back} onClick={() => navigate('/clients')} data-testid="back-to-clients">
        <Icon name="arrowLeft" size={16} /> All clients
      </button>

      {/* Identity hero */}
      <header className={styles.hero}>
        <span className={styles.heroAvatar}>{client.full_name.slice(0, 1).toUpperCase()}</span>
        <div className={styles.heroMain}>
          <h1 className={styles.heroName}>{client.full_name}</h1>
          <div className={styles.heroPills}>
            <StatusPill value={isArchived ? 'archived' : client.stage} label={isArchived ? 'Archived' : HERO_STAGE[client.stage] ?? client.stage} />
            {client.is_minor === true && <span className={styles.heroMinor}>Minor</span>}
          </div>
          <div className={styles.heroMeta}>
            {client.city && (
              <span className={styles.heroMetaItem}><Icon name="resources" size={14} /> {client.city}</span>
            )}
            {client.mobile && (
              <span className={styles.heroMetaItem}><Icon name="phone" size={14} /> {client.mobile}</span>
            )}
            {client.email && (
              <span className={styles.heroMetaItem}><Icon name="mail" size={14} /> {client.email}</span>
            )}
          </div>
        </div>
        <div className={styles.heroActions}>
          {whatsApp.href !== null && (
            <a className={styles.heroBtnGhost} href={whatsApp.href} target="_blank" rel="noreferrer">
              <Icon name="messages" size={16} /> WhatsApp
            </a>
          )}
          <button type="button" className={styles.heroBtn} onClick={() => document.getElementById('sec-plans')?.scrollIntoView({ behavior: 'smooth' })}>
            <Icon name="plans" size={16} /> Plans
          </button>
        </div>
      </header>

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

      {/* Sticky section nav */}
      <nav className={styles.sectionNav} aria-label="Client sections">
        <a href="#sec-overview">Overview</a>
        <a href="#sec-clinical">Clinical</a>
        <a href="#sec-plans">Plans</a>
        <a href="#sec-notes">Notes</a>
        <a href="#sec-messaging">Messaging</a>
        <a href="#sec-timeline">Timeline</a>
      </nav>

      <div className={styles.grid}>
        <div className={styles.main}>
          <section className={styles.group} id="sec-overview">
            <GroupHeader icon="checkins" accent="emerald" title="Overview & lifecycle" id="ov" />
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
          </section>

          <section className={styles.group} id="sec-clinical">
            <GroupHeader icon="progress" accent="teal" title="Assessments & measurements" id="cl" />
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
          </section>

          <section className={styles.group} id="sec-plans">
            <GroupHeader icon="plans" accent="amber" title="Nutrition plans" id="pl" />
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
          </section>

          <section className={styles.group} id="sec-notes">
            <GroupHeader icon="clipboard" accent="blue" title="Notes" id="nt" />
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
          </section>

          <section className={styles.group} id="sec-messaging">
            <GroupHeader icon="messages" accent="violet" title="Messaging" id="ms" />
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
          </section>

          <section className={styles.group} id="sec-timeline">
            <GroupHeader icon="progress" accent="blue" title="Timeline" id="tl" />
            <ClientTimelinePanel
              entries={timeline.entries.map(toTimelineView)}
              filters={timeline.filters.map((filter) => ({ eventType: filter.event_type, label: filter.label }))}
              selected={timeline.selected}
              loading={timeline.loading}
              loadingMore={timeline.loadingMore}
              error={timeline.error}
              hasMore={timeline.hasMore}
              onLoadMore={() => void timeline.loadMore()}
              onToggleFilter={(eventType) => timeline.toggleFilter(eventType as TimelineEventType)}
              onClearFilters={timeline.clearFilters}
            />
          </section>
        </div>

        {/* Context rail */}
        <aside className={styles.rail}>
          <span className={styles.railTitle}>Contact</span>
          <ClientSummary
            fullName={client.full_name}
            mobile={client.mobile}
            email={client.email}
            city={client.city}
            isMinor={client.is_minor}
            activatedAt={client.activated_at}
          />
          <span className={styles.railTitle}>Tags</span>
          <ClientTagsPanel
            allTags={collaboration.allTags.map(toTagView)}
            clientTagIds={collaboration.clientTags.map((tag) => tag.id)}
            error={collaboration.tagsError}
            busy={collaboration.busy}
            loading={collaboration.loading}
            onToggle={(tagId, attached) => void collaboration.toggleTag(tagId, attached)}
            onCreate={(name, colour) => void collaboration.createAndAttachTag(name, colour)}
          />
          <span className={styles.railTitle}>Check-in cadence</span>
          <ClientCheckinPanel
            schedule={toCheckinView(messaging.checkin)}
            loading={messaging.loading}
            error={messaging.checkinError}
            busy={messaging.busy}
            onSave={(update) =>
              void messaging.saveCheckin({ frequency: update.frequency, day_of_week: update.dayOfWeek, is_paused: update.isPaused })
            }
          />
          <span className={styles.railTitle}>Access</span>
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
        </aside>
      </div>
    </div>
  )
}
