/**
 * The nutrition budget — ADR-A07, made visible (API §8.4).
 *
 * 🔒 Every figure is the server's; this only renders them. A negative remaining
 * is a legitimate clinical state (EC-M4-05) and is shown as such, not hidden.
 */

import { Badge, Card, CardBody, CardHeader } from '@wellnesscrm/design-system'

export interface BudgetMacrosView {
  energyKcal: string
  proteinG: string
}

export interface BudgetView {
  target: BudgetMacrosView
  lockedConsumed: BudgetMacrosView
  unlockedCurrent: BudgetMacrosView
  remainingAvailable: BudgetMacrosView
  lockedItemCount: number
  lockedSlotCount: number
  isWithinTolerance: boolean
}

export interface WarningView {
  key: string
  message: string
}

interface Props {
  budget: BudgetView
  planTotal: BudgetMacrosView
  warnings: readonly WarningView[]
}

export function NutritionBudgetPanel({ budget, planTotal, warnings }: Props) {
  const remaining = Number(budget.remainingAvailable.energyKcal)
  const tone = remaining < 0 ? 'danger' : budget.isWithinTolerance ? 'success' : 'neutral'

  return (
    <Card data-testid="nutrition-budget">
      <CardHeader title="Nutrition budget" as="h2" />
      <CardBody>
        <dl>
          <div>
            <dt>Target</dt>
            <dd data-testid="budget-target">{budget.target.energyKcal} kcal</dd>
          </div>
          <div>
            <dt>Locked consumed</dt>
            <dd data-testid="budget-locked">
              {budget.lockedConsumed.energyKcal} kcal · {budget.lockedItemCount} item(s),{' '}
              {budget.lockedSlotCount} slot(s)
            </dd>
          </div>
          <div>
            <dt>Unlocked current</dt>
            <dd data-testid="budget-unlocked">{budget.unlockedCurrent.energyKcal} kcal</dd>
          </div>
          <div>
            <dt>Remaining available</dt>
            <dd data-testid="budget-remaining">
              <Badge tone={tone}>{budget.remainingAvailable.energyKcal} kcal</Badge>
            </dd>
          </div>
          <div>
            <dt>Plan total</dt>
            <dd data-testid="budget-plan-total">
              {planTotal.energyKcal} kcal · {planTotal.proteinG} g protein
            </dd>
          </div>
        </dl>

        {warnings.length > 0 && (
          <ul data-testid="budget-warnings">
            {warnings.map((warning) => (
              <li key={warning.key} role="status">
                {warning.message}
              </li>
            ))}
          </ul>
        )}
      </CardBody>
    </Card>
  )
}
