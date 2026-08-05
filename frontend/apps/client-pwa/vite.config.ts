import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

/**
 * Client PWA — mobile-first, and the one app with a hard performance
 * requirement: 🔒 NFR-002, "loads in ≤2.5s on 4G."
 *
 * `build.manifest` feeds `scripts/check-bundle-budget.mjs`, which enforces that
 * requirement on every commit rather than at launch, when it would already have
 * been missed.
 *
 * ⏳ **Not here yet, deliberately.** Service worker, offline plan caching, the
 * IndexedDB write queue and maskable icons are M7 (FR-M7-010…013) and belong to
 * that sprint. S0 builds the shell it will attach to. What is here is what
 * installability later depends on: the viewport, the theme colour and the web
 * app manifest link.
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    open: false,
  },
  build: {
    manifest: true,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
        },
      },
    },
  },
})
