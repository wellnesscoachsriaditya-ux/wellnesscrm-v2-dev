import type { ReactNode } from 'react'
import { cx } from '../utils/cx'
import styles from './Badge.module.css'

export type BadgeTone = 'neutral' | 'brand' | 'success' | 'warning' | 'danger' | 'info'

export interface BadgeProps {
  children: ReactNode
  tone?: BadgeTone
  className?: string
}

/**
 * Badge — a small status label.
 *
 * 🔒 WCAG 1.4.1 (use of colour): a badge's text always states the status, so
 * the colour is reinforcement rather than the signal. This matters concretely
 * here — client lifecycle stages (Lead / Active / Paused / Archived) are shown
 * as badges throughout the practitioner app, and roughly 1 in 12 men cannot
 * distinguish the green from the amber. Never ship a badge whose meaning is
 * only its colour.
 */
export function Badge({ children, tone = 'neutral', className }: BadgeProps) {
  return <span className={cx(styles.badge, styles[tone], className)}>{children}</span>
}
