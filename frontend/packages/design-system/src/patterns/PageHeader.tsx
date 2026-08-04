import type { ReactNode } from 'react'
import { cx } from '../utils/cx'
import styles from './PageHeader.module.css'

export interface Breadcrumb {
  label: string
  /** Omit on the current page — the last crumb is not a link. */
  href?: string
}

export interface PageHeaderProps {
  /**
   * The page title. Rendered as the `h1`.
   *
   * 🔒 Exactly one `h1` per page (WCAG 1.3.1 / 2.4.6). Routing every screen
   * through this component is what guarantees it, and what makes NFR-058's
   * "one primary purpose per screen" visible in the markup: if a screen needs
   * two `PageHeader`s, it is two screens.
   */
  title: ReactNode
  /** One line of context under the title. */
  description?: ReactNode
  /** Primary and secondary actions for the page. */
  actions?: ReactNode
  /**
   * Breadcrumb trail.
   *
   * ⚠️ 🔒 NFR-057 — navigation, breadcrumbs and routes derive from **one**
   * declared information architecture. These should come from the app's IA
   * manifest, never be hand-written per screen: hand-written crumbs are
   * precisely how V1's navigation drifted out of agreement with its routes.
   */
  breadcrumbs?: readonly Breadcrumb[]
  /** Status badge or metadata shown beside the title. */
  meta?: ReactNode
  className?: string
}

/** PageHeader — the top of every screen. */
export function PageHeader({
  title,
  description,
  actions,
  breadcrumbs,
  meta,
  className,
}: PageHeaderProps) {
  return (
    <header className={cx(styles.header, className)}>
      {breadcrumbs && breadcrumbs.length > 0 && (
        <nav aria-label="Breadcrumb" className={styles.breadcrumbs}>
          <ol className={styles.crumbList}>
            {breadcrumbs.map((crumb, index) => {
              const last = index === breadcrumbs.length - 1
              return (
                <li key={`${crumb.label}-${index}`} className={styles.crumb}>
                  {crumb.href && !last ? (
                    <a href={crumb.href} className={styles.crumbLink}>
                      {crumb.label}
                    </a>
                  ) : (
                    // 🔒 `aria-current="page"` tells a screen-reader user which
                    // crumb is where they are; without it the trail is just a
                    // list of similar-sounding links.
                    <span aria-current={last ? 'page' : undefined} className={styles.crumbCurrent}>
                      {crumb.label}
                    </span>
                  )}
                  {!last && (
                    <span aria-hidden="true" className={styles.separator}>
                      /
                    </span>
                  )}
                </li>
              )
            })}
          </ol>
        </nav>
      )}

      <div className={styles.row}>
        <div className={styles.titleBlock}>
          <div className={styles.titleLine}>
            <h1 className={styles.title}>{title}</h1>
            {meta && <div className={styles.meta}>{meta}</div>}
          </div>
          {description && <p className={styles.description}>{description}</p>}
        </div>
        {actions && <div className={styles.actions}>{actions}</div>}
      </div>
    </header>
  )
}
