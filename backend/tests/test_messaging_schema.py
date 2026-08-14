"""What migration 0021 and the messaging models must claim — DB §11.

🔒 These need no database, and that is the point: what they protect is a set of
*claims* — that the eight MVP message types are exactly the eight the PRD names,
that the idempotency constraint and the partial due-index exist by the names the
spec fixes, that the delivery log is on the append-only verification list. Every
one is a line someone could delete during a refactor with nothing going red.

⚠️ What they cannot prove: that the SQL executes, that RLS actually isolates, or
that the grants hold. Those need a live PostgreSQL and belong to
``tests/integration/messaging``.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.modules.messaging.models import (
    CheckinSchedule,
    MessageDispatch,
    MessageTemplate,
    NotificationPreference,
    ScheduledMessage,
)

_REPO = Path(__file__).resolve().parents[2]
_MIGRATION = _REPO / "backend" / "migrations" / "versions" / "20260814_0021_messaging.py"
_VERIFY_SQL = _REPO / "ops" / "db" / "002_verify_grants.sql"

#: 🔒 FR-M8-013…020, in the PRD's own order. Exactly eight — the scope summary
#: says "8 MVP message types", and a ninth "because it seems useful" is a
#: message a practitioner has to discover and turn off.
MVP_MESSAGE_TYPES: tuple[str, ...] = (
    "plan_delivered",
    "appointment_confirmed",
    "appointment_reminder",
    "checkin_nudge",
    "assessment_invitation",
    "lead_acknowledgement",
    "lead_notification",
    "magic_link",
)


@pytest.fixture(scope="module")
def migration() -> ModuleType:
    """Load the revision as a module so its constants can be asserted directly.

    ⚠️ By path rather than by import: ``migrations/versions`` is not a package,
    and a string search over the source would pass on a name that appears only in
    a comment.
    """
    spec = importlib.util.spec_from_file_location("_m0021", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migration_source() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


# ─── The eight MVP message types ─────────────────────────────────────────


def test_exactly_the_eight_mvp_message_types_are_seeded(migration: ModuleType) -> None:
    """🔒 FR-M8-013…020. AC-M8-008 says a new type needs only a template and a
    schedule, so these are data — but the *set* is a product decision."""
    seeded = tuple(template["code"] for template in migration._SEED_TEMPLATES)
    assert seeded == MVP_MESSAGE_TYPES


def test_only_the_magic_link_is_essential(migration: ModuleType) -> None:
    """🔒 DB §11.6 scopes the exemption to magic links and password resets.

    Widening it would let a busy tenant's nudges consume the quota a client's
    portal access depends on.
    """
    essential = {t["code"] for t in migration._SEED_TEMPLATES if t["is_essential"]}
    assert essential == {"magic_link"}


def test_no_essential_template_is_practitioner_disableable(migration: ModuleType) -> None:
    """🔒 FR-M8-027 says "any *non-essential* message type", and the database
    carries the same rule as `ck_message_templates__essential_not_disableable`."""
    for template in migration._SEED_TEMPLATES:
        assert not (template["is_essential"] and template["is_practitioner_disableable"])


def test_every_whatsapp_template_names_its_approved_provider_template(
    migration: ModuleType,
) -> None:
    """🔒 Meta rejects free text on that channel per recipient, at send time."""
    for template in migration._SEED_TEMPLATES:
        if template["default_transport"] == "whatsapp":
            assert template["provider_template_name"], template["code"]


def test_the_practitioner_notification_does_not_use_the_client_channel(
    migration: ModuleType,
) -> None:
    """🔒 M8.4's policy risk — a template used outside its approved category can
    get the number restricted. A practice notification goes to a `users` row by
    email, not through the client channel."""
    notification = _by_code(migration, "lead_notification")
    assert notification["default_transport"] == "email"


def test_every_declared_variable_is_used_by_its_body(migration: ModuleType) -> None:
    """A declared-but-unused variable is harmless; a used-but-undeclared one
    renders as literal braces to a client, and a declared one nobody supplies is
    refused at scheduling. Both halves are checked here."""
    for template in migration._SEED_TEMPLATES:
        declared = set(template["variables"])
        used = set(re.findall(r"\{([a-z][a-z0-9_]*)\}", template["body_template"]))
        assert used == declared, template["code"]


def test_no_seeded_template_carries_a_clinical_value(migration: ModuleType) -> None:
    """⚠️ NFR-033 — a template's variables become a `scheduled_messages` row that
    is retained, backed up and read by operators. Names, links and dates only."""
    permitted = {
        "client_name",
        "lead_name",
        "practitioner_name",
        "practice_name",
        "plan_url",
        "portal_url",
        "assessment_url",
        "link_url",
        "appointment_at",
        "expires_in_minutes",
        "lead_source",
    }
    for template in migration._SEED_TEMPLATES:
        assert set(template["variables"]) <= permitted, template["code"]


def test_the_check_in_staleness_matches_the_edge_case(migration: ModuleType) -> None:
    """🔒 EC-M8-05 names the number: "a check-in more than 24 h stale is dropped
    and logged rather than sent late"."""
    assert _by_code(migration, "checkin_nudge")["staleness_tolerance_minutes"] == 1440


