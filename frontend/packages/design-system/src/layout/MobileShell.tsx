import type { ReactNode } from 'react'
import { cx } from '../utils/cx'
import styles from './MobileShell.module.css'

export interface MobileNavItem {
  id: string
  label: string
  href: string
  /** Required here, unlike AppShell — a bottom bar without icons is unusable. */
  icon: ReactNode
  badge?: number
}

export interface MobileShellProps {
  /**
   * Bottom navigation items.
   *
   * ⚠️ Three to five. Below three, a bottom bar is not worth the vertical
   * space; above five, the targets become too narrow for a thumb (NFR-059).
   * 🔒 NFR-057 — from the IA manifest, not hand-written.
   */
  navItems: readonly MobileNavItem[]
  activeNavId?: string
  /** Screen title for the top bar. Omit for a full-bleed view. */
  title?: ReactNode
  /** Back button target. When set, a back affordance replaces the title's lead. */
  onBack?: () => void
  /** Single action in the top-right. */
  action?: ReactNode
  children: ReactNode
  className?: string
}

/**
 * MobileShell — the client PWA frame. Mobile-first (Arch §4.1, NFR-055).
 *
 * Bottom navigation rather than a drawer, because the client app is used
 * one-handed and the bottom of the screen is the only region a thumb reaches
 * comfortably. This is also why the nav is always visible: the 60-second rule
 * (M7.3) means a client should never have to open a menu to get anywhere.
 *
 * 🔒 Safe-area insets are honoured throughout. Without them the bottom bar sits
 * under the iOS home indicator, where taps land on the OS gesture area instead
 * of our buttons — the single most common way an installed PWA feels broken.
 */
export function MobileShell({
  navItems,
  activeNavId,
  title,
  onBack,
  action,
  children,
  className,
}: MobileShellProps) {
  return (
    <div className={cx(styles.shell, className)}>
      <a href="#main-content" className={styles.skipLink}>
        Skip to main content
      </a>

      {(title || onBack || action) && (
        <header className={styles.header}>
          {onBack && (
            <button
              type="button"
              onClick={onBack}
              className={styles.back}
              aria-label="Go back"
            >
              <svg viewBox="0 0 24 24" width="24" height="24" fill="none" aria-hidden="true">
                <path
                  d="m15 18-6-6 6-6"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </button>
          )}
          {title && <h1 className={styles.title}>{title}</h1>}
          {action && <div className={styles.action}>{action}</div>}
        </header>
      )}

      <main id="main-content" className={styles.main} tabIndex={-1}>
        {children}
      </main>

      <nav aria-label="Main" className={styles.bottomNav}>
        <ul className={styles.navList}>
          {navItems.map((item) => (
            <li key={item.id} className={styles.navItem}>
              <a
                href={item.href}
                aria-current={item.id === activeNavId ? 'page' : undefined}
                className={cx(styles.navLink, item.id === activeNavId && styles.navLinkActive)}
              >
                <span className={styles.navIcon} aria-hidden="true">
                  {item.icon}
                  {item.badge !== undefined && item.badge > 0 && (
                    <span className={styles.navBadge}>
                      {/* Cap the display so a large number does not overflow
                       * the dot; the real count is read out below. */}
                      {item.badge > 9 ? '9+' : item.badge}
                    </span>
                  )}
                </span>
                <span className={styles.navLabel}>{item.label}</span>
                {item.badge !== undefined && item.badge > 0 && (
                  <span className={styles.srOnly}>{item.badge} unread</span>
                )}
              </a>
            </li>
          ))}
        </ul>
      </nav>
    </div>
  )
}
