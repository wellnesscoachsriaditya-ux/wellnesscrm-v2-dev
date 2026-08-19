/**
 * The clinical workspace state — PRD M3.
 *
 * 🔒 The API layer (Arch §4.4). The panels render what this returns and call
 * what it exposes; they never fetch.
 *
 * Four independent sections: assessments, measurements, consultation notes,
 * and documents. Each loads its own data and provides its own mutation
 * callbacks.
 */

import { useCallback, useEffect, useState } from 'react'
import { ApiError } from '@wellnesscrm/api-client'
import {
  fetchAssessments,
  fetchMeasurements,
  fetchConsultationNotes,
  fetchDocuments,
  recordMeasurement,
  createConsultationNote,
  editConsultationNote,
  archiveConsultationNote,
  type AssessmentSummary,
  type MeasurementResponse,
  type ConsultationNoteResponse,
  type ClientDocumentResponse,
} from './clinicalApi'

function messageOf(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? `${cause.message} ${cause.action}` : fallback
}

// ─── Assessment state ────────────────────────────────────────────────────

export interface AssessmentState {
  assessments: AssessmentSummary[]
  loading: boolean
  error: string | null
}

export function useAssessments(clientId: string): AssessmentState {
  const [assessments, setAssessments] = useState<AssessmentSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    fetchAssessments(clientId, controller.signal)
      .then((rows) => {
        setAssessments(rows)
        setError(null)
      })
      .catch((err) => {
        if (controller.signal.aborted) return
        setError(messageOf(err, 'Could not load assessments'))
      })
      .finally(() => setLoading(false))
    return () => controller.abort()
  }, [clientId])

  return { assessments, loading, error }
}

// ─── Measurement state ───────────────────────────────────────────────────

export interface MeasurementState {
  measurements: MeasurementResponse[]
  loading: boolean
  error: string | null
  busy: boolean
  addMeasurement: (body: {
    measured_on: string
    weight_kg?: number | null
    height_cm?: number | null
    waist_cm?: number | null
    hip_cm?: number | null
    body_fat_pct?: number | null
    notes?: string | null
    confirm_implausible?: boolean
  }) => Promise<void>
}

export function useMeasurements(clientId: string): MeasurementState {
  const [measurements, setMeasurements] = useState<MeasurementResponse[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    fetchMeasurements(clientId, true, controller.signal)
      .then((rows) => {
        setMeasurements(rows)
        setError(null)
      })
      .catch((err) => {
        if (controller.signal.aborted) return
        setError(messageOf(err, 'Could not load measurements'))
      })
      .finally(() => setLoading(false))
    return () => controller.abort()
  }, [clientId])

  const addMeasurement = useCallback(
    async (body: Parameters<typeof recordMeasurement>[1]) => {
      setBusy(true)
      try {
        const row = await recordMeasurement(clientId, body)
        setMeasurements((prev) => [row, ...prev])
        setError(null)
      } catch (err) {
        setError(messageOf(err, 'Could not record measurement'))
      } finally {
        setBusy(false)
      }
    },
    [clientId],
  )

  return { measurements, loading, error, busy, addMeasurement }
}

// ─── Consultation notes state ────────────────────────────────────────────

export interface ConsultationNoteState {
  notes: ConsultationNoteResponse[]
  loading: boolean
  error: string | null
  busy: boolean
  addNote: (noteDate: string, body: string) => Promise<void>
  editNote: (noteId: string, body: string) => Promise<void>
  archiveNote: (noteId: string) => Promise<void>
}

export function useConsultationNotes(clientId: string): ConsultationNoteState {
  const [notes, setNotes] = useState<ConsultationNoteResponse[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    fetchConsultationNotes(clientId, controller.signal)
      .then((rows) => {
        setNotes(rows)
        setError(null)
      })
      .catch((err) => {
        if (controller.signal.aborted) return
        setError(messageOf(err, 'Could not load consultation notes'))
      })
      .finally(() => setLoading(false))
    return () => controller.abort()
  }, [clientId])

  const addNote = useCallback(
    async (noteDate: string, body: string) => {
      setBusy(true)
      try {
        const row = await createConsultationNote(clientId, { note_date: noteDate, body })
        setNotes((prev) => [row, ...prev])
        setError(null)
      } catch (err) {
        setError(messageOf(err, 'Could not add note'))
      } finally {
        setBusy(false)
      }
    },
    [clientId],
  )

  const editNote = useCallback(
    async (noteId: string, body: string) => {
      setBusy(true)
      try {
        const updated = await editConsultationNote(clientId, noteId, { body })
        setNotes((prev) => prev.map((n) => (n.id === noteId ? updated : n)))
        setError(null)
      } catch (err) {
        setError(messageOf(err, 'Could not edit note'))
      } finally {
        setBusy(false)
      }
    },
    [clientId],
  )

  const archiveNote = useCallback(
    async (noteId: string) => {
      setBusy(true)
      try {
        const archived = await archiveConsultationNote(clientId, noteId)
        setNotes((prev) => prev.map((n) => (n.id === noteId ? archived : n)))
        setError(null)
      } catch (err) {
        setError(messageOf(err, 'Could not archive note'))
      } finally {
        setBusy(false)
      }
    },
    [clientId],
  )

  return { notes, loading, error, busy, addNote, editNote, archiveNote }
}

// ─── Documents state ─────────────────────────────────────────────────────

export interface DocumentState {
  documents: ClientDocumentResponse[]
  loading: boolean
  error: string | null
}

export function useDocuments(clientId: string): DocumentState {
  const [documents, setDocuments] = useState<ClientDocumentResponse[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    fetchDocuments(clientId, false, controller.signal)
      .then((rows) => {
        setDocuments(rows)
        setError(null)
      })
      .catch((err) => {
        if (controller.signal.aborted) return
        setError(messageOf(err, 'Could not load documents'))
      })
      .finally(() => setLoading(false))
    return () => controller.abort()
  }, [clientId])

  return { documents, loading, error }
}
