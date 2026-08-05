// ESLint configuration — WellnessCRM V2 frontend
//
// Two rules here are BINDING architectural constraints, not style preferences:
//
//   1. ADR-02 — `@supabase/supabase-js` must never be imported in the frontend.
//      The browser never queries the database directly (NFR-031). Primary
//      enforcement is that the package is not installed; this rule catches an
//      accidental install before it reaches main.
//
//   2. ADR-03 — feature apps must not declare raw colour/spacing values.
//      Design tokens are the only place raw values exist.
//
// NFR-068 (no business logic in components) is enforced by
// backend/tools/check_boundaries.py, which can see across file boundaries.

import js from '@eslint/js'
import tseslint from 'typescript-eslint'
import reactHooks from 'eslint-plugin-react-hooks'
import globals from 'globals'

/** Packages the frontend must never depend on. ADR-02. */
const FORBIDDEN_PACKAGES = [
  {
    name: '@supabase/supabase-js',
    message:
      'ADR-02: the browser never accesses the database directly. All data flows ' +
      'through the FastAPI layer via @wellnesscrm/api-client.',
  },
  {
    name: '@supabase/auth-helpers-react',
    message: 'ADR-02: authentication is brokered server-side. Use the app auth hooks.',
  },
]

/** Raw colour/spacing literals in feature apps. ADR-03. */
const RAW_VALUE_PATTERNS = [
  {
    selector:
      'Literal[value=/^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/]',
    message:
      'ADR-03: raw hex colours are not permitted in feature apps. ' +
      'Use a design token from @wellnesscrm/design-system.',
  },
  {
    selector: 'Literal[value=/^(?:rgb|hsl)a?\\(/]',
    message:
      'ADR-03: raw colour functions are not permitted in feature apps. ' +
      'Use a design token from @wellnesscrm/design-system.',
  },
]

export default tseslint.config(
  {
    ignores: [
      '**/dist/**',
      '**/dist-gallery/**',
      '**/node_modules/**',
      '**/*.config.js',
      '**/api-client/generated/**',
      // Ambient module declarations shared by every project. They belong to no
      // single tsconfig — each app pulls the directory into its own `include` —
      // so the type-aware parser cannot resolve a project for them. There is
      // nothing to lint in a `declare module` block regardless.
      'types/**',
    ],
  },

  js.configs.recommended,
  ...tseslint.configs.recommendedTypeChecked,

  {
    languageOptions: {
      ecmaVersion: 2022,
      globals: { ...globals.browser },
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    plugins: { 'react-hooks': reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,

      // ADR-02 — binding
      'no-restricted-imports': ['error', { paths: FORBIDDEN_PACKAGES }],

      // General hygiene
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/consistent-type-imports': 'error',
      eqeqeq: ['error', 'always'],
      'no-console': ['error', { allow: ['warn', 'error'] }],
    },
  },

  // ADR-03 — feature apps only. The design system itself defines the raw values.
  {
    files: ['apps/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-syntax': ['error', ...RAW_VALUE_PATTERNS],
    },
  },

  // The generated API client is build output (NFR-079) — never hand-edited,
  // and not subject to authored-code rules.
  {
    files: ['packages/api-client/src/generated/**'],
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
      '@typescript-eslint/consistent-type-imports': 'off',
    },
  },

  // Build and tooling scripts are plain Node ESM, outside any tsconfig.
  //
  // ⚠️ Must come last. Flat config applies matching blocks in order, so this
  // has to follow `recommendedTypeChecked` to switch the type-aware rules back
  // off — placed earlier, the later block re-enables them and the parser fails
  // on a file that belongs to no TypeScript project.
  {
    files: ['**/*.mjs', '**/*.cjs'],
    extends: [tseslint.configs.disableTypeChecked],
    languageOptions: {
      globals: { ...globals.node },
    },
    rules: {
      // A CLI script reports by printing; that is its interface. The rule is
      // aimed at stray debugging left in application code.
      'no-console': 'off',
    },
  },
)
