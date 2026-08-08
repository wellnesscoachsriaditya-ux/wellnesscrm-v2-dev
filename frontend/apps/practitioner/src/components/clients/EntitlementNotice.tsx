/**
 * The plan-limit refusal — FR-M1-002, FR-M0-045.
 *
 * 🔒 **Everything shown here comes off the error envelope**, which is why
 * FR-M0-045 requires the API to put the limit, the usage, the plan and the
 * upgrade path in `details`: the UI must be able to explain the refusal without
 * a second request. Nothing in this file recomputes or guesses a limit.
 *
 * ⚠️ Rendered inline on the screen rather than as a toast. A toast disappears,
 * and this is a message the practitioner has to read, understand and act on —
 * exactly the case the toast primitive's own docstring rules itself out of.
 */

import { Button, Card, CardBody, CardHeader } from '@wellnesscrm/design-system'

export interface EntitlementNoticeProps {
  /** Human sentence from the API — already written for end users (NFR-063). */
  message: string
  /** What to do next. Always present on our envelope. */
  action: string
  limit?: number | undefined
  used?: number | undefined
  planCode?: string | undefined
  /** The next tier up, when one exists. Absent at the top of the ladder. */
  upgradeTo?: string | undefined
  onDismiss: () => void
}

export function EntitlementNotice({
  message,
  action,
  limit,
  used,
  planCode,
  upgradeTo,
  onDismiss,
}: EntitlementNoticeProps) {
  return (
    <Card>
      {/* `role="alert"` so the refusal is announced rather than silently
       * appearing below the control that was just used. */}
      <div role="alert">
        <CardHeader title="Your plan is full" />
        <CardBody>
          <p>{message}</p>
          <p>{action}</p>

          {limit !== undefined && used !== undefined && (
            <p data-testid="entitlement-usage">
              Using {used} of {limit} active clients
              {planCode ? ` on the ${planCode} plan` : ''}.
            </p>
          )}

          {/* ⚠️ Absent at the top of the ladder, deliberately. The API returns
           * no `upgrade_to` for the highest tier or an unrecognised plan, and a
           * button offering an upgrade that does not exist is worse than none. */}
          {upgradeTo !== undefined && (
            <p data-testid="entitlement-upgrade">Next tier up: {upgradeTo}.</p>
          )}

          <Button variant="secondary" onClick={onDismiss}>
            Dismiss
          </Button>
        </CardBody>
      </div>
    </Card>
  )
}
