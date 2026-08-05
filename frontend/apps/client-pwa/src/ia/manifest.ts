import { defineIa } from '@wellnesscrm/ia'
import { placeholder } from '../screens/Placeholder'
import { meIcon, messagesIcon, progressIcon, todayIcon } from './icons'

/**
 * 🔒 The client portal's information architecture — NFR-057.
 *
 * ⚠️ **Four destinations, and that is a design constraint rather than a
 * starting point.** M7.3's 60-second rule says every client task must be
 * completable in under a minute on a phone, and V1's recorded failure was
 * information overload. `maxNavItems` holds the line: a fifth tab has to
 * displace an existing one, which is a decision someone has to make on purpose.
 *
 * 🔒 Today's plan is the landing route (`/`) because M7.3 makes it the default
 * view — a client arriving from a WhatsApp deep link should already be looking
 * at what they came for.
 */
export const ia = defineIa(
  {
    appId: 'client-pwa',

    routes: [
      {
        id: 'today',
        path: '/',
        label: 'Today',
        nav: { order: 1, icon: todayIcon },
        view: placeholder('S6', 'Today’s meals, and one tap to mark each as followed.'),
      },
      {
        id: 'progress',
        path: '/progress',
        label: 'Progress',
        nav: { order: 2, icon: progressIcon },
        view: placeholder('S6', 'Weight and measurements over time, and how the last few weeks have gone.'),
      },
      {
        id: 'messages',
        path: '/messages',
        label: 'Messages',
        nav: { order: 3, icon: messagesIcon },
        view: placeholder('S7', 'Messages from your dietitian, and a way to send a photo or lab report.'),
      },
      {
        id: 'me',
        path: '/me',
        label: 'Me',
        nav: { order: 4, icon: meIcon },
        view: placeholder('S6', 'Your details, your plan history, and how to reach your dietitian.'),
      },
    ],
  },
  // 🔒 MobileShell requires an icon per item (NFR-059), and documents three to
  // five items. Declared here so a violation fails at module load rather than
  // as a type error at whichever line maps the items.
  { requireNavIcons: true, minNavItems: 3, maxNavItems: 5 },
)
