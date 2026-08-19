/**
 * @wellnesscrm/api-client — the typed seam between the browser and the API.
 *
 * 🔒 **NFR-079 / Arch §4.5 / API §16.2.** Types are generated from the backend's
 * OpenAPI document, and CI fails if they are stale. A backend contract change
 * therefore breaks the frontend *build* — which is the whole reason a
 * split-language stack is safe for one developer. See `README.md` for the
 * generation chain.
 *
 * ⚠️ **What is generated and what is not.** `generated/schema.ts` is build
 * output and is never hand-edited. This file is hand-written and deliberately
 * small: it holds the transport concerns the schema cannot express — the error
 * envelope of API §5.1, and the base URL.
 *
 * 🔒 **Arch §4.4 / NFR-068 — components never import this.** Data access belongs
 * in a feature hook, which passes results to components as props. The boundary
 * checker enforces it (R8): an import of `@wellnesscrm/api-client` from anything
 * under a `components/` directory fails the build.
 *
 * ⏳ **No authentication here yet, on purpose.** ADR-A02 puts the access token in
 * memory and the refresh token in an HttpOnly cookie, with rotation and reuse
 * detection. That is S1's work, alongside the endpoints that need it. A token
 * refresh implemented before any session exists could only be guesswork.
 * `credentials: 'same-origin'` is set now so the refresh cookie will be sent
 * once it exists.
 */

import type { paths } from '../generated/schema'

export type { components, operations, paths, webhooks } from '../generated/schema'

// ─── Path and method typing ──────────────────────────────────────────────

export type ApiPath = keyof paths

export type HttpMethod = 'get' | 'post' | 'put' | 'patch' | 'delete'

/**
 * The methods a given path actually declares.
 *
 * openapi-typescript emits every verb for every path, setting the unused ones to
 * `never` — so `paths['/x']['put']` is `undefined` rather than absent. Filtering
 * on that is what makes `request('put', '/api/v1/public/health')` a compile
 * error instead of a runtime 405.
 */
export type MethodsOf<P extends ApiPath> = {
  [M in Extract<keyof paths[P], HttpMethod>]-?: paths[P][M] extends undefined ? never : M
}[Extract<keyof paths[P], HttpMethod>]

type JsonOf<T> = T extends { content: { 'application/json': infer B } } ? B : never

/** The 2xx JSON body an operation returns. */
export type ResponseOf<P extends ApiPath, M extends MethodsOf<P>> = paths[P][M] extends {
  responses: infer R
}
  ? R extends { 200: infer Ok }
    ? JsonOf<Ok>
    : R extends { 201: infer Created }
      ? JsonOf<Created>
      : R extends { 202: infer Accepted }
        ? JsonOf<Accepted>
        : void
  : never

// ─── The error envelope — API §5.1 ───────────────────────────────────────

/**
 * 🔒 Stable machine-readable failure categories, mirroring
 * `app.kernel.errors.ErrorType`. The frontend branches on these, so they are
 * part of the contract: `message` is localisable and may change freely, `type`
 * may not.
 */
export type ApiErrorType =
  | 'validation_failed'
  | 'unauthenticated'
  | 'forbidden'
  | 'not_found'
  | 'entitlement_exceeded'
  | 'consent_required'
  | 'conflict'
  | 'precondition_required'
  | 'idempotency_conflict'
  | 'rate_limited'
  | 'domain_rule_violated'
  | 'integration_unavailable'
  | 'internal_error'

/** One field-level validation failure — API §5.3. */
export interface ApiFieldError {
  field: string
  code: string
  message: string
}

/** The body every failed request returns. There is no second error shape. */
export interface ApiErrorBody {
  type: ApiErrorType
  /** 🔒 NFR-063 — what happened. Safe to display. */
  message: string
  /** 🔒 NFR-063 — what to do next. Also safe to display, and always present. */
  action: string
  request_id: string
  details?: { fields?: ApiFieldError[] } & Record<string, unknown>
}

/**
 * A failed request, carrying the envelope.
 *
 * 🔒 `message` and `action` are written for end users (NFR-063) and are already
 * scrubbed of internals server-side, so showing them is safe. `request_id` is
 * what makes a support conversation tractable — surface it on error screens.
 */
