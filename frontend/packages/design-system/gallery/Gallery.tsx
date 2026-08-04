/**
 * Component gallery.
 *
 * 🔒 S0 Definition of Done: "Component gallery shows every primitive and
 * pattern." It is also the only way to review the design system before a
 * feature screen exists to put it in, and the fastest way to check a change
 * against every state at once.
 *
 * ⚠️ Deliberately not Storybook. Storybook is ~40 packages and a build
 * pipeline of its own, and NFR-078 requires every dependency to be justifiable
 * and debuggable by one developer. This file is a Vite page that imports the
 * components directly — it cannot drift from the real components, because it
 * uses them.
 */

import { useState } from 'react'
import {
  AdminShell,
  AppShell,
  Badge,
  Button,
  Card,
  CardBody,
  CardFooter,
  CardHeader,
  Checkbox,
  ConfirmDialog,
  DataList,
  EmptyState,
  ErrorState,
  FormField,
  Input,
  MobileShell,
  Modal,
  PageHeader,
  Radio,
  RadioGroup,
  Select,
  Spinner,
  TabPanel,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeaderCell,
  TableRow,
  Tabs,
  Textarea,
  ToastProvider,
  colour,
  fontSize,
  space,
  useToast,
} from '../src'

export function Gallery() {
  return (
    <ToastProvider>
      <div className="gallery">
        <header className="gallery-header">
          <h1>WellnessCRM Design System</h1>
          <p>
            ADR-03 — tokens, primitives, patterns and layout shells. Every value below comes
            from a token; this package is the only place raw values exist.
          </p>
        </header>

        <Tokens />
        <Primitives />
        <Patterns />
        <Shells />
      </div>
    </ToastProvider>
  )
}

function Section({ title, note, children }: { title: string; note?: string; children: React.ReactNode }) {
  return (
    <section className="gallery-section">
      <h2>{title}</h2>
      {note && <p className="gallery-note">{note}</p>}
      <div className="gallery-content">{children}</div>
    </section>
  )
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="gallery-row">
      <span className="gallery-label">{label}</span>
      <div className="gallery-items">{children}</div>
    </div>
  )
}

// ─── Tokens ──────────────────────────────────────────────────────────────

function Tokens() {
  const brand = Object.entries(colour).filter(([key]) => key.startsWith('brand'))
  const neutral = Object.entries(colour).filter(([key]) => key.startsWith('neutral'))
  const status = Object.entries(colour).filter(
    ([key]) => !key.startsWith('brand') && !key.startsWith('neutral'),
  )

  return (
    <Section
      title="Tokens"
      note="Colour contrast for every pair used in the system is asserted against WCAG AA in contrast.test.ts."
    >
      <Row label="Brand">
        {brand.map(([name, value]) => (
          <Swatch key={name} name={name} value={value} />
        ))}
      </Row>
      <Row label="Neutral">
        {neutral.map(([name, value]) => (
          <Swatch key={name} name={name} value={value} />
        ))}
      </Row>
      <Row label="Status">
        {status.map(([name, value]) => (
          <Swatch key={name} name={name} value={value} />
        ))}
      </Row>

      <Row label="Type scale">
        <div>
          {Object.entries(fontSize).map(([name, value]) => (
            <p key={name} style={{ fontSize: value, margin: '0 0 0.5rem' }}>
              {name} — {value} — The quick brown fox
            </p>
          ))}
        </div>
      </Row>

      <Row label="Spacing">
        <div className="gallery-spacing">
          {Object.entries(space)
            .filter(([, value]) => value !== '0')
            .map(([name, value]) => (
              <div key={name} className="gallery-spacing-item">
                <span className="gallery-spacing-bar" style={{ width: value }} />
                <code>
                  {name} · {value}
                </code>
              </div>
            ))}
        </div>
      </Row>
    </Section>
  )
}

function Swatch({ name, value }: { name: string; value: string }) {
  return (
    <div className="gallery-swatch">
      <span className="gallery-swatch-chip" style={{ background: value }} />
      <code>{name}</code>
      <code className="gallery-swatch-value">{value}</code>
    </div>
  )
}

// ─── Primitives ──────────────────────────────────────────────────────────

