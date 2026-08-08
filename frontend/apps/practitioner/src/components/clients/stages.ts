/**
 * Lifecycle stage presentation — labels and tones, in one place.
 *
 * 🔒 Lives under `components/` because it is presentation, and because R8
 * forbids a component importing anything from `features/`. A component needing
 * a stage label must be able to reach it without crossing that line.
 *
 * ⚠️ These are the practitioner-facing names, not the wire values. The API sends
 * `consultation_scheduled`; a practitioner reads "Consultation booked". Deriving
 * one from the other with a regex would put "Consultation Scheduled" on screen —
 * accurate, and not how anyone speaks.
 */

import type { BadgeTone } from '@wellnesscrm/design-system'

/** Every stage the API can report, including the archived state. */
export type StageValue =
  | 'lead'
  | 'contacted'
  | 'consultation_scheduled'
  | 'active'
  | 'paused'
  | 'churned'
  | 'archived'

export const STAGE_LABEL: Record<StageValue, string> = {
  lead: 'New enquiry',
  contacted: 'Contacted',
  consultation_scheduled: 'Consultation booked',
  active: 'Active client',
  paused: 'Paused',
  churned: 'Churned',
  archived: 'Archived',
}

/**
 * What each stage means for the practitioner, shown beside the choice.
 *
 * 🔒 The `active` and `paused` entries state the billing consequence, because
 * M1.5 makes the stage a **billing boundary** that "MUST be predictable without
 * the practitioner thinking about it". A dropdown that silently starts charging
 * is the surprise that requirement exists to prevent.
 */
export const STAGE_MEANING: Record<StageValue, string> = {
  lead: 'An enquiry you have not yet responded to. Free.',
  contacted: 'You have replied and are waiting to hear back. Free.',
  consultation_scheduled: 'A consultation is booked. Still free.',
  active: 'Counts towards your plan and receives plans, messages and check-ins.',
  paused: 'Does not count towards your plan, and receives nothing.',
  churned: 'No longer working with you. Does not count towards your plan.',
  archived: 'Hidden from your lists. Nothing has been deleted.',
}

export const STAGE_TONE: Record<StageValue, BadgeTone> = {
  lead: 'info',
  contacted: 'info',
  consultation_scheduled: 'info',
  active: 'success',
  paused: 'warning',
  churned: 'neutral',
  archived: 'neutral',
}

/** 🔒 M1.5 — the one predicate the whole limit rests on. */
export function countsTowardsLimit(stage: StageValue, isArchived: boolean): boolean {
  return stage === 'active' && !isArchived
}
