/**
 * The public enquiry form — S2 Slice H.
 *
 * 🔒 **What these pin is the privacy and consent surface**, because that is what
 * a UI regression would silently break and a code review would not notice:
 *
 * * EC-M2-07 — every "form unavailable" cause renders as the same neutral page,
 *   never as an error naming a cause.
 * * EC-M2-04 — no request is sent without consent; the prospect is pointed at
 *   the box.
 * * EC-M2-01 — a missing contact is corrected inline, before the request.
 * * FR-M2-009 — the link's `?source=` reaches the submit body.
 * * FR-M2-008 — the honeypot is hidden from people and sent when filled.
 * * AC-M2-003 — the acknowledgement replaces the form, exactly as the server
 *   wrote it.
 *
 * ⚠️ `fetch` is stubbed rather than the api-client module: the envelope decoding
 * in `@wellnesscrm/api-client` is part of what these tests exercise, and a
 * mocked client would assert that our own fake produces the shape our own code
 * expects.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { PublicEnquiryForm } from './PublicEnquiryForm'

const SLUG = 'demo-practice'

const FORM_RESPONSE = {
  form_id: '11111111-1111-1111-1111-111111111111',
  practice_name: 'Priya’s Nutrition Studio',
  title: 'Start your health journey',
  intro_text: 'Tell us a little about what you need, and Priya will reach out.',
  consent: {
    notice_id: '22222222-2222-2222-2222-222222222222',
    title: 'Privacy notice',
    body: 'We will use your details to contact you about your enquiry.',
    version: '2026-08-01',
  },
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

/** The API §5.1 envelope, for a failed request. */
function errorResponse(status: number, type: string) {
  return new Response(
    JSON.stringify({
      error: {
        type,
        message: 'Something failed.',
        action: 'Do the next thing.',
        request_id: 'req_test',
      },
    }),
    { status, headers: { 'content-type': 'application/json' } },
  )
}

function renderForm(initialEntry = `/enquire/${SLUG}`) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/enquire/:tenantSlug" element={<PublicEnquiryForm />} />
      </Routes>
    </MemoryRouter>,
  )
}

async function fillRequiredFields() {
  await userEvent.type(screen.getByLabelText(/your name/i), 'Ananya Rao')
  await userEvent.type(screen.getByLabelText(/mobile number/i), '9876543210')
  await userEvent.type(screen.getByLabelText(/what would you like help with/i), 'Lose 8kg')
  await userEvent.click(screen.getByRole('checkbox'))
}

const submittedBodies: Record<string, unknown>[] = []

/**
 * The request URL as a string.
 *
 * ⚠️ `String(input)` looks equivalent and is not: `RequestInfo` includes
 * `Request`, which stringifies to `[object Object]` — so a matcher would
 * silently stop matching. Same helper, same reason, as `ClientDetail.test.tsx`.
 */
function urlOf(input: RequestInfo | URL): string {
  if (typeof input === 'string') return input
  return input instanceof URL ? input.href : input.url
}

/** The request body as a string, for the same reason. */
function bodyOf(init: RequestInit | undefined): string {
  return typeof init?.body === 'string' ? init.body : '{}'
}

