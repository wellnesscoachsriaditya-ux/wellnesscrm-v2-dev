"""The enquiry form — reading it publicly, and editing it as a practitioner.

🔒 FR-M2-001, API §11.1 / §7.2. Two callers with opposite trust levels read this
module, and the functions are separated along that line rather than by CRUD verb:
:func:`load_public_form` runs with no tenant in scope and returns only what a
stranger may see; :func:`load_own_form` and :func:`update_form` run inside a
practitioner's session.

🔒 **The public read is the one query in the codebase that runs tenant-less**, and
it is safe for a specific reason rather than by convention — see
:func:`load_public_form` and migration 0015's ``_ENQUIRY_FORMS_POLICY``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.kernel.errors import NotFoundError, ValidationError
from app.kernel.leads import MAX_FORM_INTRO_LENGTH, MAX_FORM_TITLE_LENGTH
from app.kernel.models import Tenant
from app.modules.leads.models import EnquiryForm


def now() -> datetime:
    """Timezone-aware current time. Centralised so every row is UTC (NFR-099)."""
    return datetime.now(UTC)


#: 🔒 The title a tenant's form carries before the practitioner edits it.
#: Generic on purpose: it renders on a public page from the moment the account
#: exists, and a placeholder like "Untitled form" would be visible to prospects.
DEFAULT_FORM_TITLE = "Enquire about working together"


@dataclass(frozen=True, slots=True)
class PublicForm:
    """🔒 What an anonymous caller may see — API §11.1.

    ⚠️ **Deliberately not the ORM row.** `EnquiryForm` carries `tenant_id`,
    timestamps and `consent_notice_id`; none of those belong in a public response,
    and returning the row would make adding an internal column a disclosure. This
    is the allowlist, expressed as a type.

    ⚠️ ``tenant_id`` *is* present, because the submit endpoint needs it to set the
    tenant scope for its write. It is not serialised into the HTTP response —
    the router's own schema is the second allowlist.
    """

    form_id: uuid.UUID
    tenant_id: uuid.UUID
    #: The practice's name, so the page says who is being contacted.
    practice_name: str
    title: str
    intro_text: str | None


@dataclass(frozen=True, slots=True)
class OwnForm:
    """The practitioner's own view of their form — API §7.2."""

    id: uuid.UUID
    slug: str
    title: str
    intro_text: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    #: 🔒 The **tenant's** slug, which is what the public URL is keyed on
    #: (API §11.1) — not this form's own `slug`, which exists for Phase 2's
    #: multiple forms per tenant. Carried here so the router can build the
    #: shareable link server-side: US-M2-01's "a link I can put in my Instagram
    #: bio" is the feature, and a frontend assembling it from parts would need
    #: to know the URL shape, which is the server's to change.
    tenant_slug: str


async def load_public_form(session: AsyncSession, *, tenant_slug: str) -> PublicForm | None:
    """Resolve a shareable link to the form behind it — FR-M2-001, API §11.1.

    🔒 **Runs with no tenant in scope**, because resolving the slug is what
    *establishes* the tenant. The RLS policy admits this exact case
    (``current_tenant_id() IS NULL AND is_active``) and nothing wider: a
    practitioner's session always carries a tenant, so it can never take that
    branch and read another practice's form.

    What makes it safe is what the row is. API §11.1 calls the response
    "effectively public information" — a title, some intro prose and the practice
    name the practitioner chose to publish. No client data is on this table and
    there is no join from it to any.

    ⚠️ Returns ``None`` for an unknown slug, an inactive form, **and** a tenant
    with no form at all. The caller answers 404 identically for all three
    (API §11.1): distinguishing them would let a stranger learn which practices
    exist but have disabled enquiries, which is a fact about a business nobody
    asked us to publish.

    ⚠️ 🔒 **Suspended tenants are excluded here, not by the form's flag**
    (EC-M2-07). `is_active` is the practitioner's own switch; a suspended
    *account* must stop accepting enquiries regardless of what that switch says,
    and joining `tenants.status` is what makes the two independent.
    """
    result = await session.execute(
        select(
            EnquiryForm.id,
            EnquiryForm.tenant_id,
            Tenant.name,
            EnquiryForm.title,
            EnquiryForm.intro_text,
        )
        .join(Tenant, Tenant.id == EnquiryForm.tenant_id)
        .where(
            Tenant.slug == tenant_slug,
            # 🔒 In the query as well as the policy. The policy's `is_active`
            # covers the tenant-less path; this covers every path, including a
            # future authenticated preview that would arrive with a tenant set.
            EnquiryForm.is_active.is_(True),
            # 🔒 EC-M2-07 — an account that is not live accepts nothing.
            Tenant.status == "active",
        )
        .limit(1)
    )
    row = result.first()
    if row is None:
        return None

    return PublicForm(
        form_id=row[0],
        tenant_id=row[1],
        practice_name=row[2],
        title=row[3],
        intro_text=row[4],
    )


