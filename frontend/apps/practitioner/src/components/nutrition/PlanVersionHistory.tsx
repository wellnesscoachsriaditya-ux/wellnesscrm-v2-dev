/**
 * A plan's version history — AC-M4-008: a revision is issued while the prior
 * version stays retrievable. Renders the list; the parent decides navigation.
 */

import { Badge } from '@wellnesscrm/design-system'

export interface VersionRow {
  id: string
  versionNumber: number
  state: string
  issuedAt: string | null
}

interface Props {
  versions: readonly VersionRow[]
  currentId: string | null
}

const STATE_TONE: Record<string, 'success' | 'neutral' | 'danger'> = {
  issued: 'success',
  draft: 'neutral',
  superseded: 'neutral',
  archived: 'neutral',
  discarded: 'danger',
}

export function PlanVersionHistory({ versions, currentId }: Props) {
  if (versions.length === 0) return null

  return (
    <div data-testid="plan-version-history">
      <h3>Version history</h3>
      <ul>
        {versions.map((version) => (
          <li key={version.id} data-testid={`plan-version-${version.id}`}>
            Version {version.versionNumber}{' '}
            <Badge tone={STATE_TONE[version.state] ?? 'neutral'}>{version.state}</Badge>
            {version.id === currentId && <span> (showing)</span>}
            {version.issuedAt !== null && <span> · issued {version.issuedAt.slice(0, 10)}</span>}
          </li>
        ))}
      </ul>
    </div>
  )
}
