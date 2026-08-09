/**
 * The tag picker on the client detail screen — FR-M1-008.
 *
 * 🔒 Renders and reports (Arch §4.4). The tenant's whole vocabulary arrives as
 * `allTags`; which of them this client carries arrives as `clientTagIds`. The
 * component computes nothing beyond set membership.
 *
 * ⚠️ **A toggle, not a menu.** Applying and removing are the same gesture
 * because the API makes both idempotent — so a practitioner clicking quickly
 * gets the state they clicked for, not an error.
 */

import { useState } from 'react'
import { Badge, Button, Card, CardBody, CardHeader, FormField, Input, Select } from '@wellnesscrm/design-system'

/** 🔒 The palette the API accepts. Mirrors `kernel.collaboration.TagColour`. */
export const TAG_COLOURS = [
  'slate',
  'red',
  'amber',
  'green',
  'teal',
  'blue',
  'violet',
  'pink',
] as const

export type TagColourName = (typeof TAG_COLOURS)[number]

export interface TagView {
  id: string
  name: string
  colour: TagColourName
}

export interface ClientTagsPanelProps {
  allTags: readonly TagView[]
  clientTagIds: readonly string[]
  error?: string | null
  busy?: boolean
  onToggle: (tagId: string, attached: boolean) => void
  onCreate: (name: string, colour: TagColourName) => void
}

/**
 * Map a tag's palette entry onto a design-system badge tone.
 *
 * ⚠️ Not every palette colour has a semantic tone — `violet` and `pink` carry no
 * meaning in the design system's vocabulary, which is deliberate: a tag's colour
 * is a practitioner's own grouping, not a status. Those fall back to `neutral`
 * rather than being forced onto a tone that would imply success or danger.
 */
function toneFor(colour: TagColourName): 'neutral' | 'success' | 'warning' | 'danger' | 'info' {
  switch (colour) {
    case 'red':
      return 'danger'
    case 'amber':
      return 'warning'
    case 'green':
    case 'teal':
      return 'success'
    case 'blue':
      return 'info'
    default:
      return 'neutral'
  }
}

export function ClientTagsPanel({
  allTags,
  clientTagIds,
  error = null,
  busy = false,
  onToggle,
  onCreate,
}: ClientTagsPanelProps) {
  const [newName, setNewName] = useState('')
  const [newColour, setNewColour] = useState<TagColourName>('slate')
  const applied = new Set(clientTagIds)

  function submitNew() {
    if (newName.trim() === '') return
    onCreate(newName, newColour)
    setNewName('')
    setNewColour('slate')
  }

  return (
    <Card>
      <CardHeader title="Tags" />
      <CardBody>
        {error !== null && <p role="alert">{error}</p>}

        {allTags.length === 0 ? (
          <p>No tags yet. Create one below to start grouping your clients.</p>
        ) : (
          <ul aria-label="Tags">
            {allTags.map((tag) => {
              const attached = applied.has(tag.id)
              return (
                <li key={tag.id}>
                  {/* 🔒 `aria-pressed` rather than a checkbox: this is a toggle
                    * button whose pressed state *is* the information, and a
                    * screen reader announces it on every activation. */}
                  <Button
                    variant={attached ? 'primary' : 'secondary'}
                    aria-pressed={attached}
                    disabled={busy}
                    onClick={() => onToggle(tag.id, attached)}
                  >
                    {tag.name}
                  </Button>
                </li>
              )
            })}
          </ul>
        )}

        <div aria-label="Applied tags">
          {allTags
            .filter((tag) => applied.has(tag.id))
            .map((tag) => (
              <Badge key={tag.id} tone={toneFor(tag.colour)}>
                {tag.name}
              </Badge>
            ))}
        </div>

        <FormField
          label="New tag"
          hint="Tags are shared across your whole practice. “PCOS” and “pcos” are the same tag."
        >
          <Input
            value={newName}
            maxLength={40}
            disabled={busy}
            onChange={(event) => setNewName(event.target.value)}
          />
        </FormField>

        <FormField label="Colour">
          <Select
            value={newColour}
            disabled={busy}
            onChange={(event) => setNewColour(event.target.value as TagColourName)}
          >
            {TAG_COLOURS.map((colour) => (
              <option key={colour} value={colour}>
                {colour}
              </option>
            ))}
          </Select>
        </FormField>

        <Button onClick={submitNew} disabled={newName.trim() === ''} loading={busy}>
          Create and apply
        </Button>
      </CardBody>
    </Card>
  )
}
