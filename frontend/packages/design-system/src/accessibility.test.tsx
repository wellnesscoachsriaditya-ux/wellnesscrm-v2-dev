/**
 * Behavioural tests for the accessibility-critical components.
 *
 * 🔒 These are not "does it render" tests. Each one covers a requirement that
 * is invisible when clicking around with a mouse and broken in most hand-rolled
 * implementations: focus trapping, label association, keyboard semantics,
 * required-consequence copy. They exist because the design system's whole
 * purpose is to make ~40 screens accessible by construction (NFR-056), and an
 * unverified guarantee is not one.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'

import { Button } from './primitives/Button'
import { Checkbox } from './primitives/Checkbox'
import { Input } from './primitives/Input'
import { Modal } from './primitives/Modal'
import { RadioGroup, Radio } from './primitives/Radio'
import { Table, TableBody, TableCell, TableHead, TableHeaderCell, TableRow } from './primitives/Table'
import { TabPanel, Tabs } from './primitives/Tabs'
import { ConfirmDialog } from './patterns/ConfirmDialog'
import { DataList } from './patterns/DataList'
import { EmptyState } from './patterns/EmptyState'
import { ErrorState } from './patterns/ErrorState'
import { FormField } from './patterns/FormField'
import { PageHeader } from './patterns/PageHeader'

// ─── FormField: NFR-062 ──────────────────────────────────────────────────

describe('FormField associates its label with the control (NFR-062)', () => {
  it('finds the input by its label text', () => {
    render(
      <FormField label="Mobile number">
        <Input />
      </FormField>,
    )
    // `getByLabelText` resolves through the accessibility tree — it only passes
    // if the association is real, not merely visual.
    expect(screen.getByLabelText('Mobile number')).toBeInstanceOf(HTMLInputElement)
  })

  it('associates the hint so a screen reader reads it', () => {
    render(
      <FormField label="Mobile number" hint="Used for WhatsApp reminders">
        <Input />
      </FormField>,
    )
    expect(screen.getByLabelText('Mobile number')).toHaveAccessibleDescription(
      /Used for WhatsApp reminders/,
    )
  })

  it('marks the control invalid and links the error message', () => {
    render(
      <FormField label="Mobile number" error="Enter a mobile number with 10 digits">
        <Input />
      </FormField>,
    )
    const input = screen.getByLabelText('Mobile number')
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input).toHaveAccessibleDescription(/10 digits/)
  })

  it('announces the error via role=alert', () => {
    render(
      <FormField label="Mobile" error="Enter a mobile number with 10 digits">
        <Input />
      </FormField>,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('10 digits')
  })

  it('conveys "required" as text, not only an asterisk', () => {
    // 🔒 A screen reader announcing "asterisk" tells the user nothing.
    render(
      <FormField label="Full name" required>
        <Input />
      </FormField>,
    )
    expect(screen.getByLabelText(/required/i)).toBeInstanceOf(HTMLInputElement)
    expect(screen.getByLabelText(/required/i)).toBeRequired()
  })
})

// ─── Checkbox / Radio: NFR-062 ───────────────────────────────────────────

describe('Checkbox and Radio are labelled structurally', () => {
  it('Checkbox cannot be rendered without a label', () => {
    render(<Checkbox label="Send WhatsApp reminders" />)

    const checkbox = screen.getByLabelText('Send WhatsApp reminders')
    expect(checkbox).toBeInstanceOf(HTMLInputElement)
    expect(checkbox).not.toBeChecked()

    // The whole label row is the hit target, not just the box — NFR-059.
    expect(checkbox.closest('label')).not.toBeNull()
  })

  it('RadioGroup names the question via a fieldset legend', () => {
    // 🔒 Without this, a screen reader announces the options with no indication
    // of what is being asked.
    render(
      <RadioGroup legend="Preferred contact channel" name="channel">
        <Radio name="channel" value="whatsapp" label="WhatsApp" />
        <Radio name="channel" value="sms" label="SMS" />
      </RadioGroup>,
    )
    const group = screen.getByRole('group', { name: 'Preferred contact channel' })
    expect(within(group).getByLabelText('WhatsApp')).toBeInstanceOf(HTMLInputElement)
  })
})

// ─── Modal: focus management, NFR-061 ────────────────────────────────────

describe('Modal manages focus (NFR-061)', () => {
  function Harness() {
    const [open, setOpen] = useState(false)
    return (
      <>
        <button type="button" onClick={() => setOpen(true)}>
          Open
        </button>
        <Modal open={open} onClose={() => setOpen(false)} title="Delete plan">
          <button type="button">First</button>
          <button type="button">Second</button>
        </Modal>
      </>
    )
  }

  it('exposes itself as a modal dialog with an accessible name', async () => {
    render(<Harness />)
    await userEvent.click(screen.getByRole('button', { name: 'Open' }))

    const dialog = screen.getByRole('dialog', { name: 'Delete plan' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
  })

  it('moves focus into the dialog on open', async () => {
    render(<Harness />)
    await userEvent.click(screen.getByRole('button', { name: 'Open' }))

    expect(screen.getByRole('button', { name: 'First' })).toHaveFocus()
  })

  it('traps Tab inside the dialog', async () => {
    // 🔒 Without the trap, Tab walks into the page behind the modal — content
    // the user cannot see and did not ask for.
    //
    // DOM order is: close button (header) → First → Second (body). Focus lands
    // on First because Modal prefers the body over the close button, so tabbing
    // forward twice reaches the end of the list and must wrap to the close
    // button rather than escaping to the document.
    render(<Harness />)
    await userEvent.click(screen.getByRole('button', { name: 'Open' }))

    const close = screen.getByRole('button', { name: 'Close dialog' })
    expect(screen.getByRole('button', { name: 'First' })).toHaveFocus()

    await userEvent.tab() // First → Second
    expect(screen.getByRole('button', { name: 'Second' })).toHaveFocus()

    await userEvent.tab() // Second is last in DOM order → wraps to close
    expect(close).toHaveFocus()
  })

  it('wraps backwards with Shift+Tab', async () => {
    render(<Harness />)
    await userEvent.click(screen.getByRole('button', { name: 'Open' }))

    // Focus starts on First; the close button precedes it in the DOM.
    await userEvent.tab({ shift: true })
    expect(screen.getByRole('button', { name: 'Close dialog' })).toHaveFocus()

    // From the first element, Shift+Tab wraps to the last rather than leaving.
    await userEvent.tab({ shift: true })
    expect(screen.getByRole('button', { name: 'Second' })).toHaveFocus()
  })

  it('closes on Escape', async () => {
    render(<Harness />)
    await userEvent.click(screen.getByRole('button', { name: 'Open' }))
    await userEvent.keyboard('{Escape}')

    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('restores focus to the trigger on close', async () => {
    // Losing your place in the page after closing a dialog is disorienting.
    render(<Harness />)
    const trigger = screen.getByRole('button', { name: 'Open' })

    await userEvent.click(trigger)
    await userEvent.keyboard('{Escape}')

    expect(trigger).toHaveFocus()
  })

  it('does not close on Escape when not dismissible', async () => {
    const onClose = vi.fn()
    render(
      <Modal open onClose={onClose} title="Saving" dismissible={false}>
        <p>Working…</p>
      </Modal>,
    )
    await userEvent.keyboard('{Escape}')

    expect(onClose).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Close dialog' })).toBeNull()
  })
})

// ─── ConfirmDialog: NFR-065 ──────────────────────────────────────────────

describe('ConfirmDialog states what will be lost (NFR-065)', () => {
  const props = {
    open: true,
    onCancel: () => {},
    onConfirm: () => {},
    title: "Delete Priya Sharma's plan?",
    consequence:
      'The plan and its 3 revisions will be permanently deleted. Priya will no longer see it in her portal.',
    confirmLabel: 'Delete plan',
  }

  it('renders the consequence text', () => {
    render(<ConfirmDialog {...props} />)
    expect(screen.getByText(/permanently deleted/)).toBeInTheDocument()
    expect(screen.getByText(/no longer see it in her portal/)).toBeInTheDocument()
  })

  it('focuses Cancel rather than the destructive action', () => {
    // 🔒 Someone dismissing a dialog reflexively with Enter must not destroy
    // data. Modal focuses the first focusable element; Cancel is rendered
    // first precisely for this.
    render(<ConfirmDialog {...props} />)
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus()
  })

  it('names the action on the confirm button', () => {
    // "OK" forces the user to reconstruct what they clicked from memory.
    render(<ConfirmDialog {...props} />)
    expect(screen.getByRole('button', { name: 'Delete plan' })).toBeInTheDocument()
  })

  it('calls onConfirm when confirmed', async () => {
    const onConfirm = vi.fn()
    render(<ConfirmDialog {...props} onConfirm={onConfirm} />)

    await userEvent.click(screen.getByRole('button', { name: 'Delete plan' }))
    expect(onConfirm).toHaveBeenCalledOnce()
  })

  it('disables both actions while busy', () => {
    render(<ConfirmDialog {...props} busy />)
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Delete plan' })).toBeDisabled()
  })
})

// ─── Tabs: keyboard semantics, NFR-061 ──────────────────────────────────

describe('Tabs implements the ARIA tabs keyboard pattern (NFR-061)', () => {
  const items = [
    { id: 'overview', label: 'Overview' },
    { id: 'plans', label: 'Plans' },
    { id: 'notes', label: 'Notes' },
  ]

  function Harness() {
    const [value, setValue] = useState('overview')
    return (
      <Tabs items={items} value={value} onChange={setValue} label="Client sections">
        <TabPanel id="overview">Overview content</TabPanel>
        <TabPanel id="plans">Plans content</TabPanel>
        <TabPanel id="notes">Notes content</TabPanel>
      </Tabs>
    )
  }

  it('exposes a named tablist', () => {
    render(<Harness />)
    expect(screen.getByRole('tablist', { name: 'Client sections' })).toBeInTheDocument()
  })

  it('makes the tab list one tab stop', () => {
    // 🔒 Only the selected tab is focusable. Without this, a keyboard user tabs
    // through every tab to reach the content.
    render(<Harness />)
    expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute('tabindex', '0')
    expect(screen.getByRole('tab', { name: 'Plans' })).toHaveAttribute('tabindex', '-1')
  })

  it('moves selection with ArrowRight', async () => {
    render(<Harness />)
    screen.getByRole('tab', { name: 'Overview' }).focus()

    await userEvent.keyboard('{ArrowRight}')
    expect(screen.getByRole('tab', { name: 'Plans' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: 'Plans' })).toHaveFocus()
  })

  it('wraps at the end', async () => {
    render(<Harness />)
    screen.getByRole('tab', { name: 'Overview' }).focus()

    await userEvent.keyboard('{ArrowLeft}')
    expect(screen.getByRole('tab', { name: 'Notes' })).toHaveAttribute('aria-selected', 'true')
  })

  it('jumps to first and last with Home and End', async () => {
    render(<Harness />)
    screen.getByRole('tab', { name: 'Overview' }).focus()

    await userEvent.keyboard('{End}')
    expect(screen.getByRole('tab', { name: 'Notes' })).toHaveAttribute('aria-selected', 'true')

    await userEvent.keyboard('{Home}')
    expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute('aria-selected', 'true')
  })

  it('shows only the selected panel, correctly associated', () => {
    render(<Harness />)

    const panel = screen.getByRole('tabpanel')
    expect(panel).toHaveTextContent('Overview content')
    // 🔒 The panel must be labelled by its tab; a mismatch here is invisible
    // on screen and breaks the relationship for assistive technology.
    expect(panel).toHaveAccessibleName('Overview')
    expect(screen.queryByText('Plans content')).toBeNull()
  })

  it('skips disabled tabs when navigating', async () => {
    function WithDisabled() {
      const [value, setValue] = useState('a')
      return (
        <Tabs
          items={[
            { id: 'a', label: 'A' },
            { id: 'b', label: 'B', disabled: true },
            { id: 'c', label: 'C' },
          ]}
          value={value}
          onChange={setValue}
          label="Sections"
        />
      )
    }
    render(<WithDisabled />)
    screen.getByRole('tab', { name: 'A' }).focus()

    await userEvent.keyboard('{ArrowRight}')
    expect(screen.getByRole('tab', { name: 'C' })).toHaveAttribute('aria-selected', 'true')
  })
})

// ─── Table: semantics ────────────────────────────────────────────────────

describe('Table uses real table semantics', () => {
  it('is announced with its caption and header association', () => {
    render(
      <Table caption="Active clients">
        <TableHead>
          <TableRow>
            <TableHeaderCell>Name</TableHeaderCell>
            <TableHeaderCell numeric>Weight</TableHeaderCell>
          </TableRow>
        </TableHead>
        <TableBody>
          <TableRow>
            <TableCell>Priya Sharma</TableCell>
            <TableCell numeric>62.5</TableCell>
          </TableRow>
        </TableBody>
      </Table>,
    )

    expect(screen.getByRole('table', { name: 'Active clients' })).toBeInTheDocument()
    // `scope="col"` is what pairs a cell with its header for a screen reader.
    expect(screen.getByRole('columnheader', { name: 'Name' })).toHaveAttribute('scope', 'col')
  })

  it('keeps the caption available to assistive technology when visually hidden', () => {
    render(
      <Table caption="Active clients" captionHidden>
        <TableBody>
          <TableRow>
            <TableCell>Priya</TableCell>
          </TableRow>
        </TableBody>
      </Table>,
    )
    expect(screen.getByRole('table', { name: 'Active clients' })).toBeInTheDocument()
  })
})

// ─── EmptyState / ErrorState: NFR-063, NFR-064 ──────────────────────────

describe('EmptyState explains what to do next (NFR-064)', () => {
  it('renders title, description and action', () => {
    render(
      <EmptyState
        title="No clients yet"
        description="Add your first client to start building plans."
        action={<Button variant="primary">Add client</Button>}
      />,
    )
    expect(screen.getByText('No clients yet')).toBeInTheDocument()
    expect(screen.getByText(/Add your first client/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add client' })).toBeInTheDocument()
  })
})

describe('ErrorState states the problem and the next step (NFR-063)', () => {
  it('announces itself and renders both parts', () => {
    render(
      <ErrorState
        title="We couldn't load your clients"
        whatToDoNext="Check your connection and try again."
        reference="req_01HX2"
      />,
    )
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent("We couldn't load your clients")
    expect(alert).toHaveTextContent('Check your connection and try again.')
    // 🔒 NFR-033 — an identifier only, never a stack trace.
    expect(alert).toHaveTextContent('req_01HX2')
  })
})

// ─── PageHeader: heading structure ──────────────────────────────────────

describe('PageHeader establishes the page heading', () => {
  it('renders the title as the h1', () => {
    render(<PageHeader title="Clients" />)
    expect(screen.getByRole('heading', { level: 1, name: 'Clients' })).toBeInTheDocument()
  })

  it('marks the current breadcrumb and does not link it', () => {
    render(
      <PageHeader
        title="Priya Sharma"
        breadcrumbs={[
          { label: 'Clients', href: '/clients' },
          { label: 'Priya Sharma' },
        ]}
      />,
    )
    const nav = screen.getByRole('navigation', { name: 'Breadcrumb' })
    expect(within(nav).getByRole('link', { name: 'Clients' })).toBeInTheDocument()
    expect(within(nav).queryByRole('link', { name: 'Priya Sharma' })).toBeNull()
  })
})

// ─── DataList: semantics ────────────────────────────────────────────────

describe('DataList pairs labels with values', () => {
  it('renders a definition list', () => {
    const { container } = render(
      <DataList
        items={[
          { label: 'Mobile', value: '+91 98765 43210' },
          { label: 'Stage', value: 'Active' },
        ]}
      />,
    )
    // `dt`/`dd` is what tells a screen reader that "Mobile" names the number.
    expect(container.querySelectorAll('dt')).toHaveLength(2)
    expect(container.querySelectorAll('dd')).toHaveLength(2)
    expect(screen.getByText('+91 98765 43210')).toBeInTheDocument()
  })
})

// ─── Button ─────────────────────────────────────────────────────────────

describe('Button', () => {
  it('defaults to type=button so it cannot submit a form by accident', () => {
    render(<Button>Save</Button>)
    expect(screen.getByRole('button', { name: 'Save' })).toHaveAttribute('type', 'button')
  })

  it('is disabled and unclickable while loading', async () => {
    const onClick = vi.fn()
    render(
      <Button loading onClick={onClick}>
        Save
      </Button>,
    )
    const button = screen.getByRole('button')
    expect(button).toBeDisabled()

    await userEvent.click(button)
    expect(onClick).not.toHaveBeenCalled()
  })
})
