/**
 * Ambient types for non-TypeScript imports, shared by every workspace package.
 *
 * ⚠️ This file exists because an app that imports `@wellnesscrm/design-system`
 * type-checks the design system's **source**, not a built `.d.ts` — workspace
 * packages point `main` at `src/index.ts`. That source imports CSS Modules, so
 * every consuming project needs the declaration in scope or `tsc` fails on
 * files it does not own.
 *
 * Declaring it once here and including it from each app's tsconfig keeps that
 * from becoming the same four lines copied into four projects (NFR-069).
 */

declare module '*.module.css' {
  const classes: Record<string, string>
  export default classes
}

declare module '*.css' {
  const content: string
  export default content
}
