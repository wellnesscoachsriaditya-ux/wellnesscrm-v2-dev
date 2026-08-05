import { defineConfig } from 'vitest/config'

/**
 * Test configuration.
 *
 * Mirrors the design system's: no React plugin, esbuild's automatic JSX
 * transform, jsdom. See `packages/design-system/vitest.config.ts` for why the
 * plugin is deliberately absent.
 */
export default defineConfig({
  esbuild: {
    jsx: 'automatic',
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
