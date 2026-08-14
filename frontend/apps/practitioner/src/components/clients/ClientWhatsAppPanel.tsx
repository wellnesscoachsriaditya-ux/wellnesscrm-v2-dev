/**
 * Message this client from your own WhatsApp — the manual channel.
 *
 * 🔒 **Separate from the engine, and the copy has to keep it separate.** The
 * messaging panel below shows what *WellnessCRM* sent and what became of it.
 * This opens a conversation in the practitioner's own WhatsApp, on their own
 * number; the CRM records nothing, because it cannot know whether they pressed
 * send. Anything else would put a claim in the delivery log that is not true
 * (FR-M8-003), and the log's entire value is that its claims are.
 *
 * 🔒 Needs no Meta Business Verification, no Cloud API token and no approved
 * template. It works today, and it keeps working when the API is unavailable.
 *
 * 🔒 **Renders and reports** (Arch §4.4). The link and the refusal reason arrive
 * as props from `useClickToChat`; R8 fails the build if this file decides
 * either for itself.
 *
 * ⚠️ An anchor, not a button with an `onClick`. A real `href` is what lets a
 * practitioner long-press to open in a new window, and what makes the target
 * visible before they commit to it. `rel="noopener noreferrer"` because the
 * opened page must not reach `window.opener`.
 */

import { Card, CardBody, CardHeader, FormField, Textarea } from '@wellnesscrm/design-system'

/** 🔒 WhatsApp's own ceiling for a prefilled body — mirrored from
 * `features/messaging/clickToChat` as a plain number, because a component may
 * not import from `features/` (R8). The link builder truncates at the same
 * value; this only stops the textarea accepting what would be cut. */
const MAX_PREFILL_LENGTH = 4096

export interface ClientWhatsAppPanelProps {
  clientName: string
  message: string
  /** The link to open, or `null` when the client has no usable number. */
  href: string | null
  /** Why there is no link. States the problem and the fix (NFR-063). */
  reason: string | null
  onMessageChange: (value: string) => void
}

export function ClientWhatsAppPanel({
  clientName,
  message,
  href,
  reason,
  onMessageChange,
}: ClientWhatsAppPanelProps) {
  return (
    <Card>
      <CardHeader
        title="Message on WhatsApp"
        description="Opens your own WhatsApp with this client. You press send — WellnessCRM does not send it and does not record it."
      />
      <CardBody>
        <FormField
          label="Message"
          hint="Optional. WhatsApp opens with this ready to send, and you can edit it there."
        >
          <Textarea
            value={message}
            rows={3}
            maxLength={MAX_PREFILL_LENGTH}
            onChange={(event) => onMessageChange(event.target.value)}
          />
        </FormField>

        {href === null ? (
          // 🔒 NFR-063 — the reason states the problem and the fix. A disabled
          // control with no explanation would leave a practitioner unable to act.
          <p role="alert">{reason}</p>
        ) : (
          <a href={href} target="_blank" rel="noopener noreferrer">
            {`Open WhatsApp with ${clientName}`}
          </a>
        )}

        {/* ⚠️ Said once, plainly. A practitioner who believed this was tracked
          * would stop checking whether their client replied. */}
        <p>
          Messages you send this way are not in the message history below — that
          shows what WellnessCRM sent on your behalf.
        </p>
      </CardBody>
    </Card>
  )
}

/**
 * The same action, as an inline link beside one message.
 *
 * 🔒 Where it earns its place: a delivery that *failed*. The practitioner
 * already knows what the message was meant to say, and the alternative is
 * retyping it into WhatsApp by hand — which is exactly the manual work this
 * product exists to remove.
 *
 * ⚠️ Takes a ready `href`, not a number and a body. Building one here would be
 * the same R8 violation the panel above avoids.
 */
export function WhatsAppFallbackLink({
  href,
  label = 'Send this by hand',
}: {
  href: string | null
  label?: string
}) {
  if (href === null) return null

  return (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {label}
    </a>
  )
}
