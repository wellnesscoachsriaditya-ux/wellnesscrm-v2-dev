/**
 * WhatsApp click-to-chat — the practitioner's *own* WhatsApp, by hand.
 *
 * 🔒 **This is not the messaging engine, and the distinction is the point.**
 * M8's WhatsApp transport sends *on the practice's behalf*, from a verified
 * Business number, and records a delivery attempt. This opens the practitioner's
 * personal WhatsApp with a conversation ready to send — the coach presses send,
 * the message leaves their phone, and WellnessCRM never touches it.
 *
 * Consequences, each deliberate:
 *
 * * **No credentials.** No Meta Business Verification, no Cloud API token, no
 *   template approval. It is a URL. It keeps working when the API is
 *   unavailable, unapproved, or was never configured — which is the state this
 *   product ships in today.
 * * 🔒 **No delivery record, and no status.** Nothing here writes to
 *   `message_dispatches`. We cannot know whether the practitioner pressed send,
 *   so any row we wrote would be a claim the delivery log is not entitled to
 *   make — and the delivery log's whole value is that its claims are true
 *   (FR-M8-003).
 * * ⚠️ **It is not automation.** EC-M8-07 already tells practitioners that
 *   replies arrive in their own WhatsApp; this is the outbound half of that same
 *   manual channel.
 *
 * ⚠️ **The number is never guessed.** A stored number is E.164 by construction
 * (`ck_clients__mobile_e164`), because `kernel.clients.normalise_mobile` applied
 * the country code when the client was created. Anything that does not look like
 * E.164 here is refused rather than repaired: prefixing a bare ten-digit number
 * with +91 would be a second, weaker copy of that rule, and when it guessed
 * wrong it would open a conversation with a stranger.
 */

/**
 * The click-to-chat host.
 *
 * 🔒 `wa.me` rather than `api.whatsapp.com/send`. Both are official; `wa.me` is
 * the one that resolves to the installed app on a phone and to WhatsApp Web on a
 * desktop, which is exactly the ambiguity a practitioner should not have to
 * resolve. The `api.whatsapp.com` form is documented for web-only flows.
 */
const CLICK_TO_CHAT_HOST = 'https://wa.me'

/** Separators a person or a paste might introduce. Removed, never interpreted. */
const SEPARATORS = /[\s\-().]/g

/**
 * E.164 as the database stores it: a `+`, a non-zero country digit, then 7–14
 * more. The same shape as `ck_clients__mobile_e164`, restated here because this
 * module must be able to refuse without a round-trip.
 */
const E164 = /^\+[1-9]\d{7,14}$/

/** 🔒 WhatsApp's own limit for a prefilled body. Longer is truncated by the
 * client silently, so it is bounded here where it can be said out loud. */
export const MAX_PREFILL_LENGTH = 4096

export interface ClickToChat {
  /** The URL to open, or `null` when the client has no usable number. */
  href: string | null
  /**
   * Why there is no link, for the UI to show. `null` when there is one.
   *
   * 🔒 NFR-063 — states the problem *and* the fix. "Unavailable" alone would
   * leave a practitioner unable to act on it.
   */
  reason: string | null
}

/**
 * Digits WhatsApp will accept, or `null`.
 *
 * ⚠️ Exported for its tests: this is the half that decides whether a
 * conversation opens with the right person.
 */
export function toWhatsAppNumber(mobile: string | null | undefined): string | null {
  if (!mobile) return null

  const cleaned = mobile.replace(SEPARATORS, '')
  if (!E164.test(cleaned)) return null

  // wa.me takes the international number without the `+`.
  return cleaned.slice(1)
}

/**
 * Build a click-to-chat link for one client.
 *
 * @param mobile - The client's stored number, E.164 or absent.
 * @param message - An optional body to prefill. 🔒 URL-encoded here rather than
 *   by the caller: a newline, an ampersand or a `#` in a rendered plan message
 *   would otherwise truncate the text at the client's handset, and the
 *   practitioner would send a half-sentence without noticing.
 */
export function whatsAppChatLink(
  mobile: string | null | undefined,
  message?: string | null,
): ClickToChat {
  const number = toWhatsAppNumber(mobile)

  if (number === null) {
    return {
      href: null,
      reason: mobile
        ? 'This number is not in international format, so WhatsApp cannot open it. Edit the client and enter it with the country code.'
        : 'This client has no mobile number on file. Add one to message them on WhatsApp.',
    }
  }

  const body = (message ?? '').trim()
  if (body === '') return { href: `${CLICK_TO_CHAT_HOST}/${number}`, reason: null }

  // ⚠️ `encodeURIComponent`, not `encodeURI`: the latter leaves `&`, `+` and `#`
  // intact, and each of them ends the query value early.
  const text = encodeURIComponent(body.slice(0, MAX_PREFILL_LENGTH))
  return { href: `${CLICK_TO_CHAT_HOST}/${number}?text=${text}`, reason: null }
}
