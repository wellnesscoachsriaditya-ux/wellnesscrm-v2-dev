import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

/**
 * Practitioner application — desktop-first (Arch §4.1).
 *
 * 🔒 `build.manifest` is not optional here. `scripts/check-bundle-budget.mjs`
 * reads Vite's manifest to work out which chunks a first-time visitor actually
 * downloads; without it the budget check falls back to guessing from filenames
 * and reports a number it cannot stand behind (NFR-002).
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    open: false,
  },
  build: {
    manifest: true,
    // Distinct chunk for React so a practitioner navigating between screens
    // re-uses it from cache rather than re-downloading it inside a route chunk.
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
        },
      },
    },
  },
})
