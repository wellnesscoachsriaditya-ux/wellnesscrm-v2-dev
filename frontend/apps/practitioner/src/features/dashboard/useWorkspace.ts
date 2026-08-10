/**
 * The dashboard's state — S2 Slice G.
 *
 * 🔒 The API layer (Arch §4.4). The screen renders what this returns; it never
 * fetches, and it never derives a domain fact.
 *
 * 🔒 **Every figure here comes from an endpoint that already exists.** The
 * dashboard is the most tempting place in the product to invent a metric, and an
 * invented one would be a domain calculation in the browser (NFR-068, NFR-072).
 * Two things it therefore does *not* show:
 *
 * * **No active-client count against the plan limit.** FR-M1-001 asks for a
 *   persistent indicator, and no endpoint reports usage — those figures exist
 *   only inside a 402 envelope, at the moment of refusal. Counting
 *   `stage === 'active'` across a fetched page would be an entitlement
 *   calculation performed in the browser, and it would disagree with the server
 *   as soon as the caseload exceeded one page.
 * * **No appointments.** M6 is S8; there is no endpoint to read.
 *
 * ⚠️ **Two independent reads that fail independently.** `allSettled`, so a
 * broken enquiry queue still leaves the recent-client panel on screen. A
 * dashboard is a summary of several things: one dead panel must not take the
 * whole page with it.
 */

import { useCallback, useEffect, useState } from 'react'
import { ApiError } from '@wellnesscrm/api-client'
import { fetchClients, type ClientListItem } from '../clients/discoveryApi'
import { fetchNeedsResponse, type EnquiryListItem } from '../clients/enquiriesApi'

/**
 * How many rows each panel shows.
 *
 * 🔒 A dashboard is a prompt to act, not a second copy of the list screen. Five
 * is enough to see whether anything is waiting; each panel links to the full
 * view for the rest, which is what keeps this page to two small queries.
 */
const PANEL_SIZE = 5

export interface WorkspaceState {
  /** 🔒 Oldest first — FR-M2-011. The oldest unanswered enquiry is the urgent one. */
  waitingEnquiries: EnquiryListItem[]
  /**
   * The server's count of everything waiting, usually more than the rows above.
   * `null` when the read failed or no count was returned.
   */
  waitingTotal: number | null
  /** Most recently active clients — "carry on where you left off". */
  recentClients: ClientListItem[]
  loading: boolean
  /** ⚠️ One slot per panel: a dashboard degrades panel by panel. */
  enquiriesError: string | null
  clientsError: string | null
  reload: () => void
}

function messageOf(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? `${cause.message} ${cause.action}` : fallback
}

export function useWorkspace(): WorkspaceState {
  const [waitingEnquiries, setWaitingEnquiries] = useState<EnquiryListItem[]>([])
  const [waitingTotal, setWaitingTotal] = useState<number | null>(null)
  const [recentClients, setRecentClients] = useState<ClientListItem[]>([])
  const [loading, setLoading] = useState(true)
  const [enquiriesError, setEnquiriesError] = useState<string | null>(null)
  const [clientsError, setClientsError] = useState<string | null>(null)
  const [reloadToken, setReloadToken] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)

    void Promise.allSettled([
      // 🔒 `includeTotal` here and nowhere else on this page. The count *is* the
      // panel's point — "how many people are waiting on me" — and it costs one
      // COUNT on a screen opened occasionally, not one per keystroke.
      fetchNeedsResponse({ limit: PANEL_SIZE, includeTotal: true }, controller.signal),
      // ⚠️ `-recent_activity` is the server's own sort vocabulary (API §6.3), so
      // "recent" is defined by an index rather than by this file.
      fetchClients({ sort: '-recent_activity', limit: PANEL_SIZE }, controller.signal),
    ]).then(([enquiryResult, clientResult]) => {
      if (controller.signal.aborted) return

      if (enquiryResult.status === 'fulfilled') {
        setWaitingEnquiries(enquiryResult.value.items)
        setWaitingTotal(enquiryResult.value.page.total ?? null)
        setEnquiriesError(null)
      } else {
        setWaitingEnquiries([])
        setWaitingTotal(null)
        setEnquiriesError(
          messageOf(enquiryResult.reason, 'Enquiries waiting for a reply could not be loaded.'),
        )
      }

      if (clientResult.status === 'fulfilled') {
        setRecentClients(clientResult.value.items)
        setClientsError(null)
      } else {
        setRecentClients([])
        setClientsError(messageOf(clientResult.reason, 'Your recent clients could not be loaded.'))
      }

      setLoading(false)
    })

    return () => controller.abort()
  }, [reloadToken])

  return {
    waitingEnquiries,
    waitingTotal,
    recentClients,
    loading,
    enquiriesError,
    clientsError,
    reload: useCallback(() => setReloadToken((token) => token + 1), []),
  }
}
