/**
 * The Plans panel on Client 360 — the entry point into the plan builder.
 *
 * 🔒 Renders and calls back. Listing, creating and navigating are the screen's;
 * a plan-limit refusal (402) arrives as `createError` in the API's own words.
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
  Spinner,
} from '@wellnesscrm/design-system'

export interface PlanRow {
  planId: string
  title: string
  state: string | null
  versionNumber: number | null
}

interface Props {
  plans: readonly PlanRow[]
  loading: boolean
  error: string | null
  creating: boolean
  createError: string | null
  onCreate: (title: string) => void
  onOpen: (planId: string) => void
}

export function ClientPlansPanel({
  plans,
  loading,
  error,
  creating,
  createError,
  onCreate,
  onOpen,
}: Props) {
  const [title, setTitle] = useState('')

  function create() {
    const trimmed = title.trim()
    if (trimmed.length === 0) return
    onCreate(trimmed)
    setTitle('')
  }

  return (
    <Card data-testid="client-plans-panel">
      <CardHeader title="Plans" as="h2" />
      <CardBody>
        {loading ? (
          <Spinner label="Loading plans…" />
        ) : error !== null ? (
          <p role="alert" data-testid="client-plans-error">
            {error}
          </p>
        ) : plans.length === 0 ? (
          <EmptyState
            title="No plans yet"
            description="Create the client’s first diet plan and open it in the builder."
          />
        ) : (
          <ul data-testid="client-plans-list">
            {plans.map((plan) => (
              <li key={plan.planId} data-testid={`client-plan-${plan.planId}`}>
                <Button
                  variant="ghost"
                  onClick={() => onOpen(plan.planId)}
                  data-testid={`open-plan-${plan.planId}`}
                >
                  {plan.title}
                </Button>
                {plan.state !== null && <Badge tone="neutral">{plan.state}</Badge>}
                {plan.versionNumber !== null && <span> v{plan.versionNumber}</span>}
              </li>
            ))}
          </ul>
        )}

        <div data-testid="create-plan-form">
          <FormField label="New plan title">
            <Input
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="e.g. Weight loss — week 1"
              data-testid="new-plan-title-input"
            />
          </FormField>
          {createError !== null && (
            <p role="alert" data-testid="create-plan-error">
              {createError}
            </p>
          )}
          <Button
            variant="primary"
            loading={creating}
            disabled={title.trim().length === 0}
            onClick={create}
            data-testid="create-plan-button"
          >
            Create plan
          </Button>
        </div>
      </CardBody>
    </Card>
  )
}
