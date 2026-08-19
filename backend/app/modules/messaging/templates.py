"""The template registry — versioned content with typed variables (FR-M8-002).

🔒 **Reading and rendering only.** Templates are platform-owned reference data
(DB §11.1) and `app_user` holds no write grant, so nothing here inserts or
updates. Practitioner-editable wording is Phase 2 (FR-M8-029); what a
practitioner controls at MVP lives in ``preferences``.

🔒 **Rendering happens once, here, against the versioned row** — never in an
adapter. Two transports rendering one template differently is how a client
receives text that does not match the preview the practitioner approved
(FR-M8-026), and it is also how a WhatsApp send acquires a body Meta never
approved.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.clinical import DefinitionStatus
from app.kernel.errors import NotFoundError, ValidationError
from app.kernel.messaging import (
    MessageCategory,
    ProviderTemplateStatus,
    TemplatePolicy,
)
from app.kernel.models import TransportType
from app.modules.messaging.models import MessageTemplate

#: A ``{placeholder}`` in a body template.
#:
#: ⚠️ **Not ``str.format``.** A body containing a literal brace — a currency
#: amount, an emoji sequence, anything a future template author writes — makes
#: ``format`` raise mid-send, per recipient, in a worker. A regex substitution
#: over declared names only cannot fail that way: an unrecognised brace is left
#: exactly as written.
_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r"\{([a-z][a-z0-9_]{0,62})\}")

#: 🔒 The longest a substituted value may be. A variable is a name, a short date
#: or a URL; anything longer is prose, and prose in a variable is how a clinical
#: note reaches a `scheduled_messages` row that is retained and backed up
#: (NFR-033) — and, on WhatsApp, how a template exceeds the provider's own
#: parameter limits and is rejected per recipient.
MAX_VARIABLE_LENGTH: Final[int] = 512


@dataclass(frozen=True, slots=True)
class Template:
    """One template version, as the rest of the module sees it.

    A projection rather than the ORM row, for the reason ``ClientIdentity`` is
    one: handing the dispatch engine a mapped object would give it lazy loading
    and every column, and the fields it came to depend on would become fields
    this table could not change.
    """

    id: uuid.UUID
    code: str
    version: int
    category: MessageCategory
    is_essential: bool
    priority: int
    staleness_tolerance_minutes: int | None
    variables: Mapping[str, Any]
    body_template: str
    default_transport: TransportType
    provider_template_name: str | None
    provider_template_status: ProviderTemplateStatus
    is_practitioner_disableable: bool

    def policy(self, *, provider_status: ProviderTemplateStatus | None = None) -> TemplatePolicy:
        """The kernel's view of this template's policy — DB §11.1.

        Args:
            provider_status: Overrides the stored Meta approval state. 🔒 The
                dispatch engine passes one, and the reason is load-bearing
                enough that it is documented at the call site: provider approval
                is a WhatsApp concept, while `template_paused` is the kernel's
                single answer for "this message type is not currently sendable"
                — which is also true when a practitioner has disabled the type
                (FR-M8-027) or the template is not published.
        """
        return TemplatePolicy(
            code=self.code,
            is_essential=self.is_essential,
            priority=self.priority,
            staleness_tolerance_minutes=self.staleness_tolerance_minutes,
            provider_template_status=provider_status or self.provider_template_status,
            category=self.category,
        )


def project(row: MessageTemplate) -> Template:
    return Template(
        id=row.id,
        code=row.code,
        version=row.version,
        category=row.category,
        is_essential=row.is_essential,
        priority=row.priority,
        staleness_tolerance_minutes=row.staleness_tolerance_minutes,
        variables=dict(row.variables),
        body_template=row.body_template,
        default_transport=row.default_transport,
        provider_template_name=row.provider_template_name,
        provider_template_status=row.provider_template_status,
        is_practitioner_disableable=row.is_practitioner_disableable,
    )


async def load_current(session: AsyncSession, *, code: str) -> Template:
    """The highest published version of a message type.

    🔒 Published only. A draft template is content nobody has reviewed, and the
    one thing worse than not sending a message is sending an unreviewed one to a
    practitioner's client under their name.

    Raises:
        NotFoundError: If no published version exists. ⚠️ This is a deployment
            fault, not a user error — the eight MVP types are seeded by migration
            0021 — so the message names the code rather than blaming the caller.
    """
    row = await session.scalar(
        select(MessageTemplate)
        .where(
            MessageTemplate.code == code,
            MessageTemplate.status == DefinitionStatus.PUBLISHED,
        )
        .order_by(MessageTemplate.version.desc())
        .limit(1)
    )
    if row is None:
        raise NotFoundError(
            f"No published message template for {code!r}.",
            action="Check the message type exists in this deployment.",
        )
    return project(row)


async def load_by_id(session: AsyncSession, *, template_id: uuid.UUID) -> Template:
    """One exact template version — what a `scheduled_messages` row points at.

    🔒 By id, not by code, and the distinction is the whole point of versioning:
    a message scheduled on Monday is dispatched on Friday with the wording it was
    created against, even if a new version was published on Wednesday.
    """
    row = await session.get(MessageTemplate, template_id)
    if row is None:
        raise NotFoundError(
            "That message template no longer exists.",
            action="Reschedule the message.",
        )
    return project(row)


async def list_published(session: AsyncSession) -> list[Template]:
    """Every published message type, one row per code — FR-M8-026's preview list.

    ⚠️ Returns the *highest* published version of each code. A preview list
    showing three versions of "Plan delivered" would ask the practitioner to
    choose between things that are not choices.
    """
    rows = await session.scalars(
        select(MessageTemplate)
        .where(MessageTemplate.status == DefinitionStatus.PUBLISHED)
        .order_by(MessageTemplate.code, MessageTemplate.version.desc())
    )
    latest: dict[str, Template] = {}
    for row in rows:
        latest.setdefault(row.code, project(row))
    return sorted(latest.values(), key=lambda template: (template.priority, template.code))


# ─── Variables (FR-M8-002 — "typed variables") ───────────────────────────


def declared_variables(template: Template) -> dict[str, dict[str, Any]]:
    """The template's variable declarations, normalised.

    ⚠️ Tolerates a bare ``{"client_name": "string"}`` shorthand as well as the
    full ``{"client_name": {"type": "string", "required": true}}``. The seed uses
    the full form; the shorthand exists because a future template author will
    write it, and failing on it would turn a template edit into an outage for one
    message type.
    """
    declared: dict[str, dict[str, Any]] = {}
    for name, spec in template.variables.items():
        if isinstance(spec, Mapping):
            declared[name] = {
                "type": str(spec.get("type", "string")),
                "required": bool(spec.get("required", True)),
            }
        else:
            declared[name] = {"type": str(spec), "required": True}
    return declared


def validate_variables(template: Template, variables: Mapping[str, Any]) -> dict[str, str]:
    """🔒 Check a caller's variables against the declaration, before scheduling.

    Validation happens at *scheduling* time even though rendering happens at
    dispatch. That is deliberate: a missing variable is a caller bug, and it must
    surface in the request that made it rather than in a worker three days later
    when the check-in comes due — by which point nobody knows what produced it.

    Returns the variables as plain strings, which is what the row stores.

    Raises:
        ValidationError: On a missing required variable, an undeclared one, or a
            value long enough to be prose. Each names the template and the
            variable, because the fix is always in the caller.
    """
    declared = declared_variables(template)

    unknown = sorted(set(variables) - set(declared))
    if unknown:
        raise ValidationError(
            f"Template {template.code!r} does not declare: {', '.join(unknown)}.",
            action="Remove the variable, or declare it on the template.",
        )

    rendered: dict[str, str] = {}
    for name, spec in declared.items():
        if name not in variables or variables[name] is None:
            if spec["required"]:
                raise ValidationError(
                    f"Template {template.code!r} requires the variable {name!r}.",
                    action="Supply a value for it.",
                )
            continue

        value = str(variables[name])
        if len(value) > MAX_VARIABLE_LENGTH:
            raise ValidationError(
                f"The value for {name!r} is {len(value)} characters, over the "
                f"{MAX_VARIABLE_LENGTH} permitted.",
                action="Pass a short display value; link to the detail instead.",
            )
        rendered[name] = value

    return rendered


def render(template: Template, variables: Mapping[str, str]) -> str:
    """The message body as the client will receive it — FR-M8-026.

    🔒 The **same function** serves the preview endpoint and the dispatch path.
    A preview rendered by different code from the send is a preview of something
    else, which is precisely the reassurance FR-M8-026 exists to give.

    ⚠️ A placeholder with no value is left as written rather than replaced with
    an empty string. "Hi {client_name}" reaching a real person is bad; "Hi ,"
    reaching them is bad *and* looks like a system that does not know it is
    broken. :func:`validate_variables` is what stops either arriving.
    """

    def substitute(match: re.Match[str]) -> str:
        return variables.get(match.group(1), match.group(0))

    return _PLACEHOLDER.sub(substitute, template.body_template)


def placeholders(template: Template) -> frozenset[str]:
    """Every ``{name}`` the body actually uses.

    Used by the consistency test that asserts a template's declaration and its
    body agree: a declared-but-unused variable is harmless, while a used-but-
    undeclared one renders as literal braces to a client.
    """
    return frozenset(_PLACEHOLDER.findall(template.body_template))
