/**
 * The practitioner's landing screen — S2 Slice G.
 *
 * 🔒 **The question this page answers is "what needs me today?"** — not "how is
 * the business doing". M2.2 prices a forgotten enquiry at ₹2,500–4,000/month of
 * recurring revenue, and US-M2-03 asks for the waiting list "so none are
 * forgotten"; that is the panel at the top, and everything else is secondary.
 *
 * 🔒 **Composed from panels that already exist.** The waiting queue is the same
 * `EnquiryTable` the Leads screen renders, in the same ordering — a second table
 * with its own idea of what "waiting" looks like would be two components to keep
 * in agreement, and they would drift.
 *
 * ⚠️ **No invented metrics.** Everything on screen is a value the server sent.
 * See `useWorkspace` for what is deliberately absent and why — in particular the
 * plan-usage indicator FR-M1-001 asks for, which has no endpoint behind it and
 * cannot be counted in the browser without contradicting the server.
 */

import {
  Button,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  PageHeader,
  Spinner,
} from '@wellnesscrm/design-system'
import { useIaLocation } from '@wellnesscrm/ia'
import { useNavigate } from 'react-router-dom'
import { EnquiryTable, type EnquiryRowView } from '../components/clients/EnquiryTable'
import { RecentClientsPanel } from '../components/dashboard/RecentClientsPanel'
import type { StageValue } from '../components/clients/stages'
import { useWorkspace } from '../features/dashboard/useWorkspace'
import type { EnquiryListItem } from '../features/clients/enquiriesApi'

/**
 * Project the wire shape onto what the table renders.
 *
 * ⚠️ Deliberately identical to the Leads screen's mapping. It is duplicated
 * rather than shared because the alternative — a shared mapper in `features/` —
 * would be imported by a component, which R8 forbids; and lifting it into
 * `components/` would give a presentational module a dependency on the wire
 * type. Two short, compile-checked projections are the cheaper mistake: if the
 * contract changes, both fail to build.
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

export function Dashboard() {
  const { breadcrumbs } = useIaLocation()
  const navigate = useNavigate()
  const workspace = useWorkspace()

  const waiting = workspace.waitingEnquiries
  // ⚠️ The server's count when it sent one, otherwise what is on screen. Never
  // computed as "rows + something" — a page of five out of forty would make any
  // arithmetic here quietly wrong.
  const waitingCount = workspace.waitingTotal ?? waiting.length

  return (
    <>
      <PageHeader
        // 🔒 NFR-057 — the same word the IA declares and the navigation shows.
        // "Today" reads better in isolation and would put a heading on screen
        // that no menu item matches, which is exactly the drift one declared IA
        // exists to prevent.
        title="Dashboard"
        breadcrumbs={breadcrumbs}
        actions={
          // 🔒 NFR-011 — creation must be reachable in ≤3 interactions "from
          // anywhere", and this is the screen a practitioner lands on. This
          // click is the first of the three.
          <Button onClick={() => navigate('/clients/new')}>Add client</Button>
        }
      />

      {/* 🔒 US-M2-03 / FR-M2-011 — the queue, oldest first, above everything
        * else on the page because it is the only panel with a deadline. */}
      <Card>
        <CardHeader
          as="h2"
          title="Waiting for your reply"
          description={
            workspace.loading
              ? undefined
              : waitingCount > 0
                ? `${waitingCount} ${waitingCount === 1 ? 'person is' : 'people are'} waiting to hear from you.`
                : 'Nothing is waiting. New enquiries arrive here.'
          }
          actions={
            <Button variant="secondary" onClick={() => navigate('/leads')}>
              All enquiries
            </Button>
          }
        />
        <CardBody>
          {workspace.enquiriesError !== null ? (
            // ⚠️ Inline, not an `ErrorState`: the panel below is unaffected and
            // replacing the page over one failed read would hide working work.
            <p role="alert">{workspace.enquiriesError}</p>
          ) : (
            <EnquiryTable
              enquiries={waiting.map(toRowView)}
              view="needs-response"
              loading={workspace.loading}
              // 🔒 The dashboard shows the top of the queue and links to the
              // rest; paging here would make this a second Leads screen.
              hasMore={false}
              onOpenClient={(clientId) => navigate(`/clients/${clientId}`)}
              // ⚠️ Responding is deliberately not offered here. It is a
              // judgement a practitioner makes after reading the enquiry, and
              // the Leads screen owns that action along with its error handling.
              onRespond={() => navigate('/leads')}
              onLoadMore={() => navigate('/leads')}
            />
          )}
        </CardBody>
      </Card>

      <Card>
        <CardHeader
          as="h2"
          title="Recently active"
          description="Pick up where you left off."
          actions={
            <Button variant="secondary" onClick={() => navigate('/clients')}>
              All clients
            </Button>
          }
        />
        <CardBody>
          {workspace.clientsError !== null ? (
            <p role="alert">{workspace.clientsError}</p>
          ) : workspace.loading ? (
            <Spinner label="Loading clients…" />
          ) : workspace.recentClients.length === 0 ? (
            // 🔒 NFR-064. This is also the first-run state of the whole product,
            // so it carries the action that starts everything else.
            <EmptyState
              size="sm"
              title="No clients yet"
              description="Add your first client, or share your enquiry form and let them come to you."
              action={<Button onClick={() => navigate('/clients/new')}>Add a client</Button>}
              secondaryAction={
                <Button variant="secondary" onClick={() => navigate('/leads')}>
                  Get your form link
                </Button>
              }
            />
          ) : (
            <RecentClientsPanel
              clients={workspace.recentClients.map((client) => ({
                id: client.id,
                fullName: client.full_name,
                stage: client.stage as StageValue,
                isArchived: client.archived_at !== null,
                updatedAt: client.updated_at,
              }))}
              onOpen={(clientId) => navigate(`/clients/${clientId}`)}
            />
          )}
        </CardBody>
      </Card>
    </>
  )
}
