import { describe, expect, it, vi } from 'vitest'
import type { ComponentProps } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ToastProvider } from '@wellnesscrm/design-system'
import { ClientPlansPanel, type PlanRow } from './ClientPlansPanel'

function renderPanel(props: Partial<ComponentProps<typeof ClientPlansPanel>> = {}) {
  const onCreate = vi.fn()
  const onOpen = vi.fn()
  const base = {
    plans: [] as PlanRow[],
    loading: false,
    error: null,
    creating: false,
    createError: null,
    onCreate,
    onOpen,
  }
  render(
    <ToastProvider>
      <ClientPlansPanel {...base} {...props} />
    </ToastProvider>,
  )
  return { onCreate, onOpen }
}

describe('ClientPlansPanel', () => {
  it('shows an empty state when the client has no plans', () => {
    renderPanel()
    expect(screen.getByText('No plans yet')).toBeInTheDocument()
  })

  it('lists existing plans and opens one when clicked', async () => {
    const user = userEvent.setup()
    const { onOpen } = renderPanel({
      plans: [{ planId: 'p1', title: 'Weight loss — week 1', state: 'issued', versionNumber: 2 }],
    })

    await user.click(screen.getByTestId('open-plan-p1'))
    expect(onOpen).toHaveBeenCalledWith('p1')
  })

  it('creates a plan from the typed title', async () => {
    const user = userEvent.setup()
    const { onCreate } = renderPanel()

    await user.type(screen.getByTestId('new-plan-title-input'), 'Maintenance plan')
    await user.click(screen.getByTestId('create-plan-button'))
    expect(onCreate).toHaveBeenCalledWith('Maintenance plan')
  })

  it('surfaces a plan-limit refusal in the API’s words', () => {
    renderPanel({ createError: 'You have reached your plan’s client limit. Upgrade to add more.' })
    expect(screen.getByTestId('create-plan-error')).toHaveTextContent('client limit')
  })
})
