/**
 * The manual WhatsApp conversation, as state a screen can hand to a component.
 *
 * 🔒 **This hook exists because of R8** (Arch §4.4, NFR-068): a component may
 * render and hold local UI state, but it may not reach into `features/` to
 * decide something. The draft message lives here with the rule that turns it
 * into a link, and `ClientWhatsAppPanel` receives both as props — which is what
 * keeps the panel a renderer and this the decision.
 *
 * ⚠️ No request is made and no credential is read. See `clickToChat.ts` for why
 * that is the whole point of this path.
 */

import { useCallback, useMemo, useState } from 'react'
import { whatsAppChatLink } from './clickToChat'

export interface ClickToChatState {
  /** The draft body, as typed. */
  message: string
  setMessage: (value: string) => void
  /** The link to open, or `null` when the client has no usable number. */
  href: string | null
  /** Why there is no link — states the problem and the fix (NFR-063). */
  reason: string | null
  /**
   * A link for a body this hook does not hold — a failed message the
   * practitioner wants to send by hand.
   *
   * 🔒 Handed to `ClientMessagesPanel` as a callback so that panel never imports
   * the rule either.
   */
  linkFor: (message: string) => string | null
}

export function useClickToChat(mobile: string | null): ClickToChatState {
  const [message, setMessage] = useState('')

  const { href, reason } = useMemo(() => whatsAppChatLink(mobile, message), [mobile, message])

  const linkFor = useCallback(
    (body: string) => whatsAppChatLink(mobile, body).href,
    [mobile],
  )

  return { message, setMessage, href, reason, linkFor }
}
