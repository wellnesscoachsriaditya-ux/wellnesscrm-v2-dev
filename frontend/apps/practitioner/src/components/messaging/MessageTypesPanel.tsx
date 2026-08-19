/**
 * The practice's message types — FR-M8-026 (preview) and FR-M8-027 (control).
 *
 * 🔒 Renders and reports (Arch §4.4).
 *
 * 🔒 **The preview body comes from the server**, rendered by the same function
 * the dispatch path uses. This component displays it and never assembles one:
 * FR-M8-026 promises the practitioner sees what their client will receive, and a
 * body built here would be a promise about different code.
 *
 * 🔒 **An essential message type has no toggle at all.** Magic links carry portal
 * access; a control that looked available and then refused would teach a
 * practitioner that the settings screen lies to them.
 */

import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  Modal,
  Spinner,
} from '@wellnesscrm/design-system'

export interface MessageTypeView {
  code: string
  label: string
  description: string
  isEssential: boolean
  canDisable: boolean
  isEnabled: boolean
  /** 🔒 EC-M8-03 — Meta's approval state for this template, on its own. */
  providerStatus: string
  transport: string
}

export interface MessagePreviewView {
  templateCode: string
  body: string
  isSample: boolean
}

export interface MessageTypesPanelProps {
  types: readonly MessageTypeView[]
  preview: MessagePreviewView | null
  previewing: string | null
  loading?: boolean
  error?: string | null
  busy?: boolean
  onPreview: (code: string) => void
  onClosePreview: () => void
  onToggle: (code: string, enabled: boolean) => void
}

export function MessageTypesPanel({
  types,
  preview,
  previewing,
  loading = false,
  error = null,
  busy = false,
  onPreview,
  onClosePreview,
  onToggle,
}: MessageTypesPanelProps) {
  return (
    <Card>
      <CardHeader
        title="Message types"
        description="Everything this practice sends on your behalf. Preview any of them as your client will see it."
      />
      <CardBody>
        {loading ? (
          <Spinner label="Loading message types" />
        ) : (
          <>
            {error ? <p role="alert">{error}</p> : null}
            <ul>
              {types.map((type) => (
                <li key={type.code}>
                  <strong>{type.label}</strong>
                  <p>{type.description}</p>
                  <span>Sent by {type.transport}</span>

                  {/* 🔒 EC-M8-03 — stated per type, because one revoked template
                    * pauses its own message type and nothing else. A practice-wide
                    * "WhatsApp unavailable" banner would be untrue and would send
                    * a practitioner looking in the wrong place. */}
                  {type.providerStatus !== 'approved' && type.transport === 'whatsapp' ? (
                    <Badge tone="warning">Awaiting WhatsApp approval</Badge>
                  ) : null}

                  <Button variant="secondary" size="sm" onClick={() => onPreview(type.code)}>
                    Preview
                  </Button>

                  {type.canDisable ? (
                    <Button
                      variant="secondary"
                      size="sm"
                      disabled={busy}
                      aria-pressed={!type.isEnabled}
                      onClick={() => onToggle(type.code, !type.isEnabled)}
                    >
                      {type.isEnabled ? 'Turn off' : 'Turn on'}
                    </Button>
                  ) : (
                    <span>
                      {type.isEssential
                        ? 'Always on — this carries portal access.'
                        : 'Always on — part of the core workflow.'}
                    </span>
                  )}
                </li>
              ))}
            </ul>

            <Modal
              open={previewing !== null}
              onClose={onClosePreview}
              title="As your client will see it"
            >
              {preview ? (
                <>
                  {/* ⚠️ Said plainly. Without it a practitioner could reasonably
                    * believe they were looking at a real client's message. */}
                  {preview.isSample ? <p>Example values are shown.</p> : null}
                  <p>{preview.body}</p>
                </>
              ) : (
                <Spinner label="Loading preview" />
              )}
            </Modal>
          </>
        )}
      </CardBody>
    </Card>
  )
}
