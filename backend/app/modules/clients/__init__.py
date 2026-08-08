"""The `clients` module — the client spine.

🔒 DB §5. **Owner of** ``clients`` and ``client_stage_history``. **Writers:**
this module only. Every other module reads client identity and stage through
``kernel.clients.ClientDirectory`` (Arch R3/R6).

⚠️ This package exposes its public surface here, in ``__init__.py``, because R2
forbids importing a module's internals: ``from app.modules.clients import
service`` is a boundary violation, and the checker reports it as one. Anything
another layer needs is re-exported below.

⚠️ **This module must not import ``app.platform``** (Arch R5). It has no session
factory, no logger and no settings of its own — those arrive as arguments from
the entry point that wires it. That is what keeps the domain logic testable
without the framework, and the framework replaceable without the domain.
"""

from __future__ import annotations

from app.modules.clients.actions import CLIENT_CREATE, CLIENT_READ, CLIENT_UPDATE
from app.modules.clients.directory import ClientRepositoryDirectory
from app.modules.clients.models import Client, ClientStageHistory
from app.modules.clients.service import (
    UNSET,
    ClientCreate,
    ClientUpdate,
    Unset,
    create_client,
    get_client,
    update_client,
)

__all__ = [
    "CLIENT_CREATE",
    "CLIENT_READ",
    "CLIENT_UPDATE",
    "UNSET",
    "Client",
    "ClientCreate",
    "ClientRepositoryDirectory",
    "ClientStageHistory",
    "ClientUpdate",
    "Unset",
    "create_client",
    "get_client",
    "update_client",
]
