"""Template rendering and typed variables — FR-M8-002, FR-M8-026.

🔒 No database. ``Template`` is a projection, so every rule about *content* is
testable as a pure function — which is the point of projecting the row rather
than passing the ORM object around.
"""

from __future__ import annotations

import uuid

import pytest

from app.kernel.clinical import DefinitionStatus
from app.kernel.errors import ValidationError
from app.kernel.messaging import MessageCategory, ProviderTemplateStatus
from app.kernel.models import TransportType
from app.modules.messaging import (
    Template,
    declared_variables,
    placeholders,
    render,
    validate_variables,
)


def _template(
    *,
    body: str = "Hi {client_name}, your plan is at {plan_url}",
    variables: dict[str, object] | None = None,
    **overrides: object,
) -> Template:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "code": "plan_delivered",
        "version": 1,
        "category": MessageCategory.TRANSACTIONAL,
        "is_essential": False,
        "priority": 10,
        "staleness_tolerance_minutes": None,
        "variables": variables
        if variables is not None
        else {
            "client_name": {"type": "string", "required": True},
            "plan_url": {"type": "url", "required": True},
        },
        "body_template": body,
        "default_transport": TransportType.WHATSAPP,
        "provider_template_name": "plan_delivered_v1",
        "provider_template_status": ProviderTemplateStatus.PENDING,
        "is_practitioner_disableable": False,
    }
    defaults.update(overrides)
    return Template(**defaults)  # type: ignore[arg-type]


# ─── Rendering ───────────────────────────────────────────────────────────


def test_render_substitutes_declared_variables() -> None:
    body = render(_template(), {"client_name": "Anjali", "plan_url": "https://example.invalid/p/1"})
    assert body == "Hi Anjali, your plan is at https://example.invalid/p/1"


def test_render_leaves_an_unsupplied_placeholder_visible() -> None:
    """⚠️ Not replaced with an empty string.

    "Hi {client_name}" reaching a real person is bad. "Hi ," reaching them is bad
    *and* looks like a system that does not know it is broken —
    :func:`validate_variables` is what stops either from being scheduled.
    """
    assert render(_template(), {"plan_url": "u"}) == "Hi {client_name}, your plan is at u"


def test_render_does_not_choke_on_a_literal_brace() -> None:
    """🔒 The reason this is a regex and not ``str.format``.

    A body containing a stray brace — a currency amount, an emoji sequence —
    would make ``format`` raise mid-send, per recipient, inside a worker.
    """
    template = _template(body="Rates {like this} stay put for {client_name}", variables={})
    assert render(template, {"client_name": "Anjali"}) == ("Rates {like this} stay put for Anjali")


def test_placeholders_reports_what_the_body_uses() -> None:
    assert placeholders(_template()) == {"client_name", "plan_url"}


# ─── Declarations ────────────────────────────────────────────────────────


def test_declared_variables_normalises_the_shorthand() -> None:
    """A future template author will write the short form; failing on it would
    turn a template edit into an outage for one message type."""
    declared = declared_variables(_template(variables={"client_name": "string"}))
    assert declared == {"client_name": {"type": "string", "required": True}}


def test_optional_variables_may_be_omitted() -> None:
    template = _template(
        body="Hi {client_name}{suffix}",
        variables={
            "client_name": {"type": "string", "required": True},
            "suffix": {"type": "string", "required": False},
        },
    )
    assert validate_variables(template, {"client_name": "Anjali"}) == {"client_name": "Anjali"}


def test_a_missing_required_variable_is_refused() -> None:
    """🔒 At scheduling, in the request that caused it — not in a worker three
    days later when the check-in comes due."""
    with pytest.raises(ValidationError):
        validate_variables(_template(), {"client_name": "Anjali"})


def test_an_undeclared_variable_is_refused() -> None:
    with pytest.raises(ValidationError):
        validate_variables(_template(), {"client_name": "A", "plan_url": "u", "smuggled": "value"})


def test_a_variable_long_enough_to_be_prose_is_refused() -> None:
    """🔒 NFR-033 — a scheduled row is retained, backed up and read by operators.

    It is also how a WhatsApp template exceeds Meta's parameter limits and is
    rejected per recipient.
    """
    with pytest.raises(ValidationError):
        validate_variables(_template(), {"client_name": "A" * 600, "plan_url": "u"})


def test_values_are_stringified_for_storage() -> None:
    template = _template(
        body="{count} left", variables={"count": {"type": "number", "required": True}}
    )
    assert validate_variables(template, {"count": 3}) == {"count": "3"}


# ─── Policy projection ───────────────────────────────────────────────────


def test_policy_carries_the_stored_provider_status_by_default() -> None:
    assert _template().policy().provider_template_status is ProviderTemplateStatus.PENDING


def test_policy_accepts_an_override_for_the_dispatch_path() -> None:
    """🔒 The dispatch engine substitutes the provider status when the transport
    is not WhatsApp — applying Meta's approval state to an email send would take
    the product offline while verification is pending."""
    policy = _template().policy(provider_status=ProviderTemplateStatus.APPROVED)
    assert policy.provider_template_status is ProviderTemplateStatus.APPROVED
    assert policy.code == "plan_delivered"
    assert policy.is_essential is False


def test_definition_status_is_the_shared_lifecycle_enum() -> None:
    """Templates reuse `definition_status` rather than inventing a third
    lifecycle vocabulary — the same enum assessments use."""
    assert {status.value for status in DefinitionStatus} == {"draft", "published", "retired"}
