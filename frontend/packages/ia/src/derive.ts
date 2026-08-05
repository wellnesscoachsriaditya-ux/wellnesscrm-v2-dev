import type { Ia, IaBreadcrumb, IaNavItem, IaRoute } from './types'

/** Route params, as react-router supplies them. */
export type IaParams = Readonly<Record<string, string | undefined>>

/**
 * Substitute params into a path pattern.
 *
 * A pattern whose params are missing cannot be linked to, so it yields `null`
 * rather than a URL containing a literal `:clientId` — a broken link that looks
 * plausible is worse than no link, because the breadcrumb still renders.
 */
export function buildHref(path: string, params: IaParams = {}): string | null {
  const segments = path.split('/').map((segment) => {
    if (!segment.startsWith(':')) return segment

    const value = params[segment.slice(1)]
    return value === undefined || value === '' ? null : encodeURIComponent(value)
  })

  return segments.some((segment) => segment === null) ? null : segments.join('/')
}

/**
 * The route and its ancestors, outermost first.
 *
 * The chain is bounded by the manifest's size: `validateIa` rejects cycles, and
 * the guard here is a second line of defence for a manifest built by hand in a
 * test rather than through `defineIa`.
 */
export function ancestorsOf(ia: Ia, routeId: string): IaRoute[] {
  const chain: IaRoute[] = []
  const seen = new Set<string>()
  let current = ia.byId.get(routeId)

  while (current !== undefined && !seen.has(current.id)) {
    chain.unshift(current)
    seen.add(current.id)
    current = current.parent === undefined ? undefined : ia.byId.get(current.parent)
  }

  return chain
}

/**
 * The action a route requires, inherited from the nearest ancestor that
 * declares one.
 *
 * Inheritance rather than repetition: a detail screen under a gated list is
 * gated. Requiring each child to restate it means a forgotten line silently
 * widens the menu, which is the failure mode worth designing out.
 */
export function effectivePermission(ia: Ia, routeId: string): string | undefined {
  const chain = ancestorsOf(ia, routeId)

  for (let index = chain.length - 1; index >= 0; index -= 1) {
    const permission = chain[index]?.permission
    if (permission !== undefined) return permission
  }

  return undefined
}

/**
 * The navigation items a holder of `can` should see, in declared order.
 *
 * ⚠️ 🔒 Filtering here is **menu visibility only** (NFR-032). The server decides
 * what may actually be read or written; this decides what is worth offering.
 * A route omitted from the menu remains reachable by URL and must still be
 * refused by the API.
 *
 * @param can Predicate over an action name. Defaults to permitting everything,
 *            which is correct until S1 introduces a session with actions.
 */
export function navItemsFor(ia: Ia, can: (permission: string) => boolean = () => true): IaNavItem[] {
  const items: { route: IaRoute; section: string; order: number }[] = []

  for (const route of ia.routes) {
    if (route.nav === undefined) continue

    const permission = effectivePermission(ia, route.id)
    if (permission !== undefined && !can(permission)) continue

    items.push({ route, section: route.nav.section ?? '', order: route.nav.order })
  }

  items.sort((a, b) => (a.section === b.section ? a.order - b.order : a.section.localeCompare(b.section)))

  return items.map(({ route }) => {
    // `exactOptionalPropertyTypes` — an absent icon must be absent, not
    // present-and-undefined, or it fails the shells' required-icon types.
    const icon = route.nav?.icon
    const badge = route.nav?.badge

    return {
      id: route.id,
      label: route.label,
      href: route.path,
      ...(icon !== undefined ? { icon } : {}),
      ...(badge !== undefined ? { badge } : {}),
    }
  })
}

/**
 * The breadcrumb trail for a route.
 *
 * 🔒 NFR-057 — derived from the same parentage the router uses, never written
 * per screen. The last crumb has no `href`: it is where you already are.
 * Ancestors whose paths need params the current URL does not supply are
 * rendered as plain text rather than as links that 404.
 */
export function breadcrumbsFor(ia: Ia, routeId: string, params: IaParams = {}): IaBreadcrumb[] {
  const chain = ancestorsOf(ia, routeId)

  return chain.map((route, index) => {
    if (index === chain.length - 1) return { label: route.label }

    const href = buildHref(route.path, params)
    return href === null ? { label: route.label } : { label: route.label, href }
  })
}

/**
 * Which navigation item should read as active for a route.
 *
 * A detail screen keeps its section highlighted: standing on
 * `/clients/:clientId` should light up Clients, not nothing. Resolved by
 * walking up to the nearest ancestor that is a navigation target.
 */
export function activeNavIdFor(ia: Ia, routeId: string): string | undefined {
  const chain = ancestorsOf(ia, routeId)

  for (let index = chain.length - 1; index >= 0; index -= 1) {
    const route = chain[index]
    if (route?.nav !== undefined) return route.id
  }

  return undefined
}
