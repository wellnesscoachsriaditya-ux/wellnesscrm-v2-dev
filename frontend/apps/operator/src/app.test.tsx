import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { navItemsFor, validateIa } from '@wellnesscrm/ia'
import { App } from './App'
import { ia } from './ia/manifest'

/** 🔒 S0 Definition of Done — the app mounts and renders what the IA declares. */
describe('operator console', () => {
  it('declares a valid IA', () => {
    expect(validateIa(ia)).toEqual([])
  })

  it('renders the shell and the landing screen', () => {
    render(<App />)
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Tenants')
  })

  it('builds its navigation from the manifest, in declared order', () => {
    render(<App />)
    // Scoped by name: `PageHeader` renders a second <nav> for the breadcrumbs.
    const nav = screen.getByRole('navigation', { name: 'Main' })
    expect(Array.from(nav.querySelectorAll('a')).map((link) => link.textContent?.trim())).toEqual(
      navItemsFor(ia).map((item) => item.label),
    )
  })

  it('always states which environment the operator is acting in', () => {
    // ⚠️ 🔒 An operator who believes they are in staging while acting on
    // production data is the most expensive mistake this console can enable.
    // Unset means production, so an unconfigured deployment over-warns.
    render(<App />)
    expect(screen.getByRole('status')).toHaveTextContent(/PRODUCTION/)
  })
})
