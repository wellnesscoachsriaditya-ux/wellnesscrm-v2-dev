"""Supervisor entry-point shim.

The preview environment's supervisor launches ``uvicorn server:app``. This
project's real application factory lives in ``app.main``; this module simply
re-exports it so the platform's fixed launch command works unchanged.
"""

from app.main import app

__all__ = ["app"]
