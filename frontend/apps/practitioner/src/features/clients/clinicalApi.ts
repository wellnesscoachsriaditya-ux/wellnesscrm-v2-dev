/**
 * The clinical workspace API — PRD M3, API §7.3.
 *
 * 🔒 **Arch §4.4 / NFR-068 — the layer allowed to talk to the API.** R8 fails
 * the build if anything under `components/` imports this or calls `fetch`.
 *
 * Four resource groups, each nested under `/app/clients/{client_id}`:
 * assessments, measurements, consultation notes, and documents.
 */

import { createApiClient } from '@wellnesscrm/api-client'
import type { components } from '@wellnesscrm/api-client'

export type AssessmentSummary = components['schemas']['AssessmentSummary']
export type AssessmentResponseBody = components['schemas']['AssessmentResponseBody']
export type SaveAnswersResponse = components['schemas']['SaveAnswersResponse']
export type AssessmentCompletionResponse = components['schemas']['AssessmentCompletionResponse']
export type NutritionProfileResponse = components['schemas']['NutritionProfileResponse']
export type MeasurementResponse = components['schemas']['MeasurementResponse']
export type ConsultationNoteResponse = components['schemas']['ConsultationNoteResponse']
export type ClientDocumentResponse = components['schemas']['ClientDocumentResponse']

const api = createApiClient()

// ─── Assessments (FR-M3-001…008) ─────────────────────────────────────────

export async function fetchAssessments(
  clientId: string,
  signal?: AbortSignal,
): Promise<AssessmentSummary[]> {
  return api.request('get', '/api/v1/app/clients/{client_id}/assessments', {
    path: { client_id: clientId },
    ...(signal ? { signal } : {}),
  })
}

export async function startOrResumeAssessment(
  clientId: string,
  restart = false,
): Promise<AssessmentResponseBody> {
  return api.request('post', '/api/v1/app/clients/{client_id}/assessments', {
    path: { client_id: clientId },
    query: { restart },
  })
}

export async function fetchAssessment(
  clientId: string,
  responseId: string,
  signal?: AbortSignal,
): Promise<AssessmentResponseBody> {
  return api.request('get', '/api/v1/app/clients/{client_id}/assessments/{response_id}', {
    path: { client_id: clientId, response_id: responseId },
    ...(signal ? { signal } : {}),
  })
}

export async function saveAssessmentProgress(
  clientId: string,
  responseId: string,
  answers: Record<string, unknown>,
  completedSections?: string[],
): Promise<SaveAnswersResponse> {
  return api.request('patch', '/api/v1/app/clients/{client_id}/assessments/{response_id}', {
    path: { client_id: clientId, response_id: responseId },
    body: {
      answers,
      ...(completedSections !== undefined ? { completed_sections: completedSections } : {}),
    },
  })
}

export async function completeAssessment(
  clientId: string,
  responseId: string,
): Promise<AssessmentCompletionResponse> {
  return api.request('post', '/api/v1/app/clients/{client_id}/assessments/{response_id}/complete', {
    path: { client_id: clientId, response_id: responseId },
  })
}

export async function fetchNutritionProfile(
  clientId: string,
  signal?: AbortSignal,
): Promise<NutritionProfileResponse | null> {
  return api.request('get', '/api/v1/app/clients/{client_id}/nutrition-profile', {
    path: { client_id: clientId },
    ...(signal ? { signal } : {}),
  })
}

// ─── Measurements (FR-M3-011…015) ────────────────────────────────────────

export async function fetchMeasurements(
  clientId: string,
  preferredOnly = false,
  signal?: AbortSignal,
): Promise<MeasurementResponse[]> {
  return api.request('get', '/api/v1/app/clients/{client_id}/measurements', {
    path: { client_id: clientId },
    query: { preferred_only: preferredOnly },
    ...(signal ? { signal } : {}),
  })
}

export async function recordMeasurement(
  clientId: string,
  body: {
    measured_on: string
    weight_kg?: number | null
    height_cm?: number | null
    waist_cm?: number | null
    hip_cm?: number | null
    body_fat_pct?: number | null
    notes?: string | null
    confirm_implausible?: boolean
  },
): Promise<MeasurementResponse> {
  return api.request('post', '/api/v1/app/clients/{client_id}/measurements', {
    path: { client_id: clientId },
    body,
  })
}

// ─── Consultation notes (FR-M3-018…021) ──────────────────────────────────

export async function fetchConsultationNotes(
  clientId: string,
  signal?: AbortSignal,
): Promise<ConsultationNoteResponse[]> {
  return api.request('get', '/api/v1/app/clients/{client_id}/consultation-notes', {
    path: { client_id: clientId },
    ...(signal ? { signal } : {}),
  })
}

export async function createConsultationNote(
  clientId: string,
  body: { note_date: string; body: string },
): Promise<ConsultationNoteResponse> {
  return api.request('post', '/api/v1/app/clients/{client_id}/consultation-notes', {
    path: { client_id: clientId },
    body,
  })
}

export async function editConsultationNote(
  clientId: string,
  noteId: string,
  body: { body: string },
): Promise<ConsultationNoteResponse> {
  return api.request('patch', '/api/v1/app/clients/{client_id}/consultation-notes/{note_id}', {
    path: { client_id: clientId, note_id: noteId },
    body,
  })
}

export async function archiveConsultationNote(
  clientId: string,
  noteId: string,
): Promise<ConsultationNoteResponse> {
  return api.request('post', '/api/v1/app/clients/{client_id}/consultation-notes/{note_id}/archive', {
    path: { client_id: clientId, note_id: noteId },
  })
}

// ─── Documents (FR-M3-024…027) ───────────────────────────────────────────

export async function fetchDocuments(
  clientId: string,
  includeArchived = false,
  signal?: AbortSignal,
): Promise<ClientDocumentResponse[]> {
  return api.request('get', '/api/v1/app/clients/{client_id}/documents', {
    path: { client_id: clientId },
    query: { include_archived: includeArchived },
    ...(signal ? { signal } : {}),
  })
}

export async function attachDocument(
  clientId: string,
  body: {
    file_id: string
    document_type: string
    document_date?: string | null
    description?: string | null
  },
): Promise<ClientDocumentResponse> {
  return api.request('post', '/api/v1/app/clients/{client_id}/documents', {
    path: { client_id: clientId },
    body,
  })
}

export async function fetchDocumentUrl(
  clientId: string,
  documentId: string,
): Promise<{ url: string }> {
  return api.request('get', '/api/v1/app/clients/{client_id}/documents/{document_id}/url', {
    path: { client_id: clientId, document_id: documentId },
  })
}

export async function archiveDocument(
  clientId: string,
  documentId: string,
): Promise<ClientDocumentResponse> {
  return api.request('post', '/api/v1/app/clients/{client_id}/documents/{document_id}/archive', {
    path: { client_id: clientId, document_id: documentId },
  })
}
