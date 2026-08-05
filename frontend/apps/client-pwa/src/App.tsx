import { MobileShell } from '@wellnesscrm/design-system'
import type { MobileNavItem } from '@wellnesscrm/design-system'
import { IaProvider, IaRoutes, navItemsFor, useIaLocation } from '@wellnesscrm/ia'
import type { IaNavItem } from '@wellnesscrm/ia'
import { BrowserRouter } from 'react-router-dom'
import { ia } from './ia/manifest'
import { NotFound } from './screens/NotFound'

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
      <IaProvider ia={ia}>
        <Shell />
      </IaProvider>
    </BrowserRouter>
  )
}
