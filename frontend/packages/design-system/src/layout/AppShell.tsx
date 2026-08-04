import { useState } from 'react'
import type { ReactNode } from 'react'
import { cx } from '../utils/cx'
import styles from './AppShell.module.css'

export interface NavItem {
  id: string
  label: string
  href: string
  icon?: ReactNode
  /** Count badge, e.g. unread messages. */
  badge?: number
}

export interface AppShellProps {
  /**
   * Navigation items.
   *
   * 🔒 NFR-057 — these come from the app's **IA manifest**, not from a list
   * written here. One declared information architecture drives navigation,
   * breadcrumbs and routes so they cannot drift apart, which is precisely how
   * V1's navigation became inconsistent.
   */
  navItems: readonly NavItem[]
  /** The id of the active nav item. */
  activeNavId?: string
  /** Product mark / tenant name for the top-left. */
  brand: ReactNode
  /** Account menu, notifications — the top-right cluster. */
  userMenu?: ReactNode
  children: ReactNode
  className?: string
}

/**
 * AppShell — the practitioner application frame. Desktop-first (Arch §4.1).
 *
 * Sidebar navigation on desktop, collapsing to a drawer below `lg`. The
 * practitioner app must be "fully usable on a tablet" (NFR-055), which is why
 * the drawer exists at all — but its primary target is a laptop, unlike the
 * client PWA.
 *
 * 🔒 Landmarks are what make a page navigable without sight: `banner`, `nav`
 * and `main` let a screen-reader user jump straight to the content. The skip
 * link does the same for a keyboard user, who would otherwise tab through
 * every nav item on every page load (WCAG 2.4.1).
 */
export function AppShell({
  navItems,
  activeNavId,
  brand,
  userMenu,
  children,
  className,
}: AppShellProps) {
  const [drawerOpen, setDrawerOpen] = useState(false)

  return (
    <div className={cx(styles.shell, className)}>
      {/* 🔒 WCAG 2.4.1. Visually hidden until focused, then it appears — the
       * first Tab on any page offers a way past the navigation. */}
      <a href="#main-content" className={styles.skipLink}>
        Skip to main content
      </a>

      <header className={styles.header}>
        <button
          type="button"
          className={styles.menuButton}
          onClick={() => setDrawerOpen((open) => !open)}
          aria-expanded={drawerOpen}
          aria-controls="app-navigation"
          aria-label={drawerOpen ? 'Close navigation' : 'Open navigation'}
        >
          <svg viewBox="0 0 24 24" width="24" height="24" fill="none" aria-hidden="true">
            <path
              d={drawerOpen ? 'M18 6 6 18M6 6l12 12' : 'M3 12h18M3 6h18M3 18h18'}
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
            />
          </svg>
        </button>

        <div className={styles.brand}>{brand}</div>
        {userMenu && <div className={styles.userMenu}>{userMenu}</div>}
      </header>

      <div className={styles.body}>
        <nav
          id="app-navigation"
          aria-label="Main"
          className={cx(styles.sidebar, drawerOpen && styles.sidebarOpen)}
        >
          <ul className={styles.navList}>
            {navItems.map((item) => (
              <li key={item.id}>
                <a
                  href={item.href}
                  // 🔒 `aria-current="page"` is how a screen reader conveys
                  // "you are here". Colour alone does not.
                  aria-current={item.id === activeNavId ? 'page' : undefined}
                  className={cx(styles.navLink, item.id === activeNavId && styles.navLinkActive)}
                  onClick={() => setDrawerOpen(false)}
                >
                  {item.icon && (
                    <span className={styles.navIcon} aria-hidden="true">
                      {item.icon}
                    </span>
                  )}
                  <span className={styles.navLabel}>{item.label}</span>
                  {item.badge !== undefined && item.badge > 0 && (
                    <span className={styles.navBadge}>{item.badge}</span>
                  )}
                </a>
              </li>
            ))}
          </ul>
        </nav>

        {/* Closes the drawer on tap. `aria-hidden` because the same action is
         * available from the labelled toggle button above. */}
        {drawerOpen && (
          <div
            className={styles.scrim}
            onClick={() => setDrawerOpen(false)}
            aria-hidden="true"
          />
        )}

        <main id="main-content" className={styles.main} tabIndex={-1}>
          {children}
        </main>
      </div>
    </div>
  )
}
