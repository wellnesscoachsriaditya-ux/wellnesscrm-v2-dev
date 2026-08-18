import { AppShell, Button, Spinner, ToastProvider } from '@wellnesscrm/design-system'
import { IaProvider, IaRoutes, navItemsFor, useIaLocation } from '@wellnesscrm/ia'
import { BrowserRouter } from 'react-router-dom'
import { ia } from './ia/manifest'
import { NotFound } from './screens/NotFound'
import { Login } from './screens/Login'
import { AuthProvider, useAuth } from './features/auth/AuthProvider'

/**
 * The practitioner frame.
 *
 * 🔒 Both the navigation and the active item come from the IA (NFR-057). There
 * is no hand-written nav array here, which is the whole mechanism: adding a
 * screen to the manifest adds it to this menu, and nothing else has to remember.
 */
function Shell() {
  const { activeNavId } = useIaLocation()
  const { session, logout } = useAuth()

  // ⏳ S1 replaces the predicate with the session's actual actions. Until a
  // session exists there is nothing to filter on, and the API refuses anything
  // the practitioner may not do regardless (NFR-032).
  const navItems = navItemsFor(ia)

  return (
    <AppShell
      brand="WellnessCRM"
      navItems={navItems}
      {...(activeNavId !== undefined ? { activeNavId } : {})}
      userMenu={
        <div data-testid="account-menu">
          {session !== null && (
            <span data-testid="account-role" aria-label="Your role">
              {session.role}
            </span>
          )}
          <Button
            variant="ghost"
            size="sm"
            onClick={() => void logout()}
            data-testid="logout-button"
          >
            Sign out
          </Button>
        </div>
      }
    >
      <IaRoutes fallback={<NotFound />} />
    </AppShell>
  )
}

/**
 * The gate — the session is what decides which of two apps a caller sees.
 *
 * 🔒 A hidden route is a courtesy; this is the real boundary on the client side.
 * While the session is being established the app shows nothing actionable, so a
 * flash of the workspace cannot leak before the token is known.
 */
function Gate() {
  const { status } = useAuth()

  if (status === 'loading') {
    return <Spinner label="Loading your workspace…" />
  }

  if (status === 'anonymous') {
    return <Login />
  }

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
