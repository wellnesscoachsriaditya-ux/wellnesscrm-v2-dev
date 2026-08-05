"""Kernel database base and tenant isolation primitives.

🔒 Arch §3.1 R5: platform/ imports kernel, never the reverse. `Base` lives here
so every ORM model can subclass it without creating a boundary violation.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import DateTime, Enum
from sqlalchemy.orm import DeclarativeBase


def pg_enum(python_enum: type[enum.Enum], name: str) -> Enum:
    """Build a PostgreSQL ENUM that stores *values*, not member names.

    🔒 Two defaults in SQLAlchemy are wrong for this schema, and both fail
    silently rather than loudly:

    1. **The type name.** ``Enum(UserRole)`` names the PostgreSQL type
       ``userrole``. DB §1 requires singular ``snake_case`` (``user_role``), and
       a type name is not something a later migration can rename cheaply.

    2. **The stored representation.** SQLAlchemy persists the member *name*
       (``OWNER``), not its value (``owner``). Every ``server_default`` in DB §4
       is written as a value, so the default and the type would disagree: the
       column would accept ``'OWNER'`` from the ORM while its own default
       ``'owner'`` was rejected by the type. ``values_callable`` fixes this at
       the single place enums are constructed.
    """
    return Enum(
        python_enum,
        name=name,
        values_callable=lambda members: [member.value for member in members],
    )


class Base(DeclarativeBase):
    """Declarative base for all ORM models.

    🔒 Arch R6 — a module's models must not reference another module's tables.
    Enforced by ``tools/check_boundaries.py``, not by convention.
    """

    # 🔒 NFR-099 — every timestamp is `timestamptz`, set once here rather than
    # per column. SQLAlchemy's default for `Mapped[datetime]` is a *naive*
    # `TIMESTAMP WITHOUT TIME ZONE`, which is the wrong default for a product
    # whose tenants each declare their own timezone: a naive column silently
    # records "some wall clock somewhere" and comparisons across tenants become
    # meaningless. Fixing it in the type map means no model can get it wrong.
    #
    # `dict[Any, Any]` matches SQLAlchemy's own signature for this attribute:
    # the mapping is heterogeneous by nature (Python type → SQL type), and a
    # narrower annotation is rejected because `DateTime` is `TypeEngine[datetime]`
    # rather than `TypeEngine[object]`.
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: DateTime(timezone=True)}
