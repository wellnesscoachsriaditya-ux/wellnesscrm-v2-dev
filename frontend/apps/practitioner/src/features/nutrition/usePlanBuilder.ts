/**
 * The plan builder's state — one draft, edited through its children.
 *
 * 🔒 **No plan logic in the browser.** Every mutation is a request; after it
 * lands, the version is re-read so the budget, totals, warnings and the next
 * `row_version` all come from the server (ADR-A07, NFR-072). The hook never
 * computes a macro or a remaining figure itself.
 *
 * 🔒 **Concurrency is surfaced, not hidden (EC-M4-07).** A 409 sets `conflict`
 * and re-reads the version, so the practitioner sees the current plan and their
 * next edit uses the fresh token — rather than a silent overwrite.
 */

import { useCallback, useEffect, useState } from 'react'
import { ApiError } from '@wellnesscrm/api-client'
import * as plans from './plansApi'
import type { PlanDetail, PlanVersion } from './plansApi'

export type BuilderStatus = 'loading' | 'ready' | 'error'

type VersionSummary = PlanDetail['versions'][number]

export interface PlanBuilderState {
  status: BuilderStatus
  error: string | null
  clientId: string | null
  title: string
  /** The version currently shown — the draft when one exists, else the latest. */
  version: PlanVersion | null
  /** True when the shown version is an editable draft. */
  editable: boolean
  versions: VersionSummary[]
  saving: boolean
  conflict: boolean
  actionError: string | null
  addFood: (slotId: string, foodId: string, quantity: string, measureUnitId: string) => Promise<void>
  changeQuantity: (itemId: string, quantity: string) => Promise<void>
  removeItem: (itemId: string) => Promise<void>
  setItemLock: (itemId: string, isLocked: boolean) => Promise<void>
  setSlotLock: (slotId: string, isLocked: boolean) => Promise<void>
  removeSlot: (slotId: string) => Promise<void>
  addDay: () => Promise<void>
  addCustomSlot: (dayId: string, label: string) => Promise<void>
  discard: () => Promise<void>
  issue: () => Promise<boolean>
  reload: () => void
}

function messageOf(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? `${cause.message} ${cause.action}` : fallback
}

function isConflict(cause: unknown): boolean {
  return cause instanceof ApiError && (cause.status === 409 || cause.type === 'conflict')
}

export function usePlanBuilder(planId: string): PlanBuilderState {
  const [status, setStatus] = useState<BuilderStatus>('loading')
  const [error, setError] = useState<string | null>(null)
  const [clientId, setClientId] = useState<string | null>(null)
  const [title, setTitle] = useState('')
  const [version, setVersion] = useState<PlanVersion | null>(null)
  const [versions, setVersions] = useState<VersionSummary[]>([])
  const [saving, setSaving] = useState(false)
  const [conflict, setConflict] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [reloadToken, setReloadToken] = useState(0)

  const load = useCallback(async (signal?: AbortSignal) => {
    setStatus('loading')
    try {
      const detail = await plans.fetchPlan(planId, signal)
      setClientId(detail.plan.client_id)
      setTitle(detail.plan.title)
      setVersions(detail.versions)

      // Prefer the open draft; fall back to the plan's current issued version,
      // then to the newest. The builder is read-only when there is no draft.
      const draft = detail.versions.find((entry) => entry?.state === 'draft')
      const target =
        draft?.id ?? detail.plan.current_version_id ?? detail.versions[0]?.id ?? null
      if (target === null) {
        setStatus('error')
        setError('This plan has no versions to show.')
        return
      }
      const full = await plans.fetchVersion(target, signal)
      setVersion(full)
      setStatus('ready')
      setError(null)
    } catch (cause) {
      if (signal?.aborted) return
      setStatus('error')
      setError(messageOf(cause, 'This plan could not be loaded.'))
    }
  }, [planId])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load, reloadToken])

  const refreshVersion = useCallback(async (versionId: string) => {
    const full = await plans.fetchVersion(versionId)
    setVersion(full)
  }, [])

  /** Run a mutation that keeps the same version, then re-read that version. */
  const mutate = useCallback(
    async (run: (rowVersion: number) => Promise<unknown>) => {
      if (version === null) return
      setSaving(true)
      setConflict(false)
      setActionError(null)
      try {
        await run(version.row_version)
        await refreshVersion(version.id)
      } catch (cause) {
        if (isConflict(cause)) {
          setConflict(true)
          await refreshVersion(version.id)
        } else {
          setActionError(messageOf(cause, 'That change could not be saved.'))
        }
      } finally {
        setSaving(false)
      }
    },
    [version, refreshVersion],
  )

  const addFood = useCallback(
    (slotId: string, foodId: string, quantity: string, measureUnitId: string) =>
      mutate((rv) =>
        plans.addItem(rv, {
          slot_id: slotId,
          food_id: foodId,
          quantity,
          measure_unit_id: measureUnitId,
        }),
      ),
    [mutate],
  )

  const changeQuantity = useCallback(
    (itemId: string, quantity: string) =>
      mutate((rv) => plans.updateItem(itemId, rv, { quantity })),
    [mutate],
  )

  const removeItem = useCallback(
    (itemId: string) => mutate((rv) => plans.removeItem(itemId, rv)),
    [mutate],
  )

  const setItemLock = useCallback(
    (itemId: string, isLocked: boolean) =>
      mutate((rv) => plans.updateItem(itemId, rv, { is_locked: isLocked })),
    [mutate],
  )

  const setSlotLock = useCallback(
    (slotId: string, isLocked: boolean) =>
      mutate((rv) => plans.setSlotLock(slotId, rv, isLocked)),
    [mutate],
  )

  const removeSlot = useCallback(
    (slotId: string) => mutate((rv) => plans.removeSlot(slotId, rv)),
    [mutate],
  )

  const addDay = useCallback(
    () => mutate((rv) => plans.addDay(version?.id ?? '', rv, {})),
    [mutate, version],
  )

  const addCustomSlot = useCallback(
    (dayId: string, label: string) =>
      mutate((rv) =>
        plans.addSlot(version?.id ?? '', rv, {
          day_id: dayId,
          slot_type: 'custom',
          custom_label: label,
        }),
      ),
    [mutate, version],
  )

  const discard = useCallback(async () => {
    if (version === null) return
    setSaving(true)
    setActionError(null)
    try {
      await plans.discardVersion(version.id, version.row_version)
      setReloadToken((token) => token + 1)
    } catch (cause) {
      if (isConflict(cause)) setConflict(true)
      else setActionError(messageOf(cause, 'The draft could not be discarded.'))
    } finally {
      setSaving(false)
    }
  }, [version])

  const issue = useCallback(async (): Promise<boolean> => {
    if (version === null) return false
    setSaving(true)
    setActionError(null)
    try {
      await plans.issueVersion(version.id, version.row_version)
      setReloadToken((token) => token + 1)
      return true
    } catch (cause) {
      if (isConflict(cause)) setConflict(true)
      else setActionError(messageOf(cause, 'This plan could not be issued.'))
      return false
    } finally {
      setSaving(false)
    }
  }, [version])

  return {
    status,
    error,
    clientId,
    title,
    version,
    editable: version?.state === 'draft',
    versions,
    saving,
    conflict,
    actionError,
    addFood,
    changeQuantity,
    removeItem,
    setItemLock,
    setSlotLock,
    removeSlot,
    addDay,
    addCustomSlot,
    discard,
    issue,
    reload: useCallback(() => setReloadToken((token) => token + 1), []),
  }
}
