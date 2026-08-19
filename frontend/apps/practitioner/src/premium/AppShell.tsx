import type { ReactNode } from 'react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { IaNavItem } from '@wellnesscrm/ia'
import { Icon, cx } from './ui'
import styles from './AppShell.module.css'

const NAV_ICON: Record<string, string> = {
  dashboard: 'dashboard',
  clients: 'clients',
  leads: 'leads',
  appointments: 'appointments',
  plans: 'plans',
  progress: 'progress',
  messages: 'messages',
  reports: 'reports',
  resources: 'resources',
  settings: 'settings',
  ai: 'ai',
  checkins: 'checkins',
}

const BOTTOM_IDS = ['dashboard', 'clients', 'plans', 'messages'] as const
const BOTTOM_ICON: Record<string, string> = { dashboard: 'home', clients: 'clients', plans: 'plans', messages: 'messages' }
const BOTTOM_LABEL: Record<string, string> = { dashboard: 'Home', clients: 'Clients', plans: 'Plans', messages: 'Messages' }

export interface PremiumShellProps {
  navItems: IaNavItem[]
  activeNavId?: string
  user: { name: string; practice: string; role: string }
  onSignOut: () => void
  onSearch?: () => void
  onQuickAdd?: () => void
  children: ReactNode
}

export function PremiumShell({
  navItems,
  activeNavId,
  user,
  onSignOut,
  onQuickAdd,
  children,
}: PremiumShellProps) {
  const [moreOpen, setMoreOpen] = useState(false)
  const bottomItems = BOTTOM_IDS.map((id) => navItems.find((n) => n.id === id)).filter(Boolean) as IaNavItem[]

  return (
    <div className={cx(styles.shell, 'p-app')}>
      {/* ── Desktop sidebar ── */}
      <aside className={styles.sidebar}>
        <div className={styles.brand}>
          <span className={styles.brandMark}>
            <Icon name="progress" size={20} />
          </span>
          <span className={styles.brandText}>
            WellnessCRM<small>Coach Portal</small>
          </span>
        </div>

        <nav className={styles.nav} aria-label="Main">
          {navItems.map((item) => {
            const active = item.id === activeNavId
            return (
              <Link
                key={item.id}
                to={item.href}
                className={cx(styles.navItem, active && styles.navItemActive)}
                {...(active ? { 'aria-current': 'page' as const } : {})}
              >
                <Icon name={NAV_ICON[item.id] ?? 'dashboard'} size={20} />
                <span>{item.label}</span>
              </Link>
            )
          })}
        </nav>

        <div className={styles.upgradeCard}>
          <span className={styles.upgradeCrown}>
            <Icon name="crown" size={18} />
          </span>
          <strong>Upgrade to Pro</strong>
          <p>Unlock advanced reports, automations and more.</p>
        </div>

        <div className={styles.sidebarUser}>
          <span className={styles.sidebarAvatar}>{user.name.slice(0, 1)}</span>
          <div className={styles.sidebarUserMeta}>
            <strong>{user.name}</strong>
            <span className={styles.onlineDot}>Online</span>
          </div>
        </div>
      </aside>

      {/* ── Main column ── */}
      <div className={styles.main}>
        <header className={styles.topbar}>
          <div className={styles.searchBox}>
            <Icon name="search" size={18} />
            <input placeholder="Search clients, plans, appointments…" aria-label="Search" data-testid="command-search" />
            <kbd>⌘K</kbd>
          </div>
          <div className={styles.topActions}>
            <button type="button" className={styles.addBtn} onClick={onQuickAdd} data-testid="topbar-quick-add" aria-label="Quick add">
              <Icon name="plus" size={18} />
            </button>
            <button type="button" className={styles.iconBtn} aria-label="Notifications">
              <Icon name="bell" size={19} />
              <span className={styles.notifDot} />
            </button>
            <div className={styles.topUser} data-testid="account-menu">
              <span className={styles.topAvatar}>{user.name.slice(0, 1)}</span>
              <div className={styles.topUserMeta}>
                <strong>{user.name}</strong>
                <span data-testid="account-role">{user.practice}</span>
              </div>
              <button type="button" className={styles.signOut} onClick={onSignOut} data-testid="logout-button" title="Sign out">
                <Icon name="logout" size={18} />
              </button>
            </div>
          </div>
        </header>

        <main className={styles.content} id="main">
          {children}
        </main>
      </div>

      {/* ── Mobile bottom navigation ── */}
      <nav className={styles.bottomNav} aria-label="Mobile">
        {bottomItems.map((item) => {
          const active = item.id === activeNavId
          return (
            <Link
              key={item.id}
              to={item.href}
              className={cx(styles.bottomItem, active && styles.bottomItemActive)}
            >
              <Icon name={BOTTOM_ICON[item.id] ?? 'home'} size={22} />
              <span>{BOTTOM_LABEL[item.id] ?? item.label}</span>
            </Link>
          )
        })}
        <button type="button" className={styles.bottomItem} onClick={() => setMoreOpen(true)}>
          <Icon name="more" size={22} />
          <span>More</span>
        </button>
      </nav>

      {moreOpen && (
        <div className={styles.sheetBackdrop} onClick={() => setMoreOpen(false)}>
          <div className={styles.sheet} onClick={(e) => e.stopPropagation()}>
            <div className={styles.sheetGrip} />
            <h3>All sections</h3>
            <div className={styles.sheetGrid}>
              {navItems.map((item) => (
                <Link key={item.id} to={item.href} className={styles.sheetItem} onClick={() => setMoreOpen(false)}>
                  <Icon name={NAV_ICON[item.id] ?? 'dashboard'} size={20} />
                  {item.label}
                </Link>
              ))}
            </div>
            <button type="button" className={styles.sheetSignOut} onClick={onSignOut}>
              Sign out
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
