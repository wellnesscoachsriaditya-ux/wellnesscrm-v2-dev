import uuid
from dataclasses import dataclass

from app.kernel.events import DomainEvent, register_event


@register_event("nutrition.plan_version_issued")
@dataclass(frozen=True, slots=True)
class PlanVersionIssued(DomainEvent):
    """Fired when a plan version transitions to issued."""

    tenant_id: uuid.UUID
    plan_version_id: uuid.UUID
