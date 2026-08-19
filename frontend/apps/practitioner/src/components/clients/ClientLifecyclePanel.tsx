/**
 * The lifecycle panel on the client detail screen — stage, archive, restore.
 *
 * 🔒 **Renders and reports; it does not fetch or decide** (Arch §4.4, NFR-068).
 * Every action arrives as a callback and every piece of state as a prop, which
 * is what `check_boundaries.py` R8 enforces for anything under `components/`.
 *
 * 🔒 NFR-012 — a stage change is **two interactions** from here: open the
 * select, choose a stage. The confirmation step is deliberately absent for an
 * ordinary move, and present for archiving (NFR-065), because one is reversible
 * bookkeeping and the other removes a client from every list.
 */

import { useState } from 'react'
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  ConfirmDialog,
  FormField,
  Select,
  Textarea,
} from '@wellnesscrm/design-system'
import { STAGE_LABEL, STAGE_MEANING, STAGE_TONE, type StageValue } from './stages'

export interface ClientLifecyclePanelProps {
  stage: StageValue
  isArchived: boolean
  fullName: string
  /** The stages offered in the dropdown. `archived` is never among them. */
  selectableStages: readonly StageValue[]
  onChangeStage: (toStage: StageValue, reason: string) => void
  onArchive: () => void
  onRestore: () => void
  /** Disables the controls while a request is in flight. */
  busy?: boolean
}

export function ClientLifecyclePanel({
  stage,
  isArchived,
  fullName,
  selectableStages,
  onChangeStage,
  onArchive,
  onRestore,
  busy = false,
}: ClientLifecyclePanelProps) {
  const [pendingStage, setPendingStage] = useState<StageValue | ''>('')
  const [reason, setReason] = useState('')
  const [confirmingArchive, setConfirmingArchive] = useState(false)

  // 🔒 An archived client is not a lifecycle participant (FR-M1-010): the API
  // refuses a stage change on one, so offering the control would be an
  // invitation to a guaranteed error.
  const canChangeStage = !isArchived

  function submitStage() {
    if (pendingStage === '' || pendingStage === stage) return
    onChangeStage(pendingStage, reason)
    setPendingStage('')
    setReason('')
  }

  return (
    <Card>
      <CardHeader
        title="Lifecycle"
        actions={
          <Badge tone={isArchived ? 'neutral' : STAGE_TONE[stage]}>
            {isArchived ? `${STAGE_LABEL[stage]} · archived` : STAGE_LABEL[stage]}
          </Badge>
        }
      />
      <CardBody>
        <p data-testid="stage-meaning">{STAGE_MEANING[isArchived ? 'archived' : stage]}</p>

        {canChangeStage && (
          <>
            <FormField
              label="Move to stage"
              hint={pendingStage === '' ? undefined : STAGE_MEANING[pendingStage]}
            >
              <Select
                value={pendingStage}
                disabled={busy}
                onChange={(event) => setPendingStage(event.target.value as StageValue | '')}
              >
                <option value="">Choose a stage…</option>
                {selectableStages
                  .filter((candidate) => candidate !== stage)
                  .map((candidate) => (
                    <option key={candidate} value={candidate}>
                      {STAGE_LABEL[candidate]}
                    </option>
                  ))}
              </Select>
            </FormField>

            <FormField
              label="Reason (optional)"
              hint="Recorded against this change so the history explains itself later."
            >
              <Textarea
                value={reason}
                rows={2}
                maxLength={500}
                disabled={busy}
                onChange={(event) => setReason(event.target.value)}
              />
            </FormField>

            <Button onClick={submitStage} disabled={pendingStage === ''} loading={busy}>
              Update stage
            </Button>
          </>
        )}

        <div>
          {isArchived ? (
            <Button variant="secondary" onClick={onRestore} loading={busy}>
              Restore client
            </Button>
          ) : (
            <Button variant="secondary" onClick={() => setConfirmingArchive(true)} disabled={busy}>
              Archive client
            </Button>
          )}
        </div>
      </CardBody>

      {/* 🔒 NFR-065 — confirmation must state what is lost. Archiving deletes
       * nothing, and saying so is what stops a practitioner hesitating over a
       * reversible action, or worse, avoiding it and keeping a churned client
       * `active` where they are still billed for them. */}
      <ConfirmDialog
        open={confirmingArchive}
        onCancel={() => setConfirmingArchive(false)}
        onConfirm={() => {
          setConfirmingArchive(false)
          onArchive()
        }}
        title={`Archive ${fullName}?`}
        consequence={
          countsTowardsLimitCopy(stage) +
          ' They will disappear from your client list and stop receiving plans, ' +
          'messages and check-ins. Nothing is deleted, and you can restore them at any time.'
        }
        confirmLabel="Archive client"
        tone="primary"
        busy={busy}
      />
    </Card>
  )
}

/**
 * The billing half of the archive consequence.
 *
 * 🔒 Stated first because it is the part that changes what the practitioner
 * pays. M1.5 requires the limit to be predictable without them thinking about
 * it, which means the moment a slot is freed should be visible at the point of
 * action rather than discovered on an invoice.
 */
function countsTowardsLimitCopy(stage: StageValue): string {
  return stage === 'active'
    ? 'This frees one active-client slot on your plan.'
    : 'This client does not count towards your plan, so your usage will not change.'
}
