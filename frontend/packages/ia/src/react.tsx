import { createContext, createElement, useContext, useMemo } from 'react'
import type { ReactElement, ReactNode } from 'react'
import { matchRoutes, useLocation, useRoutes } from 'react-router-dom'
import type { RouteObject } from 'react-router-dom'
import { activeNavIdFor, breadcrumbsFor } from './derive'
import type { IaParams } from './derive'
import type { Ia, IaBreadcrumb, IaRoute } from './types'

const IaContext = createContext<Ia | null>(null)

export interface IaProviderProps {
  readonly ia: Ia
  readonly children: ReactNode
}

/**
 * Make the app's IA available to every screen below it.
 *
 * ⚠️ **Why context rather than importing the manifest.** A screen that imports
 * the manifest creates a cycle — the manifest names the screen, the screen names
 * the manifest — and the module that wins the race is decided by import order,
 * which is not something anyone should have to reason about. Context also keeps
 * screens testable in isolation: a test renders one screen under a two-route IA
 * instead of dragging in the whole application's route table.
 *
 * Must sit inside the router: `useIaLocation` reads the current location.
 */
export function IaProvider({ ia, children }: IaProviderProps): ReactElement {
  return createElement(IaContext.Provider, { value: ia }, children)
}

/** The IA of the surrounding app. Throws outside an `IaProvider`. */
export function useIa(): Ia {
  const ia = useContext(IaContext)
  if (ia === null) {
    throw new Error('useIa: no <IaProvider> above this component (NFR-057).')
  }
  return ia
}

export interface IaRoutesProps {
  /**
   * Rendered when no declared route matches.
   *
   * ⚠️ Optional in the type, but an app without one shows a blank page on a
   * typo'd URL. Every app here supplies one.
   */
  readonly fallback?: ReactNode
}

/**
 * Render the IA's routes.
 *
 * There is no `<Route>` element anywhere in this codebase outside this function.
 * That is the point: a screen cannot be added to the router without being added
 * to the IA, because the router only knows what the IA declares.
 */
export function IaRoutes({ fallback }: IaRoutesProps): ReactElement | null {
  const ia = useIa()

  const routes = useMemo<RouteObject[]>(
    () =>
      fallback === undefined
        ? [...ia.routeObjects]
        : [...ia.routeObjects, { path: '*', element: fallback }],
    [ia, fallback],
  )

  return useRoutes(routes)
}

/** Everything the shell and the current screen need to know about the URL. */
export interface IaLocation {
  /** The matched route, or `null` when the URL matches nothing declared. */
  readonly route: IaRoute | null
  readonly params: IaParams
  /** 🔒 NFR-057 — derived from the manifest, never written per screen. */
  readonly breadcrumbs: readonly IaBreadcrumb[]
  /** The nav item to mark active — the nearest navigable ancestor. */
  readonly activeNavId: string | undefined
}

/**
 * Resolve the current URL against the IA.
 *
 * Uses react-router's own matcher over the very array `IaRoutes` renders, so the
 * breadcrumbs and the highlighted menu item always describe the screen actually
 * on display. A second hand-written matcher would be a second opinion about path
 * ranking, and the two would eventually disagree.
 */
export function useIaLocation(): IaLocation {
  const ia = useIa()
  const { pathname } = useLocation()

  return useMemo<IaLocation>(() => {
    // ⚠️ react-router types `RouteObject.caseSensitive` as `boolean | undefined`
    // but its matcher's parameter as `boolean`, so under
    // `exactOptionalPropertyTypes` the library's own two types disagree. The
    // objects in `routeObjects` never set the property at all, so the assertion
    // narrows to something already true rather than papering over a mismatch.
    // Deliberately the *same array* `IaRoutes` renders — a second array is what
    // would let rendering and matching drift.
    const matchable = ia.routeObjects as Parameters<typeof matchRoutes>[0]
    const matches = matchRoutes(matchable, pathname)
    const matched = matches?.[matches.length - 1]
    const routeId = matched?.route.id

    if (matched === undefined || routeId === undefined) {
      return { route: null, params: {}, breadcrumbs: [], activeNavId: undefined }
    }

    const params: IaParams = matched.params

    return {
      route: ia.byId.get(routeId) ?? null,
      params,
      breadcrumbs: breadcrumbsFor(ia, routeId, params),
      activeNavId: activeNavIdFor(ia, routeId),
    }
  }, [ia, pathname])
}
