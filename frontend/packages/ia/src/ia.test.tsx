import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import {
  activeNavIdFor,
  ancestorsOf,
  breadcrumbsFor,
  buildHref,
  defineIa,
  effectivePermission,
  navItemsFor,
  validateIa,
  IaProvider,
  IaRoutes,
  useIaLocation,
} from './index'
import type { Ia, IaManifest, IaOptions } from './index'

/** A placeholder screen; the IA does not care what a view renders. */
const View = () => <p>view</p>

function manifest(routes: IaManifest['routes']): IaManifest {
  return { appId: 'test', routes }
}

/** A small but complete IA: a list, its detail child, and two siblings. */
function sampleIa(): Ia {
  return defineIa(
    manifest([
      { id: 'home', path: '/', label: 'Home', nav: { order: 1 }, view: View },
      {
        id: 'clients',
        path: '/clients',
        label: 'Clients',
        permission: 'clients.read',
        nav: { order: 2, badge: 3 },
        view: View,
      },
      {
        id: 'client-detail',
        path: '/clients/:clientId',
        label: 'Client',
        parent: 'clients',
        view: View,
      },
      {
        id: 'settings',
        path: '/settings',
        label: 'Settings',
        permission: 'settings.manage',
        nav: { order: 3 },
        view: View,
      },
    ]),
  )
}

function messagesFor(m: IaManifest): string[] {
  return validateIa(m).map((problem) => problem.message)
}

function messagesFor2(m: IaManifest, options: IaOptions): string[] {
  return validateIa(m, options).map((problem) => problem.message)
}

describe('validateIa', () => {
  it('accepts a well-formed manifest', () => {
    expect(
      validateIa(
        manifest([
          { id: 'home', path: '/', label: 'Home', nav: { order: 1 }, view: View },
          { id: 'clients', path: '/clients', label: 'Clients', nav: { order: 2 }, view: View },
        ]),
      ),
    ).toEqual([])
  })

  it('rejects an empty manifest', () => {
    expect(messagesFor(manifest([]))).toContain('the manifest declares no routes')
  })

  it('rejects a duplicate route id', () => {
    const problems = messagesFor(
      manifest([
        { id: 'clients', path: '/clients', label: 'Clients', view: View },
        { id: 'clients', path: '/other', label: 'Other', view: View },
      ]),
    )
    expect(problems.some((m) => m.includes('duplicate route id'))).toBe(true)
  })

  it('rejects two routes claiming one path', () => {
    const problems = messagesFor(
      manifest([
        { id: 'a', path: '/clients', label: 'A', view: View },
        { id: 'b', path: '/clients', label: 'B', view: View },
      ]),
    )
    expect(problems.some((m) => m.includes('already claimed by route "a"'))).toBe(true)
  })

  it('rejects a relative path', () => {
    const problems = messagesFor(manifest([{ id: 'a', path: 'clients', label: 'A', view: View }]))
    expect(problems.some((m) => m.includes('must be absolute'))).toBe(true)
  })

  it('rejects an empty label, which would render a gap in the breadcrumbs', () => {
    const problems = messagesFor(manifest([{ id: 'a', path: '/a', label: '   ', view: View }]))
    expect(problems.some((m) => m.includes('label is empty'))).toBe(true)
  })

  it('rejects a nav entry on a parameterised path', () => {
    const problems = messagesFor(
      manifest([{ id: 'detail', path: '/clients/:id', label: 'Client', nav: { order: 1 }, view: View }]),
    )
    expect(problems.some((m) => m.includes('cannot be a navigation target'))).toBe(true)
  })

  it('rejects a parent that is not declared', () => {
    const problems = messagesFor(
      manifest([{ id: 'child', path: '/a/b', label: 'B', parent: 'ghost', view: View }]),
    )
    expect(problems.some((m) => m.includes('is not a declared route'))).toBe(true)
  })

  it('rejects a route that is its own parent', () => {
    const problems = messagesFor(
      manifest([{ id: 'a', path: '/a', label: 'A', parent: 'a', view: View }]),
    )
    expect(problems.some((m) => m.includes('its own parent'))).toBe(true)
  })

  it('rejects a child whose path is not nested under its parent', () => {
    // 🔒 The breadcrumb would read Clients → Plans while the URL says /plans.
    const problems = messagesFor(
      manifest([
        { id: 'clients', path: '/clients', label: 'Clients', view: View },
        { id: 'plans', path: '/plans', label: 'Plans', parent: 'clients', view: View },
      ]),
    )
    expect(problems.some((m) => m.includes('is not nested under its parent'))).toBe(true)
  })

  it('accepts any child of the root path', () => {
    expect(
      validateIa(
        manifest([
          { id: 'home', path: '/', label: 'Home', view: View },
          { id: 'clients', path: '/clients', label: 'Clients', parent: 'home', view: View },
        ]),
      ),
    ).toEqual([])
  })

  it('rejects a cycle in the parent chain', () => {
    const problems = messagesFor(
      manifest([
        { id: 'a', path: '/a', label: 'A', parent: 'b', view: View },
        { id: 'b', path: '/b', label: 'B', parent: 'a', view: View },
      ]),
    )
    expect(problems.some((m) => m.includes('cycle'))).toBe(true)
  })

  it('rejects two nav items with the same order in one section', () => {
    const problems = messagesFor(
      manifest([
        { id: 'a', path: '/a', label: 'A', nav: { order: 1 }, view: View },
        { id: 'b', path: '/b', label: 'B', nav: { order: 1 }, view: View },
      ]),
    )
    expect(problems.some((m) => m.includes('sort unpredictably'))).toBe(true)
  })

  it('allows the same order in different sections', () => {
    expect(
      validateIa(
        manifest([
          { id: 'a', path: '/a', label: 'A', nav: { order: 1, section: 'Work' }, view: View },
          { id: 'b', path: '/b', label: 'B', nav: { order: 1, section: 'Admin' }, view: View },
        ]),
      ),
    ).toEqual([])
  })

  it('reports every problem at once rather than only the first', () => {
    expect(
      validateIa(
        manifest([
          { id: 'a', path: 'relative', label: '', view: View },
          { id: 'a', path: '/b', label: 'B', parent: 'ghost', view: View },
        ]),
      ).length,
    ).toBeGreaterThan(3)
  })
})

