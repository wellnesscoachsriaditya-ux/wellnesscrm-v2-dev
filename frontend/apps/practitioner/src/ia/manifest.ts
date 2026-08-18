import { defineIa } from '@wellnesscrm/ia'
import { ClientCreate } from '../screens/ClientCreate'
import { ClientDetail } from '../screens/ClientDetail'
import { ClientList } from '../screens/ClientList'
import { Dashboard } from '../screens/Dashboard'
import { Leads } from '../screens/Leads'
import { Messages } from '../screens/Messages'
import { Plans } from '../screens/Plans'
import { PlanBuilder } from '../screens/PlanBuilder'
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
      // ⚠️ **No `permission`.** Every practitioner has a landing screen, and the
      // panels on it are individually scoped by the endpoints they read — a
      // practitioner sees their own enquiries and clients, an owner sees the
      // tenant (AC-M1-006). Gating the route itself would hide the whole app
      // from anyone whose role lacked one panel's action.
      view: Dashboard,
    },

    // ─── M1 Client Record ─────────────────────────────────────────────────
    {
      id: 'clients',
      path: '/clients',
      label: 'Clients',
      permission: 'clients.read',
      nav: { order: 2, icon: clientsIcon },
      view: ClientList,
    },
    {
      id: 'client-new',
      path: '/clients/new',
      label: 'New client',
      parent: 'clients',
      view: ClientCreate,
    },
    {
      id: 'client-detail',
      path: '/clients/:clientId',
      label: 'Client',
      parent: 'clients',
      view: ClientDetail,
    },

    // ─── M2 Lead Capture & Conversion ─────────────────────────────────────
    {
      id: 'leads',
      path: '/leads',
      label: 'Leads',
      // 🔒 Mirrors `enquiry.list` — the backend action the screen's reads
      // declare. The menu is a courtesy (NFR-032); the API is what refuses.
      permission: 'enquiry.list',
      nav: { order: 3, icon: leadsIcon },
      view: Leads,
    },

    // ─── M4 Nutrition Engine / M5 AI Plan Drafting ────────────────────────
    {
      id: 'plans',
      path: '/plans',
      label: 'Plans',
      permission: 'plans.read',
      nav: { order: 4, icon: plansIcon },
      view: Plans,
    },
    {
      id: 'plan-detail',
      path: '/plans/:planId',
      label: 'Plan',
      parent: 'plans',
      view: PlanBuilder,
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
      // 🔒 The action the backend registers for reading the delivery log
      // (S5). The menu gate mirrors the API's authorization rather than
      // inventing its own name — a hidden item is a courtesy, the API is what
      // refuses.
      permission: 'message.history_read',
      nav: { order: 6, icon: messagesIcon },
      // ⚠️ Not an inbox. MVP sends and records; a client's reply reaches the
      // practitioner's own WhatsApp (EC-M8-07) and the two-way inbox is
      // Phase 2 (FR-M8-030).
      view: Messages,
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
