import type { ReactNode } from 'react'
import { cx } from '../utils/cx'
import styles from './AdminShell.module.css'

export interface AdminNavItem {
  id: string
  label: string
  href: string
}

export interface AdminShellProps {
  /** 🔒 NFR-057 — from the operator app's IA manifest. */
  navItems: readonly AdminNavItem[]
  activeNavId?: string
  /** The signed-in operator's identifier, shown persistently. */
  operatorLabel: ReactNode
  /**
   * Environment name — "production", "staging".
   *
   * ⚠️ Rendered as a prominent banner. An operator who believes they are in
   * staging while acting on production data is the most expensive mistake this
   * console can enable, and it is entirely preventable.
   */
  environment?: string
  children: ReactNode
  className?: string
}

/**
 * AdminShell — the operator console frame. Arch §4.1.
 *
 * Deliberately austere. The operator console is used by us, not by customers:
 * it is a tool for support and food curation, and every hour spent styling it
 * is an hour not spent on the product a practitioner pays for.
 *
 * ⚠️ Visual plainness is not the same as carelessness. The console can read
 * tenant data, so two things are non-negotiable and both are structural here:
 *
 * * **The environment is always visible.** See `environment` above.
 * * **The acting operator is always visible.** Every action here is audited
 *   against a named person (Arch §3.3: `admin` must never bypass authz or
 *   audit), and showing who is acting makes that concrete rather than abstract.
 */
export function AdminShell({
  navItems,
  activeNavId,
  operatorLabel,
  environment,
  children,
  className,
}: AdminShellProps) {
  const isProduction = environment?.toLowerCase() === 'production'

  return (
    <div className={cx(styles.shell, className)}>
      <a href="#main-content" className={styles.skipLink}>
        Skip to main content
      </a>

      {environment && (
        // `role="status"` rather than `alert`: it is persistent context, not a
        // new event, so it should not interrupt.
        <div
          role="status"
          className={cx(styles.environment, isProduction && styles.production)}
        >
          {isProduction ? 'PRODUCTION — actions affect real tenant data' : environment}
        </div>
      )}

      <header className={styles.header}>
        <span className={styles.brand}>WellnessCRM Operator</span>
        <nav aria-label="Main" className={styles.nav}>
          {navItems.map((item) => (
            <a
              key={item.id}
              href={item.href}
              aria-current={item.id === activeNavId ? 'page' : undefined}
              className={cx(styles.navLink, item.id === activeNavId && styles.navLinkActive)}
            >
              {item.label}
            </a>
          ))}
        </nav>
        <span className={styles.operator}>{operatorLabel}</span>
      </header>

      <main id="main-content" className={styles.main} tabIndex={-1}>
        {children}
      </main>
    </div>
  )
}
