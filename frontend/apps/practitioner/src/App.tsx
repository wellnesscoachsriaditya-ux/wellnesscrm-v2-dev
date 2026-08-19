import './premium/theme.css'
import { Spinner, ToastProvider } from '@wellnesscrm/design-system'
import { IaProvider, IaRoutes, navItemsFor, useIaLocation } from '@wellnesscrm/ia'
import { BrowserRouter, useNavigate } from 'react-router-dom'
import { ia } from './ia/manifest'
import { NotFound } from './screens/NotFound'
import { Login } from './screens/Login'
import { AuthProvider, useAuth } from './features/auth/AuthProvider'
import { PremiumShell } from './premium/AppShell'

/**
 * The premium Coach frame.
 *
 * 🔒 Navigation and the active item come from the IA (NFR-057) — the shell
 * renders exactly `navItemsFor(ia)` as `<a>` links in declared order, with
 * `aria-current` on the active one. No hand-written nav array.
 */
function Shell() {
  const { activeNavId } = useIaLocation()
  const { session, logout } = useAuth()
  const navigate = useNavigate()
  const navItems = navItemsFor(ia)

  return (
    <PremiumShell
      navItems={navItems}
      {...(activeNavId !== undefined ? { activeNavId } : {})}
      user={{ name: 'Coach', practice: session?.role ?? 'Practitioner', role: session?.role ?? '' }}
      onSignOut={() => void logout()}
      onQuickAdd={() => navigate('/clients/new')}
    >
      <IaRoutes fallback={<NotFound />} />
    </PremiumShell>
  )
}

function Gate() {
  const { status } = useAuth()
  if (status === 'loading') return <Spinner label="Loading your workspace…" />
  if (status === 'anonymous') return <Login />
  return (
    <BrowserRouter>
      <IaProvider ia={ia}>
        <Shell />
      </IaProvider>
    </BrowserRouter>
  )
}

export function App() {
  return (
    <ToastProvider>
      <AuthProvider>
        <Gate />
      </AuthProvider>
    </ToastProvider>
  )
}
