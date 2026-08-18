/**
 * A client's plans — the list behind the Client 360 panel, and the create action.
 *
 * 🔒 Arch §4.4 — the panel renders these; it does not fetch. Creating a plan
 * can be refused at the plan's client ceiling (402), which surfaces here as the
 * API's own message rather than a generic failure.
 */

import { useCallback, useEffect, useState } from 'react'
import { ApiError } from '@wellnesscrm/api-client'
import * as plans from './plansApi'
import type { CreatePlanInput, PlanSummary } from './plansApi'

export interface ClientPlansState {
  plans: PlanSummary[]
  loading: boolean
  error: string | null
  creating: boolean
  createError: string | null
  create: (input: CreatePlanInput) => Promise<{ planId: string } | null>
  reload: () => void
}

function messageOf(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? `${cause.message} ${cause.action}` : fallback
}

export function useClientPlans(clientId: string): ClientPlansState {
  const [items, setItems] = useState<PlanSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [reloadToken, setReloadToken] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    plans
      .listClientPlans(clientId, controller.signal)
      .then((value) => {
        setItems(value)
        setError(null)
        setLoading(false)
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return
        setError(messageOf(cause, 'This client’s plans could not be loaded.'))
        setLoading(false)
      })
    return () => controller.abort()
  }, [clientId, reloadToken])

  const create = useCallback(
    async (input: CreatePlanInput) => {
      setCreating(true)
      setCreateError(null)
      try {
        const created = await plans.createPlan(clientId, input)
        setReloadToken((token) => token + 1)
        return { planId: created.plan.id }
      } catch (cause) {
        setCreateError(messageOf(cause, 'The plan could not be created.'))
        return null
      } finally {
        setCreating(false)
      }
    },
    [clientId],
  )

  return {
    plans: items,
    loading,
    error,
    creating,
    createError,
    create,
    reload: useCallback(() => setReloadToken((token) => token + 1), []),
  }
}