def test_a_plan_delivery_never_goes_stale(migration: ModuleType) -> None:
    """A plan a practitioner approved is still wanted late; expiring it would
    discard work they had already done."""
    assert _by_code(migration, "plan_delivered")["staleness_tolerance_minutes"] is None


def test_an_appointment_reminder_expires_quickly(migration: ModuleType) -> None:
    """A reminder delivered after the appointment started is worse than none."""
    assert _by_code(migration, "appointment_reminder")["staleness_tolerance_minutes"] == 120


# ─── The constraints and indexes the spec fixes by name ──────────────────


@pytest.mark.parametrize(
    "name",
    [
        # 🔒 DB §11.2 / EC-M8-06 — the constraint *is* the idempotency mechanism.
        "uq_scheduled_messages__idempotency",
        # 🔒 DB §11.2 — the worker's hot path (NFR-009).
        "ix_scheduled_messages__due",
        # EC-M8-08 — cancellation on stage change.
        "ix_scheduled_messages__source",
        # 🔒 DB §11.3 — webhook idempotency.
        "uq_message_dispatches__provider_id",
        "ix_message_dispatches__tenant_created",
        # FR-M8-011 — the per-client history read.
        "ix_message_dispatches__client_created",
        "uq_checkin_schedules__client",
        "uq_notification_preferences__scope",
    ],
)
def test_the_named_constraints_and_indexes_exist(migration_source: str, name: str) -> None:
    assert name in migration_source


def test_the_due_index_is_partial_on_pending(migration_source: str) -> None:
    """🔒 The index stays small as dispatched rows accumulate into the millions,
    and `state = 'pending'` is the only state the scheduler scans."""
    assert "WHERE state = 'pending'" in migration_source


def test_the_preference_scope_index_treats_nulls_as_equal(migration_source: str) -> None:
    """🔒 Without `NULLS NOT DISTINCT` a tenant could accumulate any number of
    contradictory "tenant default" rows, and which one won would depend on
    physical order."""
    assert "NULLS NOT DISTINCT" in migration_source


def test_the_transport_enum_gains_the_logged_value(migration_source: str) -> None:
    """🔒 What makes S5 shippable before Meta Business Verification."""
    assert "ALTER TYPE transport_type ADD VALUE IF NOT EXISTS 'logged'" in migration_source


def test_the_downgrade_rebuilds_the_transport_enum(migration_source: str) -> None:
    """PostgreSQL cannot remove an enum value, so reversibility means rebuilding
    the type — and the cast fails loudly if a magic link was ever issued over the
    no-op transport, which is the correct outcome."""
    downgrade = migration_source.split("def downgrade()", 1)[1]
    assert "CREATE TYPE transport_type AS ENUM ('whatsapp', 'sms', 'email')" in downgrade


# ─── Grants (DB §11.3, DDR-15) ───────────────────────────────────────────


def test_the_delivery_log_is_on_the_append_only_verification_list() -> None:
    """🔒 `ops/db/002_verify_grants.sql` is what catches an UPDATE or DELETE
    grant creeping back onto an append-only table — including through
    `001_roles.sql`'s default privileges on future tables."""
    assert "message_dispatches" in _VERIFY_SQL.read_text(encoding="utf-8")


