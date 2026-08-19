import type { ReactNode, CSSProperties } from 'react'
import styles from './ui.module.css'

export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ')
}

export type Accent = 'emerald' | 'amber' | 'blue' | 'violet' | 'coral' | 'teal' | 'ink'

/* ── Iconography — one consistent Lucide-style set (1.8 stroke) ── */
const PATHS: Record<string, ReactNode> = {
  dashboard: (
    <>
      <rect x="3" y="3" width="7.5" height="8" rx="1.5" />
      <rect x="13.5" y="3" width="7.5" height="5" rx="1.5" />
      <rect x="13.5" y="12" width="7.5" height="9" rx="1.5" />
      <rect x="3" y="15" width="7.5" height="6" rx="1.5" />
    </>
  ),
  clients: (
    <>
      <circle cx="9" cy="8" r="3.2" />
      <path d="M2.8 20a6.2 6.2 0 0 1 12.4 0" />
      <path d="M16.5 5.4a3 3 0 0 1 0 5.7M18 20a6.4 6.4 0 0 0-2-4.6" />
    </>
  ),
  leads: (
    <>
      <path d="M4 5h16v14H4z" />
      <path d="M4 8.5 12 14l8-5.5" />
    </>
  ),
  appointments: (
    <>
      <rect x="3.5" y="5" width="17" height="16" rx="2.5" />
      <path d="M3.5 10h17M8 3v4M16 3v4" />
    </>
  ),
  plans: (
    <>
      <path d="M6 3h9l4 4v14H6z" />
      <path d="M14 3v5h5" />
      <path d="M9.5 12.5h5M9.5 16h4" />
    </>
  ),
  progress: (
    <>
      <path d="M3 3v18h18" />
      <path d="m7 14 3.5-4 3 2.5L21 6" />
    </>
  ),
  messages: <path d="M20.5 12a8.5 8.5 0 0 1-12.4 7.5L3.5 21l1.5-4.4A8.5 8.5 0 1 1 20.5 12Z" />,
  reports: (
    <>
      <path d="M4 20V10M10 20V4M16 20v-7M22 20H2" />
    </>
  ),
  resources: (
    <>
      <path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
    </>
  ),
  checkins: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="m8.5 12 2.5 2.5 4.5-5" />
    </>
  ),
  settings: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 2.5v3M12 18.5v3M4.2 7l2.6 1.5M17.2 15.5l2.6 1.5M4.2 17l2.6-1.5M17.2 8.5l2.6-1.5" />
    </>
  ),
  ai: (
    <>
      <path d="m12 3 1.9 4.6L18.5 9l-4.6 1.9L12 15l-1.9-4.1L5.5 9l4.6-1.4z" />
      <path d="M18 15l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  bell: (
    <>
      <path d="M18 8a6 6 0 0 0-12 0c0 7-3 8-3 8h18s-3-1-3-8" />
      <path d="M13.7 21a2 2 0 0 1-3.4 0" />
    </>
  ),
  search: (
    <>
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-3.2-3.2" />
    </>
  ),
  chevronRight: <path d="m9 6 6 6-6 6" />,
  chevronDown: <path d="m6 9 6 6 6-6" />,
  arrowLeft: <path d="M19 12H5M12 19l-7-7 7-7" />,
  more: (
    <>
      <circle cx="5" cy="12" r="1.6" />
      <circle cx="12" cy="12" r="1.6" />
      <circle cx="19" cy="12" r="1.6" />
    </>
  ),
  heart: <path d="M20.8 8.6a5 5 0 0 0-8.8-2.6A5 5 0 0 0 3.2 8.6c0 4.2 5.5 8 8.8 10.4 3.3-2.4 8.8-6.2 8.8-10.4Z" />,
  clipboard: (
    <>
      <rect x="8" y="3" width="8" height="4" rx="1.2" />
      <path d="M9 5H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-3" />
    </>
  ),
  video: (
    <>
      <rect x="2.5" y="6" width="13" height="12" rx="2.5" />
      <path d="m15.5 10 6-3.5v11l-6-3.5z" />
    </>
  ),
  phone: <path d="M6.6 3.5 9 3.9l1 3.3-1.7 1.3a12 12 0 0 0 5.2 5.2l1.3-1.7 3.3 1 .4 2.4A2 2 0 0 1 19 20 15 15 0 0 1 4 5a2 2 0 0 1 2.6-1.5Z" />,
  mail: (
    <>
      <rect x="3" y="5" width="18" height="14" rx="2.5" />
      <path d="m4 7 8 6 8-6" />
    </>
  ),
  logout: (
    <>
      <path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3" />
      <path d="M10 17l5-5-5-5M15 12H3" />
    </>
  ),
  crown: <path d="M3 7l4 4 5-6 5 6 4-4-2 12H5z" />,
  alert: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 8v5M12 16h.01" />
    </>
  ),
  filter: <path d="M3 5h18l-7 8v5l-4 2v-7z" />,
  sparkline: <path d="M3 15l4-5 4 3 4-7 6 6" />,
  home: <path d="M3 11 12 3l9 8M5 10v10h5v-6h4v6h5V10" />,
  calendar: (
    <>
      <rect x="3.5" y="5" width="17" height="16" rx="2.5" />
      <path d="M3.5 10h17M8 3v4M16 3v4" />
    </>
  ),
}

