/**
 * Sign in — the only screen a practitioner sees before they have a session.
 *
 * 🔒 Real authentication against `/public/auth/login` (ADR-A02). One error for
 * every failure mode, exactly as the API returns it (NFR-043): the screen shows
 * the envelope's `message` and `action` rather than inventing its own wording.
 */

import { useState } from 'react'
import type { FormEvent } from 'react'
import {
  Button,
  Card,
  CardBody,
  FormField,
  Input,
  PublicShell,
} from '@wellnesscrm/design-system'
import { ApiError } from '@wellnesscrm/api-client'
import { useAuth } from '../features/auth/AuthProvider'

interface Refusal {
  message: string
  action: string
}

export function Login() {
  const { login } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [refusal, setRefusal] = useState<Refusal | null>(null)

  async function onSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    setBusy(true)
    setRefusal(null)
    try {
      await login(email.trim(), password)
    } catch (error) {
      if (error instanceof ApiError) {
        setRefusal({ message: error.message, action: error.action })
      } else {
        setRefusal({
          message: 'We could not reach WellnessCRM.',
          action: 'Check your connection and try again.',
        })
      }
      setBusy(false)
    }
  }

  return (
    <PublicShell footer="WellnessCRM — practice management for nutrition professionals">
      <Card data-testid="login-card">
        <CardBody>
          <h1>Sign in</h1>
          <p>Welcome back. Sign in to your WellnessCRM workspace.</p>

          <form onSubmit={(event) => void onSubmit(event)} noValidate>
            <FormField label="Email">
              <Input
                type="email"
                autoComplete="username"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                data-testid="login-email-input"
                required
              />
            </FormField>

            <FormField label="Password">
              <Input
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                data-testid="login-password-input"
                required
              />
            </FormField>

            {refusal !== null && (
              <p role="alert" data-testid="login-error">
                {refusal.message} {refusal.action}
              </p>
            )}

            <Button
              type="submit"
              variant="primary"
              fullWidth
              loading={busy}
              data-testid="login-submit-button"
            >
              Sign in
            </Button>
          </form>
        </CardBody>
      </Card>
    </PublicShell>
  )
}
