import { defineConfig } from 'vitest/config'

/**
 * Test configuration.
 *
 * Separate from `vite.config.ts` so the two Vite copies (the workspace's v6 and
 * the one vitest bundles) never meet in a single type position — see the note
 * in that file.
 *
 * The React plugin is deliberately absent: these tests exercise components
 * through `@testing-library/react`, and esbuild's built-in JSX transform is
 * sufficient for that. Adding the plugin here is what would reintroduce the
 * conflict.
 */
export default defineConfig({
  esbuild: {
    jsx: 'automatic',
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
    // CSS Modules are processed so `styles.foo` resolves to a real class name
    // rather than undefined — several tests assert on rendered structure.
    css: true,
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
