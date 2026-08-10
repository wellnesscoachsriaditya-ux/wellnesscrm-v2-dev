/**
 * The shareable enquiry link, and the switch that turns it off — FR-M2-001.
 *
 * 🔒 Renders and reports (Arch §4.4). R8 fails the build if this reaches the API.
 *
 * 🔒 **US-M2-01 is the whole reason this panel exists**: "a link I can put in my
 * Instagram bio". The copy button is the feature — a URL a practitioner has to
 * select by hand on a phone is one they will not share.
 *
 * ⚠️ **Deactivating is presented as "pause", not "delete".** It is an UPDATE, and
 * migration 0015 revokes DELETE outright: every submission the form ever took
 * stays intact (EC-M2-07's "no data loss"). Wording it as removal would suggest
 * otherwise.
 */

import { useState } from 'react'
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  FormField,
  Input,
} from '@wellnesscrm/design-system'

export interface EnquiryFormPanelProps {
  /** The public URL, assembled by the screen from the tenant's slug. */
  shareUrl: string
  title: string
  isActive: boolean
  busy?: boolean
  error?: string | null
  onToggleActive: (isActive: boolean) => void
}

export function EnquiryFormPanel({
  shareUrl,
  title,
  isActive,
  busy = false,
  error = null,
  onToggleActive,
}: EnquiryFormPanelProps) {
  /**
   * 🔒 Confirmation is local state, not a toast.
   *
   * ⚠️ `navigator.clipboard` can reject — an insecure origin, a permission
   * refusal, an unsupported browser — and a copy button that silently does
   * nothing is worse than one that says it failed, because the practitioner
   * pastes an empty clipboard into their Instagram bio.
   */
  const [copied, setCopied] = useState(false)
  const [copyFailed, setCopyFailed] = useState(false)

  function copy() {
    setCopyFailed(false)
    navigator.clipboard
      .writeText(shareUrl)
      .then(() => {
        setCopied(true)
        window.setTimeout(() => setCopied(false), 2000)
      })
      .catch(() => setCopyFailed(true))
  }

  return (
    <Card>
      <CardHeader
        title="Your enquiry form"
        actions={
          isActive ? (
            <Badge tone="success">Accepting enquiries</Badge>
          ) : (
            <Badge tone="neutral">Paused</Badge>
          )
        }
      />
      <CardBody>
        <p>{title}</p>

        {/* 🔒 Read-only rather than disabled: a disabled input cannot be
          * focused or selected, so a practitioner who prefers to copy by hand —
          * or who hit the clipboard failure below — would have no way to.
          *
          * ⚠️ Wrapped in `FormField` because `Input` deliberately renders no
          * label of its own: NFR-062's programmatic association is `FormField`'s
          * job, and routing every field through it is what makes that automatic
          * rather than something each screen remembers. */}
        <FormField label="Shareable link">
          <Input value={shareUrl} readOnly onFocus={(event) => event.target.select()} />
        </FormField>

        <Button variant="secondary" onClick={copy}>
          {copied ? 'Copied' : 'Copy link'}
        </Button>

        {copyFailed && (
          <p role="alert">
            Your browser would not let us copy it. Select the link above and copy it manually.
          </p>
        )}

        {/* ⚠️ The label states what the *click* does, not the current state. A
          * button reading "Accepting enquiries" leaves the practitioner guessing
          * whether it describes or acts. */}
        <Button variant="ghost" loading={busy} onClick={() => onToggleActive(!isActive)}>
          {isActive ? 'Pause enquiries' : 'Start accepting enquiries'}
        </Button>

        {!isActive && (
          <p>
            Your link shows a “not available” page while paused. Enquiries already received are
            unaffected.
          </p>
        )}

        {error !== null && <p role="alert">{error}</p>}
      </CardBody>
    </Card>
  )
}
