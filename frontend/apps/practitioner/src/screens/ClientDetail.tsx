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
import { ClientLifecyclePanel } from '../components/clients/ClientLifecyclePanel'
import { ClientSummary } from '../components/clients/ClientSummary'
import { EntitlementNotice } from '../components/clients/EntitlementNotice'
import type { StageValue } from '../components/clients/stages'
import { useClientDetail } from '../features/clients/useClientDetail'
import type { SelectableStage } from '../features/clients/api'

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
  } = useClientDetail(clientId)

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
    </>
  )
}
