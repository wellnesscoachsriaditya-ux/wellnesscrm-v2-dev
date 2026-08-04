/**
 * Ambient types for non-TypeScript imports.
 *
 * CSS Modules are resolved by Vite at build time, so TypeScript needs to be
 * told what `import styles from './Button.module.css'` yields.
 *
 * ⚠️ The index signature is `Record<string, string>` rather than a generated
 * per-file union of class names. Generating exact names would catch a typo in
 * `styles.buton`, and the tooling to do it (`typed-css-modules` and a watch
 * process) is a build dependency plus a generated-file-freshness problem for
 * every component. NFR-078 asks whether a solo developer can justify and debug
 * each dependency; for this one the answer is no, and a missing class produces
 * an unstyled element that is obvious on sight.
 */
declare module '*.module.css' {
  const classes: Record<string, string>
  export default classes
}

declare module '*.css' {
  const content: string
  export default content
}
