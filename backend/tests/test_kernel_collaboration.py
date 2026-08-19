"""The rules of collaboration — notes, tags and shared access.

🔒 No database. ``kernel.collaboration`` is split from the persistence in
``app.modules.clients`` so every rule below is a pure function over values, and
these are the rules that decide who may rewrite a colleague's words and whether
two spellings of a tag are the same tag.

Three groups:

* **Notes** — FR-M1-007's shape, and FR-M3-020's author rule, whose interesting
  case is the tenant owner.
* **Tags** — FR-M1-008. Generous about input, strict about what counts as a
  duplicate.
* **Access** — EC-M0-04 and FR-M0-017, the predicates behind AC-M1-006.
"""

from __future__ import annotations

import uuid

import pytest

from app.kernel.collaboration import (
    MAX_NOTE_LENGTH,
    MAX_TAG_NAME_LENGTH,
    TagColour,
    assert_grant_is_meaningful,
    assert_may_archive_note,
    assert_may_edit_note,
    may_manage_access,
    tag_match_key,
    validate_note_body,
    validate_tag_name,
)
from app.kernel.context import UserRole
from app.kernel.errors import ValidationError

AUTHOR = uuid.UUID("11111111-0000-4000-8000-000000000001")
COLLEAGUE = uuid.UUID("11111111-0000-4000-8000-000000000002")


# ─── Notes (FR-M1-007, FR-M3-020) ────────────────────────────────────────


def test_a_note_is_trimmed() -> None:
    assert validate_note_body("  Discussed portion sizes.  ") == "Discussed portion sizes."


@pytest.mark.parametrize("body", ["", "   ", "\n\t "])
def test_an_empty_note_is_refused(body: str) -> None:
    """A blank note is a mis-click, and saving one puts an empty row in a thread
    a practitioner reads before a consultation."""
    with pytest.raises(ValidationError):
        validate_note_body(body)


def test_an_oversized_note_is_refused() -> None:
    """⚠️ The limit guards against a pasted document, not against detail. The
    error says how to proceed rather than just refusing."""
    with pytest.raises(ValidationError) as excinfo:
        validate_note_body("x" * (MAX_NOTE_LENGTH + 1))

    assert excinfo.value.details["max_length"] == MAX_NOTE_LENGTH
    assert "split it into two" in excinfo.value.action


def test_only_the_author_may_edit_a_note() -> None:
    """🔒 FR-M3-020 — "editable by their author"."""
    assert_may_edit_note(
        author_user_id=AUTHOR, actor_user_id=AUTHOR, actor_role=UserRole.PRACTITIONER
    )

    with pytest.raises(ValidationError):
        assert_may_edit_note(
            author_user_id=AUTHOR, actor_user_id=COLLEAGUE, actor_role=UserRole.PRACTITIONER
        )


def test_even_the_owner_may_not_edit_someone_elses_note() -> None:
    """🔒 The case that makes FR-M3-020 mean something.

    A note carries an author's name. An owner rewriting its body would put words
    in a practitioner's mouth under that practitioner's name — so the owner's
    remedy is to archive it, which is recorded as *their* act.
    """
    with pytest.raises(ValidationError):
        assert_may_edit_note(
            author_user_id=AUTHOR, actor_user_id=COLLEAGUE, actor_role=UserRole.OWNER
        )


def test_the_owner_may_archive_someone_elses_note() -> None:
    """The counterpart: wider than editing, deliberately.

    ⚠️ Both directions matter. An owner who could not remove a note posted in
    error would have no remedy at all, and the only alternative — editing it —
    is the one thing they must not do.
    """
    assert_may_archive_note(
        author_user_id=AUTHOR, actor_user_id=COLLEAGUE, actor_role=UserRole.OWNER
    )
    assert_may_archive_note(
        author_user_id=AUTHOR, actor_user_id=AUTHOR, actor_role=UserRole.PRACTITIONER
    )

    with pytest.raises(ValidationError):
        assert_may_archive_note(
            author_user_id=AUTHOR, actor_user_id=COLLEAGUE, actor_role=UserRole.PRACTITIONER
        )


# ─── Tags (FR-M1-008) ────────────────────────────────────────────────────


def test_a_tag_name_is_trimmed_and_internally_collapsed() -> None:
    """A doubled space is a typo, not an error worth refusing a save over."""
    assert validate_tag_name("  Weight   loss  ") == "Weight loss"


def test_a_tag_name_keeps_its_capitalisation() -> None:
    """🔒 Case is display information. A practitioner who typed "PCOS" should see
    "PCOS", even though it matches "pcos" for uniqueness."""
    assert validate_tag_name("PCOS") == "PCOS"


@pytest.mark.parametrize("name", ["", "   "])
def test_an_empty_tag_name_is_refused(name: str) -> None:
    with pytest.raises(ValidationError):
        validate_tag_name(name)


def test_an_oversized_tag_name_is_refused() -> None:
    with pytest.raises(ValidationError):
        validate_tag_name("x" * (MAX_TAG_NAME_LENGTH + 1))


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("PCOS", "pcos"),
        ("Weight loss", "weight  loss"),
        ("  Diabetes ", "diabetes"),
    ],
)
def test_tags_that_differ_only_in_case_or_spacing_are_the_same_tag(left: str, right: str) -> None:
    """🔒 The rule ``uq_tags__tenant_name`` enforces.

    Without it a practitioner ends up with "PCOS" and "pcos" as two labels that
    look identical in a filter list, and their caseload is split across both.
    """
    assert tag_match_key(left) == tag_match_key(right)


def test_genuinely_different_tags_do_not_collide() -> None:
    assert tag_match_key("PCOS") != tag_match_key("PCOD")


def test_the_palette_is_closed() -> None:
    """🔒 ADR-03 / NFR-060 — a named palette rather than free hex.

    Every value maps to a design token whose contrast is asserted. A practitioner
    picking their own hex could produce a tag nobody can read, and no test
    anywhere would catch it.
    """
    assert TagColour.SLATE in set(TagColour)
    assert len(set(TagColour)) == 8


# ─── Shared access (EC-M0-04, FR-M0-017) ─────────────────────────────────


def test_granting_to_the_owner_is_refused() -> None:
    """🔒 A grant that changes nothing but looks like an action.

    Left permitted, it produces a row implying shared care that does not exist —
    and revoking that row would appear to remove access it never conferred.
    """
    with pytest.raises(ValidationError):
        assert_grant_is_meaningful(owner_user_id=AUTHOR, grantee_user_id=AUTHOR)


def test_granting_to_a_colleague_is_permitted() -> None:
    assert_grant_is_meaningful(owner_user_id=AUTHOR, grantee_user_id=COLLEAGUE)


@pytest.mark.parametrize(
    ("role", "permitted"),
    [
        (UserRole.OWNER, True),
        (UserRole.PRACTITIONER, False),
        (UserRole.CLIENT, False),
        (UserRole.PLATFORM_OPERATOR, False),
        (None, False),
    ],
)
def test_only_the_owner_manages_access(role: UserRole | None, permitted: bool) -> None:
    """🔒 FR-M0-017 — the owner is the one role with a view of the whole tenant,
    and access-granting is the operation that widens that view.

    Parametrised over every role rather than spot-checked: the failure this
    guards against is a *new* role landing on the permitted side unnoticed.
    """
    assert may_manage_access(role) is permitted
