/**
 * Practice-level counts for the dashboard KPIs.
 *
 * 🔒 REAL DATA ONLY. Every figure is a server `total` from the existing client
 * discovery endpoint (`include_total`), scoped and RLS-enforced like any other
 * read. Nothing is computed from a fetched page, and no metric without an
 * endpoint (appointments, adherence, revenue) is invented — those are omitted.
 *
 * 🔒 Arch §4.4 — this is a feature hook, the layer allowed to call the API.
 */
import { useCallback, useEffect, useState } from 'react'
import { fetchClients } from '../clients/discoveryApi'

export interface PracticeCounts {
  total: number | null
  active: number | null
  leads: number | null
  contacted: number | null
  consultation: number | null
  paused: number | null
  loading: boolean
}

async function countOf(stages: string[] | undefined, signal: AbortSignal): Promise<number | null> {
  const page = await fetchClients(
    { ...(stages ? { stages } : {}), archived: 'exclude', includeTotal: true, limit: 1 },
    signal,
  )
  return page.page.total ?? null
}

export function usePracticeCounts(): PracticeCounts {
  const [state, setState] = useState<Omit<PracticeCounts, 'loading'>>({
    total: null,
    active: null,
    leads: null,
    contacted: null,
    consultation: null,
    paused: null,
  })
  const [loading, setLoading] = useState(true)

  const load = useCallback((signal: AbortSignal) => {
    setLoading(true)
    void Promise.allSettled([
      countOf(undefined, signal),
      countOf(['active'], signal),
      countOf(['lead'], signal),
      countOf(['contacted'], signal),
      countOf(['consultation_scheduled'], signal),
      countOf(['paused'], signal),
    ]).then((results) => {
      if (signal.aborted) return
      const v = (i: number): number | null => {
        const r = results[i]
        return r && r.status === 'fulfilled' ? r.value : null
      }
      setState({
        total: v(0),
        active: v(1),
        leads: v(2),
        contacted: v(3),
        consultation: v(4),
        paused: v(5),
      })
      setLoading(false)
    })
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal)
    return () => controller.abort()
  }, [load])

  return { ...state, loading }
}
