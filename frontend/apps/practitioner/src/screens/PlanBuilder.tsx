/**
 * The nutrition plan builder — the product's core workspace (S4, recomposed).
 *
 * 🔒 The screen composes; the hook fetches and mutates; the components render.
 * Everything numeric — grams, macros, the budget — is the server's, read back
 * after each change (Arch §4.4, NFR-072). A stale edit surfaces as a conflict
 * (EC-M4-07), never silently overwritten. This change is presentation only:
 * a plan hero, a sticky nutrition-budget rail and a single-column mobile reflow.
 * Every data-testid, action and optimistic-concurrency path is preserved.
 */

import { useState } from 'react'
import { Button, ErrorState, Spinner, useToast } from '@wellnesscrm/design-system'
import { useIaLocation } from '@wellnesscrm/ia'
import { useNavigate } from 'react-router-dom'
import { Icon, cx } from '../premium/ui'
import { NutritionBudgetPanel } from '../components/nutrition/NutritionBudgetPanel'
import { PlanSlotCard, type SlotView } from '../components/nutrition/PlanSlotCard'
import { PlanVersionHistory } from '../components/nutrition/PlanVersionHistory'
import type { FoodOption, PortionOption } from '../components/nutrition/FoodPicker'
import { usePlanBuilder } from '../features/nutrition/usePlanBuilder'
import { fetchFoodPortions, searchFoods } from '../features/nutrition/plansApi'
import type { NutritionBudget, PlanSlot } from '../features/nutrition/plansApi'
import styles from './PlanBuilder.module.css'

const SLOT_LABELS: Record<string, string> = {
  early_morning: 'Early morning',
  breakfast: 'Breakfast',
  mid_morning: 'Mid-morning',
  lunch: 'Lunch',
  evening_snack: 'Evening snack',
  dinner: 'Dinner',
  bedtime: 'Bedtime',
  custom: 'Custom',
}

function slotLabel(slot: PlanSlot): string {
  return slot.custom_label ?? SLOT_LABELS[slot.slot_type] ?? slot.slot_type
}

function toSlotView(slot: PlanSlot): SlotView {
  return {
    id: slot.id,
    label: slotLabel(slot),
    isLocked: slot.is_locked,
    slotTotalKcal: slot.slot_totals.energy_kcal,
    items: slot.items.map((item) => ({
      id: item.id,
      displayName: item.display_name,
      measureDisplay: item.measure_display,
      energyKcal: item.nutrition.energy_kcal,
      proteinG: item.nutrition.protein_g,
      isLocked: item.is_locked,
      itemIsLocked: item.item_is_locked,
    })),
  }
}

function toBudgetView(budget: NutritionBudget) {
  return {
    target: { energyKcal: budget.target.energy_kcal, proteinG: budget.target.protein_g },
    lockedConsumed: { energyKcal: budget.locked_consumed.energy_kcal, proteinG: budget.locked_consumed.protein_g },
    unlockedCurrent: { energyKcal: budget.unlocked_current.energy_kcal, proteinG: budget.unlocked_current.protein_g },
    remainingAvailable: { energyKcal: budget.remaining_available.energy_kcal, proteinG: budget.remaining_available.protein_g },
    lockedItemCount: budget.locked_item_count,
    lockedSlotCount: budget.locked_slot_count,
    isWithinTolerance: budget.is_within_tolerance,
  }
}

async function search(query: string): Promise<FoodOption[]> {
  const foods = await searchFoods(query)
  return foods.map((food) => ({ id: food.id, name: food.name }))
}

async function loadPortions(foodId: string): Promise<PortionOption[]> {
  const portions = await fetchFoodPortions(foodId)
  return portions.map((portion) => ({
    measureUnitId: portion.measure_unit_id,
    name: `${portion.measure_unit_name} (${portion.gram_weight} g)`,
  }))
}

