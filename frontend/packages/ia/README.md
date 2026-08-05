# @wellnesscrm/ia

🔒 **NFR-057 / Arch §4.3** — *"Navigation MUST derive from one declared
information architecture, so navigation, breadcrumbs and routes cannot drift
apart."*

V1's recorded failure was inconsistent navigation. The countermeasure is not a
convention, it is a data structure: each app declares its screens once, and the
router, the menu, the breadcrumbs and menu visibility are all read from that
declaration.

**Adding a screen is one manifest entry.** It cannot appear in the router but be
missing from navigation, because the router is generated from the same array the
navigation is.

---

## Declaring an IA

```ts
export const ia = defineIa({
  appId: 'practitioner',
  routes: [
    { id: 'clients', path: '/clients', label: 'Clients',
      permission: 'clients.read', nav: { order: 2 }, view: ClientsScreen },

    { id: 'client-detail', path: '/clients/:clientId', label: 'Client',
      parent: 'clients', view: ClientDetailScreen },
  ],
})
```

| Field | Purpose |
|---|---|
| `id` | Stable handle for `parent` and active-nav resolution |
| `path` | react-router pattern; a child's path must extend its parent's |
| `label` | Navigation, breadcrumbs, document title — one label, not three |
| `parent` | Breadcrumb trail and permission inheritance |
| `permission` | 🔒 Menu visibility only — see below |
| `nav` | Placement. Omit for routes that route but do not navigate |
| `view` | The screen |

## Deriving from it

```tsx
<BrowserRouter>
  <IaProvider ia={ia}>
    <Shell />           {/* navItemsFor(ia, can) + useIaLocation() */}
  </IaProvider>
</BrowserRouter>
```

```ts
IaRoutes({ fallback })            // the router — the only <Route> in the codebase
navItemsFor(ia, can)              // the menu, permission-filtered, in order
useIaLocation()                   // { route, params, breadcrumbs, activeNavId }
```

`useIaLocation` matches with react-router's own matcher, over the very array
`IaRoutes` renders. There is no second matcher, so the highlighted menu item and
the rendered screen cannot describe different pages.

⚠️ Screens reach the IA through `useIa()` / `useIaLocation()`, never by importing
the manifest. A screen that imports the manifest that names it is a cycle whose
resolution depends on import order, and it drags the whole route table into any
test of one screen.

## ⚠️ 🔒 `permission` is menu visibility, not authorization

ADR-05 / NFR-032: `kernel.authz.can()` on the server is the only place a
permission decision is made. Filtering the menu stops a user walking into a
screen that can only fail them; it stops nothing else. **Every route here is
reachable by typing its URL, and the API must refuse it on its own.**

A permission is inherited from the nearest ancestor that declares one, so a
detail screen under a gated list is gated without restating it — a forgotten
restatement would silently widen the menu.

## What `defineIa` rejects

Each of these is a way navigation and routes have actually drifted apart, and
each fails at module load — in a test, in the dev server's first render, in CI —
rather than becoming a bug report:

- duplicate route id, or two routes claiming one path
- a relative path, or an empty label
- a `nav` entry on a parameterised path (a menu cannot link to `/clients/:id`)
- an undeclared `parent`, a self-parent, or a cycle
- **a child whose path is not nested under its parent** — the trail would read
  Clients → Plans while the URL said `/plans`
- two nav items with the same `order` in one section, which sort unpredictably

`validateIa` returns every problem at once; `defineIa` throws with all of them
listed. One pass, not one recompile per mistake.

## Per-app constraints

Some rules are true of one app and not the others. They are passed to `defineIa`
so they fail at module load with everything else, rather than surfacing as a type
error at whichever call site happens to map the items:

```ts
defineIa(manifest, { requireNavIcons: true, minNavItems: 3, maxNavItems: 5 })
```

| Option | Why |
|---|---|
| `requireNavIcons` | 🔒 `MobileShell` types `icon` as required — a bottom bar without icons is unusable |
| `minNavItems` / `maxNavItems` | ⚠️ NFR-059: below three a bottom bar wastes vertical space; above five the thumb targets are too narrow |

## Scope

⚠️ **No domain logic** (NFR-068). This package knows about paths, labels and
parentage. It does not know what a client or a plan is.

⚠️ **The manifest is per app** (Arch §4.3); only the machinery is shared. Three
copies of a breadcrumb walker is how three apps come to answer the same question
differently (NFR-069).

## Checks

```bash
npm test -w @wellnesscrm/ia
```