describe('validateIa per-app options', () => {
  const icon = <svg />

  it('requires nav icons only when the app asks for it', () => {
    const m = manifest([{ id: 'a', path: '/a', label: 'A', nav: { order: 1 }, view: View }])
    expect(validateIa(m)).toEqual([])
    expect(messagesFor2(m, { requireNavIcons: true })).toContain(
      'this app requires an icon on every navigation entry (NFR-059)',
    )
  })

  it('accepts a nav entry that declares an icon', () => {
    expect(
      validateIa(manifest([{ id: 'a', path: '/a', label: 'A', nav: { order: 1, icon }, view: View }]), {
        requireNavIcons: true,
      }),
    ).toEqual([])
  })

  it('enforces the bottom-bar bounds — NFR-059', () => {
    const two = manifest([
      { id: 'a', path: '/a', label: 'A', nav: { order: 1 }, view: View },
      { id: 'b', path: '/b', label: 'B', nav: { order: 2 }, view: View },
    ])
    expect(messagesFor2(two, { minNavItems: 3 })[0]).toContain('requires at least 3')
    expect(messagesFor2(two, { maxNavItems: 1 })[0]).toContain('maximum of 1')
    expect(validateIa(two, { minNavItems: 2, maxNavItems: 5 })).toEqual([])
  })

  it('does not count non-navigable routes towards the bounds', () => {
    const m = manifest([
      { id: 'a', path: '/a', label: 'A', nav: { order: 1 }, view: View },
      { id: 'detail', path: '/a/:id', label: 'Detail', parent: 'a', view: View },
    ])
    expect(validateIa(m, { maxNavItems: 1 })).toEqual([])
  })
})

