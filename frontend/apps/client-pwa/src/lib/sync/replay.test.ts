/* eslint-disable */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { syncQueue } from './replay'
import { api } from '../api'
import { peekQueue, removeOperation, clearAllStores, getCache } from './db'

// Mock the API client
vi.mock('../api', () => ({
  api: {
    request: vi.fn(),
  },
}))

// Mock the IDB wrapper
vi.mock('./db', () => ({
  enqueueOperation: vi.fn(),
  peekQueue: vi.fn(),
  removeOperation: vi.fn(),
  clearAllStores: vi.fn(),
  setCache: vi.fn(),
  getCache: vi.fn(),
}))

describe('Offline Sync Replay Engine', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    vi.mocked(peekQueue).mockResolvedValue([])
    vi.mocked(getCache).mockResolvedValue(null)
    
    // Stub navigator.onLine to be true by default
    vi.stubGlobal('navigator', { onLine: true })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('aborts early if offline', async () => {
    vi.stubGlobal('navigator', { onLine: false })
    
    vi.mocked(peekQueue).mockResolvedValue([{
      op_id: 'test-1',
      type: 'test',
      payload: {},
      client_timestamp: '2026-08-15T10:00:00Z',
      retry_count: 0,
    }])

    const result = await syncQueue()
    expect(result.planChanged).toBe(false)
    expect(api.request).not.toHaveBeenCalled()
  })

  it('processes the queue and handles duplicate and applied correctly', async () => {
    vi.mocked(peekQueue).mockResolvedValueOnce([
      { op_id: 'test-1', type: 't1', payload: {}, client_timestamp: '2026-01-01', retry_count: 0 },
      { op_id: 'test-2', type: 't2', payload: {}, client_timestamp: '2026-01-02', retry_count: 0 },
      { op_id: 'test-3', type: 't3', payload: {}, client_timestamp: '2026-01-03', retry_count: 0 },
    ])

    vi.mocked(api.request).mockResolvedValueOnce({
      plan_changed: true,
      current_plan_hash: 'new_hash',
      results: [
        { op_id: 'test-1', status: 'applied' },
        { op_id: 'test-2', status: 'duplicate' },
        { op_id: 'test-3', status: 'rejected', error: 'invalid' },
      ],
    } as any)

    const result = await syncQueue()
    expect(result.planChanged).toBe(true)

    expect(api.request).toHaveBeenCalledWith('post', '/api/v1/portal/sync', expect.objectContaining({
      body: expect.objectContaining({
        operations: expect.any(Array),
      }),
    }))

    // test-1 and test-2 should be removed
    expect(removeOperation).toHaveBeenCalledWith('test-1')
    expect(removeOperation).toHaveBeenCalledWith('test-2')
    expect(removeOperation).not.toHaveBeenCalledWith('test-3')
  })

  it('sends known_plan_hash from cache', async () => {
    vi.mocked(getCache).mockResolvedValueOnce({
      plan: { content_hash: 'hash-abc' }
    })
    
    vi.mocked(peekQueue).mockResolvedValueOnce([
      { op_id: 'test-1', type: 't1', payload: {}, client_timestamp: '2026-01-01', retry_count: 0 }
    ])
    
    vi.mocked(api.request).mockResolvedValueOnce({
      plan_changed: false,
      results: [{ op_id: 'test-1', status: 'applied' }],
    } as any)

    await syncQueue()

    expect(api.request).toHaveBeenCalledWith('post', '/api/v1/portal/sync', expect.objectContaining({
      body: expect.objectContaining({
        known_plan_hash: 'hash-abc',
      }),
    }))
  })

  it('wipes the database on 401 unauthenticated', async () => {
    vi.mocked(peekQueue).mockResolvedValueOnce([
      { op_id: 'test-1', type: 't1', payload: {}, client_timestamp: '2026-01-01', retry_count: 0 }
    ])
    
    vi.mocked(api.request).mockRejectedValueOnce({
      status: 401,
      type: 'unauthenticated'
    })

    await syncQueue()

    expect(clearAllStores).toHaveBeenCalled()
  })
})
