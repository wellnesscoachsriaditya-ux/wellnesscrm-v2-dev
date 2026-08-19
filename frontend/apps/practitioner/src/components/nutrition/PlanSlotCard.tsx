/**
 * One meal slot in the plan builder — its items, their locks, and the picker.
 *
 * 🔒 Renders and calls back; it holds no plan logic. Locking, macros and totals
 * are the server's, passed in as props. The effective lock (`isLocked`) is what
 * a locked *slot* propagates to its items; `itemIsLocked` is the item's own.
 */

import { Badge, Button, Card, CardBody, CardHeader } from '@wellnesscrm/design-system'
import { FoodPicker, type FoodOption, type PortionOption } from './FoodPicker'

export interface SlotItemView {
  id: string
  displayName: string
  measureDisplay: string
  energyKcal: string
  proteinG: string
  isLocked: boolean
  itemIsLocked: boolean
}

export interface SlotView {
  id: string
  label: string
  isLocked: boolean
  slotTotalKcal: string
  items: SlotItemView[]
}

interface Props {
  slot: SlotView
  editable: boolean
  busy: boolean
  onSearch: (query: string) => Promise<FoodOption[]>
  onLoadPortions: (foodId: string) => Promise<PortionOption[]>
  onAddFood: (slotId: string, foodId: string, quantity: string, measureUnitId: string) => void
  onRemoveItem: (itemId: string) => void
  onSetItemLock: (itemId: string, isLocked: boolean) => void
  onSetSlotLock: (slotId: string, isLocked: boolean) => void
  onRemoveSlot: (slotId: string) => void
}

export function PlanSlotCard({
  slot,
  editable,
  busy,
  onSearch,
  onLoadPortions,
  onAddFood,
  onRemoveItem,
  onSetItemLock,
  onSetSlotLock,
  onRemoveSlot,
}: Props) {
  return (
    <Card data-testid={`plan-slot-${slot.id}`}>
      <CardHeader
        title={
          <>
            {slot.label} {slot.isLocked && <Badge tone="neutral">Locked</Badge>}
          </>
        }
        as="h3"
      />

      <CardBody>
        <p data-testid={`slot-total-${slot.id}`}>{slot.slotTotalKcal} kcal</p>
        {editable && (
          <div data-testid={`slot-controls-${slot.id}`}>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => onSetSlotLock(slot.id, !slot.isLocked)}
              data-testid={`slot-lock-toggle-${slot.id}`}
            >
              {slot.isLocked ? 'Unlock slot' : 'Lock slot'}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => onRemoveSlot(slot.id)}
              data-testid={`slot-remove-${slot.id}`}
            >
              Remove slot
            </Button>
          </div>
        )}

        {slot.items.length === 0 ? (
          <p data-testid={`slot-empty-${slot.id}`}>No foods in this slot yet.</p>
        ) : (
          <ul data-testid={`slot-items-${slot.id}`}>
            {slot.items.map((item) => (
              <li key={item.id} data-testid={`plan-item-${item.id}`}>
                <span>{item.displayName}</span> — <span>{item.measureDisplay}</span> ·{' '}
                <span data-testid={`item-kcal-${item.id}`}>{item.energyKcal} kcal</span> ·{' '}
                <span>{item.proteinG} g protein</span>
                {item.isLocked && <Badge tone="neutral">Locked</Badge>}
                {editable && (
                  <>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => onSetItemLock(item.id, !item.itemIsLocked)}
                      data-testid={`item-lock-toggle-${item.id}`}
                    >
                      {item.itemIsLocked ? 'Unlock' : 'Lock'}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => onRemoveItem(item.id)}
                      data-testid={`item-remove-${item.id}`}
                    >
                      Remove
                    </Button>
                  </>
                )}
              </li>
            ))}
          </ul>
        )}

        {editable && (
          <FoodPicker
            slotId={slot.id}
            busy={busy}
            onSearch={onSearch}
            onLoadPortions={onLoadPortions}
            onAdd={onAddFood}
          />
        )}
      </CardBody>
    </Card>
  )
}
