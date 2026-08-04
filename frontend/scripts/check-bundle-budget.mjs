// Bundle budget enforcement — NFR-002.
//
// 🔒 "Client PWA loads in ≤2.5s on 4G." On a 4G connection with realistic
// latency, transfer is the dominant term, and the only part of it CI can
// govern is how many bytes we ship. A budget checked at launch is a budget
// already blown: the regression is cheap to fix on the commit that causes it
// and expensive three months later, once forty screens depend on the import
// that caused it.
//
// Budgets are on **gzipped** bytes, because that is what crosses the network.
//
// ⚠️ These are initial figures, deliberately set with headroom rather than
// tightly around today's output — a budget that fails on noise gets raised
// until it means nothing. They tighten as the apps take shape; the client PWA
// is the strictest because it is the one with a hard performance requirement
// on a mid-range Android over real 4G.
//
// Usage:
//   node scripts/check-bundle-budget.mjs
//   node scripts/check-bundle-budget.mjs --report   (print sizes, never fail)

import { gzipSync } from 'node:zlib'
import { readFileSync, existsSync, readdirSync, statSync } from 'node:fs'
import { join, relative, extname } from 'node:path'
import { fileURLToPath } from 'node:url'

const FRONTEND = fileURLToPath(new URL('..', import.meta.url))

/**
 * Per-app budgets in gzipped kilobytes.
 *
 * `initial` is what a first-time visitor must download before the app renders:
 * the entry chunk plus everything it statically imports, plus CSS. Lazy routes
 * are excluded — that is the point of code splitting — but they are counted in
 * `total` so that deferring work does not silently become hoarding it.
 */
const BUDGETS = {
  'client-pwa': { initial: 180, total: 500 },
  practitioner: { initial: 260, total: 900 },
  operator: { initial: 200, total: 600 },
}

const COUNTED = new Set(['.js', '.mjs', '.css'])

/** Recursively collect asset files under a directory. */
function collect(dir) {
  const found = []
  if (!existsSync(dir)) return found

  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry)
    const stats = statSync(path)
    if (stats.isDirectory()) {
      found.push(...collect(path))
    } else if (COUNTED.has(extname(entry))) {
      found.push({ path, bytes: gzipSync(readFileSync(path)).length })
    }
  }
  return found
}

/**
 * Read Vite's manifest to determine the initial download.
 *
 * The manifest is authoritative about which chunks are statically reachable
 * from the entry — guessing from filenames would silently miscount the moment
 * a chunking strategy changes. Without a manifest we fall back to the entry
 * chunk alone and say so, rather than reporting a confident wrong number.
 */
function initialChunks(distDir) {
  for (const candidate of ['.vite/manifest.json', 'manifest.json']) {
    const manifestPath = join(distDir, candidate)
    if (!existsSync(manifestPath)) continue

    const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'))
    const entry = Object.values(manifest).find((chunk) => chunk.isEntry)
    if (!entry) break

    const files = new Set()
    const visit = (chunk) => {
      if (!chunk || files.has(chunk.file)) return
      files.add(chunk.file)
      for (const css of chunk.css ?? []) files.add(css)
      // `imports` are static — part of the initial load.
      // `dynamicImports` are deliberately not followed.
      for (const key of chunk.imports ?? []) visit(manifest[key])
    }
    visit(entry)
    return { files, source: 'manifest' }
  }
  return { files: null, source: 'fallback' }
}

const kb = (bytes) => Math.round(bytes / 1024)

const reportOnly = process.argv.includes('--report')
const rows = []
const failures = []
let checked = 0

for (const [app, budget] of Object.entries(BUDGETS)) {
  const distDir = join(FRONTEND, 'apps', app, 'dist')

  if (!existsSync(distDir)) {
    // ⚠️ Not a failure: apps are scaffolded across S0 and this script runs
    // from the first commit that has CI. It becomes a real gate for each app
    // on the commit that first builds it — no one has to remember to enable it.
    rows.push({ app, status: 'not built', initial: '—', total: '—', budget })
    continue
  }

  checked += 1
  const assets = collect(distDir)
  const totalBytes = assets.reduce((sum, asset) => sum + asset.bytes, 0)

  const { files, source } = initialChunks(distDir)
  const initialBytes = assets
    .filter((asset) => {
      const rel = relative(distDir, asset.path).split('\\').join('/')
      return files ? files.has(rel) : /index[-.][\w-]+\.(js|css)$/.test(rel)
    })
    .reduce((sum, asset) => sum + asset.bytes, 0)

  rows.push({
    app,
    status: source === 'manifest' ? 'ok' : 'ok (no manifest)',
    initial: kb(initialBytes),
    total: kb(totalBytes),
    budget,
  })

  if (kb(initialBytes) > budget.initial) {
    failures.push(
      `${app}: initial ${kb(initialBytes)} KB gzipped exceeds the ${budget.initial} KB budget ` +
        `(NFR-002). Lazy-load a route, or justify raising the budget in the same commit.`,
    )
  }
  if (kb(totalBytes) > budget.total) {
    failures.push(
      `${app}: total ${kb(totalBytes)} KB gzipped exceeds the ${budget.total} KB budget. ` +
        `Deferring code to lazy chunks does not remove it from the download.`,
    )
  }
}

console.log('\nBundle budget — gzipped KB (NFR-002)')
console.log('─'.repeat(72))
console.log('app'.padEnd(16) + 'initial'.padStart(10) + 'budget'.padStart(10) + 'total'.padStart(10) + 'budget'.padStart(10) + '   status')
for (const row of rows) {
  console.log(
    row.app.padEnd(16) +
      String(row.initial).padStart(10) +
      String(row.budget.initial).padStart(10) +
      String(row.total).padStart(10) +
      String(row.budget.total).padStart(10) +
      `   ${row.status}`,
  )
}
console.log('─'.repeat(72))

if (checked === 0) {
  console.log('No built apps yet — nothing to enforce. This becomes a gate as each app lands.\n')
  process.exit(0)
}

if (failures.length > 0 && !reportOnly) {
  console.error('\nBundle budget exceeded:\n')
  for (const failure of failures) console.error(`  ✗ ${failure}`)
  console.error('')
  process.exit(1)
}

console.log(`${checked} app(s) within budget.\n`)
