/**
 * How a lead source reads on screen — FR-M2-009, US-M2-04.
 *
 * 🔒 Presentation only. The vocabulary itself is `kernel.leads.LeadSource`, and
 * the attribution question ("which channel produces enquiries") is answered by
 * the server — this file decides nothing, it only spells the answer.
 *
 * ⚠️ **`null` is not `other`.** "We do not know where this came from" and "the
 * prospect chose Other" are different facts, and collapsing them would inflate
 * one channel with every enquiry whose link carried no parameter — making
 * US-M2-04 unanswerable exactly when a new channel started working.
 */

/**
 * Every channel the API can report — FR-M2-009.
 *
 * ⚠️ 🔒 **Declared here, not imported from `features/`.** R8 forbids a component
 * importing domain code, and a label map is presentation that every component
 * must be able to reach. The duplication is deliberate and checked: the wire
 * values come from `kernel.leads.LeadSource`, and `sources.test.ts` asserts this
 * union covers exactly the generated schema's members — so a channel added
 * server-side fails the build here rather than rendering as `undefined`.
 */
export type LeadSource =
  | 'instagram'
  | 'whatsapp'
  | 'referral'
  | 'google'
  | 'facebook'
  | 'walk_in'
  | 'other'

export const SOURCE_LABEL: Record<LeadSource, string> = {
  instagram: 'Instagram',
  whatsapp: 'WhatsApp',
  referral: 'Referral',
  google: 'Google',
  facebook: 'Facebook',
  walk_in: 'Walk-in',
  other: 'Other',
}

/** The filter list, in the order a practitioner would look for them. */
export const SOURCE_OPTIONS: readonly LeadSource[] = [
  'instagram',
  'whatsapp',
  'referral',
  'google',
  'facebook',
  'walk_in',
  'other',
]

/** How an absent source renders — see the module note on why it is not "Other". */
export const UNKNOWN_SOURCE_LABEL = 'Not recorded'

export function sourceLabel(source: LeadSource | null | undefined): string {
  return source ? SOURCE_LABEL[source] : UNKNOWN_SOURCE_LABEL
}

/**
 * How long an enquiry has been waiting, in words.
 *
 * ⚠️ Formats a **server-computed** number (Principle 3) and derives nothing. The
 * hours come from `age_hours`; a browser subtracting timestamps itself would
 * disagree across a timezone or a clock skew, and the figure deciding who gets
 * called next would differ per device.
 */
export function ageLabel(hours: number): string {
  if (hours < 1) return 'Just now'
  if (hours < 24) {
    const rounded = Math.floor(hours)
    return `${rounded} hour${rounded === 1 ? '' : 's'} ago`
  }
  const days = Math.floor(hours / 24)
  return `${days} day${days === 1 ? '' : 's'} ago`
}
