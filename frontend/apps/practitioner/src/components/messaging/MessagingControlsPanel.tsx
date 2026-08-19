/**
 * Quiet hours, the weekly cap, and what failed to reach a client.
 *
 * FR-M8-008, FR-M8-009 and AC-M8-007.
 *
 * 🔒 Renders and reports (Arch §4.4).
 *
 * 🔒 **"Deferred", not "blocked".** A message due inside quiet hours is moved to
 * the next permitted time, never dropped (AC-M8-006). The copy says so, because
 * a practitioner who believed quiet hours discarded messages would widen the
 * window to be safe — and their clients would be woken at 02:00.
 */

import { useState } from 'react'
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  FormField,
  Input,
} from '@wellnesscrm/design-system'

export interface QuietHoursView {
  start: string
  end: string
  maxPerWeek: number | null
}

export interface DeliveryFailureView {
  id: string
  templateLabel: string
  recipientAddress: string
  failureReason: string | null
  createdAt: string
}

export interface MessagingControlsPanelProps {
  quietHours: QuietHoursView
  failures: readonly DeliveryFailureView[]
  busy?: boolean
  error?: string | null
  onSave: (update: { start: string; end: string; maxPerWeek: number | null }) => void
}

function when(value: string): string {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString()
}

export function MessagingControlsPanel({
  quietHours,
  failures,
  busy = false,
  error = null,
  onSave,
}: MessagingControlsPanelProps) {
  const [start, setStart] = useState(quietHours.start)
  const [end, setEnd] = useState(quietHours.end)
  const [maxPerWeek, setMaxPerWeek] = useState(
    quietHours.maxPerWeek === null ? '' : String(quietHours.maxPerWeek),
  )

  return (
    <>
      <Card>
        <CardHeader
          title="When clients can be messaged"
          description="Messages due inside quiet hours are held and sent at the next permitted time — never dropped."
        />
        <CardBody>
          {error ? <p role="alert">{error}</p> : null}

          <FormField label="Quiet hours start" hint="Your practice's local time.">
            <Input
              type="time"
              value={start}
              disabled={busy}
              onChange={(event) => setStart(event.currentTarget.value)}
            />
          </FormField>

          <FormField label="Quiet hours end">
            <Input
              type="time"
              value={end}
              disabled={busy}
              onChange={(event) => setEnd(event.currentTarget.value)}
            />
          </FormField>

          {/* 🔒 FR-M8-008 — the cap applies across *all* message types, which is
            * the part worth stating: three features each sending "only two"
            * messages is six messages to the client. */}
          <FormField
            label="Most messages per client per week"
            hint="Counted across every message type. Leave blank for the default of seven."
          >
            <Input
              type="number"
              min={0}
              max={100}
              value={maxPerWeek}
              disabled={busy}
              onChange={(event) => setMaxPerWeek(event.currentTarget.value)}
            />
          </FormField>

          <Button
            disabled={busy}
            onClick={() =>
              onSave({
                start,
                end,
                maxPerWeek: maxPerWeek === '' ? null : Number(maxPerWeek),
              })
            }
          >
            Save
          </Button>
        </CardBody>
      </Card>

      <Card>
        <CardHeader
          title="Delivery failures"
          description="Messages that did not reach a client, and what the provider said."
        />
        <CardBody>
          {/* 🔒 AC-M8-007 / EC-M8-02 — in one place a practitioner will look. A
            * failure a practitioner has to open each client to find is a failure
            * they will not find. */}
          {failures.length === 0 ? (
            <EmptyState
              title="Nothing has failed"
              description="Messages that cannot be delivered will appear here with the reason."
            />
          ) : (
            <ul>
              {failures.map((failure) => (
                <li key={failure.id}>
                  <Badge tone="danger">Failed</Badge> <strong>{failure.templateLabel}</strong>{' '}
                  <span>
                    {failure.recipientAddress} · {when(failure.createdAt)}
                  </span>
                  {failure.failureReason ? <p>{failure.failureReason}</p> : null}
                </li>
              ))}
            </ul>
          )}
        </CardBody>
      </Card>
    </>
  )
}
