"""Messaging engine — one scheduler, one dispatch path, one delivery log.

Revision ID: 0021_messaging
Revises: 0020_plan_authoring
Create Date: 2026-08-14

🔒 **M8.3 — one engine, not three.** DB §11. Five tables serve every outbound
message in the product: appointment reminders, check-in nudges, lead follow-ups,
plan deliveries and magic links are all rows in the same queue, dispatched by the
same code, logged in the same table. The alternative the core loop invites —
three schedulers, three template stores, three retry paths — is V1's "business
logic duplicated across multiple screens" failure in its most expensive form.

═══════════════════════════════════════════════════════════════════════════
1. `transport_type` gains `logged`
═══════════════════════════════════════════════════════════════════════════

🔒 **This is what makes S5 shippable without Meta Business Verification.** The
implementation plan is explicit: "If Meta verification has not landed, this
sprint still ships — email and logged-only transports work, and the WhatsApp
adapter is swapped in when approval arrives."

`logged` is a *real* transport that records an attempt and sends nothing. It is
not a mock and not a test double: it runs in local and staging deployments, and
`message_dispatches` records it as the transport used, so nobody can later read
the delivery log and believe a WhatsApp message was delivered.

⚠️ `ALTER TYPE … ADD VALUE` runs inside this transaction (PostgreSQL 12+), but
the new value **cannot be used until the transaction commits**. That is why no
seeded template has `default_transport = 'logged'` — the substitution happens at
dispatch time, in code, from configuration.

═══════════════════════════════════════════════════════════════════════════
2. `message_templates` is platform-owned and has no `tenant_id`
═══════════════════════════════════════════════════════════════════════════

DB §11.1, verbatim: "Platform-owned, no `tenant_id` at MVP. Practitioner-editable
wording is Phase 2 (FR-M8-029)."

The consequence is a table with no RLS discriminator, so it gets the same
treatment `nutrients` and `measure_units` got in 0017: RLS enabled with a
read-all SELECT policy, and `app_user` granted SELECT only. A practitioner
*controls* messages through `notification_preferences` (FR-M8-027), never by
editing the template.

🔒 `provider_template_status` is **per template** (EC-M8-03). One revoked
WhatsApp template pauses its own message type and nothing else. Without
per-template state a single rejection looks like a total outage — and the
practitioner is told the wrong thing about their own practice.

⚠️ `app_user` holds **no UPDATE grant** on this table, including on
`provider_template_status`. Acting on a Meta revocation is operator SQL until
S12 builds `GET/POST /admin/message-templates` (API §15.1). That is deliberate:
0020's docstring argues a privilege must never land ahead of the code that needs
it, and the engine already honours whatever the column says.

═══════════════════════════════════════════════════════════════════════════
3. Idempotency is a constraint, not a code path
═══════════════════════════════════════════════════════════════════════════

🔒 EC-M8-06 — "a retry must never deliver twice." DB §11.4 requires the unique
constraint to be the enforcement: `uq_scheduled_messages__idempotency`. A
`SELECT` then `INSERT` loses the race this exists to win, and the race is not
hypothetical — a retried HTTP request and a worker replay arrive together by
construction.

═══════════════════════════════════════════════════════════════════════════
4. `message_dispatches` is append-only, with a column-level exception
═══════════════════════════════════════════════════════════════════════════

🔒 DB §11.3 — "Append-only. Status transitions from provider webhooks update the
row's status fields only; the attempt record itself is never rewritten or
deleted."

A table-level grant cannot express that. So `app_user` gets SELECT and INSERT
plus a **column-level UPDATE** on exactly the fields a delivery receipt moves:
status, provider ids, failure detail, the delivery timestamps and cost. The
attempt's identity — which template, which version, which transport, which
address, which attempt number — is unreachable to the application. The same
mechanism 0015 used for `enquiry_submissions` and 0016 for
`assessment_definitions`.

⚠️ `ops/db/002_verify_grants.sql` gains this table in the same commit. Its check
uses `has_table_privilege`, which is table-level only, so the column grant above
does not trip it — and the DELETE half of the check is exactly what must never
come back.

═══════════════════════════════════════════════════════════════════════════
5. 🔒 The webhook's tenant lookup, and why it is not an RLS hole
═══════════════════════════════════════════════════════════════════════════

A provider's delivery receipt arrives on an unauthenticated, tenant-less
connection carrying one useful fact: the provider's own message id. Finding the
dispatch it refers to means reading `message_dispatches` before any tenant is
known, and Pattern A RLS correctly refuses that.

`message_dispatches__provider_lookup` is the narrowest resolution I could
construct:

* **SELECT only.** No webhook writes through it — the route resolves a tenant
  and enqueues a job, and the job applies the status change under that tenant's
  own scope, through the ordinary isolation policy.
* **Only for a session with no tenant.** `current_tenant_id() IS NULL` means an
  authenticated practitioner session can never qualify, so this widens nothing
  for any tenant-scoped caller. Permissive policies are OR-ed, and that is
  precisely why the tenant condition has to be inside this one.
* **Only for a row whose provider id the caller already has**, supplied through
  `app.provider_message_id` and compared for equality. There is no wildcard and
  no enumeration: the caller must already possess an opaque provider-issued
  identifier for the single row it can see.

⚠️ The alternative designs were worse. A `SECURITY DEFINER` function does not
work here at all — `FORCE ROW LEVEL SECURITY` applies policies to the table
owner too, so a definer function owned by `app_migrator` would read nothing. A
separate provider-id → tenant index table would be a second store of the same
fact, with its own way to drift. Dropping RLS on the delivery log was never an
option.

═══════════════════════════════════════════════════════════════════════════
6. Seed — the eight MVP message types
═══════════════════════════════════════════════════════════════════════════

🔒 FR-M8-013…020, exactly eight, no more. AC-M8-008 requires that adding a
message type need only a template and a schedule; these are seeded as data for
the same reason `nutrition_core` is — a ninth type is an INSERT, not a release.

⚠️ Seeded **before** RLS is enabled, for the reason 0016 records at length: the
migration runs as `app_migrator`, `FORCE` applies policies to the owner, and a
read-all policy offers no `WITH CHECK` for an INSERT. Seeding first is the fix,
and the refusal afterwards is the guarantee — no application path can create a
platform-owned template.

⚠️ Every seeded template carries `provider_template_status = 'pending'`, which
is the truth: Meta Business Verification has not been granted. The dispatch
engine only applies that gate to the WhatsApp transport, so email and logged
delivery are unaffected — see `modules.messaging.dispatch` for why that is a
correctness decision rather than a convenience.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_messaging"
down_revision: str | None = "0020_plan_authoring"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ─── Enums (DB §11) ──────────────────────────────────────────────────────

#: DB §11.1 — what kind of message this is, for policy and reporting.
_MESSAGE_CATEGORY = ("transactional", "reminder", "nudge", "notification")

#: 🔒 EC-M8-03 — per template, so one revocation pauses one message type.
_PROVIDER_TEMPLATE_STATUS = ("pending", "approved", "rejected", "paused")

#: DB §11.2. 🔒 `suppressed` and `expired` are terminal *and retained*
#: (AC-M8-004) — the reason a message did not arrive is the answer to the
#: support question that always follows.
_SCHEDULED_STATE = ("pending", "dispatched", "suppressed", "expired", "cancelled")

#: 🔒 DB §11.5. Seven values for what the spec calls "the six rules":
#: `client_unsubscribed` and `consent_withdrawn` are distinct facts, and
#: conflating them would either over-suppress or lose a lawful-basis record.
#: See `kernel.messaging.SuppressionReason`, which says the same thing in Python.
_SUPPRESSION_REASON = (
    "client_stage_inactive",
    "consent_withdrawn",
    "tenant_suspended",
    "quota_exceeded",
    "frequency_capped",
    "template_paused",
    "client_unsubscribed",
)

#: DB §11.3 — the provider-reported lifecycle of one attempt.
_DISPATCH_STATUS = ("queued", "sent", "delivered", "read", "failed", "rejected")

#: DB §11.7, FR-M8-022.
_CHECKIN_FREQUENCY = ("weekly", "fortnightly", "monthly")

#: Pattern A RLS. 🔒 `message_templates` is absent — it has no `tenant_id` and
#: is handled separately below.
_TENANT_SCOPED: tuple[str, ...] = (
    "scheduled_messages",
    "message_dispatches",
    "checkin_schedules",
    "notification_preferences",
)


def _enum(name: str) -> postgresql.ENUM:
    """Reference an already-created enum without re-emitting its DDL. See 0002."""
    return postgresql.ENUM(name=name, create_type=False)


# ⚠️ Guarded on role existence, matching every migration since 0001.
_APPLY_GRANTS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        -- 🔒 Platform-owned reference data (DB §11.1). The application reads
        -- templates and never writes them: wording is Phase 2 (FR-M8-029) and
        -- provider approval state is operator work until S12's admin API.
        REVOKE INSERT, UPDATE, DELETE ON TABLE message_templates FROM app_user;
        GRANT SELECT ON TABLE message_templates TO app_user;

        -- The queue of intent. UPDATE is granted broadly — a row moves through
        -- pending → dispatched/suppressed/expired/cancelled and records a
        -- quiet-hours deferral on the way.
        --
        -- 🔒 No DELETE. AC-M8-004 requires a suppressed message to be retained
        -- *with its reason*; a row that can be deleted is a reason that can be
        -- destroyed, and "why didn't my client get it?" is the support question
        -- this table exists to answer.
        REVOKE DELETE ON TABLE scheduled_messages FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE scheduled_messages TO app_user;

        -- 🔒 DB §11.3 — the immutable delivery log, with the one exception the
        -- design requires. See section 4 of this revision's docstring.
        REVOKE UPDATE, DELETE ON TABLE message_dispatches FROM app_user;
        GRANT SELECT, INSERT ON TABLE message_dispatches TO app_user;
        GRANT UPDATE (
            status,
            provider_message_id,
            failure_code,
            failure_reason,
            sent_at,
            delivered_at,
            read_at,
            cost_amount,
            updated_at
        ) ON TABLE message_dispatches TO app_user;

        -- 🔒 FR-M8-024 — a check-in is *paused*, never deleted, and pausing must
        -- not change the client's lifecycle stage. Removing the row would lose
        -- the cadence the practitioner configured.
        REVOKE DELETE ON TABLE checkin_schedules FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE checkin_schedules TO app_user;

        -- 🔒 A preference is the behavioural half of an unsubscribe (US-M8-06);
        -- the consent ledger is the legal half. Deleting the row would silently
        -- re-enable messages to someone who opted out, so it is updated instead.
        REVOKE DELETE ON TABLE notification_preferences FROM app_user;
        GRANT SELECT, INSERT, UPDATE ON TABLE notification_preferences TO app_user;
    END IF;
END
$$;
"""