describe('defineIa', () => {
  it('throws on an invalid manifest, naming every problem', () => {
    expect(() =>
      defineIa(manifest([{ id: 'a', path: 'relative', label: 'A', parent: 'ghost', view: View }])),
    ).toThrow(/NFR-057/)
  })

  it('indexes routes by id and builds one route object per route', () => {
    const ia = sampleIa()
    expect(ia.byId.get('clients')?.label).toBe('Clients')
    expect(ia.routeObjects).toHaveLength(4)
    expect(ia.routeObjects.map((route) => route.id)).toEqual([
      'home',
      'clients',
      'client-detail',
      'settings',
    ])
  })
})

describe('buildHref', () => {
  it('returns a static path unchanged', () => {
    expect(buildHref('/clients')).toBe('/clients')
  })

  it('substitutes params', () => {
    expect(buildHref('/clients/:clientId', { clientId: 'abc' })).toBe('/clients/abc')
  })

  it('encodes param values', () => {
    expect(buildHref('/clients/:clientId', { clientId: 'a/b' })).toBe('/clients/a%2Fb')
  })

  it('returns null when a param is missing rather than a plausible broken link', () => {
    expect(buildHref('/clients/:clientId')).toBeNull()
    expect(buildHref('/clients/:clientId', { clientId: '' })).toBeNull()
  })
})

describe('ancestorsOf', () => {
  it('returns the chain outermost first, including the route itself', () => {
    expect(ancestorsOf(sampleIa(), 'client-detail').map((route) => route.id)).toEqual([
      'clients',
      'client-detail',
    ])
  })

  it('returns an empty chain for an unknown id', () => {
    expect(ancestorsOf(sampleIa(), 'nope')).toEqual([])
  })
})

describe('effectivePermission', () => {
  it('returns a route’s own permission', () => {
    expect(effectivePermission(sampleIa(), 'clients')).toBe('clients.read')
  })

  it('inherits from the nearest ancestor that declares one', () => {
    expect(effectivePermission(sampleIa(), 'client-detail')).toBe('clients.read')
  })

  it('is undefined when nothing in the chain declares one', () => {
    expect(effectivePermission(sampleIa(), 'home')).toBeUndefined()
  })
})

describe('navItemsFor', () => {
  it('returns declared nav routes in order, and only those', () => {
    expect(navItemsFor(sampleIa()).map((item) => item.id)).toEqual(['home', 'clients', 'settings'])
  })

  it('carries the badge through and omits it where undeclared', () => {
    const items = navItemsFor(sampleIa())
    expect(items.find((item) => item.id === 'clients')?.badge).toBe(3)
    expect(items.find((item) => item.id === 'home')).not.toHaveProperty('badge')
  })

  it('hides items whose effective permission is not held', () => {
    const ia = sampleIa()
    const items = navItemsFor(ia, (permission) => permission !== 'settings.manage')
    expect(items.map((item) => item.id)).toEqual(['home', 'clients'])
  })

  it('sorts by section, then by order within it', () => {
    const ia = defineIa(
      manifest([
        { id: 'z', path: '/z', label: 'Z', nav: { order: 1, section: 'Admin' }, view: View },
        { id: 'b', path: '/b', label: 'B', nav: { order: 2, section: 'Work' }, view: View },
        { id: 'a', path: '/a', label: 'A', nav: { order: 1, section: 'Work' }, view: View },
      ]),
    )
    expect(navItemsFor(ia).map((item) => item.id)).toEqual(['z', 'a', 'b'])
  })
})

describe('breadcrumbsFor', () => {
  it('gives the current page no href — it is where you already are', () => {
    const crumbs = breadcrumbsFor(sampleIa(), 'clients')
    expect(crumbs).toEqual([{ label: 'Clients' }])
  })

  it('links ancestors and resolves params from the current URL', () => {
    const crumbs = breadcrumbsFor(sampleIa(), 'client-detail', { clientId: 'abc' })
    expect(crumbs).toEqual([{ label: 'Clients', href: '/clients' }, { label: 'Client' }])
  })

  it('renders an unresolvable ancestor as text rather than a link that 404s', () => {
    const ia = defineIa(
      manifest([
        { id: 'clients', path: '/clients/:clientId', label: 'Client', view: View },
        {
          id: 'plan',
          path: '/clients/:clientId/plans/:planId',
          label: 'Plan',
          parent: 'clients',
          view: View,
        },
      ]),
    )
    expect(breadcrumbsFor(ia, 'plan', { planId: 'p1' })).toEqual([
      { label: 'Client' },
      { label: 'Plan' },
    ])
  })
})

