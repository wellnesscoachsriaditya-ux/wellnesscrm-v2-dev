import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

/**
 * Operator console — our own support and curation tool (Arch §4.1).
 *
 * 🔒 A separate build is a security property, not an optimisation: operator
 * console code never reaches a practitioner's or a client's browser.
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5175,
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
