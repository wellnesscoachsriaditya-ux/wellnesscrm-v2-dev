import { EmptyState } from '@wellnesscrm/design-system'

/**
 * Shown for a URL the IA does not declare.
 *
 * ⚠️ Clients arrive here by following a stale WhatsApp deep link, which is the
 * likeliest broken-link case in the product (FR-M7-013). So the copy names that
 * cause and points at the tab that will work, rather than saying "404".
 */
export function NotFound() {
  return (
    <EmptyState
      title="This link has expired"
      description="Links from older messages stop working after a while. Tap Today to see your current plan, or ask your dietitian to send a new link."
      size="md"
    />
  )
}