describe('activeNavIdFor', () => {
  it('is the route itself when it is a nav target', () => {
    expect(activeNavIdFor(sampleIa(), 'clients')).toBe('clients')
  })

  it('is the nearest navigable ancestor for a detail screen', () => {
    // 🔒 Standing on /clients/abc must keep "Clients" highlighted.
    expect(activeNavIdFor(sampleIa(), 'client-detail')).toBe('clients')
  })

  it('is undefined when no ancestor is navigable', () => {
    const ia = defineIa(manifest([{ id: 'orphan', path: '/orphan', label: 'Orphan', view: View }]))
    expect(activeNavIdFor(ia, 'orphan')).toBeUndefined()
  })
})

describe('IaRoutes and useIaLocation', () => {
  function Probe() {
    const { route, breadcrumbs, activeNavId, params } = useIaLocation()
    return (
      <div>
        <span data-testid="route">{route?.id ?? 'none'}</span>
        <span data-testid="active">{activeNavId ?? 'none'}</span>
        <span data-testid="crumbs">{breadcrumbs.map((crumb) => crumb.label).join(' / ')}</span>
        <span data-testid="params">{JSON.stringify(params)}</span>
      </div>
    )
  }

  function renderAt(pathname: string) {
    render(
      <MemoryRouter initialEntries={[pathname]}>
        <IaProvider ia={sampleIa()}>
          <Probe />
          <IaRoutes fallback={<p>Not found</p>} />
        </IaProvider>
      </MemoryRouter>,
    )
  }

  it('renders the view declared for the matched path', () => {
    renderAt('/clients')
    expect(screen.getByText('view')).toBeInTheDocument()
    expect(screen.getByTestId('route')).toHaveTextContent('clients')
  })

  it('renders the fallback for an undeclared path', () => {
    renderAt('/nowhere')
    expect(screen.getByText('Not found')).toBeInTheDocument()
    expect(screen.getByTestId('route')).toHaveTextContent('none')
  })

  it('resolves params, breadcrumbs and the active nav item for a detail URL', () => {
    renderAt('/clients/abc')
    expect(screen.getByTestId('route')).toHaveTextContent('client-detail')
    expect(screen.getByTestId('params')).toHaveTextContent('{"clientId":"abc"}')
    expect(screen.getByTestId('crumbs')).toHaveTextContent('Clients / Client')
    // 🔒 The whole point of NFR-057: the highlighted menu item and the rendered
    // screen come from one declaration, so they cannot disagree.
    expect(screen.getByTestId('active')).toHaveTextContent('clients')
  })

  it('prefers the static route over the parameterised one at the same depth', () => {
    const ia = defineIa(
      manifest([
        { id: 'detail', path: '/clients/:clientId', label: 'Client', view: () => <p>detail</p> },
        { id: 'new', path: '/clients/new', label: 'New client', view: () => <p>new</p> },
      ]),
    )
    render(
      <MemoryRouter initialEntries={['/clients/new']}>
        <IaProvider ia={ia}>
          <IaRoutes />
        </IaProvider>
      </MemoryRouter>,
    )
    expect(screen.getByText('new')).toBeInTheDocument()
  })

  it('refuses to resolve outside an IaProvider rather than rendering wrongly', () => {
    // Context has no sensible default: a missing provider is a wiring bug, and
    // silently returning an empty location would hide it behind a blank shell.
    const Bare = () => {
      useIaLocation()
      return null
    }
    expect(() =>
      render(
        <MemoryRouter>
          <Bare />
        </MemoryRouter>,
      ),
    ).toThrow(/NFR-057/)
  })
})
