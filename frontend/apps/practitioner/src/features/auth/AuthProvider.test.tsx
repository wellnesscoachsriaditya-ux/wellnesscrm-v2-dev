/**
 * The session gate — the boundary between the sign-in screen and the workspace.
 *
 * Everything here drives the real `AuthProvider` through the real `App`, with
 * only `fetch` stubbed, because the value of an auth test is in the wiring:
 * that a login stores a token and reveals the workspace, that a bad password
 * surfaces the API's own words, that a stored refresh token restores a session,
 * and that signing out returns to the door.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { setAccessToken } from '@wellnesscrm/api-client'
import { App } from '../../App'

const SESSION = {
  user_id: '11111111-1111-1111-1111-111111111111',
  tenant_id: '22222222-2222-2222-2222-222222222222',
  role: 'owner',
}

const TOKENS = {
  access_token: 'access-abc',
  refresh_token: 'refresh-xyz',
  token_type: 'Bearer',
  expires_in: 900,
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

/**
 * A fetch that models the session's server side: `/me` answers only while a
 * session is live, `/login` opens one, `/logout` closes it.
 */
function stubApi(options: { loggedIn?: boolean; loginFails?: boolean } = {}) {
  const state = { loggedIn: options.loggedIn ?? false }
  const impl = vi.fn((input: string) => {
    const url = input

    if (url.includes('/public/auth/login')) {
      if (options.loginFails) {
        return Promise.resolve(
          json(
            {
              error: {
                type: 'unauthenticated',
                message: 'Those details did not match an account.',
                action: 'Check your email and password and try again.',
                request_id: 'req_test',
              },
            },
            401,
          ),
        )
      }
      state.loggedIn = true
      return Promise.resolve(json(TOKENS))
    }
    if (url.includes('/public/auth/refresh')) {
      state.loggedIn = true
      return Promise.resolve(json(TOKENS))
    }
    if (url.includes('/app/auth/logout')) {
      state.loggedIn = false
      return Promise.resolve(new Response(null, { status: 204 }))
    }
    if (url.includes('/auth/me')) {
      return state.loggedIn
        ? Promise.resolve(json(SESSION))
        : Promise.resolve(
            json(
              { error: { type: 'unauthenticated', message: 'x', action: 'y', request_id: 'r' } },
              401,
            ),
          )
    }
    // Every other workspace read (the dashboard's panels) — a paginated empty
    // page, which is the shape those hooks destructure.
    return Promise.resolve(json({ items: [], page: { total: 0 } }))
  })
  globalThis.fetch = impl as unknown as typeof globalThis.fetch
  return impl
}

describe('the session gate', () => {
  beforeEach(() => {
    localStorage.clear()
    setAccessToken(null)
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows the sign-in screen to a caller with no session', async () => {
    stubApi({ loggedIn: false })
    render(<App />)
    expect(await screen.findByTestId('login-submit-button')).toBeInTheDocument()
  })

  it('signs in with real credentials and reveals the workspace', async () => {
    stubApi({ loggedIn: false })
    const user = userEvent.setup()
    render(<App />)

    await user.type(await screen.findByTestId('login-email-input'), 'coach@example.test')
    await user.type(screen.getByTestId('login-password-input'), 'a-good-password')
    await user.click(screen.getByTestId('login-submit-button'))

    expect(await screen.findByRole('navigation', { name: 'Main' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Dashboard')
  })

  it('shows the API’s own words on a bad password', async () => {
    stubApi({ loginFails: true })
    const user = userEvent.setup()
    render(<App />)

    await user.type(await screen.findByTestId('login-email-input'), 'coach@example.test')
    await user.type(screen.getByTestId('login-password-input'), 'wrong')
    await user.click(screen.getByTestId('login-submit-button'))

    const error = await screen.findByTestId('login-error')
    expect(error).toHaveTextContent('Those details did not match an account.')
    expect(error).toHaveTextContent('Check your email and password and try again.')
    expect(screen.queryByRole('navigation', { name: 'Main' })).toBeNull()
  })

  it('restores a session on boot from a stored refresh token', async () => {
    localStorage.setItem('wc.practitioner.refresh', 'refresh-from-last-time')
    stubApi({ loggedIn: false })
    render(<App />)

    // The stored token is spent for an access token, then `/me` succeeds.
    expect(await screen.findByRole('navigation', { name: 'Main' })).toBeInTheDocument()
  })

  it('signs out back to the sign-in screen', async () => {
    stubApi({ loggedIn: true })
    const user = userEvent.setup()
    render(<App />)

    await user.click(await screen.findByTestId('logout-button'))

    expect(await screen.findByTestId('login-submit-button')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('navigation', { name: 'Main' })).toBeNull())
  })
})
