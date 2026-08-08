/**
 * The client detail screen against a stubbed transport — S2 Slice B.
 *
 * 🔒 **The 402 is the test that matters here.** FR-M1-002 requires the refusal
 * to name the limit and the upgrade path, and FR-M0-045 requires the UI to
 * explain it without a second request. Both are properties of what reaches the
 * screen, so they are asserted through a real render rather than a unit test of
 * the hook.
 *
 * ⚠️ `fetch` is stubbed rather than the api-client module. The envelope decoding
 * in `@wellnesscrm/api-client` is part of what these tests exercise — a mocked
 * client would assert that our own fake produces the shape our own code expects.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { IaProvider, IaRoutes, defineIa } from '@wellnesscrm/ia'
import { ClientDetail } from './ClientDetail'

const CLIENT_ID = '11111111-2222-3333-4444-555555555555'

const CLIENT = {
  id: CLIENT_ID,
  full_name: 'Asha Menon',
  stage: 'lead',
  mobile: '+919876543210',
  email: null,
  date_of_birth: null,
  sex: null,
  city: 'Kochi',
  preferred_language: 'en',
  source: null,
  source_detail: null,
  owner_user_id: '99999999-9999-9999-9999-999999999999',
  dietary_class: null,
  is_minor: null,
  activated_at: null,
  archived_at: null,
  created_at: '2026-08-01T10:00:00Z',
  updated_at: '2026-08-01T10:00:00Z',
}

/** 🔒 The envelope API §5.1 defines — the shape the UI branches on. */
function errorResponse(status: number, body: Record<string, unknown>) {
  return new Response(JSON.stringify({ error: body }), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })
}

/** A minimal IA so the screen's breadcrumbs and params resolve as in the app. */
const ia = defineIa({
  appId: 'test',
  routes: [
    { id: 'clients', path: '/clients', label: 'Clients', nav: { order: 1 }, view: () => null },
    { id: 'client-detail', path: '/clients/:clientId', label: 'Client', parent: 'clients', view: ClientDetail },
  ],
})

function renderScreen() {
  render(
    <MemoryRouter initialEntries={[`/clients/${CLIENT_ID}`]}>
      <IaProvider ia={ia}>
        <IaRoutes />
      </IaProvider>
    </MemoryRouter>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('client detail', () => {
  it('shows the client once loaded', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(CLIENT))
    renderScreen()

    expect(await screen.findByRole('heading', { name: 'Asha Menon', level: 1 })).toBeInTheDocument()
    expect(screen.getByText('New enquiry')).toBeInTheDocument()
  })

  it('states the WhatsApp limitation when there is no mobile', async () => {
    // 🔒 EC-M1-08 — a client with only an email is legitimate and loses
    // WhatsApp delivery. The system must say so rather than leave a blank.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ ...CLIENT, mobile: null, email: 'asha@example.test' }),
    )
    renderScreen()

    expect(await screen.findByText(/whatsapp delivery is unavailable/i)).toBeInTheDocument()
  })

  it('explains a plan-limit refusal without a second request', async () => {
    // 🔒 FR-M1-002 / FR-M0-045 — the 402 carries the limit, the usage, the plan
    // and the upgrade path, and all four are rendered from that one response.
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    fetchSpy.mockResolvedValueOnce(jsonResponse(CLIENT))
    fetchSpy.mockResolvedValueOnce(
      errorResponse(402, {
        type: 'entitlement_exceeded',
        message: "You've reached 30 active clients on the Starter plan.",
        action: 'You can set a client to inactive if they have finished their programme, or upgrade to Growth.',
        request_id: 'req_test',
        details: {
          resource: 'active_clients',
          limit: 30,
          used: 30,
          plan_code: 'starter',
          upgrade_to: 'growth',
        },
      }),
    )

    renderScreen()
    await screen.findByRole('heading', { name: 'Asha Menon', level: 1 })

    await userEvent.selectOptions(screen.getByLabelText('Move to stage'), 'active')
    await userEvent.click(screen.getByRole('button', { name: 'Update stage' }))

    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent("You've reached 30 active clients on the Starter plan.")
    expect(notice).toHaveTextContent('upgrade to Growth')
    expect(screen.getByTestId('entitlement-usage')).toHaveTextContent(
      'Using 30 of 30 active clients on the starter plan.',
    )
    expect(screen.getByTestId('entitlement-upgrade')).toHaveTextContent('growth')

    // 🔒 The client is unchanged — a refused transition changes nothing.
    expect(screen.getByText('New enquiry')).toBeInTheDocument()
  })

  it('renders the server’s new state after a successful transition', async () => {
    // 🔒 Principle 3 — the client renders, never derives. The stage badge comes
    // from the response, not from what the dropdown was set to, so a server
    // that decided differently is what the practitioner sees.
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    fetchSpy.mockResolvedValueOnce(jsonResponse(CLIENT))
    fetchSpy.mockResolvedValueOnce(
      jsonResponse({ ...CLIENT, stage: 'active', activated_at: '2026-08-08T09:00:00Z' }),
    )

    renderScreen()
    await screen.findByRole('heading', { name: 'Asha Menon', level: 1 })

    await userEvent.selectOptions(screen.getByLabelText('Move to stage'), 'active')
    await userEvent.click(screen.getByRole('button', { name: 'Update stage' }))

    expect(await screen.findByText('Active client')).toBeInTheDocument()
  })

  it('posts to the stage endpoint with the chosen stage', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    fetchSpy.mockResolvedValueOnce(jsonResponse(CLIENT))
    fetchSpy.mockResolvedValueOnce(jsonResponse({ ...CLIENT, stage: 'contacted' }))

    renderScreen()
    await screen.findByRole('heading', { name: 'Asha Menon', level: 1 })

    await userEvent.selectOptions(screen.getByLabelText('Move to stage'), 'contacted')
    await userEvent.click(screen.getByRole('button', { name: 'Update stage' }))

    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(2))
    const [url, init] = fetchSpy.mock.calls[1] as [string, RequestInit]
    // 🔒 The path parameter is substituted, not left as a template.
    expect(url).toContain(`/api/v1/app/clients/${CLIENT_ID}/stage`)
    expect(init.method).toBe('POST')
    // The client always serialises the body itself, so this is a string. Checked
    // rather than coerced: `String()` on an object would silently compare
    // against '[object Object]' and the assertion would pass for the wrong body.
    expect(typeof init.body).toBe('string')
    expect(JSON.parse(init.body as string)).toEqual({ to_stage: 'contacted' })
  })

  it('restores an archived client through the restore endpoint', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    fetchSpy.mockResolvedValueOnce(
      jsonResponse({ ...CLIENT, stage: 'paused', archived_at: '2026-08-05T10:00:00Z' }),
    )
    fetchSpy.mockResolvedValueOnce(jsonResponse({ ...CLIENT, stage: 'paused' }))

    renderScreen()
    await screen.findByRole('heading', { name: 'Asha Menon', level: 1 })

    await userEvent.click(screen.getByRole('button', { name: 'Restore client' }))

    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(2))
    expect(fetchSpy.mock.calls[1]?.[0]).toContain(`/api/v1/app/clients/${CLIENT_ID}/restore`)
  })

  it('shows the request id on an unexpected failure', async () => {
    // 🔒 NFR-033 — an identifier only, and the thing that makes a support
    // conversation tractable.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      errorResponse(500, {
        type: 'internal_error',
        message: 'Something went wrong on our side.',
        action: 'Try again in a moment.',
        request_id: 'req_abc123',
      }),
    )

    renderScreen()

    expect(await screen.findByText(/req_abc123/)).toBeInTheDocument()
  })
})
