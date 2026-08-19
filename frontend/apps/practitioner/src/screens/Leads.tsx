/**
 * Leads — the enquiry workflow, M2's whole practitioner surface.
 *
 * 🔒 The screen composes; the hook fetches; the components render. That split is
 * Arch §4.4 and is enforced by `check_boundaries.py` R8, which fails the build if
 * anything under `components/` imports the API client.
 *
 * 🔒 **US-M2-03 is the job**: "see all enquiries needing a response in one place,
 * so none are forgotten". The needs-response view opens by default because that
 * is the question a practitioner arrives with; the archive is one click away for
 * the rarer "did she ever contact us?".
 *
 * ⚠️ **There is no "add lead" form here, deliberately.** FR-M2-010 wants a manual
 * lead in ≤3 interactions, and M1.3 already makes a lead a `clients` row at stage
 * `lead` — which `/clients/new` creates by default. A second form would be a
 * duplicate write path with its own validation to drift; this links to the one
 * that exists, which costs one interaction and no divergence.
 */

import { Button, Card, CardBody, ErrorState, PageHeader, Tabs } from '@wellnesscrm/design-system'
import { useIaLocation } from '@wellnesscrm/ia'
import { useNavigate } from 'react-router-dom'
import { EnquiryFormPanel } from '../components/clients/EnquiryFormPanel'
import { EnquiryTable } from '../components/clients/EnquiryTable'
import type { EnquiryRowView } from '../components/clients/EnquiryTable'
import type { StageValue } from '../components/clients/stages'
import { useEnquiries } from '../features/clients/useEnquiries'
import { useEnquiryForm } from '../features/clients/useEnquiryForm'
import type { EnquiryListItem } from '../features/clients/enquiriesApi'

/**
 * Project the wire shape onto what the table renders.
 *
 * 🔒 The mapping lives here rather than in the component, so a field rename in
 * the API is a compile error in one file instead of a silent `undefined` in the
 * table. The component's props are named for what they mean on screen; the wire
 * type is named for the contract.
 *
 * ⚠️ Contact prefers mobile over email, matching the client list and for the same
 * market reason: for an Indian dietitian the phone number *is* the client's
 * identity — it is what WhatsApp is keyed on and what they would search by.
 */
function toRowView(item: EnquiryListItem): EnquiryRowView {
  return {
    id: item.id,
    clientId: item.client_id,
    name: item.submitted_name,
    contact: item.submitted_mobile ?? item.submitted_email ?? 'No contact',
    goal: item.primary_goal,
    source: item.source,
    ageHours: item.age_hours,
    isAgeing: item.is_ageing,
    isAnswered: item.responded_at !== null,
    isRepeatEnquiry: item.is_duplicate_of_existing,
    stage: (item.client_stage as StageValue | null) ?? null,
    ownerName: item.owner_name,
  }
}

export function Leads() {
  const { breadcrumbs } = useIaLocation()
  const navigate = useNavigate()
  const list = useEnquiries('needs-response')
  const form = useEnquiryForm()

  if (list.error !== null) {
    return (
      <>
        <PageHeader title="Leads" breadcrumbs={breadcrumbs} />
        <ErrorState
          title="Your enquiries could not be loaded"
          whatToDoNext="Check your connection and try again."
          {...(list.requestId !== null ? { reference: list.requestId } : {})}
        />
      </>
    )
  }

  return (
    <>
      <PageHeader
        title="Leads"
        breadcrumbs={breadcrumbs}
        actions={
          // FR-M2-010 — the manual path, for a lead that arrived by phone.
          <Button variant="primary" onClick={() => navigate('/clients/new')}>
            Add lead
          </Button>
        }
      />

      {/* 🔒 FR-M2-001 / US-M2-01 — the link that makes the whole module work.
        * Above the list because sharing it is what fills the list. */}
      {form.shareUrl !== null && form.form !== null && (
        <EnquiryFormPanel
          shareUrl={form.shareUrl}
          title={form.form.title}
          isActive={form.form.is_active}
          busy={form.busy}
          error={form.error}
          onToggleActive={(next) => void form.setActive(next)}
        />
      )}

      {/* ⚠️ The panel above renders only once the form has loaded, and
        * `useEnquiryForm` reports its failure by leaving `form` null and setting
        * `error` — so without this branch a failed fetch showed nothing at all:
        * no share link, and no reason for its absence. The practitioner's own
        * enquiry link is US-M2-01, so silence is the one unacceptable outcome.
        *
        * Not an `ErrorState`: the enquiry *list* below is the point of the page
        * and is unaffected, and replacing the screen over a missing panel would
        * cost the practitioner their queue. */}
      {form.form === null && form.error !== null && (
        <Card>
          <CardBody>
            <p role="alert">{form.error}</p>
          </CardBody>
        </Card>
      )}

      <Tabs
        label="Enquiry views"
        items={[
          {
            id: 'needs-response',
            label: 'Needs response',
            // 🔒 The count is the point of the tab — US-M2-03's "so none are
            // forgotten" is served by the number being visible without a click.
            //
            // ⚠️ Only while that view is showing: `total` counts whichever list
            // was last fetched, so rendering it on the inactive tab would put
            // the archive's total behind the queue's label.
            ...(list.view === 'needs-response' && list.total !== null
              ? { count: list.total }
              : {}),
          },
          { id: 'all', label: 'All enquiries' },
        ]}
        value={list.view}
        onChange={(id) => list.setView(id as 'needs-response' | 'all')}
      />

      {/* ⚠️ Rendered beside the list rather than replacing it: a failed "mark
        * responded" leaves the list on screen and still correct, and discarding
        * a working view over one failed click would lose the practitioner's
        * place in the queue. */}
      {list.respondError !== null && <p role="alert">{list.respondError}</p>}

      <EnquiryTable
        enquiries={list.enquiries.map(toRowView)}
        view={list.view}
        loading={list.loading}
        loadingMore={list.loadingMore}
        hasMore={list.hasMore}
        respondingId={list.respondingId}
        onOpenClient={(clientId) => navigate(`/clients/${clientId}`)}
        onRespond={(submissionId) => void list.respond(submissionId)}
        onLoadMore={() => void list.loadMore()}
      />
    </>
  )
}
