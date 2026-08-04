import type { HTMLAttributes, ReactNode } from 'react'
import { cx } from '../utils/cx'
import styles from './Card.module.css'

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  children: ReactNode
  /** Removes the default padding, for cards holding a flush table or image. */
  flush?: boolean
}

/** Card — a bounded surface. The default container for grouped content. */
export function Card({ flush, className, children, ...props }: CardProps) {
  return (
    <div className={cx(styles.card, flush && styles.flush, className)} {...props}>
      {children}
    </div>
  )
}

export interface CardHeaderProps {
  title: ReactNode
  /** Supporting line under the title. */
  description?: ReactNode
  /** Actions aligned to the right of the title. */
  actions?: ReactNode
  /**
   * Heading level. Defaults to `h3`.
   *
   * 🔒 Headings must descend without skipping levels (WCAG 1.3.1) — a screen
   * reader user navigates by them. A card inside a section under an `h1` page
   * title is usually `h3`, but the screen knows its own structure and this
   * component cannot infer it.
   */
  as?: 'h2' | 'h3' | 'h4'
}

export function CardHeader({ title, description, actions, as: Heading = 'h3' }: CardHeaderProps) {
  return (
    <div className={styles.header}>
      <div className={styles.headerText}>
        <Heading className={styles.title}>{title}</Heading>
        {description && <p className={styles.description}>{description}</p>}
      </div>
      {actions && <div className={styles.actions}>{actions}</div>}
    </div>
  )
}

export function CardBody({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx(styles.body, className)}>{children}</div>
}

export function CardFooter({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx(styles.footer, className)}>{children}</div>
}
