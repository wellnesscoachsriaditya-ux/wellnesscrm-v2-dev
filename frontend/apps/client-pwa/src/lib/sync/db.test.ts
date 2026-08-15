/* eslint-disable */
import { describe, expect, it, beforeEach, vi } from 'vitest'
import {
  enqueueOperation,
  peekQueue,
  removeOperation,
  clearAllStores,
  setCache,
  getCache,
} from './db'

const createMockTx = (storeData: Map<string, any[]>) => {
  return {
    objectStore: (name: string) => {
      const data = storeData.get(name) || []
      return {
        put: (item: any) => {
          const keyPath = name === 'sync_queue' ? 'op_id' : 'id'
          const existing = data.findIndex(d => d[keyPath] === item[keyPath])
          if (existing >= 0) {
            data[existing] = item
          } else {
            data.push(item)
          }
          return { onsuccess: null }
        },
        getAll: () => {
          const req = { result: null as any, onsuccess: null as any }
          setTimeout(() => {
            req.result = [...data]
            if (req.onsuccess) req.onsuccess()
          }, 0)
          return req
        },
        get: (key: string) => {
          const req = { result: null as any, onsuccess: null as any }
          setTimeout(() => {
            req.result = data.find(d => d.id === key)
            if (req.onsuccess) req.onsuccess()
          }, 0)
          return req
        },
        delete: (key: string) => {
          const idx = data.findIndex(d => d.op_id === key)
          if (idx >= 0) data.splice(idx, 1)
          return { onsuccess: null }
        },
        clear: () => {
          data.length = 0
        }
      }
    },
    oncomplete: null as any,
    onerror: null as any
  }
}

describe('IndexedDB Native Wrapper', () => {
  const mockStores = new Map<string, any[]>()
  mockStores.set('sync_queue', [])
  mockStores.set('client_cache', [])

  beforeEach(async () => {
    mockStores.get('sync_queue')!.length = 0
    mockStores.get('client_cache')!.length = 0

    const mockIDB = {
      open: () => {
        const req = { result: null as any, onsuccess: null as any }
        setTimeout(() => {
          req.result = {
            transaction: (names: string | string[], mode: string) => {
              const tx = createMockTx(mockStores)
              setTimeout(() => {
                if (tx.oncomplete) tx.oncomplete()
              }, 10)
              return tx
            }
          }
          if (req.onsuccess) req.onsuccess()
        }, 0)
        return req
      }
    }
    vi.stubGlobal('indexedDB', mockIDB)

    await clearAllStores()
  })

  it('enqueues and retrieves operations in order', async () => {
    await enqueueOperation({
      op_id: 'op-1',
      type: 'log_adherence',
      payload: { value: 1 },
      client_timestamp: '2026-08-15T10:00:00Z',
      retry_count: 0,
    })

    await enqueueOperation({
      op_id: 'op-2',
      type: 'log_adherence',
      payload: { value: 2 },
      client_timestamp: '2026-08-15T09:00:00Z', // older
      retry_count: 0,
    })

    const queue = await peekQueue()
    expect(queue).toHaveLength(2)
    // Should be sorted by client_timestamp
    expect(queue[0]?.op_id).toBe('op-2')
    expect(queue[1]?.op_id).toBe('op-1')
  })

  it('removes an operation', async () => {
    await enqueueOperation({
      op_id: 'op-1',
      type: 'test',
      payload: {},
      client_timestamp: '2026-08-15T10:00:00Z',
      retry_count: 0,
    })

    await removeOperation('op-1')
    const queue = await peekQueue()
    expect(queue).toHaveLength(0)
  })

  it('sets and gets cache', async () => {
    await setCache('test_key', { foo: 'bar' })
    const data = await getCache<{ foo: string }>('test_key')
    expect(data).toEqual({ foo: 'bar' })

    const empty = await getCache('not_found')
    expect(empty).toBeNull()
  })
})
