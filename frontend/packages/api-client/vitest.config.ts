import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    // No DOM: this package is types plus a fetch wrapper. jsdom would be
    // ~2s of startup per run to provide globals nothing here touches.
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
})
