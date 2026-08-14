/**
 * A client's check-in cadence — FR-M8-022…024.
 *
 * 🔒 Renders and reports (Arch §4.4).
 *
 * 🔒 **Pausing does not change the client's stage**, and the copy says so. A
 * practitioner who believed otherwise would use the stage transition instead,
 * which is metered and visible to the client — the confusion FR-M8-024 exists to
 * prevent.
 */

import { useState } from 'react'
import {
  Button,
  Card,
  CardBody,
  CardHeader,
  FormField,
  Select,
  Spinner,
} from '@wellnesscrm/design-system'

export type CheckinFrequencyValue = 'weekly' | 'fortnightly' | 'monthly'

export interface CheckinScheduleView {
  frequency: CheckinFrequencyValue
  dayOfWeek: number | null
  timeOfDay: string
  isPaused: boolean
  nextDueOn: string | null
}

export interface ClientCheckinPanelProps {
  schedule: CheckinScheduleView | null
  loading?: boolean
  error?: string | null
  busy?: boolean
  onSave: (update: {
    frequency: CheckinFrequencyValue
    dayOfWeek: number | null
    isPaused: boolean
  }) => void
}

/** ISO weekdays — Monday is 1, matching the API and PostgreSQL's `isodow`. */
const DAYS: ReadonlyArray<{ value: string; label: string }> = [
  { value: '1', label: 'Monday' },
  { value: '2', label: 'Tuesday' },
  { value: '3', label: 'Wednesday' },
  { value: '4', label: 'Thursday' },
  { value: '5', label: 'Friday' },
  { value: '6', label: 'Saturday' },
  { value: '7', label: 'Sunday' },
]

const FREQUENCIES: ReadonlyArray<{ value: CheckinFrequencyValue; label: string }> = [
  { value: 'weekly', label: 'Weekly' },
  { value: 'fortnightly', label: 'Every two weeks' },
  { value: 'monthly', label: 'Every four weeks' },
]

export function ClientCheckinPanel({
  schedule,
  loading = false,
  error = null,
  busy = false,
  onSave,
}: ClientCheckinPanelProps) {
  const [frequency, setFrequency] = useState<CheckinFrequencyValue>(
    schedule?.frequency ?? 'weekly',
  )
  const [dayOfWeek, setDayOfWeek] = useState<string>(
    schedule?.dayOfWeek !== null && schedule?.dayOfWeek !== undefined
      ? String(schedule.dayOfWeek)
      : '',
  )

  const paused = schedule?.isPaused ?? false

  return (
    <Card>
      <CardHeader
        title="Check-ins"
        description="A recurring nudge asking the client to log their weight and how the week went."
      />
      <CardBody>
        {loading ? (
          <Spinner label="Loading check-in schedule" />
        ) : (
          <>
            {error ? <p role="alert">{error}</p> : null}

            <FormField label="How often">
              <Select
                value={frequency}
                onChange={(event) =>
                  setFrequency(event.currentTarget.value as CheckinFrequencyValue)
                }
              >
                {FREQUENCIES.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </Select>
            </FormField>

            {/* ⚠️ Offered for a weekly cadence only. The API ignores it for
              * longer ones — "every two weeks, but on a Tuesday" is either 14
              * days or 21 — and a control that silently did nothing would be
              * worse than its absence. */}
            {frequency === 'weekly' ? (
              <FormField label="Day" hint="Defaults to the day this client became active.">
                <Select
                  value={dayOfWeek}
                  onChange={(event) => setDayOfWeek(event.currentTarget.value)}
                >
                  <option value="">The day they became active</option>
                  {DAYS.map((day) => (
                    <option key={day.value} value={day.value}>
                      {day.label}
                    </option>
                  ))}
                </Select>
              </FormField>
            ) : null}

            {schedule?.nextDueOn ? <p>Next check-in: {schedule.nextDueOn}</p> : null}

            <Button
              disabled={busy}
              onClick={() =>
                onSave({
                  frequency,
                  dayOfWeek: dayOfWeek === '' ? null : Number(dayOfWeek),
                  isPaused: paused,
                })
              }
            >
              Save schedule
            </Button>

            {/* 🔒 FR-M8-024 — the pause, and the sentence that stops it being
              * mistaken for a lifecycle change. Pausing also cancels what was
              * already queued, which the copy states rather than leaving a
              * practitioner to discover from a message that still arrived. */}
            <Button
              variant="secondary"
              disabled={busy}
              onClick={() =>
                onSave({
                  frequency,
                  dayOfWeek: dayOfWeek === '' ? null : Number(dayOfWeek),
                  isPaused: !paused,
                })
              }
            >
              {paused ? 'Resume check-ins' : 'Pause check-ins'}
            </Button>
            <p>
              {paused
                ? 'Check-ins are paused. The client stays active; nothing about their stage has changed.'
                : 'Pausing stops check-ins and cancels any already queued. It does not change the client’s stage.'}
            </p>
          </>
        )}
      </CardBody>
    </Card>
  )
}
