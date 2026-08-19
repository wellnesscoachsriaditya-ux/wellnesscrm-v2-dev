import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { navItemsFor, validateIa } from '@wellnesscrm/ia'
import { setAccessToken } from '@wellnesscrm/api-client'
import { App } from './App'
import { ia } from './ia/manifest'

/**
 * 🔒 S0 Definition of Done — "all three apps deploy to staging and render."
 *
 * A build succeeding proves the code compiles, not that the app works: a shell
 * that throws on its first render builds perfectly. These assert the app mounts
 * and that what it renders comes from the IA.
 *
 * ⚠️ The app now gates on a session (S1). These render the *authenticated*
 * workspace by stubbing `/auth/me` — the gate's own behaviour is asserted
 * separately in `features/auth/AuthProvider.test.tsx`.
 */

const SESSION = {
  user_id: '11111111-1111-1111-1111-111111111111',
  tenant_id: '22222222-2222-2222-2222-222222222222',
  role: 'owner',
}

function stubAuthenticated(): void {
  const impl = vi.fn((input: string) => {
    const url = input
    const body = url.includes('/auth/me') ? SESSION : { items: [], page: { total: 0 } }
    return Promise.resolve(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )
  })
  globalThis.fetch = impl as unknown as typeof globalThis.fetch
}

describe('practitioner app', () => {
  beforeEach(() => {
    localStorage.clear()
    setAccessToken(null)
    stubAuthenticated()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('declares a valid IA', () => {
    // `defineIa` already threw at import time if not — this states the
    // requirement where a reader looks for it, and fails with the full list.
    expect(validateIa(ia)).toEqual([])
  })

  it('renders the shell and the landing screen once signed in', async () => {
    render(<App />)
    expect(await screen.findByRole('heading', { level: 1 })).toHaveTextContent('Dashboard')
    expect(screen.getByRole('navigation', { name: 'Main' })).toBeInTheDocument()
  })

  it('builds its navigation from the manifest, in declared order', async () => {
    render(<App />)
    await screen.findByRole('navigation', { name: 'Main' })
    const links = screen.getByRole('navigation', { name: 'Main' }).querySelectorAll('a')

    expect(Array.from(links).map((link) => link.textContent?.trim())).toEqual(
      navItemsFor(ia).map((item) => item.label),
    )
  })

  it('marks the current section as the active nav item', async () => {
    render(<App />)
    // 🔒 NFR-057 in one assertion: the highlighted item and the rendered screen
    // are the same declaration.
    const current = await screen.findByRole('link', { current: 'page' })
    expect(current).toHaveTextContent('Dashboard')
  })

  it('offers a way to sign out', async () => {
    render(<App />)
    expect(await screen.findByTestId('logout-button')).toBeInTheDocument()
  })

  it('offers every declared route a way to be reached from navigation or a parent', () => {
    // A route that is neither navigable nor a child of something navigable is
    // unreachable except by typing its URL — almost always a mistake.
    const orphans = ia.routes.filter(
      (route) => route.nav === undefined && route.parent === undefined,
    )
    expect(orphans).toEqual([])
  })

  it('shows the sign-in screen when there is no session', async () => {
    globalThis.fetch = vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify({ error: { type: 'unauthenticated', message: 'x', action: 'y', request_id: 'r' } }), {
          status: 401,
          headers: { 'content-type': 'application/json' },
        }),
      ),
    ) as unknown as typeof globalThis.fetch

    render(<App />)
    expect(await screen.findByTestId('login-submit-button')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('navigation', { name: 'Main' })).toBeNull())
  })
})
