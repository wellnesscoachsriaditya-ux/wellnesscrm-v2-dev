/**
 * Assessment history panel — FR-M3-007, AC-M3-007.
 *
 * 🔒 Renders and reports (Arch §4.4). Every entry is a prop; R8 fails the
 * build if anything here reaches the API.
 *
 * ⚠️ Shows assessment summaries, not full answer sets. The list is for
 * choosing which administration to open, and shipping every answer set would
 * put a client's full history in a panel that is rendered as five rows.
 */

import { Badge, Card, CardBody, CardHeader, EmptyState, Spinner } from '@wellnesscrm/design-system'

export interface AssessmentView {
  id: string
  definitionCode: string
  definitionVersion: number
  status: 'in_progress' | 'completed'
  completedBy: string | null
  startedAt: string
  completedAt: string | null
}

export interface ClinicalAssessmentPanelProps {
  assessments: readonly AssessmentView[]
  loading?: boolean
  error?: string | null
}

function statusBadge(status: string) {
  return status === 'completed'
    ? <Badge tone="success">Completed</Badge>
    : <Badge tone="warning">In progress</Badge>
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    })
  } catch {
    return iso
  }
}

export function ClinicalAssessmentPanel({
  assessments,
  loading = false,
  error = null,
}: ClinicalAssessmentPanelProps) {
  return (
    <Card>
      <CardHeader
        title="Assessments"
        description="Nutrition intake assessments — each is versioned and kept separately."
      />
      <CardBody>
        {loading && <Spinner label="Loading assessments…" />}
        {error !== null && <p role="alert" style={{ color: 'var(--ds-colour-negative)' }}>{error}</p>}
        {!loading && error === null && assessments.length === 0 && (
          <EmptyState
            title="No assessments yet"
            description="Start an assessment to capture this client's nutrition profile."
          />
        )}
        {assessments.length > 0 && (
          <ul style={{ listStyle: 'none', padding: 0, margin: 0, display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
            {assessments.map((a) => (
              <li
                key={a.id}
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  padding: '0.75rem 1rem',
                  borderRadius: 'var(--ds-radius-md, 8px)',
                  background: 'var(--ds-colour-surface-secondary, #f7f7f8)',
                }}
              >
                <div>
                  <strong>{a.definitionCode} v{a.definitionVersion}</strong>
                  <span style={{ marginLeft: '0.75rem', fontSize: '0.875rem', opacity: 0.7 }}>
                    Started {formatDate(a.startedAt)}
                    {a.completedAt && <> · Completed {formatDate(a.completedAt)}</>}
                  </span>
                </div>
                {statusBadge(a.status)}
              </li>
            ))}
          </ul>
        )}
      </CardBody>
    </Card>
  )
}
