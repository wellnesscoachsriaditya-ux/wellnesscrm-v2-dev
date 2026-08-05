import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { navItemsFor, validateIa } from '@wellnesscrm/ia'
import { App } from './App'
import { ia } from './ia/manifest'

/**
 * 🔒 S0 Definition of Done — "all three apps deploy to staging and render."
 *
 * A build succeeding proves the code compiles, not that the app works: a shell
 * that throws on its first render builds perfectly. These assert the app mounts
 * and that what it renders comes from the IA.
 */
describe('practitioner app', () => {
  it('declares a valid IA', () => {
    // `defineIa` already threw at import time if not — this states the
    // requirement where a reader looks for it, and fails with the full list.
    expect(validateIa(ia)).toEqual([])
  })

  it('renders the shell and the landing screen', () => {
    render(<App />)
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Dashboard')
    expect(screen.getByRole('navigation', { name: 'Main' })).toBeInTheDocument()
  })

  it('builds its navigation from the manifest, in declared order', () => {
    render(<App />)
    const links = screen
      .getByRole('navigation', { name: 'Main' })
      .querySelectorAll('a')

    expect(Array.from(links).map((link) => link.textContent?.trim())).toEqual(
      navItemsFor(ia).map((item) => item.label),
    )
  })

  it('marks the current section as the active nav item', () => {
    render(<App />)
    // 🔒 NFR-057 in one assertion: the highlighted item and the rendered screen
    // are the same declaration.
    const current = screen.getByRole('link', { current: 'page' })
    expect(current).toHaveTextContent('Dashboard')
  })

  it('offers every declared route a way to be reached from navigation or a parent', () => {
    // A route that is neither navigable nor a child of something navigable is
    // unreachable except by typing its URL — almost always a mistake.
    const orphans = ia.routes.filter((route) => route.nav === undefined && route.parent === undefined)
    expect(orphans).toEqual([])
  })
})
