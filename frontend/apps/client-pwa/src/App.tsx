import { MobileShell } from '@wellnesscrm/design-system'
import type { MobileNavItem } from '@wellnesscrm/design-system'
import { IaProvider, IaRoutes, navItemsFor, useIaLocation } from '@wellnesscrm/ia'
import type { IaNavItem } from '@wellnesscrm/ia'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { ia } from './ia/manifest'
import { NotFound } from './screens/NotFound'
import { PublicEnquiryForm } from './screens/PublicEnquiryForm'

/**
 * Narrow IA nav items to the bottom bar's stricter shape.
 *
 * ⚠️ The throw is unreachable — the manifest is built with `requireNavIcons`,
 * so a missing icon fails at module load with a better message than this one.
 * It exists because `MobileShell` types `icon` as required and the IA types it
 * as optional, and the honest way to bridge that is to check rather than to
 * assert. A cast here would turn a caught mistake into a blank bottom bar.
 */
function toMobileNavItems(items: readonly IaNavItem[]): MobileNavItem[] {
  return items.map((item) => {
    if (item.icon === undefined) {
      throw new Error(`IA nav item "${item.id}" has no icon; MobileShell requires one (NFR-059).`)
    }

    return {
      id: item.id,
      label: item.label,
      href: item.href,
      icon: item.icon,
      ...(item.badge !== undefined ? { badge: item.badge } : {}),
    }
  })
}

function Shell() {
  const { route, activeNavId } = useIaLocation()

  // ⏳ S1 supplies the session's actions. A client sees their own records only
  // (FR-M0-018), and the server is what enforces that (NFR-032).
  const navItems = toMobileNavItems(navItemsFor(ia))

  return (
    <MobileShell
      navItems={navItems}
      title={route?.label ?? 'WellnessCRM'}
      {...(activeNavId !== undefined ? { activeNavId } : {})}
    >
      <IaRoutes fallback={<NotFound />} />
    </MobileShell>
  )
}

export function App() {
  return (
    <BrowserRouter>
      <Routes>
        {/* 🔒 S2 Slice H — the public enquiry form, deliberately *outside*
          * `IaProvider` and `MobileShell`.
          *
          * The IA manifest describes a signed-in client's four destinations
          * (NFR-057), and a prospect is not a signed-in client: they have no
          * account, and no Today, Progress or Messages to reach. Registering
          * this path in the manifest would either add a fifth tab or add a
          * route with `nav: undefined` whose screen still rendered inside a
          * bottom bar of dead ends.
          *
          * ⚠️ It lives in this build rather than a fourth app because Arch §4.1
          * fixes the app list at three, and because the constraint that decides
          * a prospect's experience — mid-range Android, 4G, NFR-002's 2.5s — is
          * the same constraint this build is already the strictest against. The
          * path matches the server-built share URL (`_share_url`, `/enquire/
          * {slug}`), which is what a practitioner pastes into their bio. */}
        <Route path="/enquire/:tenantSlug" element={<PublicEnquiryForm />} />
        <Route
          path="*"
          element={
            <IaProvider ia={ia}>
              <Shell />
            </IaProvider>
          }
        />
      </Routes>
    </BrowserRouter>
  )
}
