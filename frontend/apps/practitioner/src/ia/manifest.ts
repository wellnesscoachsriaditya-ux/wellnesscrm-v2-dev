import { defineIa } from '@wellnesscrm/ia'
import { placeholder } from '../screens/Placeholder'
import {
  appointmentsIcon,
  clientsIcon,
  dashboardIcon,
  leadsIcon,
  messagesIcon,
  plansIcon,
  settingsIcon,
} from './icons'

/**
 * 🔒 The practitioner application's information architecture — NFR-057.
 *
 * One declaration. Navigation, breadcrumbs, the router and menu visibility are
 * all read from it, so a screen cannot exist in one and be missing from another.
 *
 * ⚠️ **The screens are placeholders; the structure is not.** Each entry names
 * the module (PRD M1–M10) and the sprint that fills it in. Declaring the shape
 * now is what lets S2 add a screen by replacing a `view`, rather than by
 * inventing navigation for it at the point of least attention.
 *
 * ⚠️ 🔒 **`permission` gates the menu, not the data** (NFR-032 / ADR-05). These
 * action names mirror the backend's authorization actions, which S1 introduces;
 * until then the `can` predicate permits everything and every route is
 * reachable. The API is what must refuse — a hidden menu item is a courtesy.
 */
export const ia = defineIa({
  appId: 'practitioner',

  routes: [
    {
      id: 'dashboard',
      path: '/',
      label: 'Dashboard',
      nav: { order: 1, icon: dashboardIcon },
      view: placeholder('S2', 'Today’s appointments, clients needing attention, and what to do next.'),
    },

    // ─── M1 Client Record ─────────────────────────────────────────────────
    {
      id: 'clients',
      path: '/clients',
      label: 'Clients',
      permission: 'clients.read',
      nav: { order: 2, icon: clientsIcon },
      view: placeholder('S2', 'Every client and lead in one list, filtered by lifecycle stage.'),
    },
    {
      id: 'client-detail',
      path: '/clients/:clientId',
      label: 'Client',
      parent: 'clients',
      view: placeholder('S2', 'One client: profile, timeline, measurements, plans and messages.'),
    },

    // ─── M2 Lead Capture & Conversion ─────────────────────────────────────
    {
      id: 'leads',
      path: '/leads',
      label: 'Leads',
      permission: 'clients.read',
      nav: { order: 3, icon: leadsIcon },
      view: placeholder('S5', 'Enquiries from the public form, with their source attribution.'),
    },

    // ─── M4 Nutrition Engine / M5 AI Plan Drafting ────────────────────────
    {
      id: 'plans',
      path: '/plans',
      label: 'Plans',
      permission: 'plans.read',
      nav: { order: 4, icon: plansIcon },
      view: placeholder('S4', 'Nutrition plans and the templates they are built from.'),
    },
    {
      id: 'plan-detail',
      path: '/plans/:planId',
      label: 'Plan',
      parent: 'plans',
      view: placeholder('S4', 'One plan: meals, portions, totals against the client’s budget.'),
    },

    // ─── M6 Appointments ──────────────────────────────────────────────────
    {
      id: 'appointments',
      path: '/appointments',
      label: 'Appointments',
      permission: 'appointments.read',
      nav: { order: 5, icon: appointmentsIcon },
      view: placeholder('S8', 'The schedule, and the reschedule history behind it.'),
    },

    // ─── M8 Messaging & Scheduling Engine ─────────────────────────────────
    {
      id: 'messages',
      path: '/messages',
      label: 'Messages',
      permission: 'messages.read',
      nav: { order: 6, icon: messagesIcon },
      view: placeholder('S7', 'WhatsApp conversations and the scheduled messages queued behind them.'),
    },

    // ─── M10 Subscription & Entitlements ──────────────────────────────────
    {
      id: 'settings',
      path: '/settings',
      label: 'Settings',
      permission: 'settings.manage',
      nav: { order: 7, icon: settingsIcon },
      view: placeholder('S9', 'Practice details, subscription, entitlements and team access.'),
    },
  ],
})
