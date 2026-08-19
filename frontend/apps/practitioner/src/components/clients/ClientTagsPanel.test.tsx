/**
 * The tag picker — FR-M1-008.
 *
 * 🔒 A toggle, not a menu: applying and removing are the same gesture, because
 * the API makes both idempotent. These tests pin that the pressed state is
 * announced (it is the whole information) and that creating a tag applies it in
 * the same action.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ClientTagsPanel } from './ClientTagsPanel'
import type { TagView } from './ClientTagsPanel'

const TAGS: TagView[] = [
  { id: 'tag-pcos', name: 'PCOS', colour: 'violet' },
  { id: 'tag-weight', name: 'Weight loss', colour: 'green' },
]

function renderPanel(overrides: Partial<Parameters<typeof ClientTagsPanel>[0]> = {}) {
  const onToggle = vi.fn()
  const onCreate = vi.fn()

  render(
    <ClientTagsPanel
      allTags={TAGS}
      clientTagIds={['tag-pcos']}
      onToggle={onToggle}
      onCreate={onCreate}
      {...overrides}
    />,
  )

  return { onToggle, onCreate }
}

describe('applying tags', () => {
  it('announces which tags are applied', () => {
    // 🔒 `aria-pressed` — the pressed state *is* the information, and a screen
    // reader gets it on every activation. Colour alone would fail WCAG 1.4.1.
    renderPanel()

    expect(screen.getByRole('button', { name: 'PCOS', pressed: true })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Weight loss', pressed: false })).toBeInTheDocument()
  })

  it('reports the current state when toggling, so the hook knows the direction', async () => {
    const { onToggle } = renderPanel()

    await userEvent.click(screen.getByRole('button', { name: 'Weight loss' }))
    expect(onToggle).toHaveBeenCalledWith('tag-weight', false)

    await userEvent.click(screen.getByRole('button', { name: 'PCOS' }))
    expect(onToggle).toHaveBeenCalledWith('tag-pcos', true)
  })

  it('says so when the practice has no tags yet', () => {
    // NFR-064 — and this empty state does a second job: it tells a practitioner
    // what tags are *for*, which an empty list cannot.
    renderPanel({ allTags: [], clientTagIds: [] })
    expect(screen.getByText(/create one below/i)).toBeInTheDocument()
  })
})

describe('creating tags', () => {
  it('creates and applies in one action', async () => {
    const { onCreate } = renderPanel()

    await userEvent.type(screen.getByLabelText('New tag'), 'Diabetes')
    await userEvent.selectOptions(screen.getByLabelText('Colour'), 'blue')
    await userEvent.click(screen.getByRole('button', { name: 'Create and apply' }))

    expect(onCreate).toHaveBeenCalledWith('Diabetes', 'blue')
  })

  it('will not create an empty tag', async () => {
    const { onCreate } = renderPanel()

    await userEvent.click(screen.getByRole('button', { name: 'Create and apply' }))

    expect(onCreate).not.toHaveBeenCalled()
  })

  it('warns that tags are case-insensitive before the server refuses', () => {
    // 🔒 The API refuses "pcos" when "PCOS" exists. Saying so up front is the
    // difference between a hint and a rejected save.
    renderPanel()
    expect(screen.getByText(/are the same tag/i)).toBeInTheDocument()
  })

  it('surfaces a failure without hiding the picker', () => {
    renderPanel({ error: 'That tag already exists.' })

    expect(screen.getByRole('alert')).toHaveTextContent('That tag already exists.')
    expect(screen.getByRole('button', { name: 'PCOS' })).toBeInTheDocument()
  })
})
