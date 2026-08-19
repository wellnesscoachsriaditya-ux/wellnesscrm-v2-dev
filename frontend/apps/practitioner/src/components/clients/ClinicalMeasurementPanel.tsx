/**
 * Measurement trend panel — FR-M3-011…015, EC-M3-02, EC-M3-05.
 *
 * 🔒 Renders and reports (Arch §4.4). Every measurement is a prop.
 *
 * 🔒 **BMI and waist-hip ratio are server-computed** (FR-M3-012). A BMI
 * calculated in the browser is a clinical figure the server cannot vouch for.
 *
 * ⚠️ 🔒 **No band, no label, no colour** on BMI. OD-08 is unresolved — Indian
 * BMI cut-offs differ from WHO — and the DoD forbids displaying a clinical
 * threshold without a citation. A number is a fact; "overweight" is a
 * judgement we are not yet entitled to publish.
 */

import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  FormField,
  Input,
  Spinner,
  Table,
  TableHead,
  TableBody,
  TableRow,
  TableHeaderCell,
  TableCell,
} from '@wellnesscrm/design-system'
import { useState } from 'react'

export interface MeasurementView {
  id: string
  measuredOn: string
  weightKg: string | null
  heightCm: string | null
  waistCm: string | null
  hipCm: string | null
  bodyFatPct: string | null
  bmi: string | null
  waistHipRatio: string | null
  source: 'practitioner' | 'client' | 'device'
  isFlaggedImplausible: boolean
  notes: string | null
  createdAt: string
}

export interface ClinicalMeasurementPanelProps {
  measurements: readonly MeasurementView[]
  loading?: boolean
  error?: string | null
  busy?: boolean
  onAdd: (body: {
    measured_on: string
    weight_kg?: number | null
    height_cm?: number | null
    waist_cm?: number | null
    hip_cm?: number | null
    body_fat_pct?: number | null
    notes?: string | null
  }) => void
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

export function ClinicalMeasurementPanel({
  measurements,
  loading = false,
  error = null,
  busy = false,
  onAdd,
}: ClinicalMeasurementPanelProps) {
  const [showForm, setShowForm] = useState(false)
  const [weightKg, setWeightKg] = useState('')
  const [heightCm, setHeightCm] = useState('')
  const [waistCm, setWaistCm] = useState('')
  const [hipCm, setHipCm] = useState('')
  const [notes, setNotes] = useState('')

  const handleSubmit = () => {
    const today = new Date().toISOString().split('T')[0]
    if (!today) return
    onAdd({
      measured_on: today,
      ...(weightKg ? { weight_kg: Number(weightKg) } : {}),
      ...(heightCm ? { height_cm: Number(heightCm) } : {}),
      ...(waistCm ? { waist_cm: Number(waistCm) } : {}),
      ...(hipCm ? { hip_cm: Number(hipCm) } : {}),
      ...(notes ? { notes } : {}),
    })
    setShowForm(false)
    setWeightKg('')
    setHeightCm('')
    setWaistCm('')
    setHipCm('')
    setNotes('')
  }

  return (
    <Card>
      <CardHeader
        title="Measurements"
        description="Weight, height and anthropometry — newest first."
        actions={
          <Button
            size="sm"
            variant="secondary"
            onClick={() => setShowForm(!showForm)}
            disabled={busy}
          >
            {showForm ? 'Cancel' : 'Record'}
          </Button>
        }
      />
      <CardBody>
        {showForm && (
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0.75rem', marginBottom: '1rem', padding: '1rem', borderRadius: 'var(--ds-radius-md, 8px)', background: 'var(--ds-colour-surface-secondary, #f7f7f8)' }}>
            <FormField label="Weight (kg)">
              <Input
                id="measurement-weight"
                type="number"
                value={weightKg}
                onChange={(e) => setWeightKg(e.target.value)}
              />
            </FormField>
            <FormField label="Height (cm)">
              <Input
                id="measurement-height"
                type="number"
                value={heightCm}
                onChange={(e) => setHeightCm(e.target.value)}
              />
            </FormField>
            <FormField label="Waist (cm)">
              <Input
                id="measurement-waist"
                type="number"
                value={waistCm}
                onChange={(e) => setWaistCm(e.target.value)}
              />
            </FormField>
            <FormField label="Hip (cm)">
              <Input
                id="measurement-hip"
                type="number"
                value={hipCm}
                onChange={(e) => setHipCm(e.target.value)}
              />
            </FormField>
            <div style={{ gridColumn: '1 / -1' }}>
              <FormField label="Notes">
                <Input
                  id="measurement-notes"
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                />
              </FormField>
            </div>
            <div style={{ gridColumn: '1 / -1', display: 'flex', justifyContent: 'flex-end' }}>
              <Button size="sm" onClick={handleSubmit} disabled={busy || (!weightKg && !heightCm && !waistCm && !hipCm)}>
                Save measurement
              </Button>
            </div>
          </div>
        )}

        {loading && <Spinner label="Loading measurements…" />}
        {error !== null && <p role="alert" style={{ color: 'var(--ds-colour-negative)' }}>{error}</p>}
        {!loading && error === null && measurements.length === 0 && (
          <EmptyState
            title="No measurements yet"
            description="Record a weight or height to start tracking this client's progress."
          />
        )}
        {measurements.length > 0 && (
          <div style={{ overflowX: 'auto' }}>
            <Table caption="Client measurements history">
              <TableHead>
                <TableRow>
                  <TableHeaderCell>Date</TableHeaderCell>
                  <TableHeaderCell>Weight</TableHeaderCell>
                  <TableHeaderCell>Height</TableHeaderCell>
                  <TableHeaderCell>BMI</TableHeaderCell>
                  <TableHeaderCell>Waist</TableHeaderCell>
                  <TableHeaderCell>Hip</TableHeaderCell>
                  <TableHeaderCell>WHR</TableHeaderCell>
                  <TableHeaderCell>Source</TableHeaderCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {measurements.map((m) => (
                  <TableRow key={m.id}>
                    <TableCell>{formatDate(m.measuredOn)}</TableCell>
                    <TableCell>
                      {m.weightKg ?? '—'}
                      {m.isFlaggedImplausible && (
                        <span style={{ marginLeft: '0.25rem' }}>
                          <Badge tone="warning">⚠</Badge>
                        </span>
                      )}
                    </TableCell>
                    <TableCell>{m.heightCm ?? '—'}</TableCell>
                    <TableCell>{m.bmi ?? '—'}</TableCell>
                    <TableCell>{m.waistCm ?? '—'}</TableCell>
                    <TableCell>{m.hipCm ?? '—'}</TableCell>
                    <TableCell>{m.waistHipRatio ?? '—'}</TableCell>
                    <TableCell>
                      <Badge tone={m.source === 'practitioner' ? 'info' : 'neutral'}>
                        {m.source}
                      </Badge>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardBody>
    </Card>
  )
}
