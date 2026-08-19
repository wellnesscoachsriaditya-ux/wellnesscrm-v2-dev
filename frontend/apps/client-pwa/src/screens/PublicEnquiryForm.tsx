/**
 * The public enquiry form — S2 Slice H, FR-M2-001…005, US-M2-05.
 *
 * 🔒 **Standalone, and outside the app shell on purpose.** Every other screen in
 * this build renders inside `MobileShell`, which gives a signed-in client a
 * bottom navigation bar. A prospect has no account and nowhere to navigate to;
 * showing them tabs for Today, Progress and Messages would offer four dead ends
 * and imply they are already a client.
 *
 * 🔒 **AC-M2-001 — under 60 seconds on a mid-range Android over 4G.** What that
 * buys, concretely: four fields and a checkbox, no account, no email
 * verification, `inputMode` set so the numeric keypad opens for the phone
 * number, `autoComplete` set so the browser can fill the lot in one tap, and a
 * single submit that answers 202 without a second step.
 *
 * ⚠️ **The acknowledgement replaces the form.** A prospect who submits and still
 * sees their filled-in fields will submit again — which is EC-M2-02's duplicate
 * path, silently handled but pointlessly exercised.
 */

import { useState } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import {
  Button,
  Card,
  CardBody,
  CardHeader,
  Checkbox,
  EmptyState,
  ErrorState,
  FormField,
  Input,
  PublicShell,
  Spinner,
  Textarea,
} from '@wellnesscrm/design-system'
import { EMPTY_DRAFT, usePublicEnquiry, type EnquiryDraft } from '../features/enquiry/usePublicEnquiry'

