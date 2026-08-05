import { AppShell } from '@wellnesscrm/design-system'
import { IaProvider, IaRoutes, navItemsFor, useIaLocation } from '@wellnesscrm/ia'
import { BrowserRouter } from 'react-router-dom'
import { ia } from './ia/manifest'
import { NotFound } from './screens/NotFound'

/**
 * The practitioner frame.
 *
 * 🔒 Both the navigation and the active item come from the IA (NFR-057). There
 * is no hand-written nav array here, which is the whole mechanism: adding a
 * screen to the manifest adds it to this menu, and nothing else has to remember.
 */
function Shell() {
  const { activeNavId } = useIaLocation()

  // ⏳ S1 replaces the predicate with the session's actual actions. Until a
  // session exists there is nothing to filter on, and the API refuses anything
  // the practitioner may not do regardless (NFR-032).
  const navItems = navItemsFor(ia)

  return (
    <AppShell
      brand="WellnessCRM"
      navItems={navItems}
      {...(activeNavId !== undefined ? { activeNavId } : {})}
    >
      <IaRoutes fallback={<NotFound />} />
    </AppShell>
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