def test_the_delivery_log_grants_update_on_status_columns_only(migration_source: str) -> None:
    """🔒 DB §11.3 — "status transitions from provider webhooks update the row's
    status fields only; the attempt record itself is never rewritten".

    A table-level grant cannot express that, so the column list *is* the rule.
    ⚠️ `template_id`, `template_version`, `transport`, `recipient_address` and
    `attempt_number` must never appear in it.
    """
    grant = migration_source.split("GRANT UPDATE (", 1)[1].split(") ON TABLE", 1)[0]
    granted = {line.strip().rstrip(",") for line in grant.splitlines() if line.strip()}

    assert granted == {
        "status",
        "provider_message_id",
        "failure_code",
        "failure_reason",
        "sent_at",
        "delivered_at",
        "read_at",
        "cost_amount",
        "updated_at",
    }


def test_templates_are_read_only_to_the_application(migration_source: str) -> None:
    """🔒 DB §11.1 — platform-owned. Practitioner-editable wording is Phase 2
    (FR-M8-029), and provider approval state is operator work until S12."""
    assert "REVOKE INSERT, UPDATE, DELETE ON TABLE message_templates FROM app_user" in (
        migration_source
    )
    assert "GRANT SELECT ON TABLE message_templates TO app_user" in migration_source


def test_no_messaging_table_grants_delete(migration_source: str) -> None:
    """🔒 AC-M8-004 — a suppressed message is retained *with its reason*. A row
    that can be deleted is a reason that can be destroyed, and "why didn't my
    client get it?" is the question these tables exist to answer.
    """
    applied = migration_source.split("_APPLY_GRANTS = ", 1)[1].split("_RESTORE", 1)[0]

    # ⚠️ Line by line rather than a substring search: `REVOKE UPDATE, DELETE ON
    # TABLE …` legitimately contains "DELETE ON TABLE", and a naive `not in`
    # either fails on the revokes or is defeated by rearranging the verbs.
    granting_delete = [
        line.strip()
        for line in applied.splitlines()
        if line.strip().startswith("GRANT") and "DELETE" in line
    ]
    assert granting_delete == []


# ─── RLS (DB §17.1) ──────────────────────────────────────────────────────


def test_every_tenant_scoped_table_is_forced_and_isolated(migration: ModuleType) -> None:
    """🔒 FORCE is not redundant with ENABLE: migrations run as `app_migrator`,
    which owns these tables and would otherwise bypass every policy."""
    assert set(migration._TENANT_SCOPED) == {
        "scheduled_messages",
        "message_dispatches",
        "checkin_schedules",
        "notification_preferences",
    }


def test_the_template_table_is_readable_by_every_tenant(migration_source: str) -> None:
    """It has no `tenant_id` to isolate on — the same treatment `nutrients` got
    in 0017 — and SELECT is the only verb the policy admits."""
    assert "CREATE POLICY message_templates__read_all ON message_templates" in migration_source
    assert "FOR SELECT TO app_user" in migration_source


def test_the_provider_lookup_policy_requires_both_conditions(migration_source: str) -> None:
    """🔒 Neither condition is redundant. Without the tenant check a tenant could
    read another tenant's delivery row given a provider id; without the equality
    check a tenant-less connection could read the whole table."""
    policy = migration_source.split("_PROVIDER_LOOKUP_POLICY = ", 1)[1].split('"""', 2)[1]
    assert "current_tenant_id() IS NULL" in policy
    assert "provider_message_id = NULLIF(current_setting('app.provider_message_id'" in policy
    assert "FOR SELECT" in policy


# ─── Models agree with the schema ────────────────────────────────────────


def test_the_five_tables_are_the_ones_the_spec_names() -> None:
    assert {
        MessageTemplate.__tablename__,
        ScheduledMessage.__tablename__,
        MessageDispatch.__tablename__,
        CheckinSchedule.__tablename__,
        NotificationPreference.__tablename__,
    } == {
        "message_templates",
        "scheduled_messages",
        "message_dispatches",
        "checkin_schedules",
        "notification_preferences",
    }


