/**
 * The plan-authoring API layer — the wiring the builder depends on.
 *
 * The value under test is the transport contract, not the domain: that every
 * mutation carries the `If-Match` token derived from the version's row_version
 * (ADR-14), that the lock toggle sends `is_locked`, and that the paths are the
 * ones the backend serves. The server owns the arithmetic; these prove the
 * requests reach it in the shape it expects.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as plans from './plansApi'

function capture() {
  const calls: Array<{ url: string; init: RequestInit }> = []
  const impl = vi.fn((url: string, init?: RequestInit) => {
    calls.push({ url, init: init ?? {} })
    return Promise.resolve(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )
  })
  globalThis.fetch = impl as unknown as typeof globalThis.fetch
  return calls
}

describe('plansApi transport', () => {
  let calls: Array<{ url: string; init: RequestInit }>

  beforeEach(() => {
    calls = capture()
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('locks a slot with the If-Match token for the row version', async () => {
    await plans.setSlotLock('slot-1', 3, true)

    const call = calls[0]
    expect(call?.init.method).toBe('PATCH')
    expect(call?.url).toContain('/api/v1/app/plan-slots/slot-1')
    const headers = call?.init.headers as Record<string, string>
    // 🔒 The weak-ETag form the backend's parse_if_match_row_version accepts.
    expect(headers['If-Match']).toBe('W/"3"')
    expect(JSON.parse(call?.init.body as string)).toEqual({ is_locked: true })
  })

  it('locks an item and unlocks it, carrying the version each time', async () => {
    await plans.updateItem('item-9', 5, { is_locked: true })
    expect((calls[0]?.init.headers as Record<string, string>)['If-Match']).toBe('W/"5"')
    expect(JSON.parse(calls[0]?.init.body as string)).toEqual({ is_locked: true })
  })

  it('adds a food as an item with the food item_type and a household measure', async () => {
    await plans.addItem(2, {
      slot_id: 'slot-1',
      food_id: 'food-7',
      quantity: '1.5',
      measure_unit_id: 'unit-3',
    })

    const call = calls[0]
    expect(call?.init.method).toBe('POST')
    expect(call?.url).toContain('/api/v1/app/plan-items')
    expect((call?.init.headers as Record<string, string>)['If-Match']).toBe('W/"2"')
    expect(JSON.parse(call?.init.body as string)).toEqual({
      item_type: 'food',
      slot_id: 'slot-1',
      food_id: 'food-7',
      quantity: '1.5',
      measure_unit_id: 'unit-3',
    })
  })

  it('reads a food’s household measures from the portions endpoint', async () => {
    await plans.fetchFoodPortions('food-7')
    expect(calls[0]?.url).toContain('/api/v1/app/nutrition/foods/food-7/portions')
    expect(calls[0]?.init.method).toBe('GET')
  })

  it('issues a plan version with its If-Match token', async () => {
    await plans.issueVersion('ver-1', 4)
    expect(calls[0]?.url).toContain('/api/v1/app/plan-versions/ver-1/issue')
    expect((calls[0]?.init.headers as Record<string, string>)['If-Match']).toBe('W/"4"')
  })
})
