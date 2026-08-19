/**
 * The premium Coach command center — S2 Slice G, redesigned.
 *
 * 🔒 **No invented metrics.** Every figure comes from an endpoint that exists:
 * the waiting-enquiry queue and recent clients (`useWorkspace`), and the caseload
 * counts (`usePracticeCounts`, server `total`s). Metrics with no endpoint
 * (appointments, adherence, revenue) are deliberately absent — see the tests.
 *
 * 🔒 The screen composes; the hooks fetch; nothing here derives a domain fact.
 */
import { useNavigate } from 'react-router-dom'
import { useWorkspace } from '../features/dashboard/useWorkspace'
import { usePracticeCounts } from '../features/dashboard/usePracticeCounts'
import { Avatar, Donut, Icon, KpiCard, LinkAction, QuickAction, SectionCard, Skeleton, StatusPill, cx } from '../premium/ui'
import type { Accent } from '../premium/ui'
import styles from './Dashboard.module.css'

function greetingWord(): string {
  const h = new Date().getHours()
  if (h < 12) return 'Good morning'
  if (h < 17) return 'Good afternoon'
  return 'Good evening'
}
const NUM = (v: number | null) => (v === null ? '—' : v.toLocaleString('en-IN'))
function ago(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })
}

export function Dashboard() {
  const navigate = useNavigate()
  const ws = useWorkspace()
  const counts = usePracticeCounts()

  const waiting = ws.waitingEnquiries
  const waitingCount = ws.waitingTotal ?? waiting.length
  const today = new Date().toLocaleDateString('en-IN', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })

  const donutSegs: Array<{ label: string; value: number; accent: Accent }> = [
    { label: 'Active', value: counts.active ?? 0, accent: 'emerald' },
    { label: 'Consultation', value: counts.consultation ?? 0, accent: 'violet' },
    { label: 'Contacted', value: counts.contacted ?? 0, accent: 'blue' },
    { label: 'Leads', value: counts.leads ?? 0, accent: 'teal' },
    { label: 'Paused', value: counts.paused ?? 0, accent: 'amber' },
  ]
  const donutTotal = donutSegs.reduce((s, x) => s + x.value, 0)

  return (
    <div className={styles.page}>
      <h1 className={styles.srOnly}>Dashboard</h1>

      <header className={styles.hero}>
        <div className={styles.heroLeft}>
          <p className={styles.greeting}>{greetingWord()}, Coach 👋</p>
          <p className={styles.subtitle}>Here's what needs your attention today.</p>
        </div>
        <div className={styles.heroActions}>
          <span className={styles.datePill}>
            <Icon name="calendar" size={16} /> {today}
          </span>
          <button type="button" className={styles.btnGhost} onClick={() => navigate('/plans')}>
            <Icon name="plans" size={16} /> Create Plan
          </button>
          <button type="button" className={styles.btnPrimary} onClick={() => navigate('/clients/new')}>
            <Icon name="plus" size={16} /> Add Client
          </button>
        </div>
      </header>

      <section className={styles.kpis} aria-label="Key figures">
        <KpiCard index={0} accent="emerald" icon="clients" label="Active Clients" value={NUM(counts.active)} />
        <KpiCard index={1} accent="amber" icon="messages" label="Waiting for Reply" value={NUM(ws.waitingTotal)} />
        <KpiCard index={2} accent="blue" icon="leads" label="New Leads" value={NUM(counts.leads)} />
        <KpiCard index={3} accent="violet" icon="progress" label="Total Clients" value={NUM(counts.total)} />
      </section>

      <div className={styles.grid}>
        <SectionCard
          title="Needs attention"
          {...(waitingCount > 0 ? { count: waitingCount } : {})}
          action={<LinkAction onClick={() => navigate('/leads')}>All enquiries</LinkAction>}
        >
          {ws.enquiriesError !== null ? (
            <p role="alert" className={styles.alert}>{ws.enquiriesError}</p>
          ) : ws.loading ? (
            <div className={styles.rows}>
              {[0, 1, 2].map((i) => (
                <div key={i} className={styles.row}><Skeleton h={40} w={40} r={20} /><Skeleton h={14} w={180} /></div>
              ))}
            </div>
          ) : waiting.length === 0 ? (
            <div className={styles.empty}>
              <span className={styles.emptyTitle}>Nothing waiting</span>
              <span className={styles.emptyDesc}>Every enquiry has been responded to. New ones will appear here.</span>
            </div>
          ) : (
            <>
              <p className={styles.subtitle} style={{ marginBottom: 8 }}>
                {waitingCount === 1 ? '1 person is waiting to hear from you.' : `${waitingCount} people are waiting to hear from you.`}
              </p>
              <div className={styles.rows}>
                {waiting.map((e) => (
                  <div key={e.id} className={styles.row}>
                    <span className={e.is_ageing ? styles.rowDotCoral : styles.rowDotAmber} />
                    <Avatar name={e.submitted_name} size={38} />
                    <div className={styles.rowMain}>
                      <span className={styles.rowName}>{e.submitted_name}</span>
                      <span className={styles.rowMeta}>{e.primary_goal ?? 'New enquiry'}</span>
                    </div>
                    <div className={styles.rowRight}>
                      <span className={styles.rowTime}>{Math.round(e.age_hours)}h waiting</span>
                      <button type="button" className={styles.rowBtn} onClick={() => navigate('/leads')}>
                        Reply <Icon name="chevronRight" size={14} />
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </SectionCard>

        <SectionCard title="Recently active" action={<LinkAction onClick={() => navigate('/clients')}>All clients</LinkAction>}>
          {ws.clientsError !== null ? (
            <p role="alert" className={styles.alert}>{ws.clientsError}</p>
          ) : ws.loading ? (
            <div className={styles.rows}>
              {[0, 1, 2].map((i) => (
                <div key={i} className={styles.row}><Skeleton h={38} w={38} r={19} /><Skeleton h={14} w={140} /></div>
              ))}
            </div>
          ) : ws.recentClients.length === 0 ? (
            <div className={styles.empty}>
              <span className={styles.emptyTitle}>No clients yet</span>
              <span className={styles.emptyDesc}>Add your first client, or share your enquiry form and let them come to you.</span>
              <div className={styles.emptyActions}>
                <button type="button" className={styles.btnPrimary} onClick={() => navigate('/clients/new')}>Add a client</button>
              </div>
            </div>
          ) : (
            <div className={styles.rows}>
              {ws.recentClients.map((c) => (
                <button key={c.id} type="button" className={cx(styles.row)} onClick={() => navigate(`/clients/${c.id}`)} style={{ border: 0, background: 'none', textAlign: 'left', cursor: 'pointer', font: 'inherit' }}>
                  <Avatar name={c.full_name} size={38} />
                  <div className={styles.rowMain}>
                    <span className={styles.rowName}>{c.full_name}</span>
                  </div>
                  <div className={styles.rowRight}>
                    <StatusPill value={c.stage} />
                    <span className={styles.rowTime}>{ago(c.updated_at)}</span>
                  </div>
                </button>
              ))}
            </div>
          )}
        </SectionCard>
      </div>

      <SectionCard title="Caseload by stage" className={cx(styles.insightSection)}>
        {counts.loading ? (
          <div className={styles.insight}><Skeleton h={168} w={168} r={84} /></div>
        ) : (
          <div className={styles.insight}>
            <Donut segments={donutSegs} centerValue={NUM(counts.total)} centerLabel="Clients" />
            <div className={styles.legend}>
              {donutSegs.map((s) => (
                <div key={s.label} className={styles.legendRow}>
                  <span className={styles.legendDot} style={{ background: `var(--p-${s.accent})` }} />
                  <span className={styles.legendLabel}>{s.label}</span>
                  <span className={styles.legendVal}>{s.value}{donutTotal > 0 ? ` · ${Math.round((s.value / donutTotal) * 100)}%` : ''}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </SectionCard>

      <SectionCard title="Quick actions">
        <div className={styles.quickGrid}>
          <QuickAction accent="emerald" icon="clients" label="Add Client" onClick={() => navigate('/clients/new')} testid="qa-add-client" />
          <QuickAction accent="amber" icon="clipboard" label="Create Plan" onClick={() => navigate('/plans')} testid="qa-create-plan" />
          <QuickAction accent="blue" icon="leads" label="View Leads" onClick={() => navigate('/leads')} testid="qa-leads" />
          <QuickAction accent="violet" icon="messages" label="Send Message" onClick={() => navigate('/messages')} testid="qa-messages" />
        </div>
      </SectionCard>
    </div>
  )
}