export function Icon({ name, size = 20, className }: { name: string; size?: number; className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {PATHS[name] ?? PATHS.dashboard}
    </svg>
  )
}

/* ── Avatar (deterministic tint from the name) ── */
const AVATAR_ACCENTS: Accent[] = ['emerald', 'blue', 'violet', 'amber', 'teal', 'coral']
function initials(name: string): string {
  const parts = name.trim().split(/\s+/)
  return ((parts[0]?.[0] ?? '') + (parts[1]?.[0] ?? '')).toUpperCase() || '·'
}
export function Avatar({ name, size = 40 }: { name: string; size?: number }) {
  const accent = AVATAR_ACCENTS[name.length % AVATAR_ACCENTS.length] ?? 'emerald'
  return (
    <span
      className={cx(styles.avatar, styles[`tint_${accent}`])}
      style={{ width: size, height: size, fontSize: size * 0.4 } as CSSProperties}
      aria-hidden="true"
    >
      {initials(name)}
    </span>
  )
}

/* ── Status pill ── */
const STATUS_ACCENT: Record<string, Accent> = {
  active: 'emerald',
  lead: 'blue',
  contacted: 'blue',
  consultation_scheduled: 'violet',
  paused: 'amber',
  churned: 'coral',
  archived: 'ink',
  confirmed: 'emerald',
  pending: 'amber',
  draft: 'ink',
  overdue: 'coral',
}
const STATUS_LABEL: Record<string, string> = {
  active: 'Active',
  lead: 'New enquiry',
  contacted: 'Contacted',
  consultation_scheduled: 'Consultation',
  paused: 'Paused',
  churned: 'Churned',
  archived: 'Archived',
}
export function StatusPill({ value, label }: { value: string; label?: string }) {
  const accent = STATUS_ACCENT[value] ?? 'ink'
  return (
    <span className={cx(styles.pill, styles[`pill_${accent}`])}>
      <span className={styles.pillDot} />
      {label ?? STATUS_LABEL[value] ?? value}
    </span>
  )
}

/* ── KPI card (icon + real value + label). No fabricated trend/sparkline. ── */
export function KpiCard({
  accent,
  icon,
  label,
  value,
  hint,
  index = 0,
}: {
  accent: Accent
  icon: string
  label: string
  value: ReactNode
  hint?: ReactNode
  index?: number
}) {
  return (
    <article className={styles.kpi} style={{ animationDelay: `${index * 45}ms` } as CSSProperties}>
      <span className={cx(styles.kpiIcon, styles[`bg_${accent}`])}>
        <Icon name={icon} size={22} />
      </span>
      <div className={styles.kpiBody}>
        <span className={styles.kpiValue}>{value}</span>
        <span className={styles.kpiLabel}>{label}</span>
        {hint !== undefined && <span className={styles.kpiHint}>{hint}</span>}
      </div>
    </article>
  )
}

