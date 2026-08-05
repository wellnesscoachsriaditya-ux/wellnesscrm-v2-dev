/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Which deployment this build is for — `development`, `staging`,
   * `production`.
   *
   * ⚠️ 🔒 Distinct from Vite's `MODE`, which is `production` for *any*
   * production build, staging included. `AdminShell` renders this as a banner
   * precisely so an operator cannot believe they are in staging while acting on
   * production data, and `MODE` cannot tell them apart.
   */
  readonly VITE_ENVIRONMENT?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
