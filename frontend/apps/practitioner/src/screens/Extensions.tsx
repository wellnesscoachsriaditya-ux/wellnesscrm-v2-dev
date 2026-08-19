/**
 * Premium extension screens for Coach modules whose backend is not yet built.
 *
 * 🔒 REAL-DATA rule: these render an honest, on-brand "not yet available" state
 * that names the backend dependency, rather than fabricating metrics. When the
 * endpoint lands, the screen body is replaced — the shell, IA and navigation are
 * already wired (the same pattern as `Placeholder`).
 */
import { useIaLocation } from '@wellnesscrm/ia'
import { PremiumPlaceholder } from '../premium/ui'
import styles from './extensions.module.css'

function Screen({ children }: { children: React.ReactNode }) {
  const { route } = useIaLocation()
  return (
    <div className={styles.wrap}>
      <h1 className={styles.srOnly}>{route?.label ?? 'WellnessCRM'}</h1>
      {children}
    </div>
  )
}

export function Appointments() {
  return (
    <Screen>
      <PremiumPlaceholder
        icon="appointments"
        accent="blue"
        title="Appointments"
        description="A premium scheduling workspace — today's timeline, upcoming consultations, reschedule history and video links — designed in the WellnessCRM visual language."
        dependency="M6 Appointments module (scheduling endpoints) is not implemented yet."
      />
    </Screen>
  )
}

export function Progress() {
  return (
    <Screen>
      <PremiumPlaceholder
        icon="progress"
        accent="teal"
        title="Progress & Retention"
        description="Practice-wide progress, adherence and retention insights. Per-client measurements already exist in Client 360; a tenant-level aggregate read is required for this dashboard."
        dependency="No practice-level progress/adherence aggregate endpoint yet."
      />
    </Screen>
  )
}

export function Reports() {
  return (
    <Screen>
      <PremiumPlaceholder
        icon="reports"
        accent="violet"
        title="Reports"
        description="Exportable practice reports and analytics, presented with the same premium cards and charts as the dashboard."
        dependency="Reporting/analytics endpoints are not implemented yet."
      />
    </Screen>
  )
}

export function Resources() {
  return (
    <Screen>
      <PremiumPlaceholder
        icon="resources"
        accent="amber"
        title="Resources"
        description="A shared library of guides, templates and client-facing material for your practice."
        dependency="Resource library endpoints are not implemented yet."
      />
    </Screen>
  )
}

export function Settings() {
  return (
    <Screen>
      <PremiumPlaceholder
        icon="settings"
        accent="ink"
        title="Practice Settings"
        description="Practice details, subscription, entitlements, team access and usage — in the WellnessCRM visual system."
        dependency="Settings/usage read endpoints (M10) are not implemented yet."
      />
    </Screen>
  )
}