export class ApiError extends Error {
  readonly type: ApiErrorType
  readonly action: string
  readonly requestId: string
  readonly status: number
  readonly details: ApiErrorBody['details']

  constructor(status: number, body: ApiErrorBody) {
    super(body.message)
    this.name = 'ApiError'
    this.status = status
    this.type = body.type
    this.action = body.action
    this.requestId = body.request_id
    this.details = body.details
  }

  /** Field errors from a `validation_failed` response, for form binding. */
  get fieldErrors(): ApiFieldError[] {
    return this.details?.fields ?? []
  }
}

/**
 * The envelope could not be parsed — the response did not come from our API.
 *
 * ⚠️ A distinct type because the causes are entirely different: a proxy error
 * page, a captive portal, an offline network. Treating those as an `ApiError`
 * would mean displaying a `type` the backend never sent.
 */
export class ApiTransportError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiTransportError'
    this.status = status
  }
}

// ─── The client ──────────────────────────────────────────────────────────

export interface ApiClientOptions {
  /**
   * Where requests go. Defaults to `VITE_API_BASE_URL`, or `/api` — same-origin,
   * which is what development uses and what avoids CORS in production.
   */
  baseUrl?: string
  /** Escape hatch for tests. Defaults to the global `fetch`. */
  fetch?: typeof globalThis.fetch
}

export interface RequestOptions {
  /**
   * Path parameters, by the name inside the braces in the path key.
   *
   * 🔒 Required for any path containing `{...}`. The generated keys are
   * templates — `/api/v1/app/clients/{client_id}` — and `request` refuses to
   * send one with a placeholder still in it. Without that check the browser
   * would request a URL containing a literal `{client_id}`, the API would answer
   * 404, and the symptom would read as "the client does not exist" rather than
   * "the call site forgot an argument".
   */
  path?: Record<string, string | number>
  /**
   * Query parameters. `undefined` values are dropped rather than sent as
   * "undefined".
   *
   * 🔒 An **array becomes repeated keys** — `?stage=active&stage=paused` — which
   * is API §6.2's filter encoding ("repeated params are OR within a field").
   * Anything else would need the backend to parse a delimiter out of one value,
   * and a tag name containing that delimiter would then silently split.
   * An empty array sends nothing, so "filter by none" and "no filter" agree.
   */
  query?: Record<
    string,
    | string
    | number
    | boolean
    | undefined
    | readonly (string | number | boolean | undefined)[]
  >
  /** JSON request body. Serialised here so callers never set Content-Type. */
  body?: unknown
  /**
   * Extra request headers, merged after the defaults.
   *
   * 🔒 This is how a caller sends `If-Match` for optimistic concurrency
   * (ADR-14): the plan aggregate is versioned by a counter, and a mutation must
   * state the revision it believes it is editing. Merged last, so a caller adds
   * to the defaults rather than having to reconstruct them — but it means a
   * caller *could* override `Authorization`, which no call site has reason to.
   */
  headers?: Record<string, string>
  signal?: AbortSignal
}

// ─── Session credential — ADR-A02 ─────────────────────────────────────────
//
// 🔒 The access token is held in module scope, not in `localStorage`: anything
// in storage is readable by any script the page loads, and ADR-A02 keeps the
// access token in memory for exactly that reason. It is sent as a bearer header
// (`Authorization: Bearer …`), which is what
// `app.platform.identity.authentication.bearer_token` reads — never a cookie,
// never a query parameter. Where the *refresh* token lives is the app's
// decision, made in its AuthProvider; this layer only knows the access token
// and how to ask for a new one.
let accessToken: string | null = null

type RefreshHandler = () => Promise<string | null>
let refreshHandler: RefreshHandler | null = null
let refreshing = false

/** Set (or clear, with `null`) the bearer token sent on every request. */
export function setAccessToken(token: string | null): void {
  accessToken = token
}

export function getAccessToken(): string | null {
  return accessToken
}

