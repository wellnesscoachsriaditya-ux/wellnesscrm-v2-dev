/**
 * Plans, at the top level — a signpost, not a list.
 *
 * 🔒 There is no cross-client "all plans" endpoint, and inventing a count or a
 * feed here would be a fabricated metric (NFR-072). A plan belongs to a client,
 * so this points at where plans are actually created and opened.
 */

import { EmptyState, PageHeader } from '@wellnesscrm/design-system'
import { useIaLocation } from '@wellnesscrm/ia'

export function Plans() {
  const { breadcrumbs } = useIaLocation()
  return (
    <div data-testid="plans-landing">
      <PageHeader title="Plans" breadcrumbs={breadcrumbs} />
      <EmptyState
        title="Plans live inside a client"
        description="Open a client from Clients, then use their Plans panel to create a diet plan and open it in the builder."
      />
    </div>
  )
}
