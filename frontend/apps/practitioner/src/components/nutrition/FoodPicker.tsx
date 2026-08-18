/**
 * Add a food to a meal slot — inline search, household measure, quantity.
 *
 * 🔒 A component: it renders and holds local UI state, and calls back to add.
 * The search, the portions lookup and the add itself are the screen's hooks;
 * this never fetches (Arch §4.4). Grams and macros are resolved server-side once
 * the item is added — this only collects "which food, how much, in what measure".
 */

import { useCallback, useState } from 'react'
import { Button, FormField, Input, Select } from '@wellnesscrm/design-system'

export interface FoodOption {
  id: string
  name: string
}

export interface PortionOption {
  measureUnitId: string
  name: string
}

interface Props {
  slotId: string
  busy: boolean
  onSearch: (query: string) => Promise<FoodOption[]>
  onLoadPortions: (foodId: string) => Promise<PortionOption[]>
  onAdd: (slotId: string, foodId: string, quantity: string, measureUnitId: string) => void
}

export function FoodPicker({ slotId, busy, onSearch, onLoadPortions, onAdd }: Props) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<FoodOption[]>([])
  const [searching, setSearching] = useState(false)
  const [selected, setSelected] = useState<FoodOption | null>(null)
  const [portions, setPortions] = useState<PortionOption[]>([])
  const [measureUnitId, setMeasureUnitId] = useState('')
  const [quantity, setQuantity] = useState('1')

  const runSearch = useCallback(async () => {
    if (query.trim().length === 0) return
    setSearching(true)
    try {
      setResults(await onSearch(query.trim()))
    } finally {
      setSearching(false)
    }
  }, [query, onSearch])

  const choose = useCallback(
    async (food: FoodOption) => {
      setSelected(food)
      const available = await onLoadPortions(food.id)
      setPortions(available)
      setMeasureUnitId(available[0]?.measureUnitId ?? '')
    },
    [onLoadPortions],
  )

  function add() {
    if (selected === null || measureUnitId === '' || quantity.trim() === '') return
    onAdd(slotId, selected.id, quantity.trim(), measureUnitId)
    setSelected(null)
    setResults([])
    setQuery('')
    setPortions([])
    setQuantity('1')
  }

  return (
    <div data-testid={`food-picker-${slotId}`}>
      <FormField label="Find a food">
        <Input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              void runSearch()
            }
          }}
          placeholder="e.g. dal, roti, paneer"
          data-testid={`food-search-input-${slotId}`}
        />
      </FormField>
      <Button
        variant="secondary"
        size="sm"
        loading={searching}
        onClick={() => void runSearch()}
        data-testid={`food-search-button-${slotId}`}
      >
        Search
      </Button>

      {results.length > 0 && (
        <ul data-testid={`food-results-${slotId}`}>
          {results.map((food) => (
            <li key={food.id}>
              <Button variant="ghost" size="sm" onClick={() => void choose(food)}>
                {food.name}
              </Button>
            </li>
          ))}
        </ul>
      )}

      {selected !== null && (
        <div data-testid={`food-add-form-${slotId}`}>
          <p>Adding: {selected.name}</p>
          <FormField label="Quantity">
            <Input
              type="number"
              min="0"
              step="0.25"
              value={quantity}
              onChange={(event) => setQuantity(event.target.value)}
              data-testid={`food-quantity-input-${slotId}`}
            />
          </FormField>
          <FormField label="Measure">
            <Select
              value={measureUnitId}
              onChange={(event) => setMeasureUnitId(event.target.value)}
              data-testid={`food-measure-select-${slotId}`}
            >
              {portions.length === 0 && <option value="">No household measure on file</option>}
              {portions.map((portion) => (
                <option key={portion.measureUnitId} value={portion.measureUnitId}>
                  {portion.name}
                </option>
              ))}
            </Select>
          </FormField>
          <Button
            variant="primary"
            size="sm"
            loading={busy}
            disabled={measureUnitId === ''}
            onClick={add}
            data-testid={`food-add-button-${slotId}`}
          >
            Add to slot
          </Button>
        </div>
      )}
    </div>
  )
}