_RESTORE_DEFAULT_PRIVILEGES = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE message_templates TO app_user;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE scheduled_messages TO app_user;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE message_dispatches TO app_user;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE checkin_schedules TO app_user;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE notification_preferences TO app_user;
    END IF;
END
$$;
"""

#: Ordinary Pattern A (DB §17.1).
_TENANT_POLICY = """
CREATE POLICY {table}__tenant_isolation ON {table}
    USING (tenant_id = current_tenant_id())
    WITH CHECK (tenant_id = current_tenant_id());
"""

#: 🔒 Platform reference data, readable by every tenant — the same shape as
#: `nutrients__read_all` in 0017. SELECT only: there is no write policy, so even
#: if a grant were restored by mistake, no application connection could insert.
_TEMPLATE_POLICY = """
CREATE POLICY message_templates__read_all ON message_templates
    FOR SELECT TO app_user
    USING (true);
"""

#: 🔒 See section 5 of the docstring. The two conditions are load-bearing and
#: neither is redundant: without the first, a tenant could read another tenant's
#: delivery row given a provider id; without the second, a tenant-less
#: connection could read the whole table.
_PROVIDER_LOOKUP_POLICY = """
CREATE POLICY message_dispatches__provider_lookup ON message_dispatches
    FOR SELECT TO app_user
    USING (
        current_tenant_id() IS NULL
        AND provider_message_id IS NOT NULL
        AND provider_message_id = NULLIF(current_setting('app.provider_message_id', true), '')
    );
