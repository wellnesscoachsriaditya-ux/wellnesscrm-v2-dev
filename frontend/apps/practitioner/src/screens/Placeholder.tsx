import { EmptyState, PageHeader } from '@wellnesscrm/design-system'
import { useIaLocation } from '@wellnesscrm/ia'

export interface PlaceholderProps {
  /** The sprint that replaces this screen, e.g. `S2`. */
  readonly sprint: string
  /** What the real screen will do, in one line. */
  readonly purpose: string
}

/**
 * The stand-in every practitioner route renders until its sprint lands.
 *
 * 🔒 S0's Definition of Done is "all three apps deploy to staging and render" —
 * the shell, the IA and the deployment are what S0 proves, not the screens.
 *
 * ⚠️ It renders through `PageHeader` and `EmptyState` rather than a bare `<h1>`
 * deliberately: the title and breadcrumbs come from the IA on every screen from
 * the very first one, so a later sprint replaces the *body* of a screen that is
 * already wired correctly instead of retrofitting the wiring. It also means the
 * first deployment exercises the manifest → breadcrumb path end to end.
 */
export function Placeholder({ sprint, purpose }: PlaceholderProps) {
  const { route, breadcrumbs } = useIaLocation()

  return (
    <>
      <PageHeader title={route?.label ?? 'WellnessCRM'} breadcrumbs={breadcrumbs} />
      <EmptyState title={`${sprint} builds this screen`} description={purpose} size="md" />
    </>
  )
}

/** Bind a placeholder to its sprint and purpose, for use as an IA `view`. */
export function placeholder(sprint: string, purpose: string) {
  return function PlaceholderScreen() {
    return <Placeholder sprint={sprint} purpose={purpose} />
  }
}