/**
 * Register the callback that renews an expired access token.
 *
 * 🔒 On a 401 the client invokes this **once** and retries the request with the
 * token it returns (DDR-05 — the refresh rotates). Left unset, a 401 surfaces to
 * the caller unchanged, which is why installing it is the AuthProvider's job and
 * not this module's default: a refresh implemented before a session exists could
 * only be guesswork, and a client with no session must behave exactly as before.
 */
export function configureRefreshHandler(handler: RefreshHandler | null): void {
  refreshHandler = handler
}

const DEFAULT_BASE_URL = '/api'

function resolveBaseUrl(explicit?: string): string {
  if (explicit !== undefined) return explicit.replace(/\/+$/, '')

  // `import.meta.env` exists under Vite; guarded so this module is also usable
  // from Node (tests, and any future server-side rendering).
  const fromEnv =
    typeof import.meta !== 'undefined'
      ? (import.meta as { env?: Record<string, string | undefined> }).env?.VITE_API_BASE_URL
      : undefined

  return (fromEnv ?? DEFAULT_BASE_URL).replace(/\/+$/, '')
}

/**
 * Build a client bound to one base URL.
 *
 * The generated `paths` keys already include the `/api/v1/...` prefix, so the
 * base URL is the origin portion only — `/api` in development means a request to
 * `/api/api/v1/...` would be wrong. The prefix is stripped from the path key
 * when the base URL already ends in it, which is the ordinary case.
 */
export function createApiClient(options: ApiClientOptions = {}) {
  const baseUrl = resolveBaseUrl(options.baseUrl)

  /**
   * The transport, resolved **per request** rather than captured at
   * construction.
   *
   * ⚠️ `const doFetch = options.fetch ?? globalThis.fetch` looks equivalent and
   * is not. A module-scope `createApiClient()` — which is how a feature's api.ts
   * is written — runs at import time, so it would snapshot whatever `fetch` was
   * bound then. Anything installing a wrapper afterwards is silently bypassed:
   * a test's stub, and equally a production tracing or offline-queue wrapper
   * added at app start. Reading it at call time costs one property lookup.
   */
  const transport = () => options.fetch ?? globalThis.fetch

  async function request<P extends ApiPath, M extends MethodsOf<P>>(
    method: M,
    path: P,
    init: RequestOptions = {},
  ): Promise<ResponseOf<P, M>> {
    const url = new URL(
      joinPath(baseUrl, expandPath(path as string, init.path)),
      currentOrigin(),
    )

    for (const [key, value] of Object.entries(init.query ?? {})) {
      if (value === undefined) continue
      // `append`, not `set` — an array becomes repeated keys (API §6.2).
      // ⚠️ Entries are filtered as well as the array itself: `String(undefined)`
      // is the literal `"undefined"`, which is the same defect the scalar branch
      // avoids by skipping. A sparse filter list is easy to produce from a UI
      // holding optional values, and the symptom would be a 422 naming an enum
      // member nobody chose.
      if (Array.isArray(value)) {
        for (const item of value) {
          if (item !== undefined) url.searchParams.append(key, String(item))
        }
      } else {
        url.searchParams.set(key, String(value))
      }
    }

    const hasBody = init.body !== undefined
    const send = (token: string | null) =>
      transport()(url.toString(), {
        method: (method as string).toUpperCase(),
        headers: {
          Accept: 'application/json',
          ...(hasBody ? { 'Content-Type': 'application/json' } : {}),
          // 🔒 The bearer credential, when a session exists. Omitted entirely
          // otherwise, so an unauthenticated call sends no empty `Authorization`.
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
          // Caller headers last — this is where `If-Match` lands (ADR-14).
          ...(init.headers ?? {}),
        },
        ...(hasBody ? { body: JSON.stringify(init.body) } : {}),
        ...(init.signal ? { signal: init.signal } : {}),
        // 🔒 ADR-A02 — the refresh token is an HttpOnly cookie. `same-origin`
        // rather than `include`: the API is same-origin by design, and `include`
        // would send credentials to any base URL someone misconfigures.
        credentials: 'same-origin',
      })

    let response = await send(accessToken)

    // 🔒 One silent renewal on expiry (ADR-A02, DDR-05). A 401 is the only
    // status a refresh can address; anything else is returned untouched. The
    // `refreshing` guard stops the refresh call's own failure from recursing,
    // and the whole block is inert until an AuthProvider registers a handler.
    if (response.status === 401 && refreshHandler !== null && !refreshing) {
      refreshing = true
      try {
        const renewed = await refreshHandler()
        if (renewed !== null) response = await send(renewed)
      } finally {
        refreshing = false
      }
    }

    return (await decode(response)) as ResponseOf<P, M>
  }

  return {
    baseUrl,
    request,
    get: <P extends Extract<ApiPath, PathsWith<'get'>>>(path: P, init?: RequestOptions) =>
      request('get' as MethodsOf<P>, path, init),
  }
}

