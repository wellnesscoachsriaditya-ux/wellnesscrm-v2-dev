"""Nutrition module authorization actions (ADR-05)."""

from app.kernel.authz import (
    DataScope,
    register_action,
)
from app.kernel.context import UserRole

# Nutrition catalogue is accessible to all practitioners in a tenant.
# Unlike clinical data which is `owner_or_assigned`, reading the catalogue
# is just `tenant_member`. Creating custom foods is also `tenant_member`.

_PRACTITIONER = frozenset({UserRole.OWNER, UserRole.PRACTITIONER})

NUTRITION_CATALOGUE_READ = register_action(
    "nutrition.catalogue.read",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    policies=(),
    is_read=True,
)

NUTRITION_CATALOGUE_WRITE = register_action(
    "nutrition.catalogue.write",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    policies=(),
    is_read=False,
)

NUTRITION_PLANS_READ = register_action(
    "nutrition.plans.read",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    policies=(),
    is_read=True,
)

NUTRITION_PLANS_WRITE = register_action(
    "nutrition.plans.write",
    roles=_PRACTITIONER,
    data_scope=DataScope.TENANT_PII,
    policies=(),
    is_read=False,
)
