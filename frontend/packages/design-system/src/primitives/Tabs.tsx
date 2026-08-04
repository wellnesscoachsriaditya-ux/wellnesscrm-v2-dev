import { createContext, useContext, useId, useRef } from 'react'
import type { ReactNode } from 'react'
import { cx } from '../utils/cx'
import styles from './Tabs.module.css'

export interface TabItem {
  /** Stable identifier, used as the value. */
  id: string
  label: ReactNode
  /** Optional count shown after the label, e.g. a number of clients. */
  count?: number
  disabled?: boolean
}

/**
 * Shared id prefix and selection, so `TabPanel` can build the exact ids that
 * `Tabs` referenced in `aria-controls`.
 *
 * 🔒 Without this, the two components would each generate their own ids and the
 * `aria-controls` / `aria-labelledby` pairing would silently point at nothing —
 * visually perfect, and completely broken for a screen reader. A context makes
 * the association impossible to get wrong from the outside.
 */
interface TabsContextValue {
  baseId: string
  value: string
}

const TabsContext = createContext<TabsContextValue | null>(null)

export interface TabsProps {
  items: readonly TabItem[]
  /** The selected tab's `id`. Controlled — the parent owns the state. */
  value: string
  onChange: (id: string) => void
  /** Accessible name for the tab list, e.g. "Client sections". */
  label: string
  /** The panels. Render `TabPanel` children here so ids stay paired. */
  children?: ReactNode
  className?: string
}

/**
 * Tabs — the ARIA tabs pattern, keyboard behaviour included.
 *
 * 🔒 NFR-061. The tab pattern has keyboard semantics that are not optional and
 * are almost always omitted:
 *
 * * **Arrow keys move between tabs**, not Tab. Tab moves *out* of the tab list
 *   into the panel — the whole list is one stop in the page's tab order.
 * * **Home/End** jump to the first and last tab.
 * * **Only the selected tab is focusable** (`tabIndex={-1}` on the others).
 *   This is what makes the list a single tab stop rather than N of them.
 *
 * Implemented once here so that no screen has to get it right again. The
 * behaviour is verified in `Tabs.test.tsx`.
 */
export function Tabs({ items, value, onChange, label, children, className }: TabsProps) {
  const baseId = useId()
  const listRef = useRef<HTMLDivElement>(null)

  const enabled = items.filter((item) => !item.disabled)

  function move(direction: 1 | -1 | 'first' | 'last') {
    if (enabled.length === 0) return

    let next: TabItem | undefined
    if (direction === 'first') {
      next = enabled[0]
    } else if (direction === 'last') {
      next = enabled[enabled.length - 1]
    } else {
      const current = enabled.findIndex((item) => item.id === value)
      // Wrap around, which is the expected behaviour for a tab list.
      const index = (current + direction + enabled.length) % enabled.length
      next = enabled[index]
    }

    if (!next) return
    onChange(next.id)
    // Focus follows selection in this pattern (automatic activation), so the
    // newly selected tab must actually receive focus.
    listRef.current?.querySelector<HTMLButtonElement>(`[data-tab-id="${next.id}"]`)?.focus()
  }

  return (
    <TabsContext.Provider value={{ baseId, value }}>
      <div className={cx(styles.tabs, className)}>
        <div ref={listRef} role="tablist" aria-label={label} className={styles.list}>
          {items.map((item) => {
            const selected = item.id === value
            return (
              <button
                key={item.id}
                type="button"
                role="tab"
                id={`${baseId}-tab-${item.id}`}
                data-tab-id={item.id}
                aria-selected={selected}
                aria-controls={`${baseId}-panel-${item.id}`}
                // 🔒 The crux of the pattern: one tab stop for the whole list.
                tabIndex={selected ? 0 : -1}
                disabled={item.disabled}
                className={cx(styles.tab, selected && styles.selected)}
                onClick={() => onChange(item.id)}
                onKeyDown={(event) => {
                  switch (event.key) {
                    case 'ArrowRight':
                      event.preventDefault()
                      move(1)
                      break
                    case 'ArrowLeft':
                      event.preventDefault()
                      move(-1)
                      break
                    case 'Home':
                      event.preventDefault()
                      move('first')
                      break
                    case 'End':
                      event.preventDefault()
                      move('last')
                      break
                  }
                }}
              >
                {item.label}
                {item.count !== undefined && <span className={styles.count}>{item.count}</span>}
              </button>
            )
          })}
        </div>
        {children}
      </div>
    </TabsContext.Provider>
  )
}

export interface TabPanelProps {
  /** Must match the `TabItem.id` this panel belongs to. */
  id: string
  children: ReactNode
  className?: string
}

/**
 * TabPanel — the content for one tab. Render inside `Tabs`.
 *
 * Reads the selection from context rather than taking a `value` prop, so a
 * panel cannot disagree with its tab list about which tab is active.
 *
 * `tabIndex={0}` makes the panel focusable, which is what lets Tab move from
 * the tab list into the content — required by the pattern when the panel's
 * first element is not itself focusable.
 */
export function TabPanel({ id, children, className }: TabPanelProps) {
  const context = useContext(TabsContext)
  if (!context) {
    throw new Error(
      'TabPanel must be rendered inside <Tabs>. It reads the selected tab and the ' +
        'shared id prefix from context, which is what keeps aria-controls paired.',
    )
  }

  if (id !== context.value) return null

  return (
    <div
      role="tabpanel"
      id={`${context.baseId}-panel-${id}`}
      aria-labelledby={`${context.baseId}-tab-${id}`}
      tabIndex={0}
      className={cx(styles.panel, className)}
    >
      {children}
    </div>
  )
}
