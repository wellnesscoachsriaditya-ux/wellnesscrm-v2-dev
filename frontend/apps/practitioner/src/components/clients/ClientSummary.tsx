/**
 * The identity block on the client detail screen.
 *
 * 🔒 Renders only. No fetching, no derivation (Arch §4.4, NFR-068) —
 * `is_minor` in particular is **server-supplied**, never computed here from a
 * date of birth. It gates guardian consent (FR-M0-028), and a second
 * implementation in the browser is a second thing that can be wrong about
 * whether somebody is a child.
 */

import { Badge, Card, CardBody, CardHeader } from '@wellnesscrm/design-system'

export interface ClientSummaryProps {
  fullName: string
  mobile: string | null
  email: string | null
  city: string | null
  /** `null` means unknown — the enquiry form does not ask for a birth date. */
  isMinor: boolean | null
  activatedAt: string | null
}

export function ClientSummary({
  fullName,
  mobile,
  email,
  city,
  isMinor,
  activatedAt,
}: ClientSummaryProps) {
  return (
    <Card>
      <CardHeader
        title={fullName}
        actions={isMinor === true ? <Badge tone="warning">Minor</Badge> : undefined}
      />
      <CardBody>
        <dl>
          <dt>Mobile</dt>
          {/* 🔒 EC-M1-08 — a client with only an email is legitimate and loses
           * WhatsApp delivery. The system must say so rather than leave a blank
           * that reads as missing data. */}
          <dd>{mobile ?? 'None on file — WhatsApp delivery is unavailable'}</dd>

          <dt>Email</dt>
          <dd>{email ?? 'None on file'}</dd>

          <dt>City</dt>
          <dd>{city ?? 'Not recorded'}</dd>

          {activatedAt !== null && (
            <>
              <dt>First activated</dt>
              {/* The check-in anchor (FR-M8-023). Shown because it is the date a
               * returning client's history is measured from, and it does not
               * reset on reactivation. */}
              <dd>{new Date(activatedAt).toLocaleDateString()}</dd>
            </>
          )}
        </dl>
      </CardBody>
    </Card>
  )
}
