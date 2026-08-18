/**
 * Plan authoring — the data access behind the plan builder (M4, API §8).
 *
 * 🔒 **Arch §4.4 / NFR-068 — the layer allowed to talk to the API.** Components
 * render what a hook gives them; this is where the calls live. `check_boundaries.py`
 * R8 fails the build if anything under `components/` imports the API client.
 *
 * 🔒 **Optimistic concurrency (ADR-14).** Every mutation carries `If-Match`,
 * derived from the version's `row_version` — the plan is edited through its
 * children, and the counter on the parent is what makes a lost update a 409
 * rather than a silent overwrite (EC-M4-07). The token is `W/"<n>"`, which the
 * backend's `parse_if_match_row_version` accepts.
 *
 * 🔒 **No plan-authoring logic lives here.** The budget, the totals, the
 * household-measure formatting and the locking arithmetic are all server-side;
 * this module moves requests and returns the server's answers unchanged.
 */

import { createApiClient } from '@wellnesscrm/api-client'
import type { components } from '@wellnesscrm/api-client'

export type Macros = components['schemas']['MacroTotalsResponse']
export type NutritionBudget = components['schemas']['NutritionBudgetResponse']
export type PlanWarning = components['schemas']['PlanWarningResponse']
export type PlanItem = components['schemas']['PlanItemResponse']
export type PlanSlot = components['schemas']['PlanSlotResponse']
export type PlanDay = components['schemas']['PlanDayResponse']
export type PlanVersion = components['schemas']['PlanVersionResponse']
export type PlanSummary = components['schemas']['PlanSummaryResponse']
export type PlanDetail = components['schemas']['PlanDetailResponse']
export type PlanCreated = components['schemas']['PlanCreateResponse']
export type FoodItem = components['schemas']['FoodItemResponse']
export type FoodPortion = components['schemas']['FoodPortionResponse']
export type MealSlotType = components['schemas']['MealSlotType']

const api = createApiClient()

/** The `If-Match` header for a plan at this revision. */
function ifMatch(rowVersion: number): Record<string, string> {
  return { 'If-Match': `W/"${rowVersion}"` }
}

// ─── Reads ─────────────────────────────────────────────────────────────────

export function listClientPlans(clientId: string, signal?: AbortSignal): Promise<PlanSummary[]> {
  return api.request('get', '/api/v1/app/clients/{client_id}/plans', {
    path: { client_id: clientId },
    ...(signal ? { signal } : {}),
  })
}

export function fetchPlan(planId: string, signal?: AbortSignal): Promise<PlanDetail> {
  return api.request('get', '/api/v1/app/plans/{plan_id}', {
    path: { plan_id: planId },
    ...(signal ? { signal } : {}),
  })
}

export function fetchVersion(versionId: string, signal?: AbortSignal): Promise<PlanVersion> {
  return api.request('get', '/api/v1/app/plan-versions/{version_id}', {
    path: { version_id: versionId },
    ...(signal ? { signal } : {}),
  })
}

export function searchFoods(query: string, signal?: AbortSignal): Promise<FoodItem[]> {
  return api.request('get', '/api/v1/app/nutrition/foods', {
    query: { q: query },
    ...(signal ? { signal } : {}),
  })
}

export function fetchFoodPortions(foodId: string, signal?: AbortSignal): Promise<FoodPortion[]> {
  return api.request('get', '/api/v1/app/nutrition/foods/{food_id}/portions', {
    path: { food_id: foodId },
    ...(signal ? { signal } : {}),
  })
}

// ─── Plan lifecycle ──────────────────────────────────────────────────────

export interface CreatePlanInput {
  title: string
  day_count?: number
  target_energy_kcal?: string
  target_protein_g?: string
  target_carbs_g?: string
  target_fat_g?: string
  goal_type?: string
}

export function createPlan(clientId: string, input: CreatePlanInput): Promise<PlanCreated> {
  return api.request('post', '/api/v1/app/clients/{client_id}/plans', {
    path: { client_id: clientId },
    body: input,
  })
}

