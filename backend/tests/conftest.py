"""Session-wide test setup.

🔒 **Importing the application must happen before any fixture snapshots the
action registry.**

`kernel.authz.REGISTRY` is populated at *import* time — a module declaring an
action registers it when Python first executes the module body. Several test
modules snapshot the registry, clear it, and restore the snapshot afterwards, so
that a test's own `register_action` calls cannot leak into another test.

Those two facts combine badly. If the first import of a route module happens
*inside* a test whose fixture has already cleared the registry, the module's
import-time registration lands in the cleared registry — and the teardown then
restores a snapshot that never contained it. The action is gone for the rest of
the session, and every later test that builds the real application fails a
startup check with `session.end` missing.

⚠️ The symptom is order-dependent and looks nothing like the cause: an unrelated
test file fails, but only when run after another one. Importing here, at
collection time, means every import-time registration has happened before the
first fixture runs.
"""

from __future__ import annotations

# Imported for the side effect described above. The application module graph —
# including every router that declares an authorization action — is loaded once,
# at collection, outside any fixture.
import app.main as _app_main

__all__ = ["_app_main"]