/** Paths declaring a given method — used to narrow the `get` shorthand. */
export type PathsWith<M extends HttpMethod> = {
  [P in ApiPath]: M extends MethodsOf<P> ? P : never
}[ApiPath]

export type ApiClient = ReturnType<typeof createApiClient>

// ─── Internals ───────────────────────────────────────────────────────────

/**
 * Substitute `{name}` placeholders in a generated path key.
 *
 * 🔒 Throws rather than sending an unresolved template. Every value is
 * `encodeURIComponent`'d: an id is normally a UUID, but a path segment built by
 * concatenation is the classic way a `/` or `?` in a value silently changes
 * which endpoint gets called.
 */
function expandPath(template: string, params: Record<string, string | number> | undefined): string {
  const expanded = template.replace(/\{([^}]+)\}/g, (_match, name: string) => {
    const value = params?.[name]
    if (value === undefined) {
      throw new TypeError(
        `Missing path parameter '${name}' for '${template}'. Pass it as ` +
          `{ path: { ${name}: … } } — sending the template unresolved would request a ` +
          `URL containing a literal '{${name}}' and read back as a 404.`,
      )
    }
    return encodeURIComponent(String(value))
  })
  return expanded
}

function joinPath(baseUrl: string, path: string): string {
  // The generated key carries the full `/api/v1/...`. If the base URL already
  // ends with that prefix — `/api` in dev, `https://host/api` in production —
  // concatenating would duplicate it.
  if (baseUrl.endsWith('/api') && path.startsWith('/api/')) {
    return baseUrl.slice(0, -'/api'.length) + path
  }
  return baseUrl + path
}

function currentOrigin(): string {
  // `new URL` needs a base for relative paths. In a browser that is the page
  // origin; under Node there is none, so a placeholder keeps URL construction
  // working for tests that supply their own `fetch`.
  return typeof globalThis.location !== 'undefined'
    ? globalThis.location.origin
    : 'http://localhost'
}

/**
 * Turn a response into a value or throw.
 *
 * 🔒 A non-2xx response is *always* an error here. Returning it and letting each
 * caller check `response.ok` is how a failure becomes a rendered blank screen —
 * every call site has to remember, and one will not.
 */
async function decode(response: Response): Promise<unknown> {
  if (response.status === 204) return undefined

  const text = await response.text()
  let parsed: unknown

  if (text.length > 0) {
    try {
      parsed = JSON.parse(text) as unknown
    } catch {
      throw new ApiTransportError(
        response.status,
        `Expected JSON from the API but received ${response.headers.get('content-type') ?? 'an unknown content type'}.`,
      )
    }
  }

  if (response.ok) return parsed

  if (isErrorEnvelope(parsed)) throw new ApiError(response.status, parsed.error)

  // 🔒 A failure that is not in our envelope did not come from our error
  // handlers — a gateway timeout page, a proxy, an offline interceptor. It must
  // not be reported with a `type` the backend never sent.
  throw new ApiTransportError(
    response.status,
    `Request failed with status ${response.status} and no API error envelope.`,
  )
}

function isErrorEnvelope(value: unknown): value is { error: ApiErrorBody } {
  if (typeof value !== 'object' || value === null || !('error' in value)) return false
  const error = (value as { error: unknown }).error
  return (
    typeof error === 'object' &&
    error !== null &&
    typeof (error as ApiErrorBody).type === 'string' &&
    typeof (error as ApiErrorBody).message === 'string'
  )
}
