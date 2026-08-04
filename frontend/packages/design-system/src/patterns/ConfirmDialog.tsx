import type { ReactNode } from 'react'
import { Button } from '../primitives/Button'
import { Modal } from '../primitives/Modal'
import styles from './ConfirmDialog.module.css'

export interface ConfirmDialogProps {
  open: boolean
  /** Called on cancel, Escape, or backdrop click. */
  onCancel: () => void
  /** Called when the user confirms. */
  onConfirm: () => void
  /**
   * The question, naming the specific thing.
   *
   * "Delete Priya Sharma's plan?" — not "Are you sure?". A generic title
   * forces the user to reconstruct what they clicked from memory.
   */
  title: ReactNode
  /**
   * 🔒 NFR-065 — "MUST state what will be lost."
   *
   * Required, and named `consequence` rather than `description` because the
   * name is the specification. This prop is the requirement: a confirmation
   * that does not say what disappears is a speed bump, not informed consent.
   *
   * Good: "The plan and its 3 revisions will be permanently deleted. Priya
   * will no longer see it in her portal."
   * Bad: "This action cannot be undone."
   */
  consequence: ReactNode
  /** Label for the confirm button. Name the verb: "Delete plan", not "OK". */
  confirmLabel?: string
  cancelLabel?: string
  /** `danger` for destructive actions; `primary` for merely significant ones. */
  tone?: 'danger' | 'primary'
  /** Disables the buttons and shows progress while the action runs. */
  busy?: boolean
}

/**
 * ConfirmDialog — confirmation for destructive and irreversible actions.
 *
 * 🔒 NFR-065: "Destructive actions MUST require confirmation and MUST state
 * what will be lost."
 *
 * ⚠️ **Why this is built in S0.** Same reasoning as `EmptyState`: as a
 * component, satisfying NFR-065 is one import; as a per-screen discipline
 * across every delete button in the product, it will not happen consistently.
 * Making `consequence` a required prop means the requirement cannot be
 * forgotten — TypeScript refuses to compile a confirmation that does not say
 * what is lost.
 *
 * Built on `Modal`, so it inherits the focus trap, Escape handling and focus
 * restoration rather than reimplementing them.
 *
 * ⚠️ **Cancel is the default focus.** `Modal` focuses the first focusable
 * element, and Cancel is rendered first for exactly this reason: someone
 * dismissing a dialog reflexively with Enter should not destroy data.
 */
export function ConfirmDialog({
  open,
  onCancel,
  onConfirm,
  title,
  consequence,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  tone = 'danger',
  busy = false,
}: ConfirmDialogProps) {
  return (
    <Modal
      open={open}
      onClose={onCancel}
      title={title}
      size="sm"
      // While the action is running, dismissal would leave the user unsure
      // whether it completed.
      dismissible={!busy}
      footer={
        <>
          <Button variant="secondary" onClick={onCancel} disabled={busy}>
            {cancelLabel}
          </Button>
          <Button variant={tone} onClick={onConfirm} loading={busy}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      <p className={styles.consequence}>{consequence}</p>
    </Modal>
  )
}
