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

/**
 * Stub `fetch` by route rather than by call order.
 *
 * ⚠️ The screen loads four things on mount — the client, its notes, its tags and
 * the tag vocabulary — and the order they resolve in is not part of the
 * contract. A `mockResolvedValueOnce` chain encodes that order as if it were,
 * so adding a panel breaks every test for reasons unrelated to what they assert.
 * Routing on the URL means a test says what it means: "the stage endpoint
 * returns this".
 */
type Route = (url: string, init: RequestInit) => Response | undefined

/**
 * The request URL as a string.
 *
 * ⚠️ `String(input)` looks equivalent and is not: `RequestInfo` includes
 * `Request`, which has no meaningful `toString` and stringifies to
 * `[object Object]` — so a route matcher would silently stop matching.
 */
function urlOf(input: RequestInfo | URL): string {
  if (typeof input === 'string') return input
  return input instanceof URL ? input.href : input.url
}

function stubRoutes(...routes: Route[]) {
  const collaborationDefaults: Route = (url) => {
    if (url.includes('/notes')) return jsonResponse([])
    // ⚠️ Before `/timeline`, because `/timeline/filters` contains both and the
    // filter list is an array while the timeline is an envelope.
    if (url.includes('/timeline/filters')) return jsonResponse([])
    if (url.includes('/timeline')) return jsonResponse({ items: [], page: { has_more: false } })
    if (url.includes('/tags')) return jsonResponse([])
    if (url.includes('/access')) return jsonResponse([])
    if (url.includes('/assessments')) return jsonResponse([])
    if (url.includes('/measurements')) return jsonResponse([])
    if (url.includes('/consultation-notes')) return jsonResponse([])
    if (url.includes('/documents')) return jsonResponse([])
    if (url.includes('/auth/me')) return jsonResponse(SESSION)
    return undefined
  }

  return vi
    .spyOn(globalThis, 'fetch')
    .mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = urlOf(input)
      for (const route of [...routes, collaborationDefaults]) {
        const response = route(url, init ?? {})
        if (response) return Promise.resolve(response)
      }
      throw new Error(`unstubbed request: ${init?.method ?? 'GET'} ${url}`)
    })
}

const SESSION = { user_id: 'user-1', tenant_id: 'tenant-1', role: 'practitioner' }

/** The client GET, which every test needs and none is about. */
function clientRoute(client: unknown): Route {
  return (url, init) => {
    const isGet = (init.method ?? 'GET').toUpperCase() === 'GET'
    return isGet && url.endsWith(`/clients/${CLIENT_ID}`) ? jsonResponse(client) : undefined
  }
}

/** A POST to one of the lifecycle actions. */
function actionRoute(suffix: string, response: Response): Route {
  return (url, init) =>
    url.endsWith(suffix) && (init.method ?? '').toUpperCase() === 'POST' ? response : undefined
}

/**
 * Who is signed in.
 *
 * 🔒 The role decides whether the access controls render at all (FR-M0-017), so
 * a test about them has to state it rather than inherit the default.
 */
