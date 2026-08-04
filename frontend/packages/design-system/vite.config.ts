import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

/**
 * Build and gallery dev server.
 *
 * `npm run gallery` (from `frontend/`) serves the component gallery — the S0
 * Definition of Done requires "a component gallery showing every primitive and
 * pattern", and it is the only practical way to review the design system before
 * a feature screen exists to put it in.
 *
 * ⚠️ Test configuration lives in `vitest.config.ts`, not here. Vitest bundles
 * its own copy of Vite, so a single file importing `defineConfig` from
 * `vitest/config` while passing plugins typed against the workspace's Vite
 * produces an unresolvable type conflict under `exactOptionalPropertyTypes`.
 * Two files, one Vite each.
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5199,
    open: false,
  },
  build: {
    // The gallery is a development tool, not a shipped artefact.
    outDir: 'dist-gallery',
  },
})
