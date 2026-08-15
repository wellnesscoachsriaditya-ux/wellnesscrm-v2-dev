import { api } from '../api'
import { peekQueue, removeOperation, getCache, clearAllStores } from './db'

let isSyncing = false

/**
 * Flushes the IndexedDB queue to the backend.
 * 
 * Guarantees:
 * 1. Only one sync runs at a time.
 * 2. Partial batch failures are handled: successful/duplicate operations are removed,
 *    rejected operations are left in the queue.
 * 3. A 401 response halts sync and wipes the IDB (session expired).
 */
export async function syncQueue(): Promise<{ planChanged: boolean }> {
  if (isSyncing) return { planChanged: false }
  
  // We check navigator.onLine just in case, though the caller usually checks it.
  if (typeof navigator !== 'undefined' && !navigator.onLine) {
    return { planChanged: false }
  }

  isSyncing = true
  let planChanged = false

  try {
    const queue = await peekQueue()
    if (queue.length === 0) {
      // Even if empty, we can ping sync to check plan hash.
      // But if we have no known hash, maybe we don't need to.
      // We'll proceed so we can check if plan_changed.
    }

    // Backend caps at 100 items per request
    const batch = queue.slice(0, 100)
    
    // Read known plan hash from cache
    // The type of TodayResponse is imported or we can use unknown
    const cachedToday = await getCache<{ plan?: { content_hash: string } }>('portal_today')
    const knownPlanHash = cachedToday?.plan?.content_hash ?? undefined

    const response = await api.request('post', '/api/v1/portal/sync', {
      body: {
        operations: batch,
        known_plan_hash: knownPlanHash,
      },
    })

    // Process results
    for (const result of response.results) {
      if (result.status === 'applied' || result.status === 'duplicate') {
        await removeOperation(result.op_id)
      }
      // if 'rejected', we leave it in the queue to be retried or dropped manually later
      // The backend contract says we branch on it, but for now leaving it in IDB is safe.
    }

    planChanged = response.plan_changed

    // If there are more items, we could loop, but for now one batch is enough. 
    // The next online event or enqueue will trigger another sync.

  } catch (error: unknown) {
    const err = error as { status?: number; type?: string }
    // If it's a 401, the session is dead.
    if (err?.status === 401 || err?.type === 'unauthenticated') {
      await clearAllStores()
    }
    // Network errors or 500s just abort the sync until the next trigger.
  } finally {
    isSyncing = false
  }

  return { planChanged }
}