export function PublicEnquiryForm() {
  const { tenantSlug = '' } = useParams<{ tenantSlug: string }>()
  const [searchParams] = useSearchParams()
  // 🔒 FR-M2-009 — "selected by the prospect or derived from a link parameter".
  // `?source=instagram` on the link a practitioner shares is what makes the
  // channel report answerable (US-M2-04) without asking the prospect anything.
  const source = searchParams.get('source')

  const enquiry = usePublicEnquiry(tenantSlug, source)
  const [draft, setDraft] = useState<EnquiryDraft>(EMPTY_DRAFT)

  function set<K extends keyof EnquiryDraft>(key: K, value: EnquiryDraft[K]) {
    setDraft((current) => ({ ...current, [key]: value }))
  }

  if (enquiry.loading) {
    return (
      <PublicShell>
        <Spinner label="Loading…" />
      </PublicShell>
    )
  }

  // 🔒 EC-M2-07 — one neutral page for an unknown slug, a paused form and a
  // suspended tenant. A prospect learning that a practice exists but has stopped
  // accepting enquiries is a fact about that business we were never asked to
  // publish, so the wording names no cause.
  if (enquiry.unavailable) {
    return (
      <PublicShell>
        <EmptyState
          title="This enquiry form is not available"
          description="Check the link, or contact the practice directly."
        />
      </PublicShell>
    )
  }

  if (enquiry.loadError !== null || enquiry.form === null) {
    return (
      <PublicShell>
        <ErrorState
          title="This form could not be loaded"
          whatToDoNext="Check your connection and try again."
          action={
            <Button onClick={enquiry.retry}>Try again</Button>
          }
        />
      </PublicShell>
    )
  }

  const form = enquiry.form

  // 🔒 AC-M2-003 / EC-M2-02 — the acknowledgement, exactly as the server wrote
  // it. It is identical for a new prospect and a returning one, and nothing here
  // adds a word that could vary.
  if (enquiry.acknowledgement !== null) {
    return (
      <PublicShell footer={form.practice_name}>
        <h1>Thank you</h1>
        <Card>
          <CardBody>
            <p role="status">{enquiry.acknowledgement}</p>
          </CardBody>
        </Card>
      </PublicShell>
    )
  }

  return (
    <PublicShell footer={form.practice_name}>
      {/* 🔒 The practice's name is the only tenant fact API §11.1 publishes, and
        * it is what tells the prospect they followed the right link. It is the
        * page's `h1` — outside the card, because `CardHeader` starts at `h2` by
        * design so a card cannot introduce a heading-level skip (WCAG 1.3.1). */}
      <h1>{form.practice_name}</h1>

      <Card>
        <CardHeader as="h2" title={form.title} />
        <CardBody>
          {form.intro_text !== null && <p>{form.intro_text}</p>}

          <form
            noValidate
            onSubmit={(event) => {
              event.preventDefault()
              void enquiry.submit(draft)
            }}
          >
            {enquiry.formError !== null && <p role="alert">{enquiry.formError}</p>}

            <FormField label="Your name" required error={enquiry.fieldErrors.full_name}>
              <Input
                value={draft.fullName}
                maxLength={120}
                autoComplete="name"
                onChange={(event) => set('fullName', event.target.value)}
              />
            </FormField>

            {/* 🔒 FR-M2-003 — mobile is the contact that matters in this market:
              * it is what WhatsApp is keyed on, and what the practitioner will
              * actually reply to. `inputMode="tel"` opens the numeric keypad,
              * which is most of the difference between a 30-second and a
              * 90-second form on a phone (AC-M2-001). */}
            <FormField
              label="Mobile number"
              required
              hint="So your dietitian can reply on WhatsApp."
              error={enquiry.fieldErrors.mobile}
            >
              <Input
                value={draft.mobile}
                inputMode="tel"
                autoComplete="tel"
                placeholder="98765 43210"
                onChange={(event) => set('mobile', event.target.value)}
              />
            </FormField>

            <FormField label="Email" hint="Optional." error={enquiry.fieldErrors.email}>
              <Input
                type="email"
                value={draft.email}
                autoComplete="email"
                onChange={(event) => set('email', event.target.value)}
              />
            </FormField>

            {/* 🔒 FR-M2-003 — the third required field. Free text rather than a
              * picklist: the goal in the prospect's own words is what the
              * practitioner reads first, and a dropdown would flatten "lose 8kg
              * before my sister's wedding" into "weight loss". */}
            <FormField
              label="What would you like help with?"
              required
              error={enquiry.fieldErrors.primary_goal}
            >
              <Textarea
                value={draft.primaryGoal}
                rows={3}
                maxLength={1000}
                onChange={(event) => set('primaryGoal', event.target.value)}
              />
            </FormField>

            {/* 🔒 FR-M2-008 — the honeypot. Hidden from people, offered to bots.
              *
              * ⚠️ `aria-hidden` and `tabIndex={-1}` together: a screen-reader
              * user must not be told about a field they must not fill, and a
              * keyboard user must not tab into it. `autoComplete="off"` stops a
              * password manager filling it and turning a real prospect into a
              * spam score. It is inline-styled rather than tokenised because it
              * is not a visual decision — `display:none` is the behaviour. */}
            <div style={{ display: 'none' }} aria-hidden="true">
              <label htmlFor="company">Company</label>
              <input
                id="company"
                name="company"
                type="text"
                tabIndex={-1}
                autoComplete="off"
                value={draft.company}
                onChange={(event) => set('company', event.target.value)}
              />
            </div>

            {/* 🔒 FR-M2-004 / NFR-051 — the notice body is displayed, not linked.
              * DPDP requires consent against text the person actually saw, and a
              * link is evidence they were offered it, not that they read it. The
              * version is shown because it is what the ledger records. */}
            <section aria-labelledby="consent-heading">
              <h3 id="consent-heading">{form.consent.title}</h3>
              <p>{form.consent.body}</p>
              <p>Version {form.consent.version}</p>

              <Checkbox
                label="I agree to the privacy notice above."
                checked={draft.consentGranted}
                onChange={(event) => set('consentGranted', event.target.checked)}
              />
              {enquiry.fieldErrors.consent !== undefined && (
                <p role="alert">{enquiry.fieldErrors.consent}</p>
              )}
            </section>

            <Button type="submit" loading={enquiry.submitting}>
              Send enquiry
            </Button>
          </form>
        </CardBody>
      </Card>
    </PublicShell>
  )
}
