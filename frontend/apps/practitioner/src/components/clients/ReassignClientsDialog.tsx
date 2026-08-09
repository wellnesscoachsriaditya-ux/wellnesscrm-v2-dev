/**
 * Hand several clients to a colleague at once — EC-M1-04.
 *
 * 🔒 Renders and reports (Arch §4.4). R8 fails the build if this reaches the API.
 *
 * 🔒 **NFR-065 — the consequence is stated before the act.** Reassignment moves
 * clients out of the practitioner's own list, and EC-M1-04 makes it
 * all-or-nothing: either every selected client moves or none does. Both facts are
 * on screen, because "why did 40 of my clients disappear" is the support ticket
 * this wording exists to prevent.
 *
 * ⚠️ **`Modal`, not `ConfirmDialog`.** This asks a question *and* collects a
 * value; `ConfirmDialog` renders no children, so an input passed to it would
 * silently not appear. The consequence text that makes `ConfirmDialog` worth
 * using is reproduced here deliberately rather than lost.
 *
 * ⚠️ **A user id, not a name picker.** `ClientAccessPanel` documents the same
 * constraint: `GrantResponse` carries identifiers only (NFR-033) and resolving
 * them is "the caller's job through the team endpoint" — which does not exist
 * yet. An id field is honest about that; an autocomplete over a non-existent
 * endpoint would simply be empty.
 */

import { useEffect, useState } from 'react'
import { Button, FormField, Input, Modal } from '@wellnesscrm/design-system'

export interface ReassignClientsDialogProps {
  open: boolean
  /** How many clients will move. Drives the wording, which is never "0". */
  count: number
  busy?: boolean
  /** 🔒 A refusal from the API, rendered in the dialog the practitioner is in. */
  error?: string | null
  onCancel: () => void
  onConfirm: (ownerUserId: string) => void
}

export function ReassignClientsDialog({
  open,
  count,
  busy = false,
  error = null,
  onCancel,
  onConfirm,
}: ReassignClientsDialogProps) {
  const [ownerUserId, setOwnerUserId] = useState('')

  // ⚠️ Cleared on close, not on confirm. A cancelled dialog that reopens holding
  // the previous id would let a mistimed second click reassign to someone the
  // practitioner had already decided against.
  useEffect(() => {
    if (!open) setOwnerUserId('')
  }, [open])

  const trimmed = ownerUserId.trim()

  function submit() {
    // 🔒 Guarded as well as disabled, because the form can also be submitted
    // with Enter — a keyboard path that bypassed the button's `disabled` would
    // otherwise send an empty owner to the API.
    if (trimmed === '' || busy) return
    onConfirm(trimmed)
  }

  return (
    <Modal
      open={open}
      onClose={onCancel}
      title={count === 1 ? 'Reassign this client?' : `Reassign ${count} clients?`}
      size="lg"
      footer={
        <>
          {/* ⚠️ Cancel first, matching `ConfirmDialog`: `Modal` focuses the first
            * focusable element, and someone dismissing a dialog reflexively with
            * Enter should not move a caseload. */}
          <Button variant="secondary" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={trimmed === ''} loading={busy}>
            {count === 1 ? 'Reassign client' : `Reassign ${count} clients`}
          </Button>
        </>
      }
    >
      {/* 🔒 The refusal, above the field that caused it. `role="alert"` so a
        * screen reader hears it without hunting — the dialog stays open on
        * failure precisely so this can be read and corrected. */}
      {error !== null && <p role="alert">{error}</p>}

      {/* 🔒 NFR-065 — what will be lost, stated plainly. The all-or-nothing rule
        * is the half a practitioner cannot infer from the UI, so it is spelled
        * out rather than implied. */}
      <p>
        {count === 1
          ? 'The new owner takes over this client. It leaves your list unless you are given access to it.'
          : `All ${count} move together — if any one of them cannot be reassigned, none of them are. They leave your list unless you are given access to them.`}
      </p>

      <form
        onSubmit={(event) => {
          event.preventDefault()
          submit()
        }}
      >
        <FormField
          label="New owner's user id"
          hint="The practitioner who will take these clients on."
        >
          <Input
            value={ownerUserId}
            disabled={busy}
            onChange={(event) => setOwnerUserId(event.target.value)}
          />
        </FormField>
      </form>
    </Modal>
  )
}
