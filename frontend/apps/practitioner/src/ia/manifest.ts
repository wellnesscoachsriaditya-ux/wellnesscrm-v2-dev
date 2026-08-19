import { defineIa } from '@wellnesscrm/ia'
import { ClientCreate } from '../screens/ClientCreate'
import { ClientDetail } from '../screens/ClientDetail'
import { ClientList } from '../screens/ClientList'
import { Dashboard } from '../screens/Dashboard'
import { Leads } from '../screens/Leads'
import { Messages } from '../screens/Messages'
import { Plans } from '../screens/Plans'
import { PlanBuilder } from '../screens/PlanBuilder'
import { Appointments, Progress, Reports, Resources, Settings } from '../screens/Extensions'
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
 * One declaration drives navigation, breadcrumbs, the router and menu
 * visibility. The premium Coach shell renders exactly these nav items, in order.
 * Modules without a backend yet render an honest premium "backend dependency"
 * screen (see `Extensions`) rather than reverting to a basic UI or faking data.
 */
export const ia = defineIa({
  appId: 'practitioner',

  routes: [
    {
      id: 'dashboard',
      path: '/',
      label: 'Dashboard',
      nav: { order: 1, icon: dashboardIcon },
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
    { id: 'client-new', path: '/clients/new', label: 'New client', parent: 'clients', view: ClientCreate },
    { id: 'client-detail', path: '/clients/:clientId', label: 'Client', parent: 'clients', view: ClientDetail },

    // ─── M2 Lead Capture & Conversion ─────────────────────────────────────
    {
      id: 'leads',
      path: '/leads',
      label: 'Leads',
      permission: 'enquiry.list',
      nav: { order: 3, icon: leadsIcon },
      view: Leads,
    },

    // ─── M6 Appointments (backend pending) ────────────────────────────────
    {
      id: 'appointments',
      path: '/appointments',
      label: 'Appointments',
      nav: { order: 4, icon: appointmentsIcon },
      view: Appointments,
    },

    // ─── M4 Nutrition Engine / M5 AI Plan Drafting ────────────────────────
    {
      id: 'plans',
      path: '/plans',
      label: 'Nutrition Plans',
      permission: 'plans.read',
      nav: { order: 5, icon: plansIcon },
      view: Plans,
    },
    { id: 'plan-detail', path: '/plans/:planId', label: 'Plan', parent: 'plans', view: PlanBuilder },

    // ─── Progress & Retention (backend pending) ───────────────────────────
    { id: 'progress', path: '/progress', label: 'Progress', nav: { order: 6, icon: dashboardIcon }, view: Progress },

    // ─── M8 Messaging ─────────────────────────────────────────────────────
    {
      id: 'messages',
      path: '/messages',
      label: 'Messages',
      permission: 'message.history_read',
      nav: { order: 7, icon: messagesIcon },
      view: Messages,
    },

    // ─── Reports & Resources (backend pending) ────────────────────────────
    { id: 'reports', path: '/reports', label: 'Reports', nav: { order: 8, icon: dashboardIcon }, view: Reports },
    { id: 'resources', path: '/resources', label: 'Resources', nav: { order: 9, icon: dashboardIcon }, view: Resources },

    // ─── M10 Subscription & Entitlements ──────────────────────────────────
    {
      id: 'settings',
      path: '/settings',
      label: 'Settings',
      permission: 'settings.manage',
      nav: { order: 10, icon: settingsIcon },
      view: Settings,
    },
  ],
})
