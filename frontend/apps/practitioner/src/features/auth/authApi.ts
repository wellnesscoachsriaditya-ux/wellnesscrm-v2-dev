/**
 * The practitioner session — sign in, renew, sign out.
 *
 * 🔒 **Arch §4.4 / NFR-068** — the one layer allowed to talk to the API. The
 * gate and the login screen render what this returns; they do not call `fetch`.
 *
 * 🔒 **ADR-A02, adapted honestly for the web.** The access token lives in the
 * api-client's memory (`setAccessToken`). The refresh token *should* be an
 * HttpOnly cookie the backend sets — but it does not set one yet; `/auth/login`
 * returns the refresh token in the body, mobile-first. Until that cookie
 * exists, a web session that must survive a reload has to keep the refresh
 * token somewhere, and `localStorage` is the least-bad option available to a
 * SPA. Swapping to the cookie later is a change to this file alone, not to the
 * contract.
 */

import {
  configureRefreshHandler,
  createApiClient,
  setAccessToken,
} from '@wellnesscrm/api-client'
import type { components } from '@wellnesscrm/api-client'

export type Tokens = components['schemas']['TokenResponse']
export type CurrentSession = components['schemas']['CurrentSessionResponse']

const api = createApiClient()

const REFRESH_KEY = 'wc.practitioner.refresh'

function storedRefreshToken(): string | null {
  try {
    return globalThis.localStorage?.getItem(REFRESH_KEY) ?? null
  } catch {
    return null
  }
}

function persistRefreshToken(token: string | null): void {
  try {
    if (token === null) globalThis.localStorage?.removeItem(REFRESH_KEY)
    else globalThis.localStorage?.setItem(REFRESH_KEY, token)
  } catch {
    // Storage is unavailable (private mode, disabled). The session then lives
    // for as long as the tab does, which is a degradation, not a failure.
  }
}

function adopt(tokens: Tokens): void {
  setAccessToken(tokens.access_token)
  persistRefreshToken(tokens.refresh_token)
}

/** Whether a prior session left a refresh token to try on boot. */
export function hasStoredSession(): boolean {
  return storedRefreshToken() !== null
}

/** Sign in and return who the caller now is. Throws `ApiError` on bad credentials. */
export async function login(email: string, password: string): Promise<CurrentSession> {
  const tokens = await api.request('post', '/api/v1/public/auth/login', {
    body: { email, password },
  })
  adopt(tokens)
  return api.request('get', '/api/v1/app/auth/me')
}

/**
 * Rotate the session and return the fresh access token, or `null` when there is
 * nothing to rotate. Registered as the api-client's silent-renewal handler.
 */
export async function refreshSession(): Promise<string | null> {
  const token = storedRefreshToken()
  if (token === null) return null
  try {
    const tokens = await api.request('post', '/api/v1/public/auth/refresh', {
      body: { refresh_token: token },
    })
    adopt(tokens)
    return tokens.access_token
  } catch {
    // The refresh token is spent or was revoked (DDR-05 reuse detection). Clear
    // it so the next boot goes straight to sign-in rather than retrying it.
    clearSession()
    return null
  }
}

/** Ask the API who the current token belongs to. Throws when unauthenticated. */
export async function fetchSession(signal?: AbortSignal): Promise<CurrentSession> {
  return api.request('get', '/api/v1/app/auth/me', { ...(signal ? { signal } : {}) })
}

/** Revoke the session server-side, then forget it locally regardless of outcome. */
export async function logout(): Promise<void> {
  try {
    await api.request('post', '/api/v1/app/auth/logout')
  } finally {
    clearSession()
  }
}

/** Forget the session locally — the access token and the stored refresh token. */
export function clearSession(): void {
  setAccessToken(null)
  persistRefreshToken(null)
}

/** Install / remove the client's 401 renewal seam. Called by the AuthProvider. */
export function installRefreshHandler(): void {
  configureRefreshHandler(refreshSession)
}

export function uninstallRefreshHandler(): void {
  configureRefreshHandler(null)
}