"""


# ─── The eight MVP message types (FR-M8-013…020) ─────────────────────────
#
# 🔒 Exactly the eight the PRD names, in its order. No ninth "because it seems
# useful": every message we send is attached to the practitioner's professional
# reputation (the rationale under FR-M8-026/027), and a type nobody asked for is
# one they have to discover and turn off.
#
# ⚠️ `staleness_tolerance_minutes` is a judgement per type, and the judgement is
# always the same question: *is this message worse than nothing if it arrives
# late?* A plan is still wanted an hour late (NULL — never stale). A reminder for
# an appointment that has already happened is worse than silence.
#
# ⚠️ `is_essential` is true for one template only. DB §11.6 scopes the exemption
# to magic links and password resets, and widening it would let a busy tenant's
# nudges consume the quota that a client's portal access depends on.
_SEED_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "code": "plan_delivered",
        "category": "transactional",
        "is_essential": False,
        "priority": 10,
        # 🔒 Never stale — the kernel's own example. A plan a practitioner
        # approved is still wanted late; expiring it would discard work.
        "staleness_tolerance_minutes": None,
        "variables": {
            "client_name": {"type": "string", "required": True},
            "practitioner_name": {"type": "string", "required": True},
            "plan_url": {"type": "url", "required": True},
        },
        "body_template": (
            "Hi {client_name}, your new nutrition plan from {practitioner_name} is ready. "
            "Open it here: {plan_url}"
        ),
        "default_transport": "whatsapp",
        "provider_template_name": "plan_delivered_v1",
        # 🔒 FR-M8-027 permits disabling any *non-essential* type, but this one
        # is the product's core loop (FR-M8-013, AC-M8-001). Turning it off
        # would leave a practitioner approving plans no client ever receives —
        # so it is not offered as a toggle.
        "is_practitioner_disableable": False,
    },
    {
        "code": "appointment_confirmed",
        "category": "transactional",
        "is_essential": False,
        "priority": 20,
        # A confirmation that arrives a day late is confusing rather than
        # useful; by then the reminder is the relevant message.
        "staleness_tolerance_minutes": 1440,
        "variables": {
            "client_name": {"type": "string", "required": True},
            "practitioner_name": {"type": "string", "required": True},
            "appointment_at": {"type": "datetime", "required": True},
        },
        "body_template": (
            "Hi {client_name}, your appointment with {practitioner_name} is confirmed "
            "for {appointment_at}."
        ),
        "default_transport": "whatsapp",
        "provider_template_name": "appointment_confirmed_v1",
        "is_practitioner_disableable": False,
    },
    {
        "code": "appointment_reminder",
        "category": "reminder",
        "is_essential": False,
        "priority": 20,
        # 🔒 Two hours. A reminder delivered after the appointment has started
        # is actively worse than no reminder — it tells the client the practice
        # is not paying attention.
        "staleness_tolerance_minutes": 120,
        "variables": {
            "client_name": {"type": "string", "required": True},
            "appointment_at": {"type": "datetime", "required": True},
        },
        "body_template": "Hi {client_name}, a reminder about your appointment at {appointment_at}.",
        "default_transport": "whatsapp",
        "provider_template_name": "appointment_reminder_v1",
        "is_practitioner_disableable": True,
    },
    {
        "code": "checkin_nudge",
        "category": "nudge",
        "is_essential": False,
        "priority": 50,
        # 🔒 EC-M8-05 names this number: "a check-in more than 24 h stale is
        # dropped and logged rather than sent late".
        "staleness_tolerance_minutes": 1440,
        "variables": {
            "client_name": {"type": "string", "required": True},
            "portal_url": {"type": "url", "required": True},
        },
        "body_template": (
            "Hi {client_name}, time for your check-in. Log your weight and how the week "
            "went here: {portal_url}"
        ),
        "default_transport": "whatsapp",
        "provider_template_name": "checkin_nudge_v1",
        "is_practitioner_disableable": True,
    },
    {
        "code": "assessment_invitation",
        "category": "transactional",
        "is_essential": False,
        "priority": 30,
        # An assessment invitation keeps its value: the practitioner is waiting
        # on the answers whenever they arrive.
        "staleness_tolerance_minutes": None,
        "variables": {
            "client_name": {"type": "string", "required": True},
            "practitioner_name": {"type": "string", "required": True},
            "assessment_url": {"type": "url", "required": True},
        },
        "body_template": (
            "Hi {client_name}, {practitioner_name} has sent you a short assessment to "
            "complete before your consultation: {assessment_url}"
        ),
        "default_transport": "whatsapp",
        "provider_template_name": "assessment_invitation_v1",
        "is_practitioner_disableable": True,
    },
    {
        "code": "lead_acknowledgement",
        "category": "transactional",
        "is_essential": False,
        "priority": 30,
        "staleness_tolerance_minutes": 1440,
        "variables": {
            "lead_name": {"type": "string", "required": True},
            "practice_name": {"type": "string", "required": True},
        },
        "body_template": (
            "Hi {lead_name}, thank you for your enquiry. {practice_name} has received it "
            "and will be in touch shortly."
        ),
        "default_transport": "whatsapp",
        "provider_template_name": "lead_acknowledgement_v1",
        "is_practitioner_disableable": True,
    },
    {
        "code": "lead_notification",
        "category": "notification",
        "is_essential": False,
        "priority": 20,
        "staleness_tolerance_minutes": 1440,
        "variables": {
            "practitioner_name": {"type": "string", "required": True},
            "lead_name": {"type": "string", "required": True},
            # ⚠️ Required, because the producer always supplies one — it passes
            # "the enquiry form" when the submission recorded no source. An
            # optional variable that the body uses would render as literal
            # braces on the day nobody passed it.
            "lead_source": {"type": "string", "required": True},
        },
        # ⚠️ 🔒 Practitioner-directed, so it carries **no** client-facing detail
        # beyond a name the practitioner is about to read anyway. The enquiry's
        # stated goal is deliberately absent: this message travels over email to
        # an inbox we do not control (NFR-033).
        "body_template": (
            "Hi {practitioner_name}, a new enquiry has arrived from {lead_name} "
            "via {lead_source}. Open your enquiries list to respond."
        ),
        # 🔒 Email by default, not WhatsApp. The recipient is a `users` row, and
        # a practitioner's own number is not a client contact — sending practice
        # notifications through the client channel is how a template ends up
        # used outside its approved category (M8.4's policy risk).
        "default_transport": "email",
        "provider_template_name": None,
        "is_practitioner_disableable": True,
    },
    {
        "code": "magic_link",
        "category": "transactional",
        # 🔒 DB §11.6, the one essential template. "A client locked out of their
        # portal because their practitioner hit a nudge quota is unacceptable."
        "is_essential": True,
        "priority": 1,
        # 🔒 Ten minutes, against a 15–30 minute link TTL (FR-M0-005). A magic
        # link delivered after it has expired is not a late message, it is a
        # broken one — and EC-M7-01 already gives the client a one-tap re-request.
        "staleness_tolerance_minutes": 10,
        "variables": {
            "client_name": {"type": "string", "required": True},
            "link_url": {"type": "url", "required": True},
            "expires_in_minutes": {"type": "number", "required": True},
        },
        "body_template": (
            "Hi {client_name}, here is your secure link: {link_url} "
            "It expires in {expires_in_minutes} minutes."
        ),
        "default_transport": "whatsapp",
        "provider_template_name": "magic_link_v1",
        # 🔒 Not disableable at any level — FR-M8-027 says "any *non-essential*
        # message type", and this is the one that is not.
        "is_practitioner_disableable": False,
    },
)


def upgrade() -> None:
    # ─── 1: the enums ─────────────────────────────────────────────────────
    for name, values in (
        ("message_category", _MESSAGE_CATEGORY),
        ("provider_template_status", _PROVIDER_TEMPLATE_STATUS),
        ("scheduled_state", _SCHEDULED_STATE),
        ("suppression_reason", _SUPPRESSION_REASON),
        ("dispatch_status", _DISPATCH_STATUS),
        ("checkin_frequency", _CHECKIN_FREQUENCY),
    ):
        sa.Enum(*values, name=name).create(op.get_bind(), checkfirst=False)

    # 🔒 See section 1 of the docstring. `IF NOT EXISTS` so a re-run against a
    # partially-migrated scratch database does not abort on the enum alone.
    op.execute("ALTER TYPE transport_type ADD VALUE IF NOT EXISTS 'logged'")

    # ─── message_templates (DB §11.1) ─────────────────────────────────────
    op.create_table(
        "message_templates",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("category", _enum("message_category"), nullable=False),
        sa.Column("is_essential", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column("staleness_tolerance_minutes", sa.Integer(), nullable=True),
        sa.Column(
            "variables",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("body_template", sa.Text(), nullable=False),
        sa.Column("default_transport", _enum("transport_type"), nullable=False),
        sa.Column("provider_template_name", sa.Text(), nullable=True),
        sa.Column(
            "provider_template_status",
            _enum("provider_template_status"),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "is_practitioner_disableable",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("status", _enum("definition_status"), server_default="draft", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        # 🔒 FR-M8-002 — versioned templates. A new version is a new row, which
        # is what lets an in-flight `scheduled_messages` row keep pointing at the
        # wording it was created against.
        sa.UniqueConstraint("code", "version", name="uq_message_templates__code_version"),
        sa.CheckConstraint("version > 0", name="ck_message_templates__version_positive"),
        sa.CheckConstraint("priority > 0", name="ck_message_templates__priority_positive"),
        sa.CheckConstraint(
            "length(btrim(code)) > 0 AND code ~ '^[a-z][a-z0-9_]{2,62}$'",
            name="ck_message_templates__code_shape",
        ),
        sa.CheckConstraint(
            "staleness_tolerance_minutes IS NULL OR staleness_tolerance_minutes > 0",
            name="ck_message_templates__staleness_positive",
        ),
        sa.CheckConstraint(
            "length(btrim(body_template)) > 0",
            name="ck_message_templates__body_present",
        ),
        # 🔒 A WhatsApp template with no approved provider name is unsendable —
        # Meta rejects free text on that channel per recipient, at send time.
        # The constraint turns a production failure into a refused INSERT.
        sa.CheckConstraint(
            "default_transport <> 'whatsapp' OR provider_template_name IS NOT NULL",
            name="ck_message_templates__whatsapp_needs_provider_name",
        ),
        # 🔒 FR-M8-027, DB §11.6 — the two flags cannot both be set. An
        # essential template that a practitioner could disable would let a
        # tenant-wide toggle lock every client out of their own portal.
        sa.CheckConstraint(
            "NOT (is_essential AND is_practitioner_disableable)",
            name="ck_message_templates__essential_not_disableable",
        ),
    )
    op.create_index("ix_message_templates__code_status", "message_templates", ["code", "status"])

    # ─── scheduled_messages (DB §11.2) ────────────────────────────────────
    #
    # 🔒 `client_id` carries **no foreign key**. `clients` belongs to another
    # module (DB §5) and R6 forbids referencing it in DDL as much as in code —
    # `tools/check_boundaries.py` rejects the model that declares one.
    # Resolution goes through the `ClientDirectory` port (Arch §3.4b), and
    # `enquiry_submissions.client_id` is the existing precedent.
    op.create_table(
        "scheduled_messages",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=True),
        sa.Column("recipient_user_id", sa.UUID(), nullable=True),
        sa.Column("template_id", sa.UUID(), nullable=False),
        sa.Column(
            "template_variables",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", _enum("scheduled_state"), server_default="pending", nullable=False),
        sa.Column("suppression_reason", _enum("suppression_reason"), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("source_module", sa.Text(), nullable=False),
        sa.Column("source_record_id", sa.UUID(), nullable=True),
        sa.Column("deferred_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["recipient_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["template_id"], ["message_templates.id"]),
        sa.PrimaryKeyConstraint("id"),
        # 🔒 EC-M8-06, DB §11.4 — the constraint *is* the idempotency mechanism.
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_scheduled_messages__idempotency"
        ),
        # A message with no recipient is a row that can never be dispatched, and
        # would sit in `pending` forever looking like a stuck queue.
        sa.CheckConstraint(
            "client_id IS NOT NULL OR recipient_user_id IS NOT NULL",
            name="ck_scheduled_messages__recipient_present",
        ),
        # 🔒 AC-M8-004 — a suppressed row without its reason is the one state
        # this table must never hold, and the reason on any other state is a bug
        # in whichever code path set it.
        sa.CheckConstraint(
            "(state = 'suppressed') = (suppression_reason IS NOT NULL)",
            name="ck_scheduled_messages__suppression_reason",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_scheduled_messages__attempts"),
        sa.CheckConstraint(
            "length(btrim(source_module)) > 0", name="ck_scheduled_messages__source_present"
        ),
    )
    # 🔒 DB §11.2 / NFR-009 — **the worker's hot path**. Partial on `pending`
    # because that is the only state the scheduler scans, and the index stays
    # small even as `dispatched` rows accumulate into the millions.
    op.execute(
        "CREATE INDEX ix_scheduled_messages__due ON scheduled_messages (scheduled_for) "
        "WHERE state = 'pending'"
    )
    # EC-M8-08 — cancelling everything a stage change invalidates.
    op.create_index(
        "ix_scheduled_messages__source",
        "scheduled_messages",
        ["tenant_id", "source_module", "source_record_id"],
    )
    # FR-M8-028 — pending messages for one client.
    op.execute(
        "CREATE INDEX ix_scheduled_messages__client ON scheduled_messages "
        "(tenant_id, client_id, scheduled_for DESC)"
    )

    # ─── message_dispatches (DB §11.3) ────────────────────────────────────
    op.create_table(
        "message_dispatches",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=True),
        # NULL for an immediate send that never queued (DB §11.3).
        sa.Column("scheduled_message_id", sa.UUID(), nullable=True),
        sa.Column("template_id", sa.UUID(), nullable=False),
        # 🔒 Denormalised deliberately (DDR-11's reasoning): the version actually
        # used. A template published later must not change what the log says was
        # sent.
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("transport", _enum("transport_type"), nullable=False),
        # 🔒 EC-M8-08 — the number or address actually used. A client who changes
        # number keeps the history of where earlier messages went, which is what
        # makes a support conversation possible.
        sa.Column("recipient_address", sa.Text(), nullable=False),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column("status", _enum("dispatch_status"), server_default="queued", nullable=False),
        sa.Column("failure_code", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("attempt_number", sa.Integer(), server_default="1", nullable=False),
        # 🔒 NFR-088 — per-tenant WhatsApp cost. Nullable because only a provider
        # that reports it can populate it, and pretending otherwise would put a
        # guess into a number someone bills against.
        sa.Column("cost_amount", sa.Numeric(10, 4), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["scheduled_message_id"], ["scheduled_messages.id"]),
        sa.ForeignKeyConstraint(["template_id"], ["message_templates.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("attempt_number > 0", name="ck_message_dispatches__attempt_positive"),
        sa.CheckConstraint("template_version > 0", name="ck_message_dispatches__version_positive"),
        sa.CheckConstraint(
            "length(btrim(recipient_address)) > 0",
            name="ck_message_dispatches__address_present",
        ),
        # 🔒 FR-M8-003 — "status and failure reason". A failure with no code is a
        # log entry nobody can act on, and AC-M8-007 requires terminal failure to
        # be visible to the practitioner as something specific.
        sa.CheckConstraint(
            "status NOT IN ('failed', 'rejected') OR failure_code IS NOT NULL",
            name="ck_message_dispatches__failure_coded",
        ),
        sa.CheckConstraint(
            "cost_amount IS NULL OR cost_amount >= 0",
            name="ck_message_dispatches__cost_non_negative",
        ),
    )
    op.execute(
        "CREATE INDEX ix_message_dispatches__tenant_created ON message_dispatches "
        "(tenant_id, created_at DESC)"
    )
    # FR-M8-011 — the practitioner's per-client message history.
    op.execute(
        "CREATE INDEX ix_message_dispatches__client_created ON message_dispatches "
        "(client_id, created_at DESC)"
    )
    # 🔒 DB §11.3 — webhook idempotency. Partial, because most rows have no
    # provider id: the logged transport issues none, and a queued row has not
    # been given one yet.
    #
    # ⚠️ Deliberately **not** scoped by tenant. The delivery receipt arrives
    # knowing only this id, so the lookup that resolves a tenant cannot itself
    # require one. Provider ids are globally unique by the provider's own
    # contract; a collision across tenants would be the provider reusing an id.
    op.execute(
        "CREATE UNIQUE INDEX uq_message_dispatches__provider_id ON message_dispatches "
        "(provider_message_id) WHERE provider_message_id IS NOT NULL"
    )

    # ─── checkin_schedules (DB §11.7) ─────────────────────────────────────
    op.create_table(
        "checkin_schedules",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("frequency", _enum("checkin_frequency"), server_default="weekly", nullable=False),
        # 🟡 FR-M8-023 — defaults to the weekday the client became active, which
        # the service supplies. NULL means "the day the schedule was created on".
        sa.Column("day_of_week", sa.SmallInteger(), nullable=True),
        sa.Column("time_of_day", sa.Time(), server_default="09:00", nullable=False),
        # 🔒 FR-M8-024 — pausable *without* changing the lifecycle stage.
        sa.Column("is_paused", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_generated_for", sa.Date(), nullable=True),
        sa.Column("next_due_on", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        # One cadence per client. Two schedules would generate two nudges a week
        # and neither would be wrong on its own.
        sa.UniqueConstraint("tenant_id", "client_id", name="uq_checkin_schedules__client"),
        # ISO weekday, Monday = 1 … Sunday = 7. Matches PostgreSQL's `isodow`,
        # so `next_due_on` arithmetic never needs a translation table.
        sa.CheckConstraint(
            "day_of_week IS NULL OR (day_of_week BETWEEN 1 AND 7)",
            name="ck_checkin_schedules__day_of_week",
        ),
        sa.CheckConstraint(
            "is_paused = (paused_at IS NOT NULL)",
            name="ck_checkin_schedules__pause_timestamped",
        ),
    )
    # The generator's scan — FR-M8-022, and the only query that reads this table
    # across clients.
    op.execute(
        "CREATE INDEX ix_checkin_schedules__due ON checkin_schedules (next_due_on) "
        "WHERE is_paused = false"
    )

    # ─── notification_preferences (DB §11.8) ──────────────────────────────
    op.create_table(
        "notification_preferences",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        # 🔒 NULL = the tenant default (FR-M8-027). A row with a client is that
        # client's override (US-M8-06).
        sa.Column("client_id", sa.UUID(), nullable=True),
        # NULL = every message type. A code rather than a template id, so a
        # preference survives a new template *version* — which is the point of
        # versioning, and would otherwise silently re-enable a muted type.
        sa.Column("template_code", sa.Text(), nullable=True),
        sa.Column("transport", _enum("transport_type"), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        # 🟡 FR-M8-009 — 21:00 → 08:00 local. NULL means "inherit": a client row
        # with no window falls back to the tenant's, and a tenant row with none
        # falls back to `kernel.messaging`'s default.
        sa.Column("quiet_hours_start", sa.Time(), nullable=True),
        sa.Column("quiet_hours_end", sa.Time(), nullable=True),
        sa.Column("max_messages_per_week", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "max_messages_per_week IS NULL OR max_messages_per_week >= 0",
            name="ck_notification_preferences__weekly_limit",
        ),
        # A half-specified window is the one that silently behaves like no
        # window at all, which is the opposite of what setting it meant.
        sa.CheckConstraint(
            "(quiet_hours_start IS NULL) = (quiet_hours_end IS NULL)",
            name="ck_notification_preferences__quiet_hours_paired",
        ),
        sa.CheckConstraint(
            "template_code IS NULL OR length(btrim(template_code)) > 0",
            name="ck_notification_preferences__template_code_present",
        ),
    )
    # 🔒 `NULLS NOT DISTINCT` (PostgreSQL 15+) is what makes this constraint
    # mean what it reads as. By default two rows with a NULL `client_id` do not
    # conflict, so a tenant could accumulate any number of contradictory
    # "tenant default" rows and which one won would depend on physical order.
    op.execute(
        "CREATE UNIQUE INDEX uq_notification_preferences__scope ON notification_preferences "
        "(tenant_id, client_id, template_code) NULLS NOT DISTINCT"
    )

    # ─── The seed (FR-M8-013…020) ─────────────────────────────────────────
    #
    # ⚠️ Before RLS is enabled, and that ordering is load-bearing — see 0016's
    # note, which this repeats for the same reason.
    for template in _SEED_TEMPLATES:
        op.execute(
            sa.text(
                """
                INSERT INTO message_templates
                    (code, version, category, is_essential, priority,
                     staleness_tolerance_minutes, variables, body_template,
                     default_transport, provider_template_name,
                     provider_template_status, is_practitioner_disableable, status)
                VALUES
                    (:code, 1, CAST(:category AS message_category), :is_essential, :priority,
                     :staleness, CAST(:variables AS jsonb), :body,
                     CAST(:transport AS transport_type), :provider_name,
                     'pending', :disableable, 'published')
                """
            ).bindparams(
                code=template["code"],
                category=template["category"],
                is_essential=template["is_essential"],
                priority=template["priority"],
                staleness=template["staleness_tolerance_minutes"],
                variables=json.dumps(template["variables"]),
                body=template["body_template"],
                transport=template["default_transport"],
                provider_name=template["provider_template_name"],
                disableable=template["is_practitioner_disableable"],
            )
        )

    # ─── RLS (DB §17.1) ───────────────────────────────────────────────────
    #
    # 🔒 FORCE is not redundant with ENABLE: without it the table owner bypasses
    # every policy, and migrations run as `app_migrator`, which owns these.
    for table in (*_TENANT_SCOPED, "message_templates"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    for table in _TENANT_SCOPED:
        op.execute(_TENANT_POLICY.format(table=table))

    op.execute(_TEMPLATE_POLICY)
    op.execute(_PROVIDER_LOOKUP_POLICY)

    # ⚠️ Last: once the revokes land, this migration's own connection keeps
    # working only because it runs as the migrator role.
    op.execute(_APPLY_GRANTS)


def downgrade() -> None:
    """Drop everything this revision created.

    ⚠️ 🔒 **Destroys the delivery log.** Every record of what was sent, to whom
    and with what outcome goes with `message_dispatches`. FR-M8-003 makes that
    log the answer to "did my client receive it?", and after this it is
    answerable only from the audit trail.

    ⚠️ Pending `scheduled_messages` are destroyed too, so anything due and not
    yet sent is simply lost — including messages a practitioner has already been
    told are scheduled (FR-M8-028).

    Reversibility exists so the chain is honestly testable in development. This
    is not a supported production operation.
    """
    op.execute(_RESTORE_DEFAULT_PRIVILEGES)

    op.drop_table("notification_preferences")
    op.drop_table("checkin_schedules")
    op.drop_table("message_dispatches")
    op.drop_table("scheduled_messages")
    op.drop_table("message_templates")

    for name in (
        "checkin_frequency",
        "dispatch_status",
        "suppression_reason",
        "scheduled_state",
        "provider_template_status",
        "message_category",
    ):
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=False)

    # 🔒 PostgreSQL cannot remove a value from an enum, so `transport_type` is
    # rebuilt without `logged` and the one surviving column is re-typed onto it.
    #
    # ⚠️ The cast fails loudly if any `magic_links` row holds `logged`. That is
    # the correct outcome: it would mean a magic link was issued over the no-op
    # transport, and silently rewriting it to something else would misreport how
    # a credential reached a person.
    op.execute("ALTER TYPE transport_type RENAME TO transport_type__old")
    op.execute("CREATE TYPE transport_type AS ENUM ('whatsapp', 'sms', 'email')")
    op.execute(
        "ALTER TABLE magic_links ALTER COLUMN issued_via TYPE transport_type "
        "USING issued_via::text::transport_type"
    )
    op.execute("DROP TYPE transport_type__old")