def test_the_template_table_has_no_tenant_column() -> None:
    """🔒 DB §11.1 — "Platform-owned, no `tenant_id` at MVP"."""
    assert "tenant_id" not in MessageTemplate.__table__.columns


@pytest.mark.parametrize(
    "model", [ScheduledMessage, MessageDispatch, CheckinSchedule, NotificationPreference]
)
def test_no_messaging_table_has_a_foreign_key_to_clients(model: Any) -> None:
    """🔒 R6 — a FK couples two module schemas in DDL, where the import checker
    cannot see it. `tools/check_boundaries.py` rejects the model that declares
    one; this states the rule where a reader of the schema will meet it.

    ⚠️ The consequence readers must handle: a `client_id` can dangle after a DPDP
    erasure, and the delivery log deliberately outlives the client.
    """
    referenced = {fk.column.table.name for fk in model.__table__.foreign_keys}
    assert "clients" not in referenced


def test_the_delivery_log_can_outlive_its_scheduled_message() -> None:
    """DB §11.3 — `scheduled_message_id` is NULL for an immediate send."""
    assert MessageDispatch.__table__.columns["scheduled_message_id"].nullable


def _by_code(migration: ModuleType, code: str) -> dict[str, Any]:
    return next(t for t in migration._SEED_TEMPLATES if t["code"] == code)


# ─── One engine (M8.3, FR-M8-001) ────────────────────────────────────────

_APP = _REPO / "backend" / "app"

#: The only places allowed to know a transport exists.
#:
#: 🔒 `integrations/messaging` implements the adapters; `modules/messaging`
#: chooses between them; `platform/messaging_wiring` builds them from settings at
#: the entry point. Anything else reaching for one would be a second dispatch
#: path — with its own retry policy, its own failure log, and none of the
#: suppression rules.
_TRANSPORT_OWNERS: tuple[str, ...] = (
    "app/integrations/messaging",
    "app/modules/messaging",
    "app/platform/messaging_wiring.py",
    # ⚠️ The kernel *declares* `get_transport`; it does not call one. Excluded by
    # name rather than by pattern so the exemption is a decision a reader sees.
    "app/kernel/notifications.py",
)

#: The only place allowed to write the messaging tables. DB §11: "Writers:
#: `messaging`". A module inserting a `scheduled_messages` row directly would be
#: scheduling its own message, which FR-M8-001 forbids in as many words.
_TABLE_OWNERS: tuple[str, ...] = (
    "app/modules/messaging",
    "app/platform/http/routers/messaging.py",
)


def _sources() -> list[tuple[str, str]]:
    return [
        (path.relative_to(_REPO / "backend").as_posix(), path.read_text(encoding="utf-8"))
        for path in _APP.rglob("*.py")
        if "__pycache__" not in path.parts
    ]


def test_only_the_messaging_module_reaches_a_transport() -> None:
    """🔒 M8.3 — "one engine, not three". The failure this guards against is the
    one V1 actually had: a module that sends its own reminder, with its own retry
    path, so "why didn't my client get it?" has a different answer depending on
    which code sent it.
    """
    offenders = [
        path
        for path, source in _sources()
        if "get_transport(" in source and not path.startswith(_TRANSPORT_OWNERS)
    ]
    assert offenders == []


def test_only_the_messaging_module_writes_the_messaging_tables() -> None:
    """🔒 FR-M8-001 — no module schedules its own messages. A producer creates
    *intent* by publishing an event; `messaging` is what turns that into a row.
    """
    offenders = [
        path
        for path, source in _sources()
        if ("ScheduledMessage(" in source or "MessageDispatch(" in source)
        and not path.startswith(_TABLE_OWNERS)
    ]
    assert offenders == []


def test_no_module_imports_a_provider_sdk() -> None:
    """🔒 FR-M0-041 — the moment a module imports a provider client, changing
    provider becomes a search-and-replace and the transport port is decorative.
    """
    offenders = [
        path
        for path, source in _sources()
        if path.startswith("app/modules/")
        and ("urllib.request" in source or "smtplib" in source or "graph.facebook" in source)
    ]
    assert offenders == []
