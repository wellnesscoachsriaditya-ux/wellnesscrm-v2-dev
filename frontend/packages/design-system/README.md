# @wellnesscrm/design-system

🔒 **ADR-03 / NFR-056** — the first frontend code written, before any feature screen.
No feature may introduce a bespoke one-off component.

V1's recorded failure was *"no unified design system from the beginning,"* and its
navigation and screens drifted apart as a result. This package is the countermeasure.
It is not a style guide — it is enforced by tooling: a lint rule rejects raw colour and
spacing values in feature apps, and `backend/tools/check_boundaries.py` (R8) rejects
domain logic in components.

---

## Purpose

One source of visual and interaction truth for three applications with different
audiences, devices and security postures:

| App | Shell | Audience |
|---|---|---|
| `apps/practitioner` | `AppShell` | Practitioners, desktop-first, tablet-capable |
| `apps/client-pwa` | `MobileShell` | Clients, mobile-first, installable |
| `apps/operator` | `AdminShell` | Us — support and food curation |

## Public interface

Everything is exported from the package root. Feature apps import from
`@wellnesscrm/design-system` and never reach into `src/` paths.

```ts
import { Button, FormField, Input, EmptyState } from '@wellnesscrm/design-system'
```

### Layers

Dependency flows one way only. A primitive never imports a pattern.

```
tokens → primitives → patterns → layout shells
```

**Tokens** — `colour`, `fontSize`, `fontWeight`, `lineHeight`, `fontFamily`, `space`,
`radius`, `shadow`, `touchTarget`, `zIndex`, `duration`, `easing`, `breakpoint`.

**Primitives (13)** — `Button`, `Input`, `Select`, `Textarea`, `Checkbox`, `Radio` +
`RadioGroup`, `Modal`, `ToastProvider` + `useToast`, `Table` (+ `TableHead`, `TableBody`,
`TableRow`, `TableHeaderCell`, `TableCell`), `Card` (+ `CardHeader`, `CardBody`,
`CardFooter`), `Badge`, `Spinner`, `Tabs` + `TabPanel`.

**Patterns (6)** — `PageHeader`, `EmptyState`, `ErrorState`, `ConfirmDialog`,
`FormField`, `DataList`.

**Layout shells (3)** — `AppShell`, `MobileShell`, `AdminShell`.

### Setup

Import the tokens stylesheet once, at the app root:

```ts
import '@wellnesscrm/design-system/tokens.css'
```

Wrap the app in `ToastProvider` if it shows toasts — the provider owns the queue and
renders the live region.

## Dependencies

React 18 as a peer dependency. Nothing else at runtime.

🔒 **No component library, no CSS framework, no `clsx`, no Storybook.** NFR-078 requires
every dependency to be justifiable and debuggable by one developer. `cx` is six lines;
the gallery is a Vite page; CSS Modules ship with Vite.

---

## The rules encoded here

Each of these is a requirement that would otherwise be forty individual acts of
discipline across forty screens. As components they cost one import.

| Requirement | How this package satisfies it |
|---|---|
| 🔒 NFR-059 — thumb-sized targets | `touchTarget` tokens; every control has `min-height` ≥ 44px, 48px for client-facing |
| 🔒 NFR-060 — contrast + text scaling | Every pair asserted against WCAG AA in `contrast.test.ts`; all type in `rem`, never `px` |
| 🔒 NFR-061 — keyboard operable | `Modal` focus trap; `Tabs` arrow/Home/End; skip links in all three shells |
| 🔒 NFR-062 — labelled inputs | `FormField` generates the id and wires `htmlFor`; `Checkbox`/`Radio` require `label` |
| 🔒 NFR-063 — useful errors | `ErrorState.whatToDoNext` is a **required prop** |
| 🔒 NFR-064 — designed empty states | `EmptyState.description` is a **required prop** |
| 🔒 NFR-065 — destructive confirmation | `ConfirmDialog.consequence` is a **required prop** |
| 🔒 NFR-057 — one declared IA | Shells take `navItems`; these come from the app's IA manifest, never hand-written |
| 🔒 WCAG 1.4.1 — not colour alone | Badges state status in text; active nav uses weight + background; toasts carry an icon and a screen-reader tone label |

**Required props are the mechanism.** A guideline in a document gets skipped under
deadline. `consequence`, `whatToDoNext` and `description` are typed as required, so
TypeScript refuses to compile a confirmation that does not say what is lost or an empty
state that does not say what to do next.

---

## Gallery

```bash
npm run gallery      # from frontend/ — http://localhost:5199
```

Shows every primitive and pattern in all states, the full token set, and the three
shells. Required by the S0 Definition of Done, and the fastest way to check a change
against every component at once.

It imports the real components, so it cannot drift from what ships.

## Checks

```bash
npm run lint         # from frontend/
npm run typecheck
npm test             # 106 tests
```

`src/accessibility.test.tsx` covers behaviour that is invisible to mouse testing and
broken in most hand-rolled implementations: focus trapping and restoration, label
association, tab keyboard semantics, required-consequence copy.

`src/tokens/contrast.test.ts` computes WCAG relative luminance for every
foreground/background pair the system uses. A palette change that drops a pair below AA
fails CI on the commit that causes it.

`src/tokens/tokens.test.ts` proves the CSS custom properties and the TypeScript token
exports agree. Two declarations of one value is the duplication NFR-069 forbids — unless
they are provably identical.

---

## Known limitations

⚠️ 🎯 **NFR-067 targets WCAG 2.1 AA and says explicitly that full conformance cannot be
verified without manual testing with assistive technology.** That remains true here.
What is automated is contrast, semantic structure, keyboard behaviour and label
association. What is not: reading order in a real screen reader, whether copy is
comprehensible, focus behaviour in browsers other than the test environment. Treat the
tests as a floor, not a certificate.

⚠️ **`neutral500` fails AA for body text on white (2.8:1)** and is retained only for
non-text use — disabled borders, dividers, the select chevron. `contrast.test.ts`
asserts this deliberately so the constraint is documented rather than rediscovered.

⚠️ **Focus visibility is verified by structure, not by pixels.** `:focus-visible` styles
exist and are tokenised, but no test proves the ring is actually visible against every
background it may appear over.

⚠️ **The select chevron embeds one raw hex value** in a data URI, because a data URI
cannot reference a CSS custom property. It is the single exception to "tokens are the
only place raw values exist", and it must be updated with `--wc-neutral-600`.

⚠️ **No `Combobox` yet.** `Select` is the native element deliberately. Searchable
food selection (S3) needs a real listbox pattern and will be a separate primitive, not
a widened `Select`.
