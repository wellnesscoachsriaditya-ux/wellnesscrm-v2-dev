import { EmptyState, PageHeader } from '@wellnesscrm/design-system'
import { useIaLocation } from '@wellnesscrm/ia'

export interface PlaceholderProps {
  readonly sprint: string
  readonly purpose: string
}

/** The stand-in every operator route renders until its sprint lands. */
export function Placeholder({ sprint, purpose }: PlaceholderProps) {
  const { route, breadcrumbs } = useIaLocation()

  return (
    <>
      <PageHeader title={route?.label ?? 'Operator'} breadcrumbs={breadcrumbs} />
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
