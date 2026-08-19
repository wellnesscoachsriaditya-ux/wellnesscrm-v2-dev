/**
 * Create a client — FR-M1-004, AC-M1-001.
 *
 * ⚠️ **Why this screen exists in Slice B.** Slice A shipped the create and read
 * endpoints with no UI, and Slice E owns the client list. Without something that
 * produces a client and navigates to it, the lifecycle controls this slice
 * builds would be unreachable in the running application. This is the smallest
 * screen that closes that gap; the list replaces it as the main entry point.
 *
 * 🔒 AC-M1-001 — a client is created from name plus one contact method. Every
 * other field is omitted rather than optional-but-present: a practitioner
 * capturing a lead mid-call has a name and a number, and a longer form is one
 * they abandon.
 */

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Button, Card, CardBody, FormField, Input, PageHeader } from '@wellnesscrm/design-system'
import { useIaLocation } from '@wellnesscrm/ia'
import { useCreateClient } from '../features/clients/useCreateClient'

export function ClientCreate() {
  const { breadcrumbs } = useIaLocation()
  const navigate = useNavigate()
  const { submit, busy, fieldErrors, formError } = useCreateClient()

  const [fullName, setFullName] = useState('')
  const [mobile, setMobile] = useState('')
  const [email, setEmail] = useState('')

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault()
    const created = await submit({ fullName, mobile, email })
    // 🔒 Straight to the client on success. NFR-011 budgets creation at three
    // interactions from anywhere, and a confirmation screen in between spends
    // one of them on nothing.
    if (created) navigate(`/clients/${created.id}`)
  }

  return (
    <>
      <PageHeader title="New client" breadcrumbs={breadcrumbs} />
      <Card>
        <CardBody>
          <form onSubmit={(event) => void onSubmit(event)} noValidate>
            {/* 🔒 NFR-063 — a form-level failure states what happened and what
             * to do, and is announced rather than appearing silently. */}
            {formError !== null && <p role="alert">{formError}</p>}

            <FormField label="Full name" required error={fieldErrors.full_name}>
              <Input
                value={fullName}
                maxLength={120}
                autoComplete="name"
                onChange={(event) => setFullName(event.target.value)}
              />
            </FormField>

            <FormField
              label="Mobile"
              hint="Indian mobile number. We store it in international format."
              error={fieldErrors.mobile}
            >
              <Input
                value={mobile}
                inputMode="tel"
                autoComplete="tel"
                placeholder="98765 43210"
                onChange={(event) => setMobile(event.target.value)}
              />
            </FormField>

            {/* 🔒 EC-M1-08 — email alone is a legitimate client record, so the
             * requirement is "one of the two" rather than "mobile". The hint
             * says so, because a form that just rejects the submission leaves
             * the practitioner guessing which field it wanted. */}
            <FormField
              label="Email"
              hint="Either a mobile or an email is enough."
              error={fieldErrors.email}
            >
              <Input
                type="email"
                value={email}
                autoComplete="email"
                onChange={(event) => setEmail(event.target.value)}
              />
            </FormField>

            <Button type="submit" loading={busy}>
              Create client
            </Button>
          </form>
        </CardBody>
      </Card>
    </>
  )
}
