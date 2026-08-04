import type { ReactNode } from 'react'
import { cx } from '../utils/cx'
import styles from './DataList.module.css'

export interface DataListItem {
  /** The field name. */
  label: ReactNode
  /** The value. Render "—" rather than omitting an empty field. */
  value: ReactNode
}

export interface DataListProps {
  items: readonly DataListItem[]
  /**
   * `stacked` puts the label above the value — correct on narrow screens and
   * for long values. `inline` puts them side by side, which reads faster for
   * short values on a wide screen.
   */
  layout?: 'stacked' | 'inline'
  className?: string
}

/**
 * DataList — labelled read-only values.
 *
 * A real `<dl>`, not a grid of divs. The `dt`/`dd` relationship is what tells a
 * screen reader that "Mobile" names the value "+91 98765 43210"; with divs, the
 * two are unrelated strings that happen to sit near each other.
 *
 * ⚠️ This is the read-only counterpart to `FormField` — used on client detail
 * views, plan summaries and confirmation screens. Where a value is editable,
 * use a form; a `DataList` that opens editors on click becomes a form with
 * worse semantics.
 */
export function DataList({ items, layout = 'stacked', className }: DataListProps) {
  return (
    <dl className={cx(styles.list, styles[layout], className)}>
      {items.map((item, index) => (
        <div key={index} className={styles.item}>
          <dt className={styles.label}>{item.label}</dt>
          <dd className={styles.value}>{item.value}</dd>
        </div>
      ))}
    </dl>
  )
}
