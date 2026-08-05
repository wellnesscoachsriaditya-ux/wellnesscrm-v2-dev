# @wellnesscrm/api-client

🔒 **Build output. Never hand-edited.** — NFR-079, Arch §4.5, API §16.2

The typed seam between the browser and the API. Types are generated from the
backend's OpenAPI document; CI fails if what is committed here does not match
what the backend currently produces. A contract change therefore breaks the
frontend **build**, not production — which is what makes a split-language stack
safe for one developer.

## The chain

```
backend code  ──①──▶  openapi.json  ──②──▶  generated/schema.ts  ──▶  src/index.ts
                                                                       (hand-written)
```

| Link | Verified by | In which CI job | Why there |
|---|---|---|---|
| ① code → schema | `python tools/export_openapi.py --check` | `backend` | needs Python |
| ② schema → types | `node scripts/check-client-freshness.mjs` | `frontend` | needs Node |

⚠️ **Neither check is sufficient alone.** ① proves the committed schema matches
the code; ② proves the committed types match the schema. The frontend job
installs no Python and the backend job installs no Node, both on purpose — so
the chain is split across the two jobs that each have half the toolchain.

## Regenerating

Run both, and commit both files with the change that caused them:

```bash
cd backend  && python tools/export_openapi.py     # ① rewrite openapi.json
cd frontend && npm run generate:client            # ② rewrite generated/schema.ts
```

Verify without writing:

```bash
cd backend  && python tools/export_openapi.py --check
cd frontend && npm run check:client
```

`npm run check:client -- --write` regenerates in place — the same result as
`generate:client`, useful when the check has just told you it is stale.

## Layout

| Path | Generated? | What |
|---|---|---|
| `openapi.json` | 🔒 yes (backend) | the contract, as exported |
| `generated/schema.ts` | 🔒 yes (`openapi-typescript`) | `paths`, `components`, `operations` |
| `src/index.ts` | no | transport: error envelope, base URL, `createApiClient` |
| `src/client.test.ts` | no | tests for the hand-written half only |

The hand-written part is deliberately small. It holds what the schema cannot
express: the API §5.1 error envelope, and the decision that a non-2xx response
**throws** rather than resolving.

## Using it

```ts
import { createApiClient, ApiError } from '@wellnesscrm/api-client'

const api = createApiClient()

try {
  const health = await api.get('/api/v1/public/health')
} catch (error) {
  if (error instanceof ApiError) {
    // 🔒 NFR-063 — both are written for end users and safe to display.
    console.error(error.message, error.action)
    // Quote this in a support conversation.
    console.error(error.requestId)
  }
}
```

Paths and methods are checked at compile time: a path that does not exist, or a
verb the endpoint does not declare, is a type error rather than a 404 or a 405.

### Errors

| Thrown | When |
|---|---|
| `ApiError` | the API returned its `{error: {...}}` envelope |
| `ApiTransportError` | a failure with no envelope — proxy page, offline, non-JSON |

⚠️ They are distinct on purpose. A gateway's HTML error page must not be
reported with an error `type` the backend never sent, because the frontend
branches on `type`.

`ApiError.fieldErrors` returns the API §5.3 field list for form binding, and is
empty when the response carried none.

## 🔒 Components must not import this

Arch §4.4 / NFR-068: data access belongs in a feature hook, which passes results
to components as props. The boundary checker enforces it — rule R8 fails the
build on an import of this package from anything under a `components/`
directory. This was V1's most damaging failure and is not a style preference.

## ⏳ Not here yet

**Authentication.** ADR-A02 puts the access token in memory (15 min) and the
refresh token in an HttpOnly cookie (~30 days, rotating with reuse detection).
That lands in S1 with the endpoints that need it — a refresh implemented before
any session exists could only be guesswork. `credentials: 'same-origin'` is
already set, so the cookie will be sent once it exists.

**`/admin/*`.** Served as a separate OpenAPI document (API §17.3) and excluded
from this one: operator endpoint shapes should not be discoverable from a
practitioner's browser. The exclusion is already applied by the exporter, before
any admin route exists.