beforeEach(() => {
  submittedBodies.length = 0
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = urlOf(input)

      if (url.endsWith(`/api/v1/public/forms/${SLUG}/submit`)) {
        submittedBodies.push(JSON.parse(bodyOf(init)) as Record<string, unknown>)
        return Promise.resolve(
          jsonResponse(
            { submitted: true, message: 'Thank you. Priya will be in touch soon.' },
            202,
          ),
        )
      }
      if (url.endsWith(`/api/v1/public/forms/${SLUG}`)) {
        return Promise.resolve(jsonResponse(FORM_RESPONSE))
      }
      throw new Error(`Unexpected request in test: ${init?.method ?? 'GET'} ${url}`)
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('the public enquiry form', () => {
  it('shows the practice’s name and the form once the definition loads', async () => {
    renderForm()
    expect(await screen.findByRole('heading', { level: 1 })).toHaveTextContent(
      'Priya’s Nutrition Studio',
    )
    expect(screen.getByLabelText(/your name/i)).toBeInTheDocument()
    expect(screen.getByRole('checkbox')).toBeInTheDocument()
  })

  it('displays the consent notice body, not a link to it — NFR-051', async () => {
    // 🔒 DPDP requires consent against text the person actually saw. A link is
    // evidence they were offered the notice, not that it was in front of them.
    renderForm()
    await screen.findByRole('heading', { level: 1 })
    expect(screen.getByText(FORM_RESPONSE.consent.body)).toBeInTheDocument()
    expect(screen.getByText(/2026-08-01/)).toBeInTheDocument()
  })

  it('sends the link’s source attribution with the submission — FR-M2-009', async () => {
    renderForm(`/enquire/${SLUG}?source=instagram`)
    await screen.findByRole('heading', { level: 1 })

    await fillRequiredFields()
    await userEvent.click(screen.getByRole('button', { name: /send enquiry/i }))

    await waitFor(() => expect(submittedBodies).toHaveLength(1))
    expect(submittedBodies[0]?.source).toBe('instagram')
    // 🔒 The notice the prospect was *shown* is echoed back, so the server can
    // refuse a submission made against superseded text (EC-M2-04).
    expect(submittedBodies[0]?.consent_notice_id).toBe(FORM_RESPONSE.consent.notice_id)
    expect(submittedBodies[0]?.consent_granted).toBe(true)
  })

  it('refuses to submit without consent, pointing at the box — EC-M2-04', async () => {
    renderForm()
    await screen.findByRole('heading', { level: 1 })

    await userEvent.type(screen.getByLabelText(/your name/i), 'Ananya Rao')
    await userEvent.type(screen.getByLabelText(/mobile number/i), '9876543210')
    await userEvent.type(screen.getByLabelText(/what would you like help with/i), 'Lose 8kg')
    await userEvent.click(screen.getByRole('button', { name: /send enquiry/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/privacy notice/i)
    // 🔒 Nothing is created without consent — so nothing is even sent.
    expect(submittedBodies).toHaveLength(0)
  })

  it('corrects a missing contact inline, before any request — EC-M2-01', async () => {
    renderForm()
    await screen.findByRole('heading', { level: 1 })

    await userEvent.type(screen.getByLabelText(/your name/i), 'Ananya Rao')
    await userEvent.type(screen.getByLabelText(/what would you like help with/i), 'Lose 8kg')
    await userEvent.click(screen.getByRole('checkbox'))
    await userEvent.click(screen.getByRole('button', { name: /send enquiry/i }))

    expect(await screen.findByText(/add a mobile number/i)).toBeInTheDocument()
    expect(submittedBodies).toHaveLength(0)
  })

  it('replaces the form with the server’s acknowledgement on success — AC-M2-003', async () => {
    renderForm()
    await screen.findByRole('heading', { level: 1 })

    await fillRequiredFields()
    await userEvent.click(screen.getByRole('button', { name: /send enquiry/i }))

    expect(await screen.findByRole('status')).toHaveTextContent(
      'Thank you. Priya will be in touch soon.',
    )
    // ⚠️ A prospect who still sees their filled-in fields submits again, which
    // is EC-M2-02's duplicate path exercised for no reason.
    expect(screen.queryByLabelText(/your name/i)).not.toBeInTheDocument()
  })

  it('hides the honeypot from people but sends it when filled — FR-M2-008', async () => {
    renderForm()
    await screen.findByRole('heading', { level: 1 })

    const honeypot = document.getElementById('company')
    expect(honeypot).not.toBeNull()
    // 🔒 Hidden from assistive technology and unreachable by keyboard: a human
    // must never be given the chance to fill it.
    expect(honeypot?.closest('div')).toHaveAttribute('aria-hidden', 'true')
    expect(honeypot).toHaveAttribute('tabindex', '-1')

    await fillRequiredFields()
    // Drive it as automation would, bypassing the fact that no person could.
    await userEvent.type(honeypot as HTMLElement, 'spam-bot')
    await userEvent.click(screen.getByRole('button', { name: /send enquiry/i }))

    await waitFor(() => expect(submittedBodies).toHaveLength(1))
    expect(submittedBodies[0]?.company).toBe('spam-bot')
    // ⚠️ Reported, never acted on: the browser does not decide what is spam.
    // The 202 is indistinguishable from any other, which is EC-M2-03.
  })

  it('measures how long the form took, for the timing signal — FR-M2-008', async () => {
    renderForm()
    await screen.findByRole('heading', { level: 1 })

    await fillRequiredFields()
    await userEvent.click(screen.getByRole('button', { name: /send enquiry/i }))

    await waitFor(() => expect(submittedBodies).toHaveLength(1))
    expect(typeof submittedBodies[0]?.elapsed_seconds).toBe('number')
  })

  it('renders one neutral page for an unavailable form — EC-M2-07', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(errorResponse(404, 'not_found'))),
    )
    renderForm()

    expect(await screen.findByText(/not available/i)).toBeInTheDocument()
    // 🔒 Unknown slug, paused form and suspended tenant are one wording. A retry
    // button would promise a retryable failure where none exists, and any
    // difference between the three would publish a fact about that business.
    expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument()
  })

  it('offers a retry when the load fails for a retryable reason', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(errorResponse(500, 'internal_error'))),
    )
    renderForm()

    expect(await screen.findByRole('button', { name: /try again/i })).toBeInTheDocument()
  })
})
