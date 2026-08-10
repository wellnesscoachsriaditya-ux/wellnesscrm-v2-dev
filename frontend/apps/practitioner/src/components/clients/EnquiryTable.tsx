/**
 * The enquiry list — FR-M2-011, AC-M2-005, US-M2-03.
 *
 * 🔒 Renders and reports (Arch §4.4). R8 fails the build if this reaches the API.
 *
 * 🔒 **A real `<table>`.** The queue is tabular data a practitioner scans by
 * column — who, how long, which channel — and a table gives row/column
 * announcements, `scope`d headers and native keyboard navigation for free, all
 * of which NFR-062 requires and a div grid has to reimplement.
 *
 * ⚠️ **Age is rendered, never computed.** `ageHours` arrives from the server
 * (Principle 3). The only thing this file decides is how to spell it.
 */

import {
  Badge,
  Button,
  EmptyState,
  Spinner,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeaderCell,
  TableRow,
} from '@wellnesscrm/design-system'
import { STAGE_LABEL, STAGE_TONE, type StageValue } from './stages'
import { ageLabel, sourceLabel, type LeadSource } from './sources'

export interface EnquiryRowView {
  id: string
  clientId: string | null
  name: string
  contact: string
  goal: string
  source: LeadSource | null
  ageHours: number
  /** 🔒 Server-decided (FR-M2-011) — the threshold is a server rule. */
  isAgeing: boolean
  isAnswered: boolean
  isRepeatEnquiry: boolean
  stage: StageValue | null
  ownerName: string | null
}

export interface EnquiryTableProps {
  enquiries: readonly EnquiryRowView[]
  /** Which question the list answers — changes the empty state's wording. */
  view: 'needs-response' | 'all'
  loading?: boolean
  loadingMore?: boolean
  hasMore?: boolean
  respondingId?: string | null
  onOpenClient: (clientId: string) => void
  onRespond: (submissionId: string) => void
  onLoadMore: () => void
}

export function EnquiryTable({
  enquiries,
  view,
  loading = false,
  loadingMore = false,
  hasMore = false,
  respondingId = null,
  onOpenClient,
  onRespond,
  onLoadMore,
}: EnquiryTableProps) {
  if (loading) return <Spinner label="Loading enquiries…" />

  if (enquiries.length === 0) {
    // 🔒 NFR-064, and the two states say opposite things. An empty *queue* is
    // success — every enquiry has been answered — and saying so is correct. An
    // empty *archive* means the form has never been used, which needs the
    // action that fixes it.
    return view === 'needs-response' ? (
      <EmptyState
        title="Nothing waiting"
        description="Every enquiry has been responded to. New ones will appear here."
      />
    ) : (
      <EmptyState
        title="No enquiries yet"
        description="Share your enquiry form link to start collecting leads."
      />
    )
  }

  return (
    <>
      <Table caption="Enquiries" captionHidden>
        <TableHead>
          <TableRow>
            <TableHeaderCell>Name</TableHeaderCell>
            <TableHeaderCell>Contact</TableHeaderCell>
            <TableHeaderCell>Looking for</TableHeaderCell>
            <TableHeaderCell>Channel</TableHeaderCell>
            {/* 🔒 AC-M2-005 — "visible in a single view with their age". */}
            <TableHeaderCell>Waiting</TableHeaderCell>
            <TableHeaderCell>Stage</TableHeaderCell>
            <TableHeaderCell>Action</TableHeaderCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {enquiries.map((enquiry) => (
            <TableRow key={enquiry.id} muted={enquiry.isAnswered}>
              <TableCell>
                {/* ⚠️ A link only when there is a client to open. An enquiry
                  * whose client was erased (FR-M0-027) keeps its evidence but
                  * has nothing to navigate to, and a dead link reads as a bug. */}
                {enquiry.clientId !== null ? (
                  <Button
                    variant="ghost"
                    onClick={() => {
                      if (enquiry.clientId !== null) onOpenClient(enquiry.clientId)
                    }}
                  >
                    {enquiry.name}
                  </Button>
                ) : (
                  <span>{enquiry.name}</span>
                )}
                {/* 🔒 EC-M2-02 — shown to the *practitioner*, never to the
                  * submitter. "They have enquired before" is what makes a repeat
                  * enquiry actionable rather than confusing. */}
                {enquiry.isRepeatEnquiry && <Badge tone="neutral">Enquired before</Badge>}
              </TableCell>
              <TableCell>{enquiry.contact}</TableCell>
              <TableCell>{enquiry.goal}</TableCell>
              <TableCell>{sourceLabel(enquiry.source)}</TableCell>
              <TableCell>
                {/* 🔒 The warning FR-M2-011 exists to raise. `isAgeing` is the
                  * server's decision — the threshold is a server rule, and a
                  * duplicate constant here would drift the day it is tuned. */}
                {enquiry.isAgeing ? (
                  <Badge tone="warning">{ageLabel(enquiry.ageHours)}</Badge>
                ) : (
                  ageLabel(enquiry.ageHours)
                )}
              </TableCell>
              <TableCell>
                {enquiry.stage !== null ? (
                  <Badge tone={STAGE_TONE[enquiry.stage]}>{STAGE_LABEL[enquiry.stage]}</Badge>
                ) : (
                  '—'
                )}
              </TableCell>
              <TableCell>
                {enquiry.isAnswered ? (
                  <Badge tone="success">Responded</Badge>
                ) : (
                  <Button
                    variant="secondary"
                    loading={respondingId === enquiry.id}
                    onClick={() => onRespond(enquiry.id)}
                  >
                    Mark responded
                  </Button>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>

      {hasMore && (
        <Button variant="secondary" onClick={onLoadMore} loading={loadingMore}>
          Load more enquiries
        </Button>
      )}
    </>
  )
}
