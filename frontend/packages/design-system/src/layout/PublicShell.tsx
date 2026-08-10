import type { ReactNode } from 'react'
import { cx } from '../utils/cx'
import styles from './PublicShell.module.css'

export interface PublicShellProps {
  children: ReactNode
  /** A line under the content — usually who the page belongs to. */
  footer?: ReactNode
  className?: string
}

/**
 * PublicShell — the frame for a page shown to someone with no account.
 *
 * 🔒 **A fourth shell, and deliberately the smallest.** `AppShell`, `MobileShell`
 * and `AdminShell` all frame a *signed-in* session: each renders navigation
 * derived from an IA manifest (NFR-057). A stranger has no session and nowhere
 * to navigate, so this renders content and nothing else. Putting the public
 * enquiry form in `MobileShell` would give a prospect a bottom bar of four
 * destinations they cannot reach and imply they are already a client.
 *
 * 🔒 **Why it lives here rather than in the app that first needed it.** ADR-03
 * forbids a feature app declaring raw colour or spacing, and a page rendered
 * outside every shell still needs a readable measure, safe-area insets and a
 * background. The alternative was inline styles in one screen, which is the
 * bespoke one-off this package exists to prevent — and the next public page
 * (magic-link redemption, FR-M0-005) would have copied them.
 *
 * ⚠️ **No skip link, unlike the other three.** A skip link exists to jump past
 * repeated navigation; there is none here, so it would be a focusable control
 * that moves focus to where it already was.
 */
export function PublicShell({ children, footer, className }: PublicShellProps) {
  return (
    <div className={cx(styles.shell, className)}>
      {/* `tabIndex={-1}` so a route change can move focus here programmatically
       * without adding the region to the tab order. */}
      <main id="main-content" className={styles.main} tabIndex={-1}>
        {children}
      </main>
      {footer !== undefined && <footer className={styles.footer}>{footer}</footer>}
    </div>
  )
}
