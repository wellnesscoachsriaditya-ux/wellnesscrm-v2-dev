/**
 * The access panel — EC-M0-04, EC-M1-04, FR-M0-017.
 *
 * 🔒 The test that matters here is the **non-owner case**. `client.read_access`
 * admits any assigned practitioner while `client.manage_access` is owner-only, so
 * the panel must show the list and withhold the controls. Getting that backwards
 * either leaks a control that 403s or hides information a practitioner may see.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ClientAccessPanel } from './ClientAccessPanel'
import type { GrantView } from './ClientAccessPanel'

const OWNER = 'user-owner'
const COLLEAGUE = 'user-colleague'

const GRANTS: GrantView[] = [
  {
    userId: COLLEAGUE,
    grantedByUserId: OWNER,
    grantedAt: '2026-08-01T10:00:00Z',
    revokedAt: null,
    isLive: true,
  },
]

function renderPanel(overrides: Partial<Parameters<typeof ClientAccessPanel>[0]> = {}) {
  const onGrant = vi.fn()
  const onRevoke = vi.fn()
  const onReassign = vi.fn()

  render(
    <ClientAccessPanel
      ownerUserId={OWNER}
      grants={GRANTS}
      canManage
      onGrant={onGrant}
      onRevoke={onRevoke}
      onReassign={onReassign}
      {...overrides}
    />,
  )

  return { onGrant, onRevoke, onReassign }
}

describe('who can see this client', () => {
  it('names the owning practitioner', () => {
    renderPanel()
    expect(screen.getByText(OWNER)).toBeInTheDocument()
  })

  it('lists the colleagues holding a live grant', () => {
    renderPanel()
    expect(screen.getByRole('list', { name: 'Shared access' })).toHaveTextContent(COLLEAGUE)
  })

  it('leaves out revoked grants', () => {
    // 🔒 EC-M1-04 keeps the history, but "who can see this client" is a question
    // about live access. A revoked row in this list overstates it.
    renderPanel({
      grants: [
        {
          userId: 'user-former',
          grantedByUserId: OWNER,
          grantedAt: '2026-07-01T10:00:00Z',
          revokedAt: '2026-07-20T10:00:00Z',
          isLive: false,
        },
      ],
    })

    expect(screen.queryByText('user-former')).not.toBeInTheDocument()
    expect(screen.getByText(/not shared with anyone/i)).toBeInTheDocument()
  })

  it('explains the empty case rather than showing a bare list', () => {
    // NFR-064 — and it states the consequence, which an empty list cannot.
    renderPanel({ grants: [] })
    expect(screen.getByText(/only the owning practitioner can open this client/i)).toBeInTheDocument()
  })
})

describe('the owner-only rule (FR-M0-017)', () => {
  it('offers no controls to a practitioner, and says who to ask', () => {
    // 🔒 The API would refuse them with a 403. Hiding the control is the
    // difference between a rule explained and a rule discovered by refusal.
    renderPanel({ canManage: false })

    expect(screen.getByText(/only the account owner can change who has access/i)).toBeInTheDocument()
    expect(screen.queryByLabelText('Share with a colleague')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove access' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Transfer' })).not.toBeInTheDocument()
  })

  it('still shows a practitioner who else has access', () => {
    // 🔒 `client.read_access` is not owner-only. Withholding the list would hide
    // information they are entitled to see.
    renderPanel({ canManage: false })
    expect(screen.getByRole('list', { name: 'Shared access' })).toHaveTextContent(COLLEAGUE)
  })
})

describe('granting and revoking', () => {
  it('grants access to the id the owner entered', async () => {
    const { onGrant } = renderPanel()

    await userEvent.type(screen.getByLabelText('Share with a colleague'), '  user-new  ')
    await userEvent.click(screen.getByRole('button', { name: 'Give access' }))

    // Trimmed — a pasted id carries whitespace, and the API would reject it as
    // a malformed uuid for a reason the practitioner cannot see.
    expect(onGrant).toHaveBeenCalledWith('user-new')
  })

  it('will not grant to an empty id', async () => {
    const { onGrant } = renderPanel()
    await userEvent.click(screen.getByRole('button', { name: 'Give access' }))
    expect(onGrant).not.toHaveBeenCalled()
  })

  it('confirms before removing access, naming what is lost', async () => {
    // 🔒 NFR-065. Revoking is the destructive direction here.
    const { onRevoke } = renderPanel()

    await userEvent.click(screen.getByRole('button', { name: 'Remove access' }))
    expect(screen.getByText(/no longer be able to open this client/i)).toBeInTheDocument()
    expect(onRevoke).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: 'Remove their access' }))
    expect(onRevoke).toHaveBeenCalledWith(COLLEAGUE)
  })

  it('surfaces a failure without hiding who has access', () => {
    renderPanel({ error: 'That practitioner already has access to this client.' })

    expect(screen.getByRole('alert')).toHaveTextContent('already has access')
    expect(screen.getByRole('list', { name: 'Shared access' })).toHaveTextContent(COLLEAGUE)
  })
})

describe('transferring ownership (EC-M1-04)', () => {
  it('confirms before transferring', async () => {
    const { onReassign } = renderPanel()

    await userEvent.type(screen.getByLabelText('Transfer ownership'), 'user-next')
    await userEvent.click(screen.getByRole('button', { name: 'Transfer' }))

    expect(screen.getByText(/become the owning practitioner/i)).toBeInTheDocument()
    expect(onReassign).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: 'Transfer client' }))
    expect(onReassign).toHaveBeenCalledWith('user-next')
  })

  it('offers a shortcut to hand the client to someone who already has access', async () => {
    // Reassigning to a practitioner who has never seen this client is a data
    // move nobody asked for, so the shortcut is offered only for a grantee.
    const { onReassign } = renderPanel()

    await userEvent.click(screen.getByRole('button', { name: 'Make owner' }))
    await userEvent.click(screen.getByRole('button', { name: 'Transfer client' }))

    expect(onReassign).toHaveBeenCalledWith(COLLEAGUE)
  })
})
