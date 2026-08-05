import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { navItemsFor, validateIa } from '@wellnesscrm/ia'
import { App } from './App'
import { ia } from './ia/manifest'

/** 🔒 S0 Definition of Done — the app mounts and renders what the IA declares. */
describe('client PWA', () => {
  it('declares a valid IA, icons and all', () => {
    expect(validateIa(ia, { requireNavIcons: true, minNavItems: 3, maxNavItems: 5 })).toEqual([])
  })

  it('renders the shell and lands on today’s plan', () => {
    render(<App />)
    // 🔒 M7.3 — a client arriving from a WhatsApp deep link should already be
    // looking at what they came for, not at a menu.
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Today')
  })

  it('builds the bottom bar from the manifest, in declared order', () => {
    render(<App />)
    const nav = screen.getByRole('navigation', { name: 'Main' })
    expect(Array.from(nav.querySelectorAll('a')).map((link) => link.textContent?.trim())).toEqual(
      navItemsFor(ia).map((item) => item.label),
    )
  })

  it('gives every bottom-bar item an icon', () => {
    // ⚠️ NFR-059 — a bottom bar of four text labels is unusable at thumb size.
    render(<App />)
    const links = screen.getByRole('navigation', { name: 'Main' }).querySelectorAll('a')
    for (const link of links) {
      expect(link.querySelector('svg')).not.toBeNull()
    }
  })

  it('marks the landing route as the active tab', () => {
    render(<App />)
    expect(screen.getByRole('link', { current: 'page' })).toHaveTextContent('Today')
  })
})
