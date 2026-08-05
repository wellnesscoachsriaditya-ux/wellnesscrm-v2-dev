import { EmptyState, PageHeader } from '@wellnesscrm/design-system'

/**
 * Shown for a URL the IA does not declare.
 *
 * 🔒 NFR-063 — an error must say what to do next. "404" alone tells a
 * practitioner nothing they can act on, which is why `ErrorState.whatToDoNext`
 * is a required prop in the design system; the same standard applies here.
 */
export function NotFound() {
  return (
    <>
      <PageHeader title="Page not found" />
      <EmptyState
        title="This page does not exist"
        description="The link may be out of date, or the address mistyped. Use the navigation to get back to your clients."
        size="md"
      />
    </>
  )
}
