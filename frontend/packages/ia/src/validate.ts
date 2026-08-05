import type { IaManifest, IaRoute } from './types'

/** One thing wrong with a manifest. */
export interface IaProblem {
  /** The offending route, or `null` for a whole-manifest problem. */
  readonly routeId: string | null
  readonly message: string
}

/** Constraints an individual app imposes on top of the universal ones. */
export interface IaOptions {
  /**
   * Require every navigation entry to declare an icon.
   *
   * 🔒 `MobileShell` types `icon` as required — a bottom bar without icons is
   * unusable (NFR-059). Declaring the constraint here means a missing icon is
   * caught at module load alongside every other IA problem, rather than as a
   * type error at the one call site that happens to map the items.
   */
  readonly requireNavIcons?: boolean
  /**
   * Bounds on how many navigation entries the app may declare.
   *
   * ⚠️ `MobileShell` documents three to five: below three a bottom bar is not
   * worth the vertical space, above five the targets are too narrow for a thumb
   * (NFR-059). A bound stated in a comment is a bound that gets exceeded.
   */
  readonly maxNavItems?: number
  readonly minNavItems?: number
}

/** Does this path pattern take parameters? */
export function hasParams(path: string): boolean {
  return path.includes(':') || path.includes('*')
}

/**
 * Split a path into segments, ignoring the leading slash and any trailing one.
 * `/clients/:id` → `['clients', ':id']`; `/` → `[]`.
 */
export function segmentsOf(path: string): string[] {
  return path.split('/').filter((segment) => segment.length > 0)
}

/**
 * Is `childPath` nested beneath `parentPath`?
 *
 * The root `/` is the parent of everything. Otherwise the child's segments must
 * begin with the parent's, so `/clients/:id` sits under `/clients` but `/plans`
 * does not.
 */
function isNestedUnder(childPath: string, parentPath: string): boolean {
  const parent = segmentsOf(parentPath)
  if (parent.length === 0) return true

  const child = segmentsOf(childPath)
  if (child.length <= parent.length) return false

  return parent.every((segment, index) => child[index] === segment)
}

/**
 * Check a manifest against the invariants NFR-057 depends on.
 *
 * 🔒 Every rule here is a way navigation, breadcrumbs and routes have actually
 * drifted apart in real applications — V1 among them. Catching them requires
 * looking at the manifest as a whole, which is exactly why the IA is data:
 * a set of `<Route>` elements scattered across a tree cannot be checked at all.
 *
 * Returns every problem rather than the first, so a bad manifest is fixed in
 * one pass instead of one recompile per mistake.
 */
export function validateIa(manifest: IaManifest, options: IaOptions = {}): IaProblem[] {
  const problems: IaProblem[] = []
  const { routes } = manifest

  if (routes.length === 0) {
    problems.push({ routeId: null, message: 'the manifest declares no routes' })
    return problems
  }

  // ─── Pass 1: per-route facts, and the id index the later passes need ───
  const byId = new Map<string, IaRoute>()
  const pathOwner = new Map<string, string>()

  for (const route of routes) {
    if (byId.has(route.id)) {
      problems.push({ routeId: route.id, message: `duplicate route id "${route.id}"` })
    } else {
      byId.set(route.id, route)
    }

    if (!route.path.startsWith('/')) {
      problems.push({
        routeId: route.id,
        message: `path "${route.path}" must be absolute — every IA path starts with "/"`,
      })
    }

    const owner = pathOwner.get(route.path)
    if (owner !== undefined) {
      problems.push({
        routeId: route.id,
        message: `path "${route.path}" is already claimed by route "${owner}"`,
      })
    } else {
      pathOwner.set(route.path, route.id)
    }

    if (route.label.trim().length === 0) {
      problems.push({ routeId: route.id, message: 'label is empty — breadcrumbs would render a gap' })
    }

    // 🔒 A menu cannot link to a pattern. `/clients/:clientId` has no single
    // destination, so a nav entry for it would have to invent one.
    if (route.nav !== undefined && hasParams(route.path)) {
      problems.push({
        routeId: route.id,
        message:
          `"${route.path}" takes parameters, so it cannot be a navigation target. ` +
          'Give the list route the nav entry and make this its child.',
      })
    }

    if (options.requireNavIcons === true && route.nav !== undefined && route.nav.icon === undefined) {
      problems.push({
        routeId: route.id,
        message: 'this app requires an icon on every navigation entry (NFR-059)',
      })
    }
  }

  // ─── Pass 2: parentage ─────────────────────────────────────────────────
  for (const route of routes) {
    if (route.parent === undefined) continue

    const parent = byId.get(route.parent)
    if (parent === undefined) {
      problems.push({
        routeId: route.id,
        message: `parent "${route.parent}" is not a declared route`,
      })
      continue
    }

    if (parent.id === route.id) {
      problems.push({ routeId: route.id, message: 'route is its own parent' })
      continue
    }

    if (!isNestedUnder(route.path, parent.path)) {
      problems.push({
        routeId: route.id,
        message:
          `path "${route.path}" is not nested under its parent "${parent.path}". ` +
          'Breadcrumbs would describe a hierarchy the URL does not have.',
      })
    }
  }

  // ─── Pass 3: cycles ────────────────────────────────────────────────────
  // A cycle makes `breadcrumbsFor` non-terminating, so it must be impossible
  // to construct one rather than merely unlikely.
  for (const route of routes) {
    const seen = new Set<string>([route.id])
    let current = route.parent

    while (current !== undefined) {
      if (seen.has(current)) {
        problems.push({
          routeId: route.id,
          message: `parent chain forms a cycle through "${current}"`,
        })
        break
      }
      seen.add(current)
      current = byId.get(current)?.parent
    }
  }

  // ─── Pass 4: navigation ordering ───────────────────────────────────────
  const ordersBySection = new Map<string, Map<number, string>>()
  let navCount = 0

  for (const route of routes) {
    if (route.nav === undefined) continue
    navCount += 1

    const section = route.nav.section ?? ''
    let orders = ordersBySection.get(section)
    if (orders === undefined) {
      orders = new Map<number, string>()
      ordersBySection.set(section, orders)
    }

    const holder = orders.get(route.nav.order)
    if (holder !== undefined) {
      problems.push({
        routeId: route.id,
        message:
          `nav order ${route.nav.order} is already used by "${holder}"` +
          (section === '' ? '' : ` in section "${section}"`) +
          '. Two items with the same order sort unpredictably.',
      })
    } else {
      orders.set(route.nav.order, route.id)
    }
  }

  // ─── Pass 5: navigation size ───────────────────────────────────────────
  if (options.minNavItems !== undefined && navCount < options.minNavItems) {
    problems.push({
      routeId: null,
      message: `${navCount} navigation entr${navCount === 1 ? 'y' : 'ies'}; this app requires at least ${options.minNavItems}`,
    })
  }

  if (options.maxNavItems !== undefined && navCount > options.maxNavItems) {
    problems.push({
      routeId: null,
      message: `${navCount} navigation entries exceeds this app's maximum of ${options.maxNavItems} (NFR-059)`,
    })
  }

  return problems
}
