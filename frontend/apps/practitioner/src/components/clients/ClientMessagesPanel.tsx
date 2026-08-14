/**
 * A client's messages — FR-M8-011 (what was sent) and FR-M8-028 (what is next).
 *
 * 🔒 Renders and reports (Arch §4.4). Every entry is a prop and every action a
 * callback; R8 fails the build if anything here reaches the API.
 *
 * 🔒 **"Queued" and "sent" are different words on purpose.** A pending message
 * has not been through suppression yet — the engine decides at dispatch, against
 * live state — so a panel that said "will be sent on Friday" would be promising
 * something the product deliberately does not promise (DB §11.5).
 *
 * ⚠️ **A failure reason is operator-facing text from a provider.** It is shown
 * to the practitioner, who needs it to act, and must never be forwarded to a
 * client.
 */

import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  Spinner,
} from '@wellnesscrm/design-system'
import type { BadgeTone } from '@wellnesscrm/design-system'

export interface MessageHistoryView {
  id: string
  templateCode: string
  transport: string
  status: string
  recipientAddress: string
  attemptNumber: number
  failureReason: string | null
  createdAt: string
}

export interface PendingMessageView {
  id: string
  templateCode: string
  scheduledFor: string
  /** 🔒 AC-M8-006 — set when quiet hours moved this message. */
  deferredFrom: string | null
  preview: string
}

export interface ClientMessagesPanelProps {
  history: readonly MessageHistoryView[]
  pending: readonly PendingMessageView[]
  loading?: boolean
  loadingMore?: boolean
  hasMore?: boolean
  historyError?: string | null
  pendingError?: string | null
  busy?: boolean
  onLoadMore: () => void
  onCancel: (scheduledMessageId: string) => void
}

/**
 * How a delivery status reads.
 *
 * 🔒 `failed` and `rejected` are both "Failed" to a practitioner: the
 * distinction is the provider's, and acting on it is the same either way. The
 * *reason* is what differs, and that is rendered separately.
 */
const STATUS_LABELS: Record<string, string> = {
  queued: 'Queued',
  sent: 'Sent',
  delivered: 'Delivered',
  read: 'Read',
  failed: 'Failed',
  rejected: 'Failed',
}

const STATUS_TONES: Record<string, BadgeTone> = {
  queued: 'neutral',
  sent: 'info',
  delivered: 'success',
  read: 'success',
  failed: 'danger',
  rejected: 'danger',
}

/** How a message type reads. ⚠️ A code is not a label — `checkin_nudge` on a
 * practitioner's screen is jargon leaking out of the database. */
const TEMPLATE_LABELS: Record<string, string> = {
  plan_delivered: 'Plan delivered',
  appointment_confirmed: 'Appointment confirmed',
  appointment_reminder: 'Appointment reminder',
  checkin_nudge: 'Check-in',
  assessment_invitation: 'Assessment invitation',
  lead_acknowledgement: 'Enquiry acknowledgement',
  lead_notification: 'New enquiry notice',
  magic_link: 'Portal link',
}

function templateLabel(code: string): string {
  return TEMPLATE_LABELS[code] ?? code
}

function when(value: string): string {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString()
}

export function ClientMessagesPanel({
  history,
  pending,
  loading = false,
  loadingMore = false,
  hasMore = false,
  historyError = null,
  pendingError = null,
  busy = false,
  onLoadMore,
  onCancel,
}: ClientMessagesPanelProps) {
  return (
    <Card>
      <CardHeader
        title="Messages"
        description="What has been sent to this client, and what is queued to go."
      />
      <CardBody>
        {loading ? (
          <Spinner label="Loading messages" />
        ) : (
          <>
            <section aria-labelledby="pending-messages-heading">
              <h3 id="pending-messages-heading">Scheduled</h3>
              {pendingError ? <p role="alert">{pendingError}</p> : null}
              {pending.length === 0 ? (
                <p>Nothing is queued for this client.</p>
              ) : (
                <ul>
                  {pending.map((message) => (
                    <li key={message.id}>
                      <strong>{templateLabel(message.templateCode)}</strong>{' '}
                      <span>queued for {when(message.scheduledFor)}</span>
                      {/* 🔒 AC-M8-006 — a deferral is stated rather than hidden.
                        * A practitioner who scheduled something for 09:00 and
                        * sees 08:00 the next day deserves to know why. */}
                      {message.deferredFrom ? (
                        <Badge tone="neutral">Moved out of quiet hours</Badge>
                      ) : null}
                      {/* 🔒 FR-M8-028 — the rendered body, because "one message
                        * is scheduled" is not something a practitioner can act
                        * on. What it says is. */}
                      <p>{message.preview}</p>
                      <Button
                        variant="secondary"
                        size="sm"
                        disabled={busy}
                        onClick={() => onCancel(message.id)}
                      >
                        Cancel
                      </Button>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section aria-labelledby="message-history-heading">
              <h3 id="message-history-heading">History</h3>
              {historyError ? <p role="alert">{historyError}</p> : null}
              {history.length === 0 ? (
                <EmptyState
                  title="No messages yet"
                  description="Delivered plans, check-ins and reminders appear here with what happened to each."
                />
              ) : (
                <ul>
                  {history.map((entry) => (
                    <li key={entry.id}>
                      <Badge tone={STATUS_TONES[entry.status] ?? 'neutral'}>
                        {STATUS_LABELS[entry.status] ?? entry.status}
                      </Badge>{' '}
                      <strong>{templateLabel(entry.templateCode)}</strong>{' '}
                      <span>
                        {entry.transport} · {when(entry.createdAt)}
                      </span>
                      {/* ⚠️ Attempt number is shown only when it is not the
                        * first: "attempt 1" on every row is noise, while
                        * "attempt 3" is the thing worth noticing. */}
                      {entry.attemptNumber > 1 ? (
                        <span> · attempt {entry.attemptNumber}</span>
                      ) : null}
                      {entry.failureReason ? <p>{entry.failureReason}</p> : null}
                    </li>
                  ))}
                </ul>
              )}
              {hasMore ? (
                <Button variant="secondary" disabled={loadingMore} onClick={onLoadMore}>
                  {loadingMore ? 'Loading…' : 'Load more'}
                </Button>
              ) : null}
            </section>
          </>
        )}
      </CardBody>
    </Card>
  )
}
