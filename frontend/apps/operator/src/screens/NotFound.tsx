import { EmptyState, PageHeader } from '@wellnesscrm/design-system'

/** Shown for a URL the IA does not declare. */
export function NotFound() {
  return (
    <>
      <PageHeader title="Page not found" />
      <EmptyState
        title="This page does not exist"
        description="The address may be mistyped, or the view may have moved. Use the navigation to get back to tenant search."
        size="md"
      />
    </>
  )
}
