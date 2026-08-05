import { AdminShell } from '@wellnesscrm/design-system'
import { IaProvider, IaRoutes, navItemsFor, useIaLocation } from '@wellnesscrm/ia'
import { BrowserRouter } from 'react-router-dom'
import { ia } from './ia/manifest'
import { NotFound } from './screens/NotFound'

/**
 * ⚠️ 🔒 Falls back to `production` when unset, not to `development`.
 *
 * A misconfigured deployment should over-warn rather than under-warn: an
 * operator wrongly told they are on production is cautious, and an operator
 * wrongly told they are on staging acts on real tenant data.
 */
function environmentName(): string {
  const declared = import.meta.env.VITE_ENVIRONMENT
  return declared === undefined || declared.trim() === '' ? 'production' : declared
}

function Shell() {
  const { activeNavId } = useIaLocation()
  const navItems = navItemsFor(ia)

  return (
    <AdminShell
      navItems={navItems}
      environment={environmentName()}
      // ⏳ S1 puts the signed-in operator here. Until then the console shows
      // that no one is identified rather than a plausible-looking name — every
      // action here is audited against a named person (Arch §3.3), and a
      // placeholder that reads like a real one is the wrong thing to get used to.
      operatorLabel="No session — S1"
      {...(activeNavId !== undefined ? { activeNavId } : {})}
    >
      <IaRoutes fallback={<NotFound />} />
    </AdminShell>
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