/* ── Donut from real segments ── */
export function Donut({
  segments,
  size = 168,
  centerValue,
  centerLabel,
}: {
  segments: Array<{ label: string; value: number; accent: Accent }>
  size?: number
  centerValue: ReactNode
  centerLabel: string
}) {
  const total = segments.reduce((sum, s) => sum + s.value, 0) || 1
  const r = size / 2 - 12
  const c = 2 * Math.PI * r
  let offset = 0
  return (
    <div className={styles.donutWrap}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className={styles.donutSvg}>
        <circle cx={size / 2} cy={size / 2} r={r} className={styles.donutTrack} strokeWidth={14} fill="none" />
        {segments.map((s) => {
          const len = (s.value / total) * c
          const dash = `${len} ${c - len}`
          const el = (
            <circle
              key={s.label}
              cx={size / 2}
              cy={size / 2}
              r={r}
              className={cx(styles.donutSeg, styles[`stroke_${s.accent}`])}
              strokeWidth={14}
              fill="none"
              strokeDasharray={dash}
              strokeDashoffset={-offset}
              strokeLinecap="round"
              transform={`rotate(-90 ${size / 2} ${size / 2})`}
            />
          )
          offset += len
          return el
        })}
      </svg>
      <div className={styles.donutCenter}>
        <span className={styles.donutValue}>{centerValue}</span>
        <span className={styles.donutLabel}>{centerLabel}</span>
      </div>
    </div>
  )
}

/* ── Section card shell ── */
export function SectionCard({
  title,
  count,
  action,
  children,
  className,
}: {
  title?: ReactNode
  count?: number
  action?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section className={cx(styles.section, className)}>
      {(title || action) && (
        <header className={styles.sectionHead}>
          <div className={styles.sectionTitleRow}>
            {title && <h2 className={styles.sectionTitle}>{title}</h2>}
            {count !== undefined && <span className={styles.countBadge}>{count}</span>}
          </div>
          {action}
        </header>
      )}
      {children}
    </section>
  )
}

export function LinkAction({ children, onClick }: { children: ReactNode; onClick: () => void }) {
  return (
    <button type="button" className={styles.linkAction} onClick={onClick}>
      {children}
      <Icon name="chevronRight" size={15} />
    </button>
  )
}

export function QuickAction({
  accent,
  icon,
  label,
  onClick,
  testid,
}: {
  accent: Accent
  icon: string
  label: string
  onClick: () => void
  testid?: string
}) {
  return (
    <button type="button" className={styles.quickAction} onClick={onClick} data-testid={testid}>
      <span className={cx(styles.quickIcon, styles[`bg_${accent}`])}>
        <Icon name={icon} size={22} />
      </span>
      <span className={styles.quickLabel}>{label}</span>
    </button>
  )
}

export function Skeleton({ h = 16, w = '100%', r = 8 }: { h?: number; w?: number | string; r?: number }) {
  return <span className={styles.skeleton} style={{ height: h, width: w, borderRadius: r } as CSSProperties} />
}

/* ── Premium empty / backend-dependency screen for not-yet-built modules ── */
export function PremiumPlaceholder({
  icon,
  accent,
  title,
  description,
  dependency,
}: {
  icon: string
  accent: Accent
  title: string
  description: string
  dependency?: string
}) {
  return (
    <div className={styles.placeholder}>
      <span className={cx(styles.placeholderIcon, styles[`bg_${accent}`])}>
        <Icon name={icon} size={30} />
      </span>
      <h2 className={styles.placeholderTitle}>{title}</h2>
      <p className={styles.placeholderDesc}>{description}</p>
      {dependency && (
        <p className={styles.placeholderDep}>
          <Icon name="alert" size={15} /> Backend dependency: {dependency}
        </p>
      )}
    </div>
  )
}