export interface UpdateVersionInput {
  title?: string
  practitioner_notes?: string
  target_energy_kcal?: string
  target_protein_g?: string
  target_carbs_g?: string
  target_fat_g?: string
  goal_type?: string
}

export function updateVersion(
  versionId: string,
  rowVersion: number,
  input: UpdateVersionInput,
): Promise<PlanVersion> {
  return api.request('patch', '/api/v1/app/plan-versions/{version_id}', {
    path: { version_id: versionId },
    headers: ifMatch(rowVersion),
    body: input,
  })
}

export function discardVersion(versionId: string, rowVersion: number): Promise<PlanVersion> {
  return api.request('post', '/api/v1/app/plan-versions/{version_id}/discard', {
    path: { version_id: versionId },
    headers: ifMatch(rowVersion),
  })
}

export function issueVersion(versionId: string, rowVersion: number): Promise<unknown> {
  return api.request('post', '/api/v1/app/plan-versions/{version_id}/issue', {
    path: { version_id: versionId },
    headers: ifMatch(rowVersion),
  })
}

// ─── Structure — days and slots ────────────────────────────────────────────

export function addSlot(
  versionId: string,
  rowVersion: number,
  body: { day_id: string; slot_type: MealSlotType; custom_label?: string },
): Promise<unknown> {
  return api.request('post', '/api/v1/app/plan-versions/{version_id}/slots', {
    path: { version_id: versionId },
    headers: ifMatch(rowVersion),
    body,
  })
}

export function addDay(
  versionId: string,
  rowVersion: number,
  body: { label?: string; slot_types?: MealSlotType[] },
): Promise<unknown> {
  return api.request('post', '/api/v1/app/plan-versions/{version_id}/days', {
    path: { version_id: versionId },
    headers: ifMatch(rowVersion),
    body,
  })
}

export function renameSlot(
  slotId: string,
  rowVersion: number,
  body: { custom_label?: string; target_time?: string; sort_order?: number },
): Promise<unknown> {
  return api.request('patch', '/api/v1/app/plan-slots/{slot_id}', {
    path: { slot_id: slotId },
    headers: ifMatch(rowVersion),
    body,
  })
}

export function setSlotLock(slotId: string, rowVersion: number, isLocked: boolean): Promise<unknown> {
  return api.request('patch', '/api/v1/app/plan-slots/{slot_id}', {
    path: { slot_id: slotId },
    headers: ifMatch(rowVersion),
    body: { is_locked: isLocked },
  })
}

export function removeSlot(slotId: string, rowVersion: number): Promise<void> {
  return api.request('delete', '/api/v1/app/plan-slots/{slot_id}', {
    path: { slot_id: slotId },
    headers: ifMatch(rowVersion),
  })
}

// ─── Items ───────────────────────────────────────────────────────────────

export interface AddItemInput {
  slot_id: string
  food_id: string
  quantity: string
  measure_unit_id: string
  notes?: string
  client_note?: string
}

export function addItem(rowVersion: number, input: AddItemInput): Promise<unknown> {
  return api.request('post', '/api/v1/app/plan-items', {
    headers: ifMatch(rowVersion),
    body: { item_type: 'food', ...input },
  })
}

export function updateItem(
  itemId: string,
  rowVersion: number,
  body: { quantity?: string; measure_unit_id?: string; is_locked?: boolean; sort_order?: number },
): Promise<unknown> {
  return api.request('patch', '/api/v1/app/plan-items/{item_id}', {
    path: { item_id: itemId },
    headers: ifMatch(rowVersion),
    body,
  })
}

export function removeItem(itemId: string, rowVersion: number): Promise<void> {
  return api.request('delete', '/api/v1/app/plan-items/{item_id}', {
    path: { item_id: itemId },
    headers: ifMatch(rowVersion),
  })
}