function Primitives() {
  const [modalOpen, setModalOpen] = useState(false)
  const [tab, setTab] = useState('overview')
  const { show } = useToast()

  return (
    <Section title="Primitives">
      <Row label="Button — variants">
        <Button variant="primary">Primary</Button>
        <Button variant="secondary">Secondary</Button>
        <Button variant="ghost">Ghost</Button>
        <Button variant="danger">Danger</Button>
      </Row>
      <Row label="Button — sizes">
        <Button size="sm">Small</Button>
        <Button size="md">Medium (44px)</Button>
        <Button size="lg">Large (48px)</Button>
      </Row>
      <Row label="Button — states">
        <Button variant="primary" loading>
          Saving
        </Button>
        <Button disabled>Disabled</Button>
      </Row>

      <Row label="Input">
        <Input placeholder="Client name" />
        <Input placeholder="Invalid" invalid />
        <Input placeholder="Disabled" disabled />
      </Row>

      <Row label="Select">
        <Select defaultValue="active">
          <option value="lead">Lead</option>
          <option value="active">Active</option>
          <option value="paused">Paused</option>
        </Select>
      </Row>

      <Row label="Textarea">
        <Textarea placeholder="Consultation notes…" />
      </Row>

      <Row label="Checkbox">
        <Checkbox label="Send WhatsApp reminders" description="Uses the client's saved number." />
      </Row>

      <Row label="Radio">
        <RadioGroup legend="Preferred contact channel" name="gallery-channel">
          <Radio name="gallery-channel" value="whatsapp" label="WhatsApp" defaultChecked />
          <Radio name="gallery-channel" value="sms" label="SMS" />
          <Radio name="gallery-channel" value="email" label="Email" />
        </RadioGroup>
      </Row>

      <Row label="Badge">
        <Badge>Neutral</Badge>
        <Badge tone="brand">Active</Badge>
        <Badge tone="success">Delivered</Badge>
        <Badge tone="warning">At risk</Badge>
        <Badge tone="danger">Failed</Badge>
        <Badge tone="info">Draft</Badge>
      </Row>

      <Row label="Spinner">
        <Spinner size="sm" />
        <Spinner size="md" />
        <Spinner size="lg" />
      </Row>

      <Row label="Card">
        <Card>
          <CardHeader
            title="Priya Sharma"
            description="Active since March 2026"
            actions={<Button size="sm">Edit</Button>}
          />
          <CardBody>Weekly check-in due tomorrow.</CardBody>
          <CardFooter>
            <Button size="sm" variant="ghost">
              Dismiss
            </Button>
            <Button size="sm" variant="primary">
              Open
            </Button>
          </CardFooter>
        </Card>
      </Row>

      <Row label="Table">
        <Table caption="Active clients">
          <TableHead>
            <TableRow>
              <TableHeaderCell>Name</TableHeaderCell>
              <TableHeaderCell>Stage</TableHeaderCell>
              <TableHeaderCell numeric>Weight (kg)</TableHeaderCell>
            </TableRow>
          </TableHead>
          <TableBody>
            <TableRow>
              <TableCell>Priya Sharma</TableCell>
              <TableCell>
                <Badge tone="brand">Active</Badge>
              </TableCell>
              <TableCell numeric>62.5</TableCell>
            </TableRow>
            <TableRow>
              <TableCell>Rahul Menon</TableCell>
              <TableCell>
                <Badge tone="warning">At risk</Badge>
              </TableCell>
              <TableCell numeric>78.0</TableCell>
            </TableRow>
          </TableBody>
        </Table>
      </Row>

      <Row label="Tabs">
        <Tabs
          items={[
            { id: 'overview', label: 'Overview' },
            { id: 'plans', label: 'Plans', count: 3 },
            { id: 'notes', label: 'Notes' },
            { id: 'billing', label: 'Billing', disabled: true },
          ]}
          value={tab}
          onChange={setTab}
          label="Client sections"
        >
          <TabPanel id="overview">Overview content. Arrow keys move between tabs.</TabPanel>
          <TabPanel id="plans">Three plans.</TabPanel>
          <TabPanel id="notes">Consultation notes.</TabPanel>
        </Tabs>
      </Row>

      <Row label="Modal">
        <Button onClick={() => setModalOpen(true)}>Open modal</Button>
        <Modal
          open={modalOpen}
          onClose={() => setModalOpen(false)}
          title="Edit client"
          footer={
            <>
              <Button variant="secondary" onClick={() => setModalOpen(false)}>
                Cancel
              </Button>
              <Button variant="primary" onClick={() => setModalOpen(false)}>
                Save
              </Button>
            </>
          }
        >
          <FormField label="Full name">
            <Input defaultValue="Priya Sharma" />
          </FormField>
        </Modal>
      </Row>

      <Row label="Toast">
        <Button onClick={() => show({ tone: 'success', message: 'Plan saved.' })}>Success</Button>
        <Button onClick={() => show({ tone: 'info', message: 'Draft autosaved.' })}>Info</Button>
        <Button onClick={() => show({ tone: 'warning', message: 'Client is near their limit.' })}>
          Warning
        </Button>
        <Button onClick={() => show({ tone: 'error', message: "Couldn't reach the server." })}>
          Error
        </Button>
      </Row>
    </Section>
  )
}

// ─── Patterns ────────────────────────────────────────────────────────────

