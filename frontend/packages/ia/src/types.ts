import type { ComponentType, ReactNode } from 'react'
import type { RouteObject } from 'react-router-dom'

/**
 * Where a route appears in the application's navigation.
 *
 * A route without `nav` still exists and still routes — it simply is not a
 * navigation destination. Detail screens are the usual case: `/clients/:id` is
 * reachable and produces breadcrumbs, but it is not a menu item.
 */
export interface IaNavPlacement {
  /**
   * Position within `section`. Lower sorts first.
   *
   * ⚠️ Declared rather than derived from array order. Array order is invisible
   * in a diff — moving an entry three lines up silently reorders the menu —
   * and it forces one file's layout to encode two different orderings (the
   * router's and the menu's). Duplicate orders within a section are rejected,
   * so the result is always deterministic.
   */
  readonly order: number
  /** Optional grouping label, for shells that divide their navigation. */
  readonly section?: string
  /**
   * Navigation icon.
   *
   * Required by `MobileShell` (a bottom bar without icons is unusable) and
   * optional elsewhere. The manifest carries it because Arch §4.3 lists icon
   * as part of the declared IA.
   */
  readonly icon?: ReactNode
  /** Count badge — unread messages, pending reviews. */
  readonly badge?: number
}

/** One screen, declared once. */
export interface IaRoute {
  /** Stable identifier. Referenced by `parent` and by active-nav resolution. */
  readonly id: string
  /**
   * Absolute path pattern in react-router syntax, e.g. `/clients/:clientId`.
   *
   * A child's path MUST extend its parent's, which is what keeps breadcrumbs
   * honest: a trail that reads Clients → Plans while the URL says `/plans`
   * is the drift NFR-057 exists to prevent.
   */
  readonly path: string
  /** Human label. Used for navigation, breadcrumbs and the document title. */
  readonly label: string
  /** Parent route id. Drives breadcrumbs and permission inheritance. */
  readonly parent?: string
  /**
   * Action required to see this route in navigation.
   *
   * ⚠️ 🔒 **This is menu visibility, not authorization.** NFR-032 / ADR-05:
   * `kernel.authz.can()` is the only place a permission decision is made, and
   * it is on the server. Hiding a menu item stops a user stumbling into a
   * screen that will only fail; it stops nothing else. Every route reachable
   * by typing its URL must still be refused by the API.
   *
   * Inherited from `parent` when omitted.
   */
  readonly permission?: string
  /** Navigation placement. Omit for routes that route but do not navigate. */
  readonly nav?: IaNavPlacement
  /** The screen component. */
  readonly view: ComponentType
}

/** The manifest as authored, before validation. */
export interface IaManifest {
  /** Which application this is — `practitioner`, `client-pwa`, `operator`. */
  readonly appId: string
  readonly routes: readonly IaRoute[]
}

/**
 * A validated manifest with the lookups navigation, breadcrumbs and the router
 * all read from. Produced by `defineIa`; never constructed by hand.
 */
export interface Ia {
  readonly appId: string
  readonly routes: readonly IaRoute[]
  readonly byId: ReadonlyMap<string, IaRoute>
  /**
   * react-router route objects, built once.
   *
   * 🔒 Rendering and active-route resolution share this exact array, so they
   * cannot disagree about which route is current. A second, hand-written
   * matcher would be a second opinion on the URL, and the two would eventually
   * differ on a path neither author had in mind.
   */
  readonly routeObjects: readonly RouteObject[]
}

/** A breadcrumb, shaped for the design system's `PageHeader`. */
export interface IaBreadcrumb {
  readonly label: string
  /** Omitted on the current page — the last crumb is not a link. */
  readonly href?: string
}

/** A navigation item, shaped for the design system's layout shells. */
export interface IaNavItem {
  readonly id: string
  readonly label: string
  readonly href: string
  readonly icon?: ReactNode
  readonly badge?: number
}
