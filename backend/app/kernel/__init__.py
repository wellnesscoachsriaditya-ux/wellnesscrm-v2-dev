"""Kernel — cross-cutting capabilities.

🔒 Arch §3: the one module with dependencies pointing at it, not from it (R3).
Every domain module needs tenant, identity, authz, audit, events. None may
import another domain module.
"""

from app.kernel.db import Base

__all__ = ["Base"]
