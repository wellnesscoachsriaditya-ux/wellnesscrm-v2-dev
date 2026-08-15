/* eslint-disable */
import { useState, useEffect } from 'react'
import { api } from '../api'
import { getCache, setCache, clearAllStores } from '../sync/db'

type TodayResponse = any // For now, we can use any or import the real type if needed

export function useToday() {
  const [data, setData] = useState<TodayResponse | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    let mounted = true

    async function loadData() {
      try {
        if (typeof navigator !== 'undefined' && !navigator.onLine) {
          throw new Error('Offline')
        }

        const response = await api.get('/api/v1/portal/today')
        
        if (mounted) {
          setData(response)
          setError(null)
          
          // Verify if we switched clients, clear stores to prevent leaking data
          const cached = await getCache<any>('portal_today')
          // Note: If the API provided client_id, we'd check it here. 
          // But since Pattern C RLS isolates data, we don't strictly have client_id in the payload.
          // However, if we receive a 401, we clear the cache (done in the catch block).
          
          await setCache('portal_today', response)
        }
      } catch (err: any) {
        if (err?.status === 401 || err?.type === 'unauthenticated') {
          await clearAllStores()
          if (mounted) {
            setError(err)
            setData(null)
          }
          return
        }

        // Fallback to cache for network errors (Offline)
        const cached = await getCache<any>('portal_today')
        if (cached && mounted) {
          setData(cached)
          setError(null)
        } else if (mounted) {
          setError(err)
        }
      } finally {
        if (mounted) {
          setIsLoading(false)
        }
      }
    }

    loadData()

    const handlePlanChanged = () => {
      setIsLoading(true)
      loadData()
    }
    window.addEventListener('wellnesscrm:plan_changed', handlePlanChanged)

    return () => {
      mounted = false
      window.removeEventListener('wellnesscrm:plan_changed', handlePlanChanged)
    }
  }, [])

  return { data, isLoading, error }
}
