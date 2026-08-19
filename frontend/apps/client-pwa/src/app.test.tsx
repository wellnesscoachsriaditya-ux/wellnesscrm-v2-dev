import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
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

/**
 * 🔒 S2 Slice H — the public enquiry form is served by this build but is *not*
 * part of the client portal.
 *
 * The property under test is the composition in `App`, not the screen (which
 * `PublicEnquiryForm.test.tsx` covers). A prospect has no account and no Today,
 * Progress or Messages to reach; rendering the form inside `MobileShell` would
 * hand them a bottom bar of four dead ends and imply they are already a client.
 * That regression would look entirely reasonable in a diff — someone tidying the
 * route into the IA manifest — and nothing else would catch it.
 */
describe('the public enquiry route', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    window.history.pushState({}, '', '/')
  })

  it('renders the form without the client’s navigation', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              form_id: '11111111-1111-1111-1111-111111111111',
              practice_name: 'Priya’s Nutrition Studio',
              title: 'Start your health journey',
              intro_text: null,
              consent: {
                notice_id: '22222222-2222-2222-2222-222222222222',
                title: 'Privacy notice',
                body: 'We will use your details to contact you.',
                version: '2026-08-01',
              },
            }),
            { status: 200, headers: { 'content-type': 'application/json' } },
          ),
        ),
      ),
    )

    window.history.pushState({}, '', '/enquire/demo-practice')
    render(<App />)

    expect(await screen.findByRole('heading', { level: 1 })).toHaveTextContent(
      'Priya’s Nutrition Studio',
    )
    await waitFor(() => {
      expect(screen.queryByRole('navigation', { name: 'Main' })).not.toBeInTheDocument()
    })
  })
})
