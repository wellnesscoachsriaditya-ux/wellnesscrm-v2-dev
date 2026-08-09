"""Collaboration around a client — notes, tags and shared access.

🔒 DB §5.4–5.5, FR-M1-007/008, FR-M3-020/021, EC-M0-04. Three concepts that hang
off the spine rather than being part of it, which is why they live here and not
in ``kernel.clients``: a note is written *about* a client by a practitioner, a
tag is a tenant's own vocabulary applied *to* clients, and an assignment is a
statement about *who may see* one.

As with ``kernel.clients``, this module holds rules only — pure functions over
values, testable without a database. Persistence lives in
``app.modules.clients``.

🔒 **Notes are never client-visible** (FR-M3-021). That is enforced three ways,
and the redundancy is deliberate: the client realm has no route to them, the
authorization actions permit only practitioner roles, and ``client_notes`` has
**no client-realm RLS policy at all** (DB §17.1 — "enforced by the *absence* of a
policy, which is stronger than a condition"). Nothing in this file can enforce
it; it is recorded here because this is where a reader looks for the rule.
"""

from __future__ import annotations

import enum
import re
import uuid

from app.kernel.context import UserRole
from app.kernel.errors import ValidationError

# ─── Notes (FR-M1-007, FR-M3-020) ────────────────────────────────────────

#: 🔒 Longest note body. Generous — a consultation summary is prose, and a limit
#: that clipped one would lose clinical context the practitioner meant to keep.
#:
#: ⚠️ It is a guard against a pasted document rather than a product rule: a note
#: is loaded with its client and rendered in a thread, so an unbounded column
#: turns one screen into an unbounded response. Structured clinical records get
#: their own tables in S3 (``consultation_notes``, DB §7.6).
MAX_NOTE_LENGTH = 5_000


def validate_note_body(body: str) -> str:
    """Trim and check a note — FR-M1-007.

    Raises:
        ValidationError: If the note is empty or over :data:`MAX_NOTE_LENGTH`.
    """
    trimmed = body.strip()
    if not trimmed:
        raise ValidationError(
            "A note needs some text.",
            action="Write what you want to record, then save.",
        )
    if len(trimmed) > MAX_NOTE_LENGTH:
        raise ValidationError(
            "That note is too long.",
            action=f"Use {MAX_NOTE_LENGTH:,} characters or fewer, or split it into two notes.",
            details={"max_length": MAX_NOTE_LENGTH, "length": len(trimmed)},
        )
    return trimmed


def assert_may_edit_note(
    *, author_user_id: uuid.UUID, actor_user_id: uuid.UUID, actor_role: UserRole | None
) -> None:
    """🔒 FR-M3-020 — a note is editable **by its author**, and by nobody else.

    ⚠️ The tenant owner is refused too, and that is the interesting case. An
    owner may archive a colleague's note (see :func:`assert_may_archive_note`) —
    managing the practice's records is their job — but rewriting its body would
    put words in a practitioner's mouth under that practitioner's name. The note
    carries an author, so the author must be the only one who can change what it
    says.

    Raises:
        ValidationError: If the actor is not the author. 🔒 Not an
            ``AuthorizationError``: the actor legitimately reaches this note and
            may read and archive it, so this is a rule about the *operation*
            rather than about access, and a 403 would misdescribe it.
    """
    if author_user_id != actor_user_id:
        raise ValidationError(
            "Only the practitioner who wrote a note can edit it.",
            action="Add your own note instead, or ask them to change theirs.",
            details={"actor_role": actor_role.value if actor_role else None},
        )


def assert_may_archive_note(
    *, author_user_id: uuid.UUID, actor_user_id: uuid.UUID, actor_role: UserRole | None
) -> None:
    """Whether this actor may remove a note from the thread.

    🔒 The author, or the tenant owner. Wider than editing on purpose: the owner
    is accountable for what the practice records and must be able to remove
    something posted in error or against policy, which is a different act from
    silently altering its content.

    Raises:
        ValidationError: If the actor is neither the author nor the owner.
    """
    if actor_role is UserRole.OWNER or author_user_id == actor_user_id:
        return
    raise ValidationError(
        "Only the practitioner who wrote a note, or the account owner, can remove it.",
        action="Ask them to remove it.",
    )


# ─── Tags (FR-M1-008) ────────────────────────────────────────────────────