export function PlanBuilder() {
  const { params } = useIaLocation()
  const planId = params.planId ?? ''
  const builder = usePlanBuilder(planId)
  const { show } = useToast()
  const navigate = useNavigate()
  const [dayIndex, setDayIndex] = useState(0)

  if (builder.status === 'loading') return <Spinner label="Loading the plan…" />

  if (builder.status === 'error' || builder.version === null) {
    return (
      <ErrorState
        title={builder.error ?? 'That plan could not be loaded'}
        whatToDoNext="Go back to the client and open the plan again."
      />
    )
  }

  const version = builder.version
  const days = version.days
  const activeDay = days[Math.min(dayIndex, days.length - 1)] ?? days[0]
  const currentVersionNumber = builder.versions.find((v) => v.id === version.id)?.version_number ?? null

  async function onIssue() {
    const ok = await builder.issue()
    if (ok) show({ tone: 'success', message: 'Plan issued. The client can now see it.' })
  }

  return (
    <div className={styles.page} data-testid="plan-builder">
      <button type="button" className={styles.back} onClick={() => navigate(-1)} data-testid="plan-back">
        <Icon name="arrowLeft" size={16} /> Back
      </button>

      <header className={styles.hero}>
        <span className={styles.heroIcon}>
          <Icon name="plans" size={28} />
        </span>
        <div className={styles.heroMain}>
          <h1 className={styles.heroTitle}>{builder.title}</h1>
          <div className={styles.heroPills} data-testid="plan-state">
            <span className={cx(styles.statePill, builder.editable ? styles.stateDraft : styles.stateIssued)}>
              <Icon name={builder.editable ? 'clipboard' : 'checkins'} size={13} />
              {version.state}
            </span>
            <span className={styles.versionTag}>Version {'version_number' in version ? (version as { version_number?: number }).version_number ?? '' : ''}</span>
            {!builder.editable && <span className={styles.readonly}>— issued plans are read-only</span>}
          </div>
        </div>
      </header>

      {builder.conflict && (
        <p role="alert" data-testid="plan-conflict" className={cx(styles.alert, styles.conflict)}>
          Someone else changed this plan while you were editing. It has been reloaded — please re-apply your change.
        </p>
      )}
      {builder.actionError !== null && (
        <p role="alert" data-testid="plan-action-error" className={styles.alert}>
          {builder.actionError}
        </p>
      )}

      {days.length > 1 && (
        <nav aria-label="Plan days" data-testid="plan-day-nav" className={styles.dayNav}>
          {days.map((day, index) => (
            <Button
              key={day.id}
              variant={day.id === activeDay?.id ? 'primary' : 'ghost'}
              size="sm"
              onClick={() => setDayIndex(index)}
              data-testid={`plan-day-${day.day_number}`}
            >
              {day.label}
            </Button>
          ))}
        </nav>
      )}

      <div className={styles.grid}>
        <div className={styles.main}>
          {activeDay?.slots.map((slot) => (
            <PlanSlotCard
              key={slot.id}
              slot={toSlotView(slot)}
              editable={builder.editable}
              busy={builder.saving}
              onSearch={search}
              onLoadPortions={loadPortions}
              onAddFood={(slotId, foodId, quantity, measureUnitId) =>
                void builder.addFood(slotId, foodId, quantity, measureUnitId)
              }
              onRemoveItem={(itemId) => void builder.removeItem(itemId)}
              onSetItemLock={(itemId, isLocked) => void builder.setItemLock(itemId, isLocked)}
              onSetSlotLock={(slotId, isLocked) => void builder.setSlotLock(slotId, isLocked)}
              onRemoveSlot={(slotId) => void builder.removeSlot(slotId)}
            />
          ))}

          {builder.editable && (
            <div className={styles.actions} data-testid="plan-structure-actions">
              <Button
                variant="secondary"
                size="sm"
                loading={builder.saving}
                onClick={() => activeDay && void builder.addCustomSlot(activeDay.id, 'New meal')}
                data-testid="add-slot-button"
              >
                Add a meal
              </Button>
              <Button
                variant="secondary"
                size="sm"
                loading={builder.saving}
                onClick={() => void builder.addDay()}
                data-testid="add-day-button"
              >
                Add a day
              </Button>
            </div>
          )}

          {builder.editable && (
            <div className={styles.issueBar} data-testid="plan-issue-actions">
              <Button
                variant="danger"
                size="sm"
                loading={builder.saving}
                onClick={() => void builder.discard()}
                data-testid="discard-plan-button"
              >
                Discard draft
              </Button>
              <Button
                variant="primary"
                loading={builder.saving}
                onClick={() => void onIssue()}
                data-testid="issue-plan-button"
              >
                Issue plan
              </Button>
            </div>
          )}
        </div>

        <aside className={styles.rail}>
          <span className={styles.railTitle}>Nutrition budget</span>
          <NutritionBudgetPanel
            budget={toBudgetView(version.nutrition_budget)}
            planTotal={{ energyKcal: version.plan_totals.energy_kcal, proteinG: version.plan_totals.protein_g }}
            warnings={version.warnings.map((warning) => ({
              key: `${warning.rule_code}-${JSON.stringify(warning.scope)}`,
              message: warning.message,
            }))}
          />
          <span className={styles.railTitle}>Version history</span>
          <PlanVersionHistory
            versions={builder.versions.map((entry) => ({
              id: entry.id,
              versionNumber: entry.version_number,
              state: entry.state,
              issuedAt: entry.issued_at,
            }))}
            currentId={version.id}
          />
        </aside>
      </div>
    </div>
  )
}
