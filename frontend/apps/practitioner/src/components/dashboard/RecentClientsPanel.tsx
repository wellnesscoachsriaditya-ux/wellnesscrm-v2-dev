/**
 * Recently active clients, on the dashboard — S2 Slice G.
 *
 * 🔒 Renders and reports (Arch §4.4). R8 fails the build if anything under
 * `components/` reaches the API.
 *
 * 🔒 **A list, not a table.** The client *list* screen is tabular because a
 * practitioner scans it by column; this is five rows answering "who was I last
 * working with", and a seven-column table for that is noise. It also survives a
 * narrow viewport without horizontal scrolling, which matters because the
 * dashboard is the screen most likely to be opened on a phone between
 * consultations (NFR-055).
 *
 * ⚠️ **"Recent" is the server's definition**, not this file's: the caller asks
 * for `-recent_activity`, an index-backed ordering from API §6.3. Nothing here
 * sorts.
 */

import { Badge, Button } from '@wellnesscrm/design-system'
import { STAGE_LABEL, STAGE_TONE, type StageValue } from '../clients/stages'

export interface RecentClientView {
  id: string
  fullName: string
  stage: StageValue
  isArchived: boolean
  /** ISO-8601, as the API sends it. Formatted for display, never recomputed. */
  updatedAt: string
}

export interface RecentClientsPanelProps {
  clients: readonly RecentClientView[]
  onOpen: (clientId: string) => void
}

/**
 * The date a practitioner reads, in their own locale.
 *
 * ⚠️ Date only, no time. "14 Jul" is what makes a row scannable; a timestamp
 * invites comparison at a precision the ordering does not promise.
 */
function dayLabel(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
}

export function RecentClientsPanel({ clients, onOpen }: RecentClientsPanelProps) {
  return (
    <ul aria-label="Recently active clients">
      {clients.map((client) => (
        <li key={client.id}>
          {/* 🔒 A button, not a bare row handler: the whole name must be
            * reachable and activatable from the keyboard (NFR-062), and a
            * `<div onClick>` is neither focusable nor announced as actionable. */}
          <Button variant="ghost" onClick={() => onOpen(client.id)}>
            {client.fullName}
          </Button>

          <Badge tone={STAGE_TONE[client.stage]}>{STAGE_LABEL[client.stage]}</Badge>

          {/* 🔒 EC-M1-02 — an archived client keeps its stage, so the stage badge
            * alone would show "Active client" for somebody who is filed away.
            * Both facts have to be visible or the row misleads. */}
          {client.isArchived && <Badge tone="neutral">Archived</Badge>}

          <time dateTime={client.updatedAt}>{dayLabel(client.updatedAt)}</time>
        </li>
      ))}
    </ul>
  )
}
