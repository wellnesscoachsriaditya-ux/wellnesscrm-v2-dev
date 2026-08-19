/**
 * Messages — the practice's messaging controls (PRD M8, S5).
 *
 * 🔒 The screen composes; the hook fetches; the components render (Arch §4.4).
 *
 * ⚠️ **This is not an inbox.** MVP sends; it does not receive — a client's reply
 * reaches the practitioner's own WhatsApp (EC-M8-07), and the two-way inbox is
 * Phase 2 (FR-M8-030). The copy says so, because a screen called "Messages" that
 * showed no incoming messages would read as broken rather than as unbuilt.
 */

import { PageHeader, Spinner } from '@wellnesscrm/design-system'
import {
  MessageTypesPanel,
  type MessagePreviewView,
  type MessageTypeView,
} from '../components/messaging/MessageTypesPanel'
import {
  MessagingControlsPanel,
  type DeliveryFailureView,
} from '../components/messaging/MessagingControlsPanel'
import { useMessageSettings } from '../features/messaging/useMessageSettings'
import type { MessagePreference, MessageTemplate } from '../features/messaging/messagingApi'

/**
 * What each message type is *for*, in the practitioner's language.
 *
 * ⚠️ Held here rather than served: a template's code is a wire identifier and
 * its body is the client's copy — neither is a description of when it fires, and
 * that is what a practitioner deciding whether to turn it off needs.
 */
const DESCRIPTIONS: Record<string, { label: string; description: string }> = {
  plan_delivered: {
    label: 'Plan delivered',
    description: 'Sent the moment you issue a plan, with a link the client can open on their phone.',
  },
  appointment_confirmed: {
    label: 'Appointment confirmed',
    description: 'Sent when an appointment is booked.',
  },
  appointment_reminder: {
    label: 'Appointment reminder',
    description: 'Sent before an appointment. Skipped if it would arrive after the appointment started.',
  },
  checkin_nudge: {
    label: 'Check-in',
    description: 'Your recurring nudge asking the client to log their weight and how the week went.',
  },
  assessment_invitation: {
    label: 'Assessment invitation',
    description: 'Asks a client to complete their assessment before a consultation.',
  },
  lead_acknowledgement: {
    label: 'Enquiry acknowledgement',
    description: 'Replies to someone who submits your enquiry form, so they know it arrived.',
  },
  lead_notification: {
    label: 'New enquiry notice',
    description: 'Emails you when a new enquiry comes in.',
  },
  magic_link: {
    label: 'Portal link',
    description: 'The secure link a client uses to open their portal.',
  },
}

/** The tenant-wide toggle for one message type, or `true` when none is set. */
function isEnabled(preferences: readonly MessagePreference[], code: string): boolean {
  const row = preferences.find(
    (preference) => preference.client_id === null && preference.template_code === code,
  )
  return row?.is_enabled ?? true
}

function toTypeView(
  template: MessageTemplate,
  preferences: readonly MessagePreference[],
): MessageTypeView {
  const copy = DESCRIPTIONS[template.code]
  return {
    code: template.code,
    label: copy?.label ?? template.code,
    description: copy?.description ?? '',
    isEssential: template.is_essential,
    canDisable: template.is_practitioner_disableable,
    isEnabled: isEnabled(preferences, template.code),
    providerStatus: template.provider_template_status,
    transport: template.default_transport,
  }
}

function toFailureView(
  failure: ReturnType<typeof useMessageSettings>['failures'][number],
): DeliveryFailureView {
  return {
    id: failure.id,
    templateLabel: DESCRIPTIONS[failure.template_code]?.label ?? failure.template_code,
    recipientAddress: failure.recipient_address,
    failureReason: failure.failure_reason,
    createdAt: failure.created_at,
  }
}

/** The practice-wide defaults row — `client_id` and `template_code` both null. */
const DEFAULT_QUIET_START = '21:00'
const DEFAULT_QUIET_END = '08:00'

export function Messages() {
  const settings = useMessageSettings()

  const defaults = settings.preferences.find(
    (preference) => preference.client_id === null && preference.template_code === null,
  )

  const preview: MessagePreviewView | null = settings.preview
    ? {
        templateCode: settings.preview.template_code,
        body: settings.preview.body,
        isSample: settings.preview.is_sample,
      }
    : null

  return (
    <>
      <PageHeader
        title="Messages"
        description="What this practice sends on your behalf, and when. Replies from clients arrive on your own WhatsApp."
      />

      {settings.loading ? (
        <Spinner label="Loading messaging settings" />
      ) : (
        <>
          <MessageTypesPanel
            types={settings.templates.map((template) =>
              toTypeView(template, settings.preferences),
            )}
            preview={preview}
            previewing={settings.previewing}
            error={settings.error ?? settings.settingsError}
            busy={settings.busy}
            onPreview={(code) => void settings.showPreview(code)}
            onClosePreview={settings.closePreview}
            onToggle={(code, enabled) =>
              void settings.save({ template_code: code, is_enabled: enabled })
            }
          />

          <MessagingControlsPanel
            quietHours={{
              // ⚠️ The displayed default matches `kernel.messaging`'s, so an
              // unset practice sees what the engine will actually do rather than
              // an empty field it must guess at.
              start: (defaults?.quiet_hours_start ?? DEFAULT_QUIET_START).slice(0, 5),
              end: (defaults?.quiet_hours_end ?? DEFAULT_QUIET_END).slice(0, 5),
              maxPerWeek: defaults?.max_messages_per_week ?? null,
            }}
            failures={settings.failures.map(toFailureView)}
            busy={settings.busy}
            error={settings.settingsError}
            onSave={(update) =>
              void settings.save({
                quiet_hours_start: `${update.start}:00`,
                quiet_hours_end: `${update.end}:00`,
                max_messages_per_week: update.maxPerWeek,
              })
            }
          />
        </>
      )}
    </>
  )
}