function sessionRoute(role: 'owner' | 'practitioner'): Route {
  return (url) => (url.includes('/auth/me') ? jsonResponse({ ...SESSION, role }) : undefined)
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('client detail', () => {
  it('shows the client once loaded', async () => {
    stubRoutes(clientRoute(CLIENT))
    renderScreen()

    expect(await screen.findByRole('heading', { name: 'Asha Menon', level: 1 })).toBeInTheDocument()
    expect(screen.getByText('New enquiry')).toBeInTheDocument()
  })

  it('states the WhatsApp limitation when there is no mobile', async () => {
    // 🔒 EC-M1-08 — a client with only an email is legitimate and loses
    // WhatsApp delivery. The system must say so rather than leave a blank.
    stubRoutes(clientRoute({ ...CLIENT, mobile: null, email: 'asha@example.test' }))
    renderScreen()

    expect(await screen.findByText(/whatsapp delivery is unavailable/i)).toBeInTheDocument()
  })

  it('explains a plan-limit refusal without a second request', async () => {
    // 🔒 FR-M1-002 / FR-M0-045 — the 402 carries the limit, the usage, the plan
    // and the upgrade path, and all four are rendered from that one response.
    stubRoutes(
      clientRoute(CLIENT),
      actionRoute(
        '/stage',
        errorResponse(402, {
          type: 'entitlement_exceeded',
          message: "You've reached 30 active clients on the Starter plan.",
          action:
            'You can set a client to inactive if they have finished their programme, or upgrade to Growth.',
          request_id: 'req_test',
          details: {
            resource: 'active_clients',
            limit: 30,
            used: 30,
            plan_code: 'starter',
            upgrade_to: 'growth',
          },
        }),
      ),
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
    stubRoutes(
      clientRoute(CLIENT),
      actionRoute(
        '/stage',
        jsonResponse({ ...CLIENT, stage: 'active', activated_at: '2026-08-08T09:00:00Z' }),
      ),
    )

    renderScreen()
    await screen.findByRole('heading', { name: 'Asha Menon', level: 1 })

    await userEvent.selectOptions(screen.getByLabelText('Move to stage'), 'active')
    await userEvent.click(screen.getByRole('button', { name: 'Update stage' }))

    expect(await screen.findByText('Active client')).toBeInTheDocument()
  })

  it('posts to the stage endpoint with the chosen stage', async () => {
    const fetchSpy = stubRoutes(
      clientRoute(CLIENT),
      actionRoute('/stage', jsonResponse({ ...CLIENT, stage: 'contacted' })),
    )

    renderScreen()
    await screen.findByRole('heading', { name: 'Asha Menon', level: 1 })

    await userEvent.selectOptions(screen.getByLabelText('Move to stage'), 'contacted')
    await userEvent.click(screen.getByRole('button', { name: 'Update stage' }))

    // ⚠️ Found by URL, not by call index. The screen loads four things on mount
    // and their order is not part of the contract, so an index here would break
    // the next time a panel is added.
    const call = await waitFor(() => {
      const found = fetchSpy.mock.calls.find(
        ([url, init]) => urlOf(url).endsWith('/stage') && init?.method === 'POST',
      )
      if (!found) throw new Error('no POST to /stage yet')
      return found as [string, RequestInit]
    })

    // 🔒 The path parameter is substituted, not left as a template.
    expect(call[0]).toContain(`/api/v1/app/clients/${CLIENT_ID}/stage`)
    // The client always serialises the body itself, so this is a string. Checked
    // rather than coerced: `String()` on an object would silently compare
    // against '[object Object]' and the assertion would pass for the wrong body.
    expect(typeof call[1].body).toBe('string')
    expect(JSON.parse(call[1].body as string)).toEqual({ to_stage: 'contacted' })
  })

  it('restores an archived client through the restore endpoint', async () => {
    const fetchSpy = stubRoutes(
      clientRoute({ ...CLIENT, stage: 'paused', archived_at: '2026-08-05T10:00:00Z' }),
      actionRoute('/restore', jsonResponse({ ...CLIENT, stage: 'paused' })),
    )

    renderScreen()
    await screen.findByRole('heading', { name: 'Asha Menon', level: 1 })

    await userEvent.click(screen.getByRole('button', { name: 'Restore client' }))

    await waitFor(() =>
      expect(
        fetchSpy.mock.calls.some(([url]) => urlOf(url).endsWith(`/clients/${CLIENT_ID}/restore`)),
      ).toBe(true),
    )
  })

  it('offers no access controls to a practitioner, and does to an owner', async () => {
    // 🔒 FR-M0-017 / EC-M0-04 — `client.manage_access` is owner-only while
    // `client.read_access` is not. The session's role is what decides, so this
    // is asserted through the screen rather than the panel: the wiring from
    // `/auth/me` to `canManage` is the part that can silently invert.
    stubRoutes(clientRoute(CLIENT))
    renderScreen()
    await screen.findByRole('heading', { name: 'Asha Menon', level: 1 })

    expect(screen.getByText(/only the account owner can change who has access/i)).toBeInTheDocument()
    expect(screen.queryByLabelText('Share with a colleague')).not.toBeInTheDocument()
  })

  it('lets the owner share the client with a colleague', async () => {
    // ⚠️ `sessionRoute` overrides the default practitioner session. The owner
    // role is what puts the controls on screen, so it has to come first.
    const fetchSpy = stubRoutes(
      sessionRoute('owner'),
      clientRoute(CLIENT),
      (url, init) =>
        url.endsWith('/access') && (init.method ?? 'GET').toUpperCase() === 'POST'
          ? jsonResponse({
              user_id: 'user-new',
              granted_by_user_id: 'user-1',
              granted_at: '2026-08-09T10:00:00Z',
              revoked_at: null,
              is_live: true,
            })
          : undefined,
    )

    renderScreen()
    await screen.findByRole('heading', { name: 'Asha Menon', level: 1 })

    await userEvent.type(await screen.findByLabelText('Share with a colleague'), 'user-new')
    await userEvent.click(screen.getByRole('button', { name: 'Give access' }))

    const call = await waitFor(() => {
      const found = fetchSpy.mock.calls.find(
        ([url, init]) => urlOf(url).endsWith('/access') && init?.method === 'POST',
      )
      if (!found) throw new Error('no POST to /access yet')
      return found as [string, RequestInit]
    })

    expect(JSON.parse(call[1].body as string)).toEqual({ user_id: 'user-new' })
  })

  it('renders the timeline the server returned', async () => {
    // 🔒 FR-M1-018 through the real transport: the envelope's `items`/`page`
    // shape is decoded, projected onto the panel's props, and rendered. A
    // mocked hook would assert our own fake matches our own expectations.
    stubRoutes(clientRoute(CLIENT), (url) =>
      url.includes('/timeline') && !url.includes('/filters')
        ? jsonResponse({
            items: [
              {
                id: 'evt-1',
                event_type: 'stage_changed',
                occurred_at: '2026-08-08T09:00:00Z',
                summary: 'New enquiry → Contacted',
                source_module: 'clients',
                source_record_id: null,
                actor_type: 'practitioner',
                actor_id: 'user-1',
              },
            ],
            page: { next_cursor: null, has_more: false },
          })
        : undefined,
    )

    renderScreen()
    await screen.findByRole('heading', { name: 'Asha Menon', level: 1 })

    expect(await screen.findByText('New enquiry → Contacted')).toBeInTheDocument()
  })

  it('sends each chosen timeline filter as its own query parameter', async () => {
    // 🔒 API §6.2 — repeated keys, not a comma-joined string. FastAPI decodes
    // repeated keys into a list; a joined value is rejected as an invalid enum
    // member, so this failing would 422 on a filter picked from our own UI.
    const fetchSpy = stubRoutes(
      clientRoute(CLIENT),
      (url) =>
        url.includes('/timeline/filters')
          ? jsonResponse([{ event_type: 'note_added', label: 'Notes' }])
          : undefined,
      (url) =>
        url.includes('/timeline')
          ? jsonResponse({ items: [], page: { next_cursor: null, has_more: false } })
          : undefined,
    )

    renderScreen()
    await screen.findByRole('heading', { name: 'Asha Menon', level: 1 })

    await userEvent.click(await screen.findByRole('button', { name: 'Notes' }))

    const filtered = await waitFor(() => {
      const found = fetchSpy.mock.calls
        .map(([url]) => urlOf(url))
        .find((url) => url.includes('event_type=note_added'))
      if (!found) throw new Error('no filtered timeline request yet')
      return found
    })

    expect(new URL(filtered).searchParams.getAll('event_type')).toEqual(['note_added'])
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
