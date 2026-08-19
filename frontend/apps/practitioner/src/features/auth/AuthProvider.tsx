/**
 * The session gate's state — who is signed in, and the two verbs that change it.
 *
 * 🔒 One provider owns the session so the whole app agrees on it. The gate reads
 * `status` to decide between the sign-in screen and the workspace; the shell
 * reads `session` for the account menu; feature hooks stay unaware of any of it,
 * because the api-client carries the token for them.
 */

import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import * as auth from './authApi'
import type { CurrentSession } from './authApi'

export type AuthStatus = 'loading' | 'authenticated' | 'anonymous'

export interface AuthValue {
  status: AuthStatus
  session: CurrentSession | null
  login: (email: string, password: string) => Promise<void>
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthValue | null>(null)

export function useAuth(): AuthValue {
  const value = useContext(AuthContext)
  if (value === null) throw new Error('useAuth must be used within <AuthProvider>')
  return value
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>('loading')
  const [session, setSession] = useState<CurrentSession | null>(null)

  // The client's silent-renewal seam is live only while the app is mounted.
  useEffect(() => {
    auth.installRefreshHandler()
    return () => auth.uninstallRefreshHandler()
  }, [])

  useEffect(() => {
    let cancelled = false

    async function bootstrap(): Promise<void> {
      // A refresh token from a previous session is spent for an access token
      // before we ask who we are — otherwise the first `/me` would 401.
      if (auth.hasStoredSession()) await auth.refreshSession()
      try {
        const me = await auth.fetchSession()
        if (!cancelled) {
          setSession(me)
          setStatus('authenticated')
        }
      } catch {
        if (!cancelled) {
          setSession(null)
          setStatus('anonymous')
        }
      }
    }

    void bootstrap()
    return () => {
      cancelled = true
    }
  }, [])

  const login = useCallback(async (email: string, password: string) => {
    const me = await auth.login(email, password)
    setSession(me)
    setStatus('authenticated')
  }, [])

  const logout = useCallback(async () => {
    await auth.logout()
    setSession(null)
    setStatus('anonymous')
  }, [])

  return (
    <AuthContext.Provider value={{ status, session, login, logout }}>
      {children}
    </AuthContext.Provider>
  )
}
