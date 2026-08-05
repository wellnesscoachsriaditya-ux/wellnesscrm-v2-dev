import { createElement } from 'react'
import type { RouteObject } from 'react-router-dom'
import type { Ia, IaManifest } from './types'
import { validateIa } from './validate'
import type { IaOptions } from './validate'

/**
 * Validate a manifest and build the lookups everything else derives from.
 *
 * 🔒 NFR-057 — **the** entry point. Navigation, breadcrumbs, the router and
 * permission-gated menu visibility all read from the value this returns, so
 * adding a screen is one manifest entry and cannot be half-done: a route that
 * exists but is missing from navigation is a deliberate `nav`-less entry, not
 * an oversight nobody notices until a user reports it.
 *
 * Throws rather than returning problems. A malformed IA is a programming error
 * discovered at module load — in a test, in the dev server's first render, in
 * CI — and there is no sensible way for an application to carry on with a
 * navigation structure that contradicts itself.
 */
export function defineIa(manifest: IaManifest, options: IaOptions = {}): Ia {
  const problems = validateIa(manifest, options)

  if (problems.length > 0) {
    const detail = problems
      .map(({ routeId, message }) => `  • ${routeId === null ? '(manifest)' : routeId}: ${message}`)
      .join('\n')

    throw new Error(`Invalid IA manifest for "${manifest.appId}" (NFR-057):\n${detail}`)
  }

  const byId = new Map(manifest.routes.map((route) => [route.id, route]))

  // Elements are built once, here, so rendering and active-route resolution
  // share one array and cannot disagree about what the URL means.
  const routeObjects: RouteObject[] = manifest.routes.map((route) => ({
    id: route.id,
    path: route.path,
    element: createElement(route.view),
  }))

  return { appId: manifest.appId, routes: manifest.routes, byId, routeObjects }
}
