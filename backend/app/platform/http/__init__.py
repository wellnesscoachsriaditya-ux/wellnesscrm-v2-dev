"""HTTP layer — framework wiring only.

Arch §5.2: this package owns HTTP concerns and nothing else. 🔒 No domain logic
lives here. Routers declare their authorization action and call exactly one
service; the middleware pipeline establishes context, and the error handlers
translate the exception taxonomy into the approved response envelope.
"""
