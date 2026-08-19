"""The `messaging` module — one engine for every outbound message (M8.3).

🔒 DB §11. **Owner of** ``message_templates``, ``scheduled_messages``,
``message_dispatches``, ``checkin_schedules`` and ``notification_preferences``.
**Writers:** this module only.

🔒 **One scheduler, one dispatch path** (FR-M8-001). No other module schedules or
sends. A module that needs a client contacted publishes an event; a subscriber
here turns it into a ``scheduled_messages`` row; the sweep hands it to the job
queue; the dispatch engine evaluates suppression against live state and calls a
transport. Every reminder in the product is a row in that pipeline, which is what
makes AC-M8-008 true — a new message type is a template and a schedule, with no
new infrastructure.

⚠️ This package exposes its public surface here, in ``__init__.py``, because R2
forbids importing a module's internals: ``from app.modules.messaging import
dispatch`` is a boundary violation and the checker reports it as one.

⚠️ **This module must not import ``app.platform``** (R5). Three consequences,
each of which looks like an omission until you know why:

* **Transports** are reached through ``kernel.notifications.get_transport``. The
  adapters live in ``app/integrations/messaging`` and are wired by the entry
  point, so nothing here knows a provider exists.
* **The job queue** is reached through ``kernel.jobs.get_job_enqueuer``, for the
  same reason and by the same shape of seam.
* **The link base URL** comes from :func:`configure_link_base_url`, called by the
  entry point with ``settings.app_base_url``.
"""

from __future__ import annotations

from app.modules.messaging.actions import (
    CHECKIN_SCHEDULE_READ,
    CHECKIN_SCHEDULE_UPDATE,
    MESSAGE_CANCEL,
    MESSAGE_HISTORY_READ,
    MESSAGE_PENDING_READ,
    MESSAGE_PREFERENCE_READ,
    MESSAGE_PREFERENCE_UPDATE,
    MESSAGE_SEND,
    MESSAGE_TEMPLATE_PREVIEW,
)
from app.modules.messaging.checkins import (
    CHECKIN_TEMPLATE_CODE,
    CheckinSettings,
    next_occurrence,
)
from app.modules.messaging.checkins import (
    configure as configure_checkin,
)
from app.modules.messaging.checkins import (
    generate_due as generate_due_checkins,
)
from app.modules.messaging.checkins import (
    load as load_checkin_schedule,
)
from app.modules.messaging.checkins import (
    pause as pause_checkins,
)
from app.modules.messaging.checkins import (
    resume as resume_checkins,
)
from app.modules.messaging.dispatch import (
    NON_RETRYABLE_CODES,
    DispatchOutcome,
    RetryableDispatchError,
    dispatch_scheduled,
    resolve_exhausted,
)
from app.modules.messaging.history import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    HistoryEntry,
    HistoryPage,
    recent_failures,
)
from app.modules.messaging.history import (
    list_for_client as list_message_history,
)
from app.modules.messaging.jobs import STATUS_JOB_TYPE, register_jobs
from app.modules.messaging.links import (
    assessment_url,
    base_url,
    configure_link_base_url,
    plan_url,
    portal_url,
)
from app.modules.messaging.models import (
    CheckinSchedule,
    MessageDispatch,
    MessageTemplate,
    NotificationPreference,
    ScheduledMessage,
)
from app.modules.messaging.preferences import (
    DEFAULT_PREFERENCE,
    ResolvedPreference,
    assert_disableable,
)
from app.modules.messaging.preferences import (
    list_for_tenant as list_preferences,
)
from app.modules.messaging.preferences import (
    resolve as resolve_preference,
)
from app.modules.messaging.preferences import (
    upsert as upsert_preference,
)
from app.modules.messaging.scheduler import (
    SweepResult,
    active_tenant_ids,
    sweep_tenant,
)
from app.modules.messaging.scheduling import (
    DISPATCH_JOB_TYPE,
    MessageRequest,
    cancel_for_client,
    cancel_for_source,
    cancel_one,
    list_pending,
    load_scheduled,
    schedule,
)
from app.modules.messaging.templates import (
    Template,
    declared_variables,
    list_published,
    placeholders,
    render,
    validate_variables,
)
from app.modules.messaging.templates import (
    load_by_id as load_template,
)
from app.modules.messaging.templates import (
    load_current as load_current_template,
)
from app.modules.messaging.webhooks import (
    StatusUpdate,
    apply_status,
    tenant_for_provider_message,
)

__all__ = [
    "CHECKIN_SCHEDULE_READ",
    "CHECKIN_SCHEDULE_UPDATE",
    "CHECKIN_TEMPLATE_CODE",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_PREFERENCE",
    "DISPATCH_JOB_TYPE",
    "MAX_PAGE_SIZE",
    "MESSAGE_CANCEL",
    "MESSAGE_HISTORY_READ",
    "MESSAGE_PENDING_READ",
    "MESSAGE_PREFERENCE_READ",
    "MESSAGE_PREFERENCE_UPDATE",
    "MESSAGE_SEND",
    "MESSAGE_TEMPLATE_PREVIEW",
    "NON_RETRYABLE_CODES",
    "STATUS_JOB_TYPE",
    "CheckinSchedule",
    "CheckinSettings",
    "DispatchOutcome",
    "HistoryEntry",
    "HistoryPage",
    "MessageDispatch",
    "MessageRequest",
    "MessageTemplate",
    "NotificationPreference",
    "ResolvedPreference",
    "RetryableDispatchError",
    "ScheduledMessage",
    "StatusUpdate",
    "SweepResult",
    "Template",
    "active_tenant_ids",
    "apply_status",
    "assert_disableable",
    "assessment_url",
    "base_url",
    "cancel_for_client",
    "cancel_for_source",
    "cancel_one",
    "configure_checkin",
    "configure_link_base_url",
    "declared_variables",
    "dispatch_scheduled",
    "generate_due_checkins",
    "list_message_history",
    "list_pending",
    "list_preferences",
    "list_published",
    "load_checkin_schedule",
    "load_current_template",
    "load_scheduled",
    "load_template",
    "next_occurrence",
    "pause_checkins",
    "placeholders",
    "plan_url",
    "portal_url",
    "recent_failures",
    "register_jobs",
    "render",
    "resolve_exhausted",
    "resolve_preference",
    "resume_checkins",
    "schedule",
    "sweep_tenant",
    "tenant_for_provider_message",
    "upsert_preference",
    "validate_variables",
]
