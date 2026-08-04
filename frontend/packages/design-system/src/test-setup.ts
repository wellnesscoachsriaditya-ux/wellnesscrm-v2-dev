/**
 * Vitest setup.
 *
 * `@testing-library/jest-dom` supplies the accessibility-aware matchers the
 * tests rely on — `toHaveAccessibleName`, `toHaveAccessibleDescription`,
 * `toBeRequired`. These read the accessibility tree rather than the DOM, which
 * is the whole point: a test that checks `className` proves nothing about
 * whether a screen reader can use the component.
 */
import '@testing-library/jest-dom/vitest'
