/**
 * The premium client management workspace — FR-M1-021/022.
 *
 * 🔒 The screen composes; `useClientList` fetches; nothing here derives a domain
 * fact or bypasses the api-client (Arch §4.4, R8). Scoping (AC-M1-006) is a
 * server WHERE clause — there is nothing here to get wrong.
 */
import { useNavigate } from 'react-router-dom'
import { Avatar, Icon, StatusPill, Skeleton, cx } from '../premium/ui'
import { useClientList } from '../features/clients/useClientList'
import type { ClientListItem } from '../features/clients/discoveryApi'
import styles from './ClientList.module.css'

const STAGE_CHIPS: Array<{ id: string; label: string }> = [
  { id: 'active', label: 'Active' },
  { id: 'lead', label: 'Leads' },
  { id: 'consultation_scheduled', label: 'Consultation' },
  { id: 'contacted', label: 'Contacted' },
  { id: 'paused', label: 'Paused' },
]

function ago(iso: string): string {
  return new Date(iso).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })
}

export function ClientList() {
  const navigate = useNavigate()
  const list = useClientList()

  const open = (id: string) => navigate(`/clients/${id}`)

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <div>
          <h1 className={styles.title}>Clients</h1>
          <p className={styles.subtitle}>Manage and track your clients{list.total !== null ? ` · ${list.total} total` : ''}</p>
        </div>
        <button type="button" className={styles.btnPrimary} onClick={() => navigate('/clients/new')} data-testid="add-client-button">
          <Icon name="plus" size={16} /> Add Client
        </button>
      </header>

      <div className={styles.chips}>
        <button
          type="button"
          className={cx(styles.chip, list.filters.stages.length === 0 && styles.chipActive)}
          onClick={() => list.clearFilters()}
        >
          All Clients
        </button>
        {STAGE_CHIPS.map((c) => (
          <button
            key={c.id}
            type="button"
            className={cx(styles.chip, list.filters.stages.includes(c.id) && styles.chipActive)}
            onClick={() => list.toggleStage(c.id)}
          >
            {c.label}
          </button>
        ))}
      </div>

      <div className={styles.toolbar}>
        <div className={styles.search}>
          <Icon name="search" size={18} />
          <input
            placeholder="Search clients by name, email or phone…"
            value={list.filters.search}
            onChange={(e) => list.setSearch(e.target.value)}
            data-testid="client-search-input"
          />
        </div>
        {list.sortOptions.length > 0 && (
          <select className={styles.sort} value={list.filters.sort} onChange={(e) => list.setSort(e.target.value)} aria-label="Sort clients">
            {list.sortOptions.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        )}
      </div>

      {list.error !== null ? (
        <div className={styles.errorBox}>Your clients could not be loaded. {list.requestId ? `Reference: ${list.requestId}` : ''}</div>
      ) : list.loading ? (
        <div className={styles.tableCard}>
          {[0, 1, 2, 3, 4].map((i) => (
            <div key={i} style={{ display: 'flex', gap: 12, padding: 16, alignItems: 'center' }}>
              <Skeleton h={40} w={40} r={20} /><Skeleton h={16} w={220} />
            </div>
          ))}
        </div>
      ) : list.clients.length === 0 ? (
        <div className={styles.emptyBox}>{list.isFiltered ? 'No clients match these filters.' : 'No clients yet — add your first client to get started.'}</div>
      ) : (
        <>
          {/* Desktop table */}
          <div className={styles.tableCard}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>Client</th><th>Status</th><th>City</th><th>Last activity</th><th aria-label="Open" />
                </tr>
              </thead>
              <tbody>
                {list.clients.map((c: ClientListItem) => (
                  <tr key={c.id} className={styles.tr} onClick={() => open(c.id)} data-testid={`client-row-${c.id}`}>
                    <td className={styles.td}>
                      <div className={styles.clientCell}>
                        <Avatar name={c.full_name} size={40} />
                        <div>
                          <div className={styles.clientName}>{c.full_name}</div>
                          <div className={styles.clientEmail}>{c.email ?? c.mobile ?? 'No contact'}</div>
                        </div>
                      </div>
                    </td>
                    <td className={styles.td}><StatusPill value={c.archived_at !== null ? 'archived' : c.stage} /></td>
                    <td className={cx(styles.td, styles.muted)}>{c.city ?? '—'}</td>
                    <td className={cx(styles.td, styles.muted)}>{ago(c.updated_at)}</td>
                    <td className={styles.td}><Icon name="chevronRight" size={18} className={cx(styles.chev)} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Mobile cards */}
          <div className={styles.cards}>
            {list.clients.map((c: ClientListItem) => (
              <button key={c.id} type="button" className={styles.card} onClick={() => open(c.id)}>
                <Avatar name={c.full_name} size={44} />
                <div className={styles.cardMain}>
                  <span className={styles.cardName}>{c.full_name}</span>
                  <span className={styles.cardMeta}>{c.email ?? c.mobile ?? 'No contact'}</span>
                </div>
                <div className={styles.cardRight}>
                  <StatusPill value={c.archived_at !== null ? 'archived' : c.stage} />
                  <span className={styles.cardMeta}>{ago(c.updated_at)}</span>
                </div>
              </button>
            ))}
          </div>

          {list.hasMore && (
            <button type="button" className={styles.loadMore} onClick={() => void list.loadMore()} disabled={list.loadingMore}>
              {list.loadingMore ? 'Loading…' : 'Load more clients'}
            </button>
          )}
        </>
      )}
    </div>
  )
}
