/**
 * Who can see this client — EC-M0-04, EC-M1-04, FR-M0-017.
 *
 * 🔒 Renders and reports (Arch §4.4). Every fact is a prop and every action a
 * callback; R8 fails the build if anything here reaches the API.
 *
 * 🔒 **The management controls appear only for the owner.** `client.manage_access`
 * is owner-only (`modules/clients/actions.py`) while `client.read_access` is not,
 * so a practitioner assigned to this client legitimately sees *who else* can
 * reach them and cannot change it. Showing them a grant form would teach that
 * rule by refusing them — so `canManage` gates the controls, not the list.
 *
 * ⚠️ **Grants are shown as user ids, not names.** `GrantResponse` carries
 * identifiers only, deliberately (NFR-033), and says resolving them is "the
 * caller's job through the team endpoint" — which does not exist yet. Until it
 * does, this panel is honest about being an id list rather than inventing a
 * lookup. Slice E or the team screen is where names arrive.
 */

import { useState } from 'react'
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  ConfirmDialog,
  EmptyState,
  FormField,
  Input,
  Spinner,
} from '@wellnesscrm/design-system'

export interface GrantView {
  userId: string
  grantedByUserId: string
  grantedAt: string
  revokedAt: string | null
  isLive: boolean
}

export interface ClientAccessPanelProps {
  /** The owning practitioner — always has access, and never appears as a grant. */
  ownerUserId: string
  grants: readonly GrantView[]
  /** 🔒 Owner-only. False hides every control but leaves the list visible. */
  canManage: boolean
  error?: string | null
  busy?: boolean
  /**
   * ⚠️ **Distinct from `busy`.** `busy` is a mutation in flight; this is the
   * first read still arriving. Without it the panel states that nobody else has
   * access while the grant list is still loading — a claim about who can see a
   * client, made before we know.
   */
  loading?: boolean
  onGrant: (userId: string) => void
  onRevoke: (userId: string) => void
  onReassign: (userId: string) => void
}

export function ClientAccessPanel({
  ownerUserId,
  grants,
  canManage,
  error = null,
  busy = false,
  loading = false,
  onGrant,
  onRevoke,
  onReassign,
}: ClientAccessPanelProps) {
  const [granteeId, setGranteeId] = useState('')
  const [newOwnerId, setNewOwnerId] = useState('')
  const [revoking, setRevoking] = useState<string | null>(null)
  const [reassigning, setReassigning] = useState<string | null>(null)

  // ⚠️ Live grants only in the count a practitioner reads. A revoked row is
  // history the API returns on request (EC-M1-04); mixing it into "who can see
  // this client" would overstate access.
  const live = grants.filter((grant) => grant.isLive)

  function submitGrant() {
    if (granteeId.trim() === '') return
    onGrant(granteeId.trim())
    setGranteeId('')
  }

  return (
    <Card>
      <CardHeader
        title="Access"
        description="Who in your practice can open this client's record."
      />
      <CardBody>
        {error !== null && <p role="alert">{error}</p>}

        <p>
          Owning practitioner: <Badge tone="info">{ownerUserId}</Badge>
        </p>

        {loading ? (
          <Spinner label="Loading access…" />
        ) : live.length === 0 ? (
          <EmptyState
            title="Not shared with anyone"
            description="Only the owning practitioner can open this client."
          />
        ) : (
          <ul aria-label="Shared access">
            {live.map((grant) => (
              <li key={grant.userId}>
                <span>{grant.userId}</span>{' '}
                <time dateTime={grant.grantedAt}>
                  {new Date(grant.grantedAt).toLocaleDateString()}
                </time>
                {canManage && (
                  <>
                    <Button
                      variant="ghost"
                      disabled={busy}
                      onClick={() => setRevoking(grant.userId)}
                    >
                      Remove access
                    </Button>
                    {/* 🔒 EC-M1-04 — handing the client over. Offered only for
                      * someone who already has access, because reassigning to a
                      * practitioner who has never seen this client is a data
                      * move nobody asked for. */}
                    <Button
                      variant="ghost"
                      disabled={busy}
                      onClick={() => setReassigning(grant.userId)}
                    >
                      Make owner
                    </Button>
                  </>
                )}
              </li>
            ))}
          </ul>
        )}

        {canManage ? (
          <>
            <FormField
              label="Share with a colleague"
              hint="Enter their user id. They get the same access to this client as you."
            >
              <Input
                value={granteeId}
                disabled={busy}
                onChange={(event) => setGranteeId(event.target.value)}
              />
            </FormField>
            <Button onClick={submitGrant} disabled={granteeId.trim() === ''} loading={busy}>
              Give access
            </Button>

            <FormField
              label="Transfer ownership"
              hint="The new owner takes over this client. You keep access only if you are given it."
            >
              <Input
                value={newOwnerId}
                disabled={busy}
                onChange={(event) => setNewOwnerId(event.target.value)}
              />
            </FormField>
            <Button
              variant="secondary"
              disabled={newOwnerId.trim() === ''}
              loading={busy}
              onClick={() => setReassigning(newOwnerId.trim())}
            >
              Transfer
            </Button>
          </>
        ) : (
          // 🔒 FR-M0-017 — names the person to ask, so a practitioner is not left
          // wondering why there is nothing to click.
          <p>Only the account owner can change who has access.</p>
        )}
      </CardBody>

      {/* 🔒 NFR-065. The consequence names what the colleague loses, which is the
        * fact that decides whether the owner meant to do this. */}
      <ConfirmDialog
        open={revoking !== null}
        onCancel={() => setRevoking(null)}
        onConfirm={() => {
          if (revoking !== null) onRevoke(revoking)
          setRevoking(null)
        }}
        title="Remove this colleague's access?"
        consequence="They will no longer be able to open this client. The record of the grant is kept."
        // ⚠️ Deliberately not "Remove access", which is the trigger's label. Two
        // buttons with one name is ambiguous to a screen reader reading the
        // dialog, and to a test trying to click the confirmation.
        confirmLabel="Remove their access"
        tone="danger"
        busy={busy}
      />

      <ConfirmDialog
        open={reassigning !== null}
        onCancel={() => setReassigning(null)}
        onConfirm={() => {
          if (reassigning !== null) onReassign(reassigning)
          setReassigning(null)
          setNewOwnerId('')
        }}
        title="Transfer this client?"
        consequence="They become the owning practitioner. Nothing about the client's history changes."
        confirmLabel="Transfer client"
        tone="primary"
        busy={busy}
      />
    </Card>
  )
}
