/**
 * The practitioner dashboard — S2 Slice G.
 *
 * 🔒 **What these pin is the dashboard's restraint**, which is the property most
 * likely to be lost to a well-meant addition:
 *
 * * It shows no number it had to compute. The plan-usage indicator is the
 *   specific temptation — FR-M1-001 asks for one and no endpoint reports usage —
 *   so its absence is asserted rather than merely left untested.
 * * Its two panels fail independently. A summary page that dies whole is worse
 *   than one that dies in part: the practitioner loses the working half too.
 * * The waiting count is the server's, not the number of rows on screen.
 *
 * ⚠️ `fetch` is stubbed rather than the api-client module, for the reason given
 * in `ClientDetail.test.tsx`: the envelope decoding is part of what is tested.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { IaProvider, IaRoutes, defineIa } from '@wellnesscrm/ia'
import { Dashboard } from './Dashboard'

/** A minimal IA so breadcrumbs resolve as they do in the app. */
const ia = defineIa({
  appId: 'test',
  routes: [
    { id: 'dashboard', path: '/', label: 'Dashboard', nav: { order: 1 }, view: Dashboard },
    { id: 'clients', path: '/clients', label: 'Clients', nav: { order: 2 }, view: () => null },
    { id: 'leads', path: '/leads', label: 'Leads', nav: { order: 3 }, view: () => null },
  ],
})

const WAITING_ENQUIRY = {
  id: 'sub-1',
  client_id: 'client-1',
  submitted_name: 'Asha Menon',
  submitted_mobile: '+919876543210',
  submitted_email: null,
  primary_goal: 'Lose 8kg before my sister’s wedding',
  source: 'instagram',
  source_detail: null,
  is_duplicate_of_existing: false,
  submitted_at: '2026-08-08T10:00:00Z',
  responded_at: null,
  responded_by_user_id: null,
  age_hours: 50,
  is_ageing: true,
  client_stage: 'lead',
  client_owner_user_id: null,
  owner_name: 'Priya',
}

const RECENT_CLIENT = {
  id: 'client-9',
  full_name: 'Ravi Kumar',
  stage: 'active',
  mobile: '+919812345678',
  email: null,
  city: 'Kochi',
  dietary_class: null,
  owner_user_id: '99999999-9999-9999-9999-999999999999',
  owner_name: 'Priya',
  tags: [],
  archived_at: null,
  created_at: '2026-08-01T10:00:00Z',
  updated_at: '2026-08-09T10:00:00Z',
}

function enquiryPage(items: unknown[], total: number | null) {
  return {
    items,
    ageing_after_hours: 24,
    page: { has_more: false, next_cursor: null, total },
  }
}

function clientPage(items: unknown[]) {
  return { items, page: { has_more: false, next_cursor: null, total: null } }
}

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })
}

function errorResponse() {
  return new Response(
    JSON.stringify({
      error: {
        type: 'internal_error',
        message: 'It broke.',
        action: 'Try again shortly.',
        request_id: 'req_test',
      },
    }),
    { status: 500, headers: { 'content-type': 'application/json' } },
  )
}

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === 'string') return input
  return input instanceof URL ? input.href : input.url
}

/**
 * What a stubbed read should answer with: a body to return, or a 500.
 *
 * ⚠️ A named type rather than `unknown | 'error'` — `unknown` swallows the
 * string literal in a union, so the sentinel would not be visible to a reader
 * or to the type checker.
 */
type ReadStub = 'error' | Record<string, unknown>

/**
 * Stub both reads, routing on the URL rather than on call order.
 *
 * ⚠️ The two requests are issued together and resolve in no guaranteed order, so
 * a `mockResolvedValueOnce` chain would encode an ordering that is not part of
 * the contract.
 */
function stubReads(options: { enquiries?: ReadStub; clients?: ReadStub } = {}) {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = urlOf(input)

      if (url.includes('/enquiries')) {
        if (options.enquiries === 'error') return Promise.resolve(errorResponse())
        return Promise.resolve(jsonResponse(options.enquiries ?? enquiryPage([WAITING_ENQUIRY], 7)))
      }
      if (url.includes('/clients')) {
        if (options.clients === 'error') return Promise.resolve(errorResponse())
        return Promise.resolve(jsonResponse(options.clients ?? clientPage([RECENT_CLIENT])))
      }
      throw new Error(`Unexpected request in test: ${url}`)
    }),
  )
}

function renderDashboard() {
  render(
    <MemoryRouter initialEntries={['/']}>
      <IaProvider ia={ia}>
        <IaRoutes />
      </IaProvider>
    </MemoryRouter>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('the dashboard', () => {
  it('shows what is waiting and who was recently active', async () => {
    stubReads()
    renderDashboard()

    expect(await screen.findByText('Asha Menon')).toBeInTheDocument()
    expect(await screen.findByText('Ravi Kumar')).toBeInTheDocument()
  })

  it('reports the server’s waiting total rather than the row count', async () => {
    // 🔒 One row on screen, seven waiting. Showing "1" would tell the
    // practitioner they are nearly clear when they are not.
    stubReads()
    renderDashboard()
    await screen.findByText('Asha Menon')

    expect(screen.getByText(/7 people are waiting/i)).toBeInTheDocument()
  })

  it('keeps the client panel when the enquiry read fails', async () => {
    stubReads({ enquiries: 'error' })
    renderDashboard()

    expect(await screen.findByText('Ravi Kumar')).toBeInTheDocument()
    expect(await screen.findByRole('alert')).toBeInTheDocument()
  })

  it('keeps the enquiry panel when the client read fails', async () => {
    stubReads({ clients: 'error' })
    renderDashboard()

    expect(await screen.findByText('Asha Menon')).toBeInTheDocument()
    expect(await screen.findByRole('alert')).toBeInTheDocument()
  })

  it('shows no plan-usage figure — nothing reports one', async () => {
    // 🔒 FR-M1-001 asks for a usage indicator; no endpoint provides usage, and
    // those figures exist only inside a 402 envelope. Counting fetched rows
    // would be an entitlement calculation in the browser (NFR-068) that
    // disagrees with the server as soon as a second page exists. If someone adds
    // one, this fails and sends them to build the endpoint first.
    stubReads()
    renderDashboard()
    await screen.findByText('Asha Menon')

    expect(screen.queryByText(/of \d+ active clients/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/plan limit/i)).not.toBeInTheDocument()
  })

  it('asks the server for the oldest-first queue — FR-M2-011', async () => {
    stubReads()
    renderDashboard()
    await screen.findByText('Asha Menon')

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls
    const enquiryUrl = calls.map((call) => urlOf(call[0] as RequestInfo)).find((url) =>
      url.includes('/enquiries'),
    )
    // 🔒 The dedicated endpoint owns the ordering; the dashboard does not sort,
    // and must not reach for the general archive instead.
    expect(enquiryUrl).toContain('/enquiries/needs-response')
  })

  it('says so, calmly, when nothing is waiting', async () => {
    // 🔒 NFR-064, and an empty queue is *success* here — every enquiry has been
    // answered. The wording has to read that way rather than as an absence.
    stubReads({ enquiries: enquiryPage([], 0) })
    renderDashboard()

    expect(await screen.findByText(/nothing waiting/i)).toBeInTheDocument()
  })

  it('offers the first client when there are none — the product’s first run', async () => {
    stubReads({ clients: clientPage([]) })
    renderDashboard()

    expect(await screen.findByText(/no clients yet/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /add a client/i })).toBeInTheDocument()
  })
})
