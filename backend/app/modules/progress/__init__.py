"""The `progress` module — adherence and engagement, PRD M7/M9, DB §12.

🔒 **Owner of ``adherence_logs``** (DB §12). The client portal writes through
:func:`log_adherence`; nothing else writes the table.

⚠️ This package exposes its whole public surface here, in ``__init__.py``,
because R2 forbids importing a module's internals: ``from app.modules.progress
import adherence`` is a boundary violation and the checker reports it as one.

⚠️ **This module must not import ``app.platform``** (Arch R5) and must not import
another module (R3). It receives the tenant, the client and the plan version as
arguments from the entry point that composes them.
"""

from __future__ import annotations

from app.modules.progress.adherence import (
    BACKDATING_WINDOW_DAYS,
    assert_within_window,
)
from app.modules.progress.adherence import log as log_adherence
from app.modules.progress.models import AdherenceLog, AdherenceValue

__all__ = [
    "BACKDATING_WINDOW_DAYS",
    "AdherenceLog",
    "AdherenceValue",
    "assert_within_window",
    "log_adherence",
]