class TagColour(enum.StrEnum):
    """The palette a tag may use — DB §5.4 ("``color`` for UI").

    🔒 **A named palette rather than free hex, deliberately.** Three reasons, in
    order of weight:

    1. **Contrast is a guarantee, not a hope.** Every value here maps to a design
       token whose contrast is asserted by ``tokens/contrast.test.ts`` (NFR-060).
       A practitioner picking ``#ffff00`` would produce a tag nobody can read,
       and no test anywhere would catch it.
    2. ADR-03 — raw colour values live in the design system and nowhere else.
       Free hex in the database is that rule broken through the back door.
    3. A closed set survives a theme change. Hex does not: a dark mode added
       later has to re-interpret every value a user ever chose.

    ⚠️ 🟡 The *set* is PROPOSED. DB §5.4 names the column and stops there, so
    these eight are my choice — enough to group a caseload without becoming a
    decision the practitioner has to make carefully.
    """

    SLATE = "slate"
    RED = "red"
    AMBER = "amber"
    GREEN = "green"
    TEAL = "teal"
    BLUE = "blue"
    VIOLET = "violet"
    PINK = "pink"


#: 🔒 Longest tag name. Short by intent: a tag is a label a practitioner scans in
#: a list, and anything longer is a note wearing a label's clothes.
MAX_TAG_NAME_LENGTH = 40

#: Characters that carry no meaning in a tag but break matching — collapsed
#: rather than rejected, because a doubled space is a typo, not an error worth
#: refusing a save over.
_WHITESPACE_RUN = re.compile(r"\s+")


def validate_tag_name(name: str) -> str:
    """Normalise a tag name for storage — FR-M1-008.

    Trims, collapses internal whitespace, and preserves the practitioner's
    capitalisation. ⚠️ Case is **display** information: "PCOS" and "pcos" are the
    same tag (see :func:`tag_match_key`) but a practitioner who typed "PCOS"
    should see "PCOS".

    Raises:
        ValidationError: If the name is empty or too long.
    """
    trimmed = _WHITESPACE_RUN.sub(" ", name.strip())
    if not trimmed:
        raise ValidationError(
            "A tag needs a name.",
            action="Enter a short label, like “PCOS” or “Weight loss”.",
        )
    if len(trimmed) > MAX_TAG_NAME_LENGTH:
        raise ValidationError(
            "That tag name is too long.",
            action=f"Use {MAX_TAG_NAME_LENGTH} characters or fewer.",
            details={"max_length": MAX_TAG_NAME_LENGTH},
        )
    return trimmed


def tag_match_key(name: str) -> str:
    """The value uniqueness is decided on — DB §5.4 ``uq_tags__tenant_name``.

    🔒 Case-insensitive. A practitioner who has a "PCOS" tag and types "pcos"
    means the tag they already have; creating a second one would split their own
    caseload across two labels that look identical in a filter list.

    ⚠️ ``casefold`` rather than ``lower``. It handles the cases ``lower`` gets
    wrong — the product ships in a market with four scripts in common use, and
    Unicode has pairs where lowercasing is not enough to make two spellings of
    the same word compare equal.

    The database enforces this with a unique index on the same expression;
    matching them is what stops the application and the constraint disagreeing.
    """
    return _WHITESPACE_RUN.sub(" ", name.strip()).casefold()


# ─── Shared access (EC-M0-04, FR-M0-017) ─────────────────────────────────


def assert_grant_is_meaningful(*, owner_user_id: uuid.UUID, grantee_user_id: uuid.UUID) -> None:
    """Refuse a grant that would change nothing — EC-M0-04.

    🔒 Granting a client to their own owning practitioner is a no-op that
    *looks* like an action. Left permitted, it produces a row implying shared
    care that does not exist, and a revoke of that row would appear to remove
    access it never conferred — which is how somebody eventually concludes the
    grant model is broken.

    Raises:
        ValidationError: If the grantee already owns the client.
    """
    if owner_user_id == grantee_user_id:
        raise ValidationError(
            "That practitioner already owns this client.",
            action="They have full access already — no grant is needed.",
        )


def may_manage_access(actor_role: UserRole | None) -> bool:
    """Whether this role may grant or revoke access to a client.

    🔒 **Owner only, at MVP.** FR-M0-017 makes the owner the one role with a view
    of the whole tenant, and access-granting is the operation that widens that
    view — concentrating it in the role that already has it keeps "who can see
    this client" answerable by asking one person.

    ⚠️ A practitioner sharing *their own* client with a colleague is a real
    workflow and is deliberately not supported yet. It needs a rule for who may
    then revoke, and getting that wrong strands a client with access nobody
    intended. Left for when a clinic actually asks for it.
    """
    return actor_role is UserRole.OWNER
