import { defineConfig } from 'vitest/config'

/**
 * See `packages/design-system/vitest.config.ts` for why the React plugin is
 * absent and esbuild's automatic JSX transform is used instead.
 */
export default defineConfig({
  esbuild: {
    jsx: 'automatic',
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
    css: true,
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