async def ensure_form(
    session: AsyncSession, *, tenant_id: uuid.UUID, practice_name: str
) -> uuid.UUID:
    """Return the tenant's active form id, creating it if there is none.

    🔒 **Created on first access rather than at registration**, and the choice is
    deliberate. Registration (S1) predates this module; adding a write there would
    put `leads` in the identity path and give `platform.identity` a reason to
    import a module (R5/R3). Creating on demand keeps the dependency pointing the
    right way, and the form's content is a default either way.

    ⚠️ Idempotent under the partial unique index
    (``uq_enquiry_forms__one_active_per_tenant``). Two concurrent first-time
    reads race; the loser's INSERT violates the index and it re-reads. That is
    the same pattern the codebase uses for tags, and it is why the index exists
    rather than a check-then-insert.
    """
    existing = await _active_form_id(session, tenant_id=tenant_id)
    if existing is not None:
        return existing

    form = EnquiryForm(
        tenant_id=tenant_id,
        title=DEFAULT_FORM_TITLE,
        intro_text=_default_intro(practice_name),
    )
    session.add(form)
    try:
        # ⚠️ A savepoint, so a concurrent creator's unique violation does not
        # poison the caller's transaction — the enquiry list must still render
        # after losing the race.
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        if "uq_enquiry_forms__one_active_per_tenant" not in str(exc):
            raise
        settled = await _active_form_id(session, tenant_id=tenant_id)
        if settled is None:  # pragma: no cover — the index guarantees one of the two
            raise
        return settled
    return form.id


async def _active_form_id(session: AsyncSession, *, tenant_id: uuid.UUID) -> uuid.UUID | None:
    result: uuid.UUID | None = await session.scalar(
        select(EnquiryForm.id).where(
            EnquiryForm.tenant_id == tenant_id, EnquiryForm.is_active.is_(True)
        )
    )
    return result


def _default_intro(practice_name: str) -> str:
    """The intro text a form carries until the practitioner rewrites it.

    ⚠️ Names the practice, because a prospect arriving from an Instagram link
    needs to know the form belongs to the person whose post they read.
    """
    return (
        f"Tell {practice_name} a little about what you would like help with, "
        "and they will get back to you."
    )


async def load_own_form(session: AsyncSession, *, tenant_id: uuid.UUID) -> OwnForm | None:
    """The tenant's own form, as the practitioner edits it — API §7.2."""
    result = await session.execute(
        select(EnquiryForm).where(
            EnquiryForm.tenant_id == tenant_id, EnquiryForm.is_active.is_(True)
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return _own(row, tenant_slug=await _tenant_slug(session, tenant_id=tenant_id))


def _own(row: EnquiryForm, *, tenant_slug: str) -> OwnForm:
    return OwnForm(
        id=row.id,
        slug=row.slug,
        title=row.title,
        intro_text=row.intro_text,
        is_active=row.is_active,
        created_at=row.created_at,
        updated_at=row.updated_at,
        tenant_slug=tenant_slug,
    )


async def _tenant_slug(session: AsyncSession, *, tenant_id: uuid.UUID) -> str:
    """The tenant's public slug — DB §4.1.

    ⚠️ Read here rather than carried on the form row. Duplicating it would create
    a second copy that drifts if a slug is ever changed, and `tenants.slug` is
    the one the public endpoint resolves against.
    """
    slug: str | None = await session.scalar(select(Tenant.slug).where(Tenant.id == tenant_id))
    if slug is None:  # pragma: no cover - a tenant always exists for a session
        raise _not_found()
    return slug


async def update_form(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    form_id: uuid.UUID,
    title: str | None = None,
    intro_text: str | None = None,
    intro_cleared: bool = False,
    is_active: bool | None = None,
) -> OwnForm:
    """Edit the form's presentation — FR-M2-001, API §7.2.

    Args:
        intro_cleared: 🔒 How "remove the intro" is distinguished from "leave it
            alone". Both arrive as ``intro_text=None`` in a partial update, and
            conflating them means a practitioner editing only the title silently
            loses their intro text. The router derives this from whether the key
            was present in the request body.

    🔒 **Deactivating is an update, never a delete.** Migration 0015 revokes
    DELETE: submissions reference the form, and removing it would orphan the
    evidence NFR-051 rests on.

    Raises:
        ValidationError: On an empty or over-long title.
        NotFoundError: If the form is not this tenant's.
    """
    values: dict[str, object] = {}

    if title is not None:
        trimmed = title.strip()
        if not trimmed:
            raise ValidationError(
                "Your form needs a heading.",
                action="Enter a short title prospects will see.",
                details={"field": "title"},
            )
        if len(trimmed) > MAX_FORM_TITLE_LENGTH:
            raise ValidationError(
                "That heading is too long.",
                action=f"Use {MAX_FORM_TITLE_LENGTH} characters or fewer.",
                details={"field": "title", "max_length": str(MAX_FORM_TITLE_LENGTH)},
            )
        values["title"] = trimmed

    if intro_cleared:
        values["intro_text"] = None
    elif intro_text is not None:
        trimmed_intro = intro_text.strip()
        if len(trimmed_intro) > MAX_FORM_INTRO_LENGTH:
            raise ValidationError(
                "That introduction is too long.",
                action=f"Use {MAX_FORM_INTRO_LENGTH} characters or fewer.",
                details={"field": "intro_text", "max_length": str(MAX_FORM_INTRO_LENGTH)},
            )
        values["intro_text"] = trimmed_intro or None

    if is_active is not None:
        values["is_active"] = is_active

    if not values:
        # Nothing to change. Re-read rather than erroring: a PATCH with no
        # effective change is not a client mistake worth a 422.
        current = await load_own_form(session, tenant_id=tenant_id)
        if current is None:
            raise _not_found()
        return current

    values["updated_at"] = now()

    result = await session.execute(
        update(EnquiryForm)
        .where(EnquiryForm.id == form_id, EnquiryForm.tenant_id == tenant_id)
        .values(**values)
        .returning(EnquiryForm)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise _not_found()
    return _own(row, tenant_slug=await _tenant_slug(session, tenant_id=tenant_id))


def _not_found() -> NotFoundError:
    return NotFoundError(
        "That enquiry form could not be found.",
        action="Reload the page and try again.",
    )
