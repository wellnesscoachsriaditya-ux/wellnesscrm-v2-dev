/**
 * Clinical documents panel — FR-M3-024…027, EC-M3-04.
 *
 * 🔒 Renders and reports (Arch §4.4).
 *
 * ⚠️ Metadata only. The bytes live in object storage; `file_id` is passed to the
 * download action which will fetch a signed URL (ADR-12).
 */

import { useState, useRef } from 'react'
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  ConfirmDialog,
  EmptyState,
  FormField,
  Input,
  Spinner,
  Table,
  TableHead,
  TableBody,
  TableRow,
  TableHeaderCell,
  TableCell,
} from '@wellnesscrm/design-system'

export interface ClientDocumentView {
  id: string
  fileId: string
  documentType: string
  documentDate: string | null
  uploadedBy: 'practitioner' | 'client' | 'system'
  description: string | null
  createdAt: string
  archivedAt: string | null
}

export interface ClinicalDocumentsPanelProps {
  documents: readonly ClientDocumentView[]
  loading?: boolean
  error?: string | null
  busy?: boolean
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    })
  } catch {
    return iso
  }
}

export function ClinicalDocumentsPanel({
  documents,
  loading = false,
  error = null,
  busy = false,
}: ClinicalDocumentsPanelProps) {
  // ⏳ Uploading is deferred to S3 Slice A where the file upload hook and signed URL flow exist.
  // This panel renders the list.

  return (
    <Card>
      <CardHeader 
        title="Documents" 
        description="Lab reports, prescriptions, and clinical files."
        actions={
          <Button size="sm" variant="secondary" disabled={true} title="Upload arriving in S3">
            Upload document
          </Button>
        }
      />
      <CardBody>
        {error !== null && <p role="alert" style={{ color: 'var(--ds-colour-negative)' }}>{error}</p>}
        {loading && <Spinner label="Loading documents…" />}
        
        {!loading && error === null && documents.length === 0 && (
          <EmptyState
            title="No documents attached"
            description="Upload lab reports or medical records for this client."
          />
        )}
        
        {documents.length > 0 && (
          <div style={{ overflowX: 'auto' }}>
            <Table caption="Attached clinical documents">
              <TableHead>
                <TableRow>
                  <TableHeaderCell>Type</TableHeaderCell>
                  <TableHeaderCell>Document date</TableHeaderCell>
                  <TableHeaderCell>Description</TableHeaderCell>
                  <TableHeaderCell>Added by</TableHeaderCell>
                  <TableHeaderCell>Added on</TableHeaderCell>
                  <TableHeaderCell>Actions</TableHeaderCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {documents.map((doc) => (
                  <TableRow key={doc.id}>
                    <TableCell><strong>{doc.documentType}</strong></TableCell>
                    <TableCell>{doc.documentDate ? formatDate(doc.documentDate) : '—'}</TableCell>
                    <TableCell>{doc.description ?? '—'}</TableCell>
                    <TableCell>
                      <Badge tone={doc.uploadedBy === 'client' ? 'info' : 'neutral'}>
                        {doc.uploadedBy === 'client' ? 'Client' : 'Practice'}
                      </Badge>
                    </TableCell>
                    <TableCell>{formatDate(doc.createdAt)}</TableCell>
                    <TableCell>
                      {doc.archivedAt ? (
                        <Badge tone="warning">Archived</Badge>
                      ) : (
                        <Button size="sm" variant="secondary" disabled={true} title="Download arriving in S3">
                          Download
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardBody>
    </Card>
  )
}
