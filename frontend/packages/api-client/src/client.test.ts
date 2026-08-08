/**
 * The transport layer's contract — the part of the client that is not generated.
 *
 * The generated types are not tested here: they are `openapi-typescript`'s
 * output, and testing them would be testing that tool. What is worth testing is
 * everything this package adds around them — error decoding, URL construction,
 * and the guarantee that a failed request cannot be mistaken for a successful
 * one.
 */

import { describe, expect, it, vi } from 'vitest'

import {
  ApiError,
  ApiTransportError,
  createApiClient,
  type ApiErrorBody,
} from './index'

/** A `fetch` that returns one canned response and records what it was called with. */
function stubFetch(body: unknown, init: ResponseInit = {}) {
  const calls: Array<{ url: string; init: RequestInit }> = []
  const impl = vi.fn((url: string | URL | Request, requestInit?: RequestInit) => {
    // `url` is only ever a string here — the client builds it with `URL` and
    // passes `.toString()`. Narrowed rather than coerced so a future change that
    // passes a `Request` fails loudly instead of recording '[object Object]'.
    if (typeof url !== 'string') throw new TypeError('stubFetch expects a string URL')
    calls.push({ url, init: requestInit ?? {} })

    const status = init.status ?? 200
    const payload = typeof body === 'string' ? body : JSON.stringify(body)
    const response = new Response(
      // The Response constructor rejects any body on a null-body status, so the
      // 204 case must pass null rather than the empty string.
      NULL_BODY_STATUSES.has(status) ? null : payload,
      {
        status: 200,
        headers: { 'content-type': 'application/json' },
        ...init,
      },
    )
    return Promise.resolve(response)
  })
  return { fetch: impl as unknown as typeof globalThis.fetch, calls }
}

const NULL_BODY_STATUSES = new Set([101, 103, 204, 205, 304])

const ENVELOPE: ApiErrorBody = {
  type: 'validation_failed',
  message: 'Two fields need attention.',
  action: 'Correct them and submit again.',
  request_id: 'req_0123456789abcdef',
  details: {
    fields: [{ field: 'email', code: 'value_error', message: 'Not a valid email address.' }],
  },
}

describe('createApiClient', () => {
  it('returns the parsed body of a successful request', async () => {
    const { fetch } = stubFetch({ status: 'ok' })
    const client = createApiClient({ baseUrl: '/api', fetch })

    await expect(client.get('/api/v1/public/health')).resolves.toEqual({ status: 'ok' })
  })

  it('does not duplicate the /api prefix the generated paths already carry', async () => {
    // The generated keys are full paths (`/api/v1/public/health`), while the
    // base URL is `/api` in development. Naive concatenation gives
    // `/api/api/v1/...`, which 404s in a way that looks like a routing bug.
    const { fetch, calls } = stubFetch({ status: 'ok' })
    const client = createApiClient({ baseUrl: '/api', fetch })

    await client.get('/api/v1/public/health')

    expect(calls[0]?.url).toContain('/api/v1/public/health')
    expect(calls[0]?.url).not.toContain('/api/api/')
  })

  it('appends a non-/api base URL without rewriting it', async () => {
    const { fetch, calls } = stubFetch({ status: 'ok' })
    const client = createApiClient({ baseUrl: 'https://api.example.test', fetch })

    await client.get('/api/v1/public/health')

    expect(calls[0]?.url).toBe('https://api.example.test/api/v1/public/health')
  })

  it('strips a trailing slash from the base URL', async () => {
    const { fetch, calls } = stubFetch({ status: 'ok' })
    const client = createApiClient({ baseUrl: 'https://api.example.test/', fetch })

    await client.get('/api/v1/public/health')

    expect(calls[0]?.url).toBe('https://api.example.test/api/v1/public/health')
  })

  it('omits undefined query parameters rather than sending the string "undefined"', async () => {
    const { fetch, calls } = stubFetch({ status: 'ok' })
    const client = createApiClient({ baseUrl: '/api', fetch })

    await client.request('get', '/api/v1/public/health', {
      query: { page: 2, cursor: undefined, active: true },
    })

    const url = new URL(calls[0]?.url ?? '')
    expect(url.searchParams.get('page')).toBe('2')
    expect(url.searchParams.get('active')).toBe('true')
    expect(url.searchParams.has('cursor')).toBe(false)
  })

  it('sends credentials so the ADR-A02 refresh cookie will be included', async () => {
    const { fetch, calls } = stubFetch({ status: 'ok' })
    const client = createApiClient({ baseUrl: '/api', fetch })

    await client.get('/api/v1/public/health')

    expect(calls[0]?.init.credentials).toBe('same-origin')
  })

  it('sets no Content-Type when there is no body', async () => {
    // A GET with `Content-Type: application/json` and no body triggers a CORS
    // preflight for nothing.
    const { fetch, calls } = stubFetch({ status: 'ok' })
    const client = createApiClient({ baseUrl: '/api', fetch })

    await client.get('/api/v1/public/health')

    const headers = calls[0]?.init.headers as Record<string, string>
    expect(headers['Content-Type']).toBeUndefined()
    expect(headers.Accept).toBe('application/json')
  })

  it('substitutes path parameters into the generated template', async () => {
    const { fetch, calls } = stubFetch({ id: 'abc' })
    const client = createApiClient({ baseUrl: '/api', fetch })

    await client.request('post', '/api/v1/app/clients/{client_id}/archive', {
      path: { client_id: '11111111-2222-3333-4444-555555555555' },
    })

    expect(new URL(calls[0]?.url ?? '').pathname).toBe(
      '/api/v1/app/clients/11111111-2222-3333-4444-555555555555/archive',
    )
  })

  it('refuses to send a path with an unresolved placeholder', async () => {
    // 🔒 The failure this prevents is a silent one: an unsubstituted template
    // requests a URL containing a literal `{client_id}`, the API answers 404,
    // and the screen reports "that client could not be found" — which sends
    // the reader looking at the database rather than at the call site.
    const { fetch, calls } = stubFetch({ id: 'abc' })
    const client = createApiClient({ baseUrl: '/api', fetch })

    await expect(
      client.request('post', '/api/v1/app/clients/{client_id}/archive', {}),
    ).rejects.toThrow(/Missing path parameter 'client_id'/)
    expect(calls).toHaveLength(0)
  })

  it('encodes a path parameter rather than concatenating it', async () => {
    // A `/` or `?` in a value would otherwise change which endpoint is called.
    const { fetch, calls } = stubFetch({ id: 'abc' })
    const client = createApiClient({ baseUrl: '/api', fetch })

    await client.request('post', '/api/v1/app/clients/{client_id}/archive', {
      path: { client_id: 'a/../b' },
    })

    expect(new URL(calls[0]?.url ?? '').pathname).toBe('/api/v1/app/clients/a%2F..%2Fb/archive')
  })

  it('resolves the global fetch per request, not at construction', async () => {
    // 🔒 A feature's `api.ts` calls `createApiClient()` at module scope, so it
    // runs at import time. Capturing `globalThis.fetch` there would snapshot
    // whatever was bound at that instant and silently bypass anything installed
    // afterwards — a test's stub, or a production tracing/offline wrapper added
    // during app start. The symptom is a real request escaping from a test.
    const client = createApiClient({ baseUrl: '/api' })

    const { fetch, calls } = stubFetch({ status: 'ok' })
    const original = globalThis.fetch
    globalThis.fetch = fetch as unknown as typeof globalThis.fetch
    try {
      await client.get('/api/v1/public/health')
    } finally {
      globalThis.fetch = original
    }

    expect(calls).toHaveLength(1)
  })
})

