export const DB_NAME = 'wellnesscrm_client'
export const DB_VERSION = 1
export const QUEUE_STORE = 'sync_queue'
export const CACHE_STORE = 'client_cache'

export interface SyncOperation {
  op_id: string
  type: string
  payload: unknown
  client_timestamp: string
  retry_count: number
}

function openDB(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = window.indexedDB.open(DB_NAME, DB_VERSION)

    request.onerror = () => reject(request.error || new Error('IDB open failed'))

    request.onsuccess = () => resolve(request.result)

    request.onupgradeneeded = (event) => {
      const db = (event.target as IDBOpenDBRequest).result

      if (!db.objectStoreNames.contains(QUEUE_STORE)) {
        db.createObjectStore(QUEUE_STORE, { keyPath: 'op_id' })
      }

      if (!db.objectStoreNames.contains(CACHE_STORE)) {
        db.createObjectStore(CACHE_STORE, { keyPath: 'id' })
      }
    }
  })
}

export async function clearAllStores(): Promise<void> {
  const db = await openDB()
  return new Promise((resolve, reject) => {
    const tx = db.transaction([QUEUE_STORE, CACHE_STORE], 'readwrite')
    tx.objectStore(QUEUE_STORE).clear()
    tx.objectStore(CACHE_STORE).clear()
    tx.oncomplete = () => resolve()
    tx.onerror = () => reject(tx.error || new Error('Unknown IndexedDB error'))
  })
}

// ─── Queue Operations ───────────────────────────────────────────────────

export async function enqueueOperation(op: SyncOperation): Promise<void> {
  const db = await openDB()
  return new Promise((resolve, reject) => {
    const tx = db.transaction(QUEUE_STORE, 'readwrite')
    tx.objectStore(QUEUE_STORE).put(op)
    tx.oncomplete = () => {
      if (typeof window !== 'undefined') {
        window.dispatchEvent(new CustomEvent('wellnesscrm:queue_changed'))
      }
      resolve()
    }
    tx.onerror = () => reject(tx.error || new Error('Unknown IndexedDB error'))
  })
}

export async function peekQueue(): Promise<SyncOperation[]> {
  const db = await openDB()
  return new Promise((resolve, reject) => {
    const tx = db.transaction(QUEUE_STORE, 'readonly')
    const request = tx.objectStore(QUEUE_STORE).getAll()
    request.onsuccess = () => {
      const items = request.result as SyncOperation[]
      items.sort((a, b) => a.client_timestamp.localeCompare(b.client_timestamp))
      resolve(items)
    }
    tx.onerror = () => reject(tx.error || new Error('IDB getAll failed'))
  })
}

export async function removeOperation(opId: string): Promise<void> {
  const db = await openDB()
  return new Promise((resolve, reject) => {
    const tx = db.transaction(QUEUE_STORE, 'readwrite')
    tx.objectStore(QUEUE_STORE).delete(opId)
    tx.oncomplete = () => {
      if (typeof window !== 'undefined') {
        window.dispatchEvent(new CustomEvent('wellnesscrm:queue_changed'))
      }
      resolve()
    }
    tx.onerror = () => reject(tx.error || new Error('IDB delete failed'))
  })
}

// ─── Cache Operations ───────────────────────────────────────────────────

export async function setCache<T>(key: string, value: T): Promise<void> {
  const db = await openDB()
  return new Promise((resolve, reject) => {
    const tx = db.transaction(CACHE_STORE, 'readwrite')
    tx.objectStore(CACHE_STORE).put({ id: key, data: value, updated_at: new Date().toISOString() })
    tx.oncomplete = () => resolve()
    tx.onerror = () => reject(tx.error || new Error('IDB put failed'))
  })
}

export async function getCache<T>(key: string): Promise<T | null> {
  const db = await openDB()
  return new Promise((resolve, reject) => {
    const tx = db.transaction(CACHE_STORE, 'readonly')
    const request = tx.objectStore(CACHE_STORE).get(key)
    request.onsuccess = () => {
      if (request.result) {
        resolve((request.result as { data: T }).data)
      } else {
        resolve(null)
      }
    }
    tx.onerror = () => reject(tx.error || new Error('IDB get failed'))
  })
}
