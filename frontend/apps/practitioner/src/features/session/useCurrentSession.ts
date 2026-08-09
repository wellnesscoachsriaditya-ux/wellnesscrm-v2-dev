/**
 * Who is signed in — the answer the collaboration UI branches on.
 *
 * 🔒 Two rules need it, and neither can be expressed without it:
 * FR-M3-020 (only a note's author may edit it) and FR-M0-017 (only the owner
 * manages access). Both are enforced server-side regardless; this is what lets
 * the UI *show the right controls* instead of teaching the rules by refusal.
 *
 * ⚠️ Fetched once per mount rather than cached globally. A shared cache is the
 * right answer once several screens need it — and inventing one now, for one
 * consumer, would be a cache with no eviction policy and no invalidation story.
 */

import { useEffect, useState } from 'react'
import { createApiClient } from '@wellnesscrm/api-client'
import type { components } from '@wellnesscrm/api-client'

export type CurrentSession = components['schemas']['CurrentSessionResponse']

const api = createApiClient()

export interface SessionState {
  session: CurrentSession | null
  loading: boolean
}

export function useCurrentSession(): SessionState {
  const [session, setSession] = useState<CurrentSession | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const controller = new AbortController()

    api
      .request('get', '/api/v1/app/auth/me', { signal: controller.signal })
      .then((value) => {
        setSession(value)
        setLoading(false)
      })
      .catch(() => {
        // ⚠️ Swallowed deliberately. A failure here means the UI cannot tell
        // whose notes are whose, so it falls back to showing no edit controls —
        // which is the safe direction, and matches what the API would do anyway.
        if (controller.signal.aborted) return
        setSession(null)
        setLoading(false)
      })

    return () => controller.abort()
  }, [])

  return { session, loading }
}