function Patterns() {
  const [confirmOpen, setConfirmOpen] = useState(false)

  return (
    <Section
      title="Patterns"
      note="EmptyState and ConfirmDialog are built here deliberately — NFR-064 and NFR-065 apply to ~40 screens."
    >
      <Row label="PageHeader">
        <div style={{ width: '100%' }}>
          <PageHeader
            title="Priya Sharma"
            description="Active client since March 2026"
            breadcrumbs={[{ label: 'Clients', href: '#' }, { label: 'Priya Sharma' }]}
            meta={<Badge tone="brand">Active</Badge>}
            actions={
              <>
                <Button variant="secondary">Message</Button>
                <Button variant="primary">New plan</Button>
              </>
            }
          />
        </div>
      </Row>

      <Row label="FormField">
        <div style={{ display: 'grid', gap: '1.5rem', maxWidth: '24rem', width: '100%' }}>
          <FormField label="Full name" required>
            <Input placeholder="Priya Sharma" />
          </FormField>
          <FormField label="Mobile number" hint="Used for WhatsApp reminders." required>
            <Input placeholder="+91 98765 43210" />
          </FormField>
          <FormField label="Mobile number" error="Enter a mobile number with 10 digits.">
            <Input defaultValue="9876" invalid />
          </FormField>
        </div>
      </Row>

      <Row label="DataList">
        <div style={{ maxWidth: '32rem', width: '100%' }}>
          <DataList
            layout="inline"
            items={[
              { label: 'Mobile', value: '+91 98765 43210' },
              { label: 'Stage', value: <Badge tone="brand">Active</Badge> },
              { label: 'Joined', value: '14 March 2026' },
            ]}
          />
        </div>
      </Row>

      <Row label="EmptyState">
        <Card>
          <EmptyState
            title="No clients yet"
            description="Add your first client to start building plans and tracking progress."
            action={<Button variant="primary">Add client</Button>}
            secondaryAction={<Button variant="ghost">Import from CSV</Button>}
            icon={
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                <path
                  d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            }
          />
        </Card>
      </Row>

      <Row label="ErrorState">
        <Card>
          <ErrorState
            title="We couldn't load your clients"
            whatToDoNext="Check your connection and try again. If this keeps happening, contact support with the reference below."
            action={<Button variant="primary">Try again</Button>}
            reference="req_01HX2K9M4P"
          />
        </Card>
      </Row>

      <Row label="ConfirmDialog">
        <Button variant="danger" onClick={() => setConfirmOpen(true)}>
          Delete plan
        </Button>
        <ConfirmDialog
          open={confirmOpen}
          onCancel={() => setConfirmOpen(false)}
          onConfirm={() => setConfirmOpen(false)}
          title="Delete Priya Sharma's plan?"
          consequence="The plan and its 3 revisions will be permanently deleted. Priya will no longer see it in her portal, and her adherence history for this plan will be lost."
          confirmLabel="Delete plan"
        />
      </Row>
    </Section>
  )
}

// ─── Shells ──────────────────────────────────────────────────────────────

const NAV = [
  { id: 'dashboard', label: 'Dashboard', href: '#' },
  { id: 'clients', label: 'Clients', href: '#', badge: 4 },
  { id: 'plans', label: 'Plans', href: '#' },
  { id: 'messages', label: 'Messages', href: '#' },
]

function Shells() {
  return (
    <Section
      title="Layout shells"
      note="Rendered at reduced scale. Nav items come from each app's IA manifest (NFR-057), never hand-written per screen."
    >
      <Row label="AppShell — practitioner">
        <div className="gallery-frame">
          <AppShell navItems={NAV} activeNavId="clients" brand="WellnessCRM">
            <PageHeader title="Clients" description="4 active" />
          </AppShell>
        </div>
      </Row>

      <Row label="MobileShell — client PWA">
        <div className="gallery-frame gallery-frame-phone">
          <MobileShell
            navItems={[
              { id: 'today', label: 'Today', href: '#', icon: <NavDot /> },
              { id: 'plan', label: 'Plan', href: '#', icon: <NavDot /> },
              { id: 'progress', label: 'Progress', href: '#', icon: <NavDot /> },
              { id: 'messages', label: 'Messages', href: '#', icon: <NavDot />, badge: 2 },
            ]}
            activeNavId="today"
            title="Today"
          >
            <Card>
              <CardBody>Breakfast — poha with peanuts</CardBody>
            </Card>
          </MobileShell>
        </div>
      </Row>

      <Row label="AdminShell — operator">
        <div className="gallery-frame">
          <AdminShell
            navItems={[
              { id: 'tenants', label: 'Tenants', href: '#' },
              { id: 'foods', label: 'Food catalogue', href: '#' },
              { id: 'jobs', label: 'Jobs', href: '#' },
            ]}
            activeNavId="foods"
            operatorLabel="ops@wellnesscrm"
            environment="staging"
          >
            <PageHeader title="Food catalogue" />
          </AdminShell>
        </div>
      </Row>
    </Section>
  )
}

function NavDot() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <circle cx="12" cy="12" r="9" />
    </svg>
  )
}
