// API-client freshness — the second half of NFR-079.
//
// 🔒 Arch §4.5 / API §16.2: "`packages/api-client` is build output, never
// hand-edited, and CI fails if it is stale." A backend contract change must
// break the frontend *build*, not production.
//
// ─── The chain, and who verifies which link ──────────────────────────────
//
//   backend code  ──①──▶  openapi.json  ──②──▶  generated/schema.ts
//
//   ① `python tools/export_openapi.py --check`   (backend CI job — has Python)
//   ② this script                                 (frontend CI job — has Node)
//
// ⚠️ **This script checks link ② only.** It cannot check ① because the frontend
// CI job has no Python and no installed backend, by design — adding both to
// keep one check in one file would double that job's install time on every run.
// Together the two checks are airtight: ① proves the committed schema matches
// the code, ② proves the committed types match the schema. Neither alone is
// sufficient, and a reviewer should know which one they are reading.
//
// The check works by regenerating into a temporary file and comparing bytes.
// Byte comparison rather than semantic comparison is deliberate: the committed
// file must be exactly what the generator produces, so that `npm run
// generate:client` is always the complete fix.
//
// Usage:
//   node scripts/check-client-freshness.mjs
//   node scripts/check-client-freshness.mjs --write   (regenerate in place)

import { spawnSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { mkdtempSync, readFileSync, rmSync, existsSync, copyFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const FRONTEND = fileURLToPath(new URL('..', import.meta.url))
const PACKAGE = join(FRONTEND, 'packages', 'api-client')
const SCHEMA = join(PACKAGE, 'openapi.json')
const GENERATED = join(PACKAGE, 'generated', 'schema.ts')

const write = process.argv.includes('--write')

/** Fail with a message that contains the fix, not just the symptom. */
function fail(lines) {
  console.error('\nAPI client is not current (NFR-079)\n' + '─'.repeat(72))
  for (const line of lines) console.error(line)
  console.error('')
  process.exit(1)
}

// ─── Preconditions ───────────────────────────────────────────────────────

if (!existsSync(SCHEMA)) {
  fail([
    `The OpenAPI schema is missing: ${SCHEMA}`,
    '',
    'It is exported from the backend, which owns the contract:',
    '  cd backend && python tools/export_openapi.py',
  ])
}

try {
  JSON.parse(readFileSync(SCHEMA, 'utf8'))
} catch (error) {
  fail([
    `The OpenAPI schema is not valid JSON: ${SCHEMA}`,
    `  ${error.message}`,
    '',
    'Re-export it rather than repairing it by hand — it is generated:',
    '  cd backend && python tools/export_openapi.py',
  ])
}

if (!existsSync(GENERATED) && !write) {
  fail([
    `The generated client is missing: ${GENERATED}`,
    '',
    'Generate it:',
    '  cd frontend && npm run generate:client',
  ])
}

// ─── Regenerate into a temporary file ────────────────────────────────────
//
// The CLI is invoked rather than the Node API so that this check produces
// byte-identical output to `npm run generate:client`. If the two ever diverged,
// the check would demand a state the documented fix does not produce.

const require = createRequire(import.meta.url)

let generatorEntry
try {
  const manifestPath = require.resolve('openapi-typescript/package.json')
  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'))
  const bin = typeof manifest.bin === 'string' ? manifest.bin : manifest.bin['openapi-typescript']
  generatorEntry = join(manifestPath, '..', bin)
} catch {
  fail([
    'openapi-typescript is not installed.',
    '',
    '  cd frontend && npm ci',
  ])
}

const scratch = mkdtempSync(join(tmpdir(), 'wellnesscrm-client-'))
const candidate = join(scratch, 'schema.ts')

try {
  // `process.execPath` rather than the .cmd shim: the shim does not exist on
  // Linux and needs a shell on Windows. Running the entry point with the
  // current Node binary works identically on both.
  const result = spawnSync(process.execPath, [generatorEntry, SCHEMA, '--output', candidate], {
    encoding: 'utf8',
    cwd: PACKAGE,
  })

  if (result.status !== 0) {
    fail([
      'The generator failed to run:',
      '',
      (result.stderr || result.stdout || '(no output)').trim(),
    ])
  }

  if (write) {
    copyFileSync(candidate, GENERATED)
    console.log(`Regenerated ${GENERATED.replace(FRONTEND, '')}`)
    process.exit(0)
  }

  const expected = readFileSync(candidate, 'utf8')
  const actual = readFileSync(GENERATED, 'utf8')

  if (expected !== actual) {
    // Locating the first difference turns "they differ" into something a
    // reader can act on without diffing two files by hand.
    const expectedLines = expected.split('\n')
    const actualLines = actual.split('\n')
    const at = expectedLines.findIndex((line, i) => line !== actualLines[i])

    fail([
      'The committed TypeScript client does not match its OpenAPI schema.',
      '',
      `  schema:     packages/api-client/openapi.json`,
      `  generated:  packages/api-client/generated/schema.ts`,
      at >= 0 ? `  first difference at line ${at + 1}:` : '',
      at >= 0 ? `    committed: ${(actualLines[at] ?? '(end of file)').trim().slice(0, 100)}` : '',
      at >= 0 ? `    expected:  ${(expectedLines[at] ?? '(end of file)').trim().slice(0, 100)}` : '',
      '',
      '🔒 `generated/` is build output and is never hand-edited. Regenerate it',
      'and commit the result alongside the change that caused it:',
      '',
      '  cd frontend && npm run generate:client',
    ].filter(Boolean))
  }

  const paths = Object.keys(JSON.parse(readFileSync(SCHEMA, 'utf8')).paths ?? {}).length
  console.log(
    `API client is current — ${paths} path(s), ${actual.split('\n').length} generated lines.`,
  )
} finally {
  rmSync(scratch, { recursive: true, force: true })
}
