/**
 * @wellnesscrm/ia — information architecture as data.
 *
 * 🔒 **NFR-057 / Arch §4.3** — "navigation MUST derive from one declared
 * information architecture, so navigation, breadcrumbs and routes cannot drift
 * apart." V1's recorded failure was inconsistent navigation; this package is
 * the countermeasure, and it is mechanical rather than procedural.
 *
 * Each app declares one manifest. From it derive:
 *
 *   • the router          — `IaRoutes`
 *   • the navigation      — `navItemsFor`
 *   • the breadcrumbs     — `breadcrumbsFor`, via `useIaLocation`
 *   • menu visibility     — `effectivePermission` + the `can` predicate
 *
 * ⚠️ **Why a package rather than a file per app.** The manifest is per-app
 * (Arch §4.3) but the machinery is identical for all three, and three copies of
 * a breadcrumb walker is how the three apps would come to behave differently
 * on the same question (NFR-069).
 *
 * ⚠️ **This package holds no domain logic** (NFR-068). It knows about paths,
 * labels and parentage — never about clients, plans or entitlements.
 */

export { defineIa } from './define'
export { validateIa, hasParams, segmentsOf } from './validate'
export type { IaOptions, IaProblem } from './validate'

export {
  activeNavIdFor,
  ancestorsOf,
  breadcrumbsFor,
  buildHref,
  effectivePermission,
  navItemsFor,
} from './derive'
export type { IaParams } from './derive'

export { IaProvider, IaRoutes, useIa, useIaLocation } from './react'
export type { IaLocation, IaProviderProps, IaRoutesProps } from './react'

export type {
  Ia,
  IaBreadcrumb,
  IaManifest,
  IaNavItem,
  IaNavPlacement,
  IaRoute,
} from './types'
