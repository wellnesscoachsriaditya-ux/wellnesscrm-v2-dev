"""Tests that need a live PostgreSQL.

🔒 Separated from the unit suite because they have an external dependency the
rest of the tests deliberately do not. `pytest` collects them normally; each one
skips with an actionable message when no database is reachable, so the default
`pytest` run stays green on a machine with no PostgreSQL while making the gap
visible in the summary rather than silent.

⚠️ These are the only tests that can satisfy AC-M0-003. Everything in
`tests/test_kernel_schema.py` verifies that policies are *declared*; nothing
there proves PostgreSQL *enforces* them, because none of it runs SQL.
"""