describe('error decoding — API §5.1', () => {
  it('throws ApiError carrying the envelope, never returns it', async () => {
    // 🔒 The load-bearing assertion of this file. If a 4xx resolved instead of
    // throwing, every call site would have to remember to check — and one would
    // not, rendering a blank screen instead of the error.
    const { fetch } = stubFetch({ error: ENVELOPE }, { status: 422 })
    const client = createApiClient({ baseUrl: '/api', fetch })

    await expect(client.get('/api/v1/public/health')).rejects.toBeInstanceOf(ApiError)
  })

  it('exposes type, action and request_id — NFR-063', async () => {
    const { fetch } = stubFetch({ error: ENVELOPE }, { status: 422 })
    const client = createApiClient({ baseUrl: '/api', fetch })

    const error = await client.get('/api/v1/public/health').catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.type).toBe('validation_failed')
    expect(apiError.message).toBe('Two fields need attention.')
    // 🔒 NFR-063 — every error states what to do next. An error screen with no
    // action is the failure this assertion guards against.
    expect(apiError.action).toBe('Correct them and submit again.')
    expect(apiError.requestId).toBe('req_0123456789abcdef')
    expect(apiError.status).toBe(422)
  })

  it('exposes field errors for form binding — API §5.3', async () => {
    const { fetch } = stubFetch({ error: ENVELOPE }, { status: 422 })
    const client = createApiClient({ baseUrl: '/api', fetch })

    const error = (await client.get('/api/v1/public/health').catch((e: unknown) => e)) as ApiError

    expect(error.fieldErrors).toEqual([
      { field: 'email', code: 'value_error', message: 'Not a valid email address.' },
    ])
  })

  it('reports no field errors when the envelope carries none', async () => {
    const { fetch } = stubFetch(
      { error: { ...ENVELOPE, type: 'not_found', details: undefined } },
      { status: 404 },
    )
    const client = createApiClient({ baseUrl: '/api', fetch })

    const error = (await client.get('/api/v1/public/health').catch((e: unknown) => e)) as ApiError

    expect(error.fieldErrors).toEqual([])
  })

  it('raises ApiTransportError, not ApiError, for a failure outside the envelope', async () => {
    // 🔒 A gateway's HTML error page must not be reported with an error `type`
    // the backend never sent — the frontend branches on `type`.
    const { fetch } = stubFetch('<html>502 Bad Gateway</html>', {
      status: 502,
      headers: { 'content-type': 'text/html' },
    })
    const client = createApiClient({ baseUrl: '/api', fetch })

    const error = await client.get('/api/v1/public/health').catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiTransportError)
    expect(error).not.toBeInstanceOf(ApiError)
    expect((error as ApiTransportError).status).toBe(502)
  })

  it('raises ApiTransportError when a 2xx body is not JSON', async () => {
    const { fetch } = stubFetch('not json at all', {
      status: 200,
      headers: { 'content-type': 'text/plain' },
    })
    const client = createApiClient({ baseUrl: '/api', fetch })

    await expect(client.get('/api/v1/public/health')).rejects.toBeInstanceOf(ApiTransportError)
  })

  it('rejects a failure whose body is JSON but not our envelope', async () => {
    const { fetch } = stubFetch({ detail: 'Not Found' }, { status: 404 })
    const client = createApiClient({ baseUrl: '/api', fetch })

    // FastAPI's own `{detail}` shape, which our handlers translate. Seeing it
    // here means a response escaped `register_error_handlers`.
    await expect(client.get('/api/v1/public/health')).rejects.toBeInstanceOf(ApiTransportError)
  })

  it('resolves to undefined for 204 rather than trying to parse an empty body', async () => {
    const { fetch } = stubFetch('', { status: 204 })
    const client = createApiClient({ baseUrl: '/api', fetch })

    await expect(client.request('get', '/api/v1/public/health')).resolves.toBeUndefined()
  })
})
