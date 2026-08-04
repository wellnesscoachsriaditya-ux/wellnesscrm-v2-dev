import type { ReactNode, TdHTMLAttributes, ThHTMLAttributes } from 'react'
import { cx } from '../utils/cx'
import styles from './Table.module.css'

export interface TableProps {
  /**
   * A description of the table's contents.
   *
   * 🔒 Required. A `<caption>` is how a screen-reader user knows what a table
   * holds before navigating into it, and tables are the primary way this
   * product presents data. Pass `captionHidden` to keep it out of the visual
   * design — hidden from sight is fine; absent is not.
   */
  caption: ReactNode
  /** Hides the caption visually while keeping it for assistive technology. */
  captionHidden?: boolean
  children: ReactNode
  className?: string
}

/**
 * Table — tabular data.
 *
 * ⚠️ A real `<table>`, not divs with `role="grid"`. Screen readers give tables
 * dedicated navigation (move by row, by column, announce headers with cells)
 * that only works with genuine table semantics, and every div-based
 * reimplementation loses it.
 *
 * 🔒 NFR-064 — every list and data view has a designed empty state. This
 * component does not render one itself: an empty table still needs its headers
 * in some contexts and not in others, so the choice belongs to the screen.
 * Render `<EmptyState>` instead of the table when there are no rows.
 */
export function Table({ caption, captionHidden = false, children, className }: TableProps) {
  return (
    <div className={styles.scroll}>
      <table className={cx(styles.table, className)}>
        <caption className={cx(styles.caption, captionHidden && styles.srOnly)}>
          {caption}
        </caption>
        {children}
      </table>
    </div>
  )
}

export function TableHead({ children }: { children: ReactNode }) {
  return <thead className={styles.head}>{children}</thead>
}

export function TableBody({ children }: { children: ReactNode }) {
  return <tbody>{children}</tbody>
}

export interface TableRowProps {
  children: ReactNode
  /** Visually de-emphasises the row without removing it. */
  muted?: boolean
  className?: string
}

export function TableRow({ children, muted, className }: TableRowProps) {
  return <tr className={cx(styles.row, muted && styles.muted, className)}>{children}</tr>
}

export interface TableHeaderCellProps extends ThHTMLAttributes<HTMLTableCellElement> {
  /** Right-align for numeric columns. */
  numeric?: boolean
}

/**
 * A header cell.
 *
 * 🔒 `scope` defaults to `col`. Without it, a screen reader cannot reliably
 * pair a cell with its header, which is most of what makes a table navigable.
 */
export function TableHeaderCell({
  numeric,
  className,
  scope = 'col',
  children,
  ...props
}: TableHeaderCellProps) {
  return (
    <th scope={scope} className={cx(styles.th, numeric && styles.numeric, className)} {...props}>
      {children}
    </th>
  )
}

export interface TableCellProps extends TdHTMLAttributes<HTMLTableCellElement> {
  numeric?: boolean
}

export function TableCell({ numeric, className, children, ...props }: TableCellProps) {
  return (
    <td className={cx(styles.td, numeric && styles.numeric, className)} {...props}>
      {children}
    </td>
  )
}
