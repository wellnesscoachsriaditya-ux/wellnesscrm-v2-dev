import { defineIa } from '@wellnesscrm/ia'
import { placeholder } from '../screens/Placeholder'

/**
 * 🔒 The operator console's information architecture — NFR-057.
 *
 * ⚠️ **Deliberately austere, and deliberately narrow.** Arch §15.4: aggregate
 * views serve most support cases, so the console is built to answer support
 * questions rather than to browse tenant data. Every route here is scoped to
 * that, and 🔒 impersonation stays out entirely (FR-M11-012, Phase 2).
 *
 * ⚠️ No icons: `AdminNavItem` has no icon field. The console is a tool we use,
 * and every hour spent styling it is an hour not spent on the product a
 * practitioner pays for.
 *
 * 🔒 Every action here is audited against a named operator, and `admin` never
 * bypasses authz or audit (Arch §3.3). The `platform.*` actions below are that
 * surface as the menu sees it — the server is what enforces it.
 */
export const ia = defineIa({
  appId: 'operator',

  routes: [
    {
      id: 'tenants',
      path: '/',
      label: 'Tenants',
      permission: 'platform.tenants.read',
      nav: { order: 1 },
      view: placeholder('S11', 'Find a practice, see its account state, and open a support view.'),
    },
    {
      id: 'tenant-detail',
      path: '/tenants/:tenantId',
      label: 'Tenant',
      parent: 'tenants',
      view: placeholder('S11', 'One practice: subscription, entitlements and aggregate usage.'),
    },

    // ─── Food curation — the moat, and mostly our work ────────────────────
    {
      id: 'foods',
      path: '/foods',
      label: 'Food catalogue',
      permission: 'platform.foods.curate',
      nav: { order: 2 },
      view: placeholder('S3', 'The curated Indian food database: entries, portions and their sources.'),
    },
    {
      id: 'food-detail',
      path: '/foods/:foodId',
      label: 'Food',
      parent: 'foods',
      view: placeholder('S3', 'One food: nutrients per portion, provenance and review state.'),
    },

    // ─── FR-M11-004 — failures must be visible, never silently discarded ──
    {
      id: 'delivery',
      path: '/delivery',
      label: 'Delivery log',
      permission: 'platform.delivery.read',
      nav: { order: 3 },
      view: placeholder('S7', 'Message delivery attempts, and the dead-letter queue behind them.'),
    },

    // ─── NFR-086 — dependency status surfaces here rather than failing the
    //     health check, so a WhatsApp outage does not take the app down ────
    {
      id: 'health',
      path: '/health',
      label: 'Platform health',
      permission: 'platform.health.read',
      nav: { order: 4 },
      view: placeholder('S11', 'Job queues, external provider status, and what is currently degraded.'),
    },
  ],
})
