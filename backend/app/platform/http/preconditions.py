"""``ETag`` and ``If-Match`` for aggregates versioned by a counter — ADR-14.

🔒 **EC-M4-07.** Two practitioners in one clinic editing the same plan is not an
exotic case; it is Tuesday. Without a precondition the second save silently
discards the first one's work, with no error and no trace. The counter makes that
detectable, and detectable is the whole requirement.

⚠️ **Why a counter rather than ``updated_at``**, which is what
``routers/clients.py`` uses for clients. ``diet_plan_versions`` already carries
``row_version int`` because DB §8.11 put it there, and a plan is edited through
its *children* — a slot, an item, an alternative. Those writes must invalidate a
stale view of the parent, and bumping an integer on the parent is one statement
that also takes the row lock. Reusing ``updated_at`` would mean touching a column
nothing else reads and hoping every child path remembered to.

Both flavours coexist on purpose. A client is one row and its timestamp is
already maintained; a plan is a tree and its version is deliberate.
"""

from __future__ import annotations

from app.kernel.errors import PreconditionRequiredError


def etag_for_row_version(row_version: int) -> str:
    """The concurrency token for an aggregate at this revision.

    Weak (``W/``) because the representation is server-computed — two responses
    at the same ``row_version`` carry the same plan even if the JSON differs by a
    whitespace, and byte equality was never the claim.
    """
    return f'W/"{row_version}"'


def parse_if_match_row_version(raw: str | None, resource: str) -> int:
    """Recover the revision a caller is asserting they last saw.

    Args:
        raw: The ``If-Match`` header, or ``None`` when it was not sent.
        resource: What is being guarded, for the error payload.

    Returns:
        The integer revision the caller expects to be editing.

    Raises:
        PreconditionRequiredError: 428, when the header is absent or malformed.
            🔒 Refusing is the safe direction in both cases. An unparseable token
            treated as "no precondition" would silently downgrade the request to
            last-write-wins, which is the exact data loss this exists to prevent
            — and a missing one means the client never read the plan it is
            claiming to edit.
    """
    if raw is None:
        raise PreconditionRequiredError(resource)

    token = raw.strip()
    if token.startswith("W/"):
        token = token[2:]
    token = token.strip('"')

    try:
        return int(token)
    except ValueError as exc:
        raise PreconditionRequiredError(resource) from exc
