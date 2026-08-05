import { EmptyState } from '@wellnesscrm/design-system'

export interface PlaceholderProps {
  readonly sprint: string
  readonly purpose: string
}

/**
 * The stand-in every client route renders until its sprint lands.
 *
 * ⚠️ No `PageHeader` here, unlike the practitioner app. `MobileShell` owns the
 * screen title in its top bar — a second heading below it would put two `h1`s
 * on one screen and waste the vertical space a phone does not have. The title
 * comes from the IA either way; the shell reads it, not the screen.
 */
export function Placeholder({ sprint, purpose }: PlaceholderProps) {
  return <EmptyState title={`${sprint} builds this screen`} description={purpose} size="md" />
}

/** Bind a placeholder to its sprint and purpose, for use as an IA `view`. */
export function placeholder(sprint: string, purpose: string) {
  return function PlaceholderScreen() {
    return <Placeholder sprint={sprint} purpose={purpose} />
  }
}
