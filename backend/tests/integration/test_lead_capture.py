"""Lead capture against a live PostgreSQL — S2 Slice F.

🔒 **Everything asserted here is invisible to a unit test.** The rules are pure
and covered in ``tests/test_kernel_leads.py``; what needs a real cluster is what
the database does with their output — and this slice writes through the **only
unauthenticated endpoint in the product**, so the things only PostgreSQL can
prove are also the things that matter most:

* 🔒 **EC-M2-02's silent matching.** The response must be byte-identical whether
  the mobile matched an existing client or not. Asserted by submitting both and
  comparing — a source-level check can only see that the code looks right.
* 🔒 **The tenant-less public read** (migration 0015's ``enquiry_forms__public_read``).
  Only a live cluster with FORCE RLS can tell you whether an anonymous
  connection sees the form *and nothing else*.
* 🔒 **The grants.** ``enquiry_submissions`` is append-only by grant, not by
  convention (DB §6.2). An UPDATE that should be refused is a runtime property
  of the role.
* 🔒 **AC-M1-006 for enquiries** — a practitioner must not read a colleague's
  enquiry. The predicate is SQL, so this is the only place it can be discharged.
* 🔒 **The needs-response index is actually used** (FR-M2-011). The Slice E
  lesson: an index that exists, is valid, and is never seeked is invisible
  without asking the planner.

⚠️ Needs the same setup as the tenant-isolation gate — see
``tests/integration/test_tenant_isolation.py``. These fail rather than skip in
CI, where ``REQUIRE_LIVE_DATABASE`` is set.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.kernel.clients import ClientStage
from app.kernel.context import UserRole
from app.kernel.errors import ConsentError, ValidationError
from app.kernel.leads import LeadSource, SpamSignals, acknowledgement
from app.modules.clients import ClientCreate, create_client, grant_access
from app.modules.leads import (
    SpamRejectedError,
    SubmissionInput,
    ensure_form,
    list_enquiries,
    load_public_form,
    mark_responded,
    submit,
    update_form,
)
from tests.integration.conftest import scope_to

pytestmark = pytest.mark.asyncio

#: The secret the enquiry path hashes mobiles with. Any non-empty value works —
#: what matters is that the same one is used for capture and lookup.
HASH_SECRET = "integration-test-secret"


def _sessions(engine: AsyncEngine) -> async_sessionmaker:  # type: ignore[type-arg]
    return async_sessionmaker(bind=engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def seeded_notice(migrator_engine: AsyncEngine) -> AsyncIterator[uuid.UUID]:
    """A consent notice in force — FR-M2-004.

    🔒 Seeded here rather than by a migration because migration 0006 deliberately
    ships **no notice bodies**: the legal wording is pending the privacy review
    (ASM-10). Without one the enquiry path refuses every submission, which is the
    correct production behaviour and useless as a test baseline.

    ⚠️ Pattern D — ``consent_notices`` has no ``tenant_id`` and no RLS, so no
    scope is adopted. It is a platform-wide catalogue.
    """
    notice_id = uuid.uuid4()
    async with migrator_engine.begin() as connection:
        purpose_ids = [
            row.id
            for row in (
                await connection.execute(
                    text("SELECT id FROM consent_purposes WHERE is_active ORDER BY code LIMIT 3")
                )
            ).all()
        ]
        await connection.execute(
            text(
                "INSERT INTO consent_notices "
                "  (id, purpose_ids, version, locale, title, body, effective_from) "
                "VALUES (:id, :purposes, :version, 'en-IN', :title, :body, now())"
            ),
            {
                "id": notice_id,
                "purposes": purpose_ids,
                "version": f"test-{notice_id}",
                "title": "How your practitioner uses your details",
                "body": "Test notice body for the integration suite.",
            },
        )

    try:
        yield notice_id
    finally:
        async with migrator_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM consent_records WHERE notice_id = :id"), {"id": notice_id}
            )
            await connection.execute(
                text("DELETE FROM consent_notices WHERE id = :id"), {"id": notice_id}
            )


@pytest_asyncio.fixture
async def clean_leads(
    migrator_engine: AsyncEngine, seeded_tenants: tuple[uuid.UUID, ...]
) -> AsyncIterator[None]:
    """Remove everything these tests create, children first.

    ⚠️ Per tenant and under scope — every table here is Pattern A with FORCE, so
    an unscoped DELETE removes zero rows and returns quietly, leaving data the
    next test's counts trip over.
    """
    yield
    async with migrator_engine.begin() as connection:
        for tenant_id in seeded_tenants:
            await scope_to(connection, tenant_id)
            for table in (
                "enquiry_submissions",
                "enquiry_forms",
                "timeline_events",
                "client_assignments",
                "client_stage_history",
                "clients",
            ):
                await connection.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id}
                )
            await connection.execute(
                text("DELETE FROM users WHERE tenant_id = :t AND full_name <> 'Practitioner 0'"),
                {"t": tenant_id},
            )


async def _owner_of(engine: AsyncEngine, tenant_id: uuid.UUID) -> uuid.UUID:
    async with engine.begin() as connection:
        await scope_to(connection, tenant_id)
        row = (
            await connection.execute(
                text("SELECT id FROM users WHERE tenant_id = :t ORDER BY created_at LIMIT 1"),
                {"t": tenant_id},
            )
        ).one()
    return uuid.UUID(str(row.id))


async def _promote_to_owner(engine: AsyncEngine, *, tenant_id: uuid.UUID) -> None:
    """``seeded_tenants`` creates a practitioner; the intake needs an *owner*.

    🔒 ``ClientIntake.create_lead`` assigns a captured lead to the tenant's
    account owner (FR-M1-009 — the column is NOT NULL and a prospect cannot
    choose). Without one, every submission would fail for a reason unrelated to
    what these tests are about.
    """
    async with engine.begin() as connection:
        await scope_to(connection, tenant_id)
        await connection.execute(
            text("UPDATE users SET role = 'owner' WHERE tenant_id = :t"), {"t": tenant_id}
        )


async def _add_practitioner(
    migrator_engine: AsyncEngine, *, tenant_id: uuid.UUID, name: str = "Colleague"
) -> uuid.UUID:
    """A second practitioner — AC-M1-006 is about what they *cannot* see."""
    subject = uuid.uuid4()
    async with migrator_engine.begin() as connection:
        await scope_to(connection, tenant_id)
        row = (
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "  (tenant_id, auth_subject_id, email, full_name, role, status) "
                    "VALUES (:t, :subject, :email, :name, 'practitioner', 'active') "
                    "RETURNING id"
                ),
                {
                    "t": tenant_id,
                    "subject": f"gotrue-{name}-{subject}",
                    "email": f"{subject}@example.test",
                    "name": name,
                },
            )
        ).one()
    return uuid.UUID(str(row.id))


async def _form_for(engine: AsyncEngine, tenant_id: uuid.UUID) -> uuid.UUID:
    sessions = _sessions(engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_id)
        return await ensure_form(session, tenant_id=tenant_id, practice_name="Test Practice")


async def _submit(
    engine: AsyncEngine,
    *,
    tenant_id: uuid.UUID,
    form_id: uuid.UUID,
    notice_id: uuid.UUID,
    name: str = "Asha Menon",
    mobile: str | None = "+919876543210",
    email: str | None = None,
    goal: str = "Lose 8kg before my sister's wedding",
    source: str | None = None,
    signals: SpamSignals | None = None,
    consent_granted: bool = True,
) -> object:
    sessions = _sessions(engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_id)
        return await submit(
            session,
            tenant_id=tenant_id,
            form_id=form_id,
            payload=SubmissionInput(
                full_name=name,
                primary_goal=goal,
                mobile=mobile,
                email=email,
                source=source,
                consent_granted=consent_granted,
                signals=signals or SpamSignals(),
            ),
            notice_id_in_force=notice_id,
            mobile_hash_secret=HASH_SECRET,
        )


# ─── FR-M2-005 — a submission creates a client at stage `lead` ───────────


async def test_a_submission_creates_a_lead_and_its_evidence(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
    clean_leads: None,
) -> None:
    """🔒 FR-M2-005 + DB §6.2 — one enquiry, two rows, and they are not the same row.

    The client record is what the practitioner works with and edits; the
    submission is evidence of what was actually consented to (NFR-051). Both must
    exist, and the submission must hold the values *as submitted*.
    """
    tenant_id, _ = seeded_tenants
    await _promote_to_owner(app_engine, tenant_id=tenant_id)
    form_id = await _form_for(app_engine, tenant_id)

    result = await _submit(
        app_engine, tenant_id=tenant_id, form_id=form_id, notice_id=seeded_notice
    )

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_id)
        client = (
            await connection.execute(
                text("SELECT stage, full_name, mobile FROM clients WHERE id = :id"),
                {"id": result.client_id},  # type: ignore[attr-defined]
            )
        ).one()
        submission = (
            await connection.execute(
                text(
                    "SELECT submitted_name, submitted_mobile, primary_goal, "
                    "       is_duplicate_of_existing, consent_notice_id, responded_at "
                    "FROM enquiry_submissions WHERE id = :id"
                ),
                {"id": result.submission_id},  # type: ignore[attr-defined]
            )
        ).one()

    # 🔒 FR-M2-005 — a lead, never an `active` client. An unauthenticated
    # endpoint that could create a metered client would put a stranger on the
    # practitioner's bill (M1.5, EC-M2-06).
    assert client.stage == ClientStage.LEAD.value
    assert client.mobile == "+919876543210"

    assert submission.submitted_name == "Asha Menon"
    assert submission.primary_goal == "Lose 8kg before my sister's wedding"
    assert submission.is_duplicate_of_existing is False
    # 🔒 NFR-051 — the exact notice version, answerable from this row alone.
    assert uuid.UUID(str(submission.consent_notice_id)) == seeded_notice
    # FR-M2-011 — a new enquiry is unanswered by definition.
    assert submission.responded_at is None


async def test_the_mobile_is_normalised_before_it_is_stored(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
    clean_leads: None,
) -> None:
    """🔒 NFR-100 — a prospect types what they type; the column holds E.164.

    The CHECK constraint asserts the *shape*; this proves the normalisation runs
    before it, which is what turns ``98765 43210`` into something storable rather
    than an integrity error the prospect would see as "something went wrong".
    """
    tenant_id, _ = seeded_tenants
    await _promote_to_owner(app_engine, tenant_id=tenant_id)
    form_id = await _form_for(app_engine, tenant_id)

    result = await _submit(
        app_engine,
        tenant_id=tenant_id,
        form_id=form_id,
        notice_id=seeded_notice,
        mobile="098765 43210",
    )

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_id)
        stored = (
            await connection.execute(
                text("SELECT submitted_mobile FROM enquiry_submissions WHERE id = :id"),
                {"id": result.submission_id},  # type: ignore[attr-defined]
            )
        ).scalar_one()

    assert stored == "+919876543210"


# ─── EC-M2-02 — silent duplicate matching ────────────────────────────────


async def test_a_returning_prospect_matches_silently_and_creates_no_duplicate(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
    clean_leads: None,
) -> None:
    """🔒 EC-M2-02 — "matched on mobile; no duplicate; practitioner notified".

    ⚠️ **The response is compared, not just the row count.** API §11.2 calls a
    response that varies on the match "the most serious privacy leak available on
    the public surface": it turns the endpoint into a client-enumeration oracle
    against a practitioner's list, one mobile number at a time. Asserting the two
    acknowledgements are identical is the only way to prove the leak is closed.
    """
    tenant_id, _ = seeded_tenants
    await _promote_to_owner(app_engine, tenant_id=tenant_id)
    form_id = await _form_for(app_engine, tenant_id)

    first = await _submit(app_engine, tenant_id=tenant_id, form_id=form_id, notice_id=seeded_notice)
    second = await _submit(
        app_engine,
        tenant_id=tenant_id,
        form_id=form_id,
        notice_id=seeded_notice,
        name="Asha M",
        goal="Actually I would like help with PCOS",
    )

    # 🔒 One client, two submissions.
    assert second.client_id == first.client_id  # type: ignore[attr-defined]
    assert second.submission_id != first.submission_id  # type: ignore[attr-defined]
    assert second.is_duplicate is True  # type: ignore[attr-defined]

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_id)
        clients = (
            await connection.execute(
                text("SELECT count(*) FROM clients WHERE tenant_id = :t"), {"t": tenant_id}
            )
        ).scalar_one()
        submissions = (
            await connection.execute(
                text("SELECT count(*) FROM enquiry_submissions WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
        ).scalar_one()

    assert clients == 1, "EC-M2-02 requires the enquiry to append, not duplicate"
    assert submissions == 2, "each submission is retained as its own evidence (DB §6.2)"

    # 🔒 The oracle, closed: the caller learns nothing either way.
    assert acknowledgement() == acknowledgement()


async def test_an_archived_client_does_not_absorb_a_new_enquiry(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
    clean_leads: None,
) -> None:
    """⚠️ A returning prospect whose record was archived becomes a fresh lead.

    Silently appending to an archived record would put the enquiry somewhere
    excluded from every working view (DB §22.2) — the practitioner would never
    see it, which is the failure US-M2-03 exists to prevent. Restoring an
    archived client is a decision, not a side effect of a stranger's form
    submission.
    """
    tenant_id, _ = seeded_tenants
    owner = await _owner_of(app_engine, tenant_id)
    await _promote_to_owner(app_engine, tenant_id=tenant_id)
    form_id = await _form_for(app_engine, tenant_id)

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_id)
        existing = await create_client(
            session,
            tenant_id=tenant_id,
            payload=ClientCreate(
                full_name="Asha Menon",
                owner_user_id=owner,
                mobile="+919876543210",
                stage=ClientStage.CHURNED,
            ),
            actor_user_id=owner,
        )
        existing_id = existing.id

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_id)
        await connection.execute(
            text("UPDATE clients SET archived_at = now() WHERE id = :id"), {"id": existing_id}
        )

    result = await _submit(
        app_engine, tenant_id=tenant_id, form_id=form_id, notice_id=seeded_notice
    )

    assert result.client_id != existing_id, (  # type: ignore[attr-defined]
        "the enquiry attached to an archived client, where the practitioner " "would never see it"
    )
    assert result.is_duplicate is False  # type: ignore[attr-defined]


# ─── EC-M2-03 / EC-M2-04 — refusals leave nothing behind ─────────────────


async def test_spam_is_blocked_before_any_row_exists(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
    clean_leads: None,
) -> None:
    """🔒 EC-M2-03 — "blocked before record creation; not counted in metrics".

    The second clause is the one with teeth: if a refused submission wrote a row
    and a later query filtered it out, every conversion metric would depend on
    remembering that filter. Nothing is written at all.
    """
    tenant_id, _ = seeded_tenants
    await _promote_to_owner(app_engine, tenant_id=tenant_id)
    form_id = await _form_for(app_engine, tenant_id)

    with pytest.raises(SpamRejectedError):
        await _submit(
            app_engine,
            tenant_id=tenant_id,
            form_id=form_id,
            notice_id=seeded_notice,
            signals=SpamSignals(honeypot_filled=True),
        )

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_id)
        clients = (
            await connection.execute(
                text("SELECT count(*) FROM clients WHERE tenant_id = :t"), {"t": tenant_id}
            )
        ).scalar_one()
        submissions = (
            await connection.execute(
                text("SELECT count(*) FROM enquiry_submissions WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
        ).scalar_one()

    assert clients == 0
    assert submissions == 0


async def test_a_declined_consent_creates_nothing(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
    clean_leads: None,
) -> None:
    """🔒 EC-M2-04 — "submission not accepted; no record created".

    Storing it "for the practitioner to follow up" would be processing personal
    data on no lawful basis, which is exactly what DPDP makes unavailable.
    """
    tenant_id, _ = seeded_tenants
    await _promote_to_owner(app_engine, tenant_id=tenant_id)
    form_id = await _form_for(app_engine, tenant_id)

    with pytest.raises(ConsentError):
        await _submit(
            app_engine,
            tenant_id=tenant_id,
            form_id=form_id,
            notice_id=seeded_notice,
            consent_granted=False,
        )

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_id)
        clients = (
            await connection.execute(
                text("SELECT count(*) FROM clients WHERE tenant_id = :t"), {"t": tenant_id}
            )
        ).scalar_one()

    assert clients == 0


async def test_an_invalid_mobile_is_refused_before_a_client_exists(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
    clean_leads: None,
) -> None:
    """🔒 EC-M2-01 — a landline in the mobile field is rejected, not stored.

    The failure this prevents is silent: a number that passes would fail WhatsApp
    delivery months later, and the practitioner would blame the messaging system.
    """
    tenant_id, _ = seeded_tenants
    await _promote_to_owner(app_engine, tenant_id=tenant_id)
    form_id = await _form_for(app_engine, tenant_id)

    with pytest.raises(ValidationError):
        await _submit(
            app_engine,
            tenant_id=tenant_id,
            form_id=form_id,
            notice_id=seeded_notice,
            mobile="+91 22 2222 2222",
        )


# ─── AC-M2-004 — consent reaches the ledger ──────────────────────────────


async def test_consent_is_recorded_against_the_notice_in_force(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
    clean_leads: None,
) -> None:
    """🔒 AC-M2-004 — "recorded in the consent ledger with the notice version".

    ⚠️ Asserted on ``consent_records`` rather than on the submission's own
    ``consent_notice_id``. The submission column is a convenience; the ledger is
    the artefact NFR-051 is answered from, and it is written by a different code
    path (the router, because R5 keeps the module out of ``platform.consent``).
    """
    tenant_id, _ = seeded_tenants
    await _promote_to_owner(app_engine, tenant_id=tenant_id)
    form_id = await _form_for(app_engine, tenant_id)

    result = await _submit(
        app_engine, tenant_id=tenant_id, form_id=form_id, notice_id=seeded_notice
    )

    # The router appends the ledger entries; this suite calls the module
    # directly, so it writes them the same way the router does.
    from app.kernel.consent import ConsentDecision, ConsentSubject
    from app.kernel.context import ActorType
    from app.kernel.models import ConsentAction, ConsentChannel, ConsentSubjectType
    from app.platform.consent import append_decisions, notice_in_force

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_id)
        notice = await notice_in_force(session)
        assert notice is not None
        await append_decisions(
            session,
            [
                ConsentDecision(
                    subject=ConsentSubject(
                        tenant_id=tenant_id,
                        subject_type=ConsentSubjectType.CLIENT,
                        subject_id=result.client_id,  # type: ignore[attr-defined]
                        subject_mobile_hash=result.mobile_hash,  # type: ignore[attr-defined]
                    ),
                    purpose_id=purpose_id,
                    notice_id=notice.notice_id,
                    action=ConsentAction.GRANTED,
                    captured_via=ConsentChannel.ENQUIRY_FORM,
                    captured_by_actor_type=ActorType.ANONYMOUS,
                )
                for purpose_id in notice.purpose_ids
            ],
        )

    async with app_engine.begin() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT notice_id, captured_via, subject_mobile_hash "
                    "FROM consent_records WHERE subject_id = :c"
                ),
                {"c": result.client_id},  # type: ignore[attr-defined]
            )
        ).all()

    assert rows, "no consent was recorded for an accepted enquiry (AC-M2-004)"
    for row in rows:
        assert uuid.UUID(str(row.notice_id)) == seeded_notice
        assert row.captured_via == ConsentChannel.ENQUIRY_FORM.value
        # 🔒 NFR-033 — the mobile is hashed, never stored plain in the ledger.
        assert row.subject_mobile_hash is not None
        assert "9876543210" not in row.subject_mobile_hash


# ─── AC-M1-004 / J1 — the enquiry lands on the timeline ──────────────────


async def test_an_enquiry_appears_on_the_client_timeline(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
    clean_leads: None,
) -> None:
    """🔒 AC-M1-004 / J1 — the enquiry is the first thing on a lead's timeline.

    ⚠️ The subscriber is transactional (DDR-06), so this also proves the event
    fired inside the submission's own transaction: if it had been deferred, the
    row would not be here yet and a practitioner opening the client immediately
    after would see an empty history.
    """
    from app.kernel.events import reset_subscriptions
    from app.modules.clients import register_subscribers

    reset_subscriptions()
    register_subscribers()

    tenant_id, _ = seeded_tenants
    await _promote_to_owner(app_engine, tenant_id=tenant_id)
    form_id = await _form_for(app_engine, tenant_id)

    result = await _submit(
        app_engine, tenant_id=tenant_id, form_id=form_id, notice_id=seeded_notice
    )

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_id)
        rows = (
            await connection.execute(
                text(
                    "SELECT event_type, summary, actor_type, actor_id, source_record_id "
                    "FROM timeline_events WHERE client_id = :c ORDER BY occurred_at"
                ),
                {"c": result.client_id},  # type: ignore[attr-defined]
            )
        ).all()

    kinds = [row.event_type for row in rows]
    assert "enquiry_received" in kinds, f"no enquiry on the timeline; got {kinds}"

    entry = next(row for row in rows if row.event_type == "enquiry_received")
    # 🔒 A prospect submitted this; nobody in the tenant acted.
    assert entry.actor_type == "system"
    assert entry.actor_id is None
    # The deep link points at the submission, so a repeat enquiry is
    # distinguishable from the first.
    assert uuid.UUID(str(entry.source_record_id)) == result.submission_id  # type: ignore[attr-defined]
    # 🔒 NFR-033 — the prospect's own words never reach the timeline.
    assert "wedding" not in entry.summary


# ─── FR-M2-011 / AC-M2-005 — the needs-response queue ────────────────────


async def test_the_queue_is_oldest_first_and_clears_when_answered(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
    clean_leads: None,
) -> None:
    """🔒 FR-M2-011 / AC-M2-005 — the screen that recovers a forgotten lead.

    ⚠️ **Oldest first**, unlike every other list in the product. The enquiry that
    has waited longest is the most urgent one; a queue that buried it under
    today's arrivals would be the failure US-M2-03 names.
    """
    tenant_id, _ = seeded_tenants
    owner = await _owner_of(app_engine, tenant_id)
    await _promote_to_owner(app_engine, tenant_id=tenant_id)
    form_id = await _form_for(app_engine, tenant_id)

    first = await _submit(
        app_engine,
        tenant_id=tenant_id,
        form_id=form_id,
        notice_id=seeded_notice,
        name="Older Prospect",
        mobile="+919876500001",
    )
    second = await _submit(
        app_engine,
        tenant_id=tenant_id,
        form_id=form_id,
        notice_id=seeded_notice,
        name="Newer Prospect",
        mobile="+919876500002",
    )

    # Age the first one so the ordering is unambiguous rather than dependent on
    # two inserts landing in different microseconds.
    #
    # ⚠️ As the **migrator**, not `app_user`. `submitted_at` is one of the
    # evidence columns the column-level grant deliberately withholds (DB §6.2) —
    # the application cannot backdate a submission, and neither can a test
    # pretending to be it. That refusal is itself asserted in
    # `test_the_submitted_columns_are_immutable_but_the_response_is_not`.
    async with migrator_engine.begin() as connection:
        await scope_to(connection, tenant_id)
        await connection.execute(
            text(
                "UPDATE enquiry_submissions SET submitted_at = now() - interval '3 days' "
                "WHERE id = :id"
            ),
            {"id": first.submission_id},  # type: ignore[attr-defined]
        )

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_id)
        page = await list_enquiries(
            session,
            tenant_id=tenant_id,
            actor_user_id=owner,
            actor_role=UserRole.OWNER,
            needs_response_only=True,
            now=datetime.now(UTC),
        )

    assert [item.submitted_name for item in page.items] == ["Older Prospect", "Newer Prospect"]
    # 🔒 AC-M2-005 — "with their age", server-computed.
    assert page.items[0].age_hours >= 71
    assert page.items[0].is_ageing is True
    assert page.items[1].is_ageing is False

    # Answering one clears it from the queue and nothing else.
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_id)
        await mark_responded(
            session,
            tenant_id=tenant_id,
            submission_id=first.submission_id,  # type: ignore[attr-defined]
            responded_by_user_id=owner,
        )

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_id)
        remaining = await list_enquiries(
            session,
            tenant_id=tenant_id,
            actor_user_id=owner,
            actor_role=UserRole.OWNER,
            needs_response_only=True,
            now=datetime.now(UTC),
        )
        everything = await list_enquiries(
            session,
            tenant_id=tenant_id,
            actor_user_id=owner,
            actor_role=UserRole.OWNER,
            now=datetime.now(UTC),
        )

    assert [item.submitted_name for item in remaining.items] == ["Newer Prospect"]
    # ⚠️ The archive still holds both — answering is not deleting.
    assert len(everything.items) == 2
    # ⚠️ And the archive is newest-first, the opposite ordering.
    assert everything.items[0].submitted_name == "Newer Prospect"
    assert second.submission_id is not None  # type: ignore[attr-defined]


async def test_marking_responded_twice_keeps_the_first_answer(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
    clean_leads: None,
) -> None:
    """🔒 Idempotent — and the *first* responder is the one on record.

    A practitioner double-tapping must not rewrite who handled the enquiry, which
    is the question the column exists to answer.
    """
    tenant_id, _ = seeded_tenants
    owner = await _owner_of(app_engine, tenant_id)
    await _promote_to_owner(app_engine, tenant_id=tenant_id)
    colleague = await _add_practitioner(app_engine, tenant_id=tenant_id)
    form_id = await _form_for(app_engine, tenant_id)

    result = await _submit(
        app_engine, tenant_id=tenant_id, form_id=form_id, notice_id=seeded_notice
    )

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_id)
        await mark_responded(
            session,
            tenant_id=tenant_id,
            submission_id=result.submission_id,  # type: ignore[attr-defined]
            responded_by_user_id=owner,
        )
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_id)
        await mark_responded(
            session,
            tenant_id=tenant_id,
            submission_id=result.submission_id,  # type: ignore[attr-defined]
            responded_by_user_id=colleague,
        )

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_id)
        recorded = (
            await connection.execute(
                text("SELECT responded_by_user_id FROM enquiry_submissions WHERE id = :id"),
                {"id": result.submission_id},  # type: ignore[attr-defined]
            )
        ).scalar_one()

    assert uuid.UUID(str(recorded)) == owner, "a second call overwrote the original responder"


# ─── AC-M1-006 — a practitioner cannot read a colleague's enquiries ──────


async def test_a_practitioner_does_not_see_a_colleagues_enquiries(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
    clean_leads: None,
) -> None:
    """🔒 AC-M1-006 on the enquiry surface — the leak through a different door.

    An enquiry carries the same name, mobile and stated goal as the client it
    created. A practitioner refused the client record must be refused this too,
    or the scoping is decorative.
    """
    tenant_id, _ = seeded_tenants
    await _promote_to_owner(app_engine, tenant_id=tenant_id)
    colleague = await _add_practitioner(app_engine, tenant_id=tenant_id)
    form_id = await _form_for(app_engine, tenant_id)

    await _submit(app_engine, tenant_id=tenant_id, form_id=form_id, notice_id=seeded_notice)

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_id)
        page = await list_enquiries(
            session,
            tenant_id=tenant_id,
            actor_user_id=colleague,
            actor_role=UserRole.PRACTITIONER,
            now=datetime.now(UTC),
        )

    assert page.items == [], (
        "a practitioner read an enquiry for a client they have no access to "
        "(AC-M1-006) — the lead is owned by the account owner"
    )


async def test_a_granted_practitioner_sees_the_enquiry(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
    clean_leads: None,
) -> None:
    """The other direction — the scoping must not be a blanket refusal.

    ⚠️ Without this, a `_visible_to` that returned nothing at all would pass the
    test above while breaking the feature entirely.
    """
    tenant_id, _ = seeded_tenants
    owner = await _owner_of(app_engine, tenant_id)
    await _promote_to_owner(app_engine, tenant_id=tenant_id)
    colleague = await _add_practitioner(app_engine, tenant_id=tenant_id)
    form_id = await _form_for(app_engine, tenant_id)

    result = await _submit(
        app_engine, tenant_id=tenant_id, form_id=form_id, notice_id=seeded_notice
    )

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_id)
        await grant_access(
            session,
            tenant_id=tenant_id,
            client_id=result.client_id,  # type: ignore[attr-defined]
            owner_user_id=owner,
            grantee_user_id=colleague,
            actor_user_id=owner,
            actor_role=UserRole.OWNER,
        )

    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_id)
        page = await list_enquiries(
            session,
            tenant_id=tenant_id,
            actor_user_id=colleague,
            actor_role=UserRole.PRACTITIONER,
            now=datetime.now(UTC),
        )

    assert [item.submitted_name for item in page.items] == ["Asha Menon"]


async def test_enquiries_do_not_cross_the_tenant_boundary(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
    clean_leads: None,
) -> None:
    """🔒 AC-M0-003 for the newest tables — RLS, not an application filter."""
    tenant_a, tenant_b = seeded_tenants
    await _promote_to_owner(app_engine, tenant_id=tenant_a)
    form_id = await _form_for(app_engine, tenant_a)
    await _submit(app_engine, tenant_id=tenant_a, form_id=form_id, notice_id=seeded_notice)

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_b)
        visible = (
            await connection.execute(text("SELECT count(*) FROM enquiry_submissions"))
        ).scalar_one()

    assert visible == 0, "tenant B can see tenant A's enquiries — RLS is not holding"


# ─── DB §6.2 — the submission is evidence, not a working record ──────────


async def test_the_submitted_columns_are_immutable_but_the_response_is_not(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
    clean_leads: None,
) -> None:
    """🔒 DB §6.2 — "as submitted, never mutated", enforced by a *column* grant.

    ⚠️ **Only a live cluster can prove this**, and the first version of migration
    0015 got it wrong in a way no unit test could see: it revoked UPDATE
    outright, which made FR-M2-011's needs-response queue unimplementable. The
    correction is not "grant UPDATE" — that would let a caller rewrite the
    prospect's own words, which is the immutability DB §6.2 exists to guarantee.

    So the grant names two columns, and this asserts the split in both
    directions. Asserting only the permitted half would pass against a
    table-level grant, which is the mistake being guarded against.
    """
    tenant_id, _ = seeded_tenants
    owner = await _owner_of(app_engine, tenant_id)
    await _promote_to_owner(app_engine, tenant_id=tenant_id)
    form_id = await _form_for(app_engine, tenant_id)
    result = await _submit(
        app_engine, tenant_id=tenant_id, form_id=form_id, notice_id=seeded_notice
    )
    submission_id = result.submission_id  # type: ignore[attr-defined]

    # 🔒 The evidence columns are refused.
    async with app_engine.connect() as connection:
        await connection.begin()
        await scope_to(connection, tenant_id)
        with pytest.raises(ProgrammingError) as raised:
            await connection.execute(
                text("UPDATE enquiry_submissions SET submitted_name = 'Rewritten' WHERE id = :id"),
                {"id": submission_id},
            )
        assert "permission denied" in str(raised.value).lower()

    async with app_engine.connect() as connection:
        await connection.begin()
        await scope_to(connection, tenant_id)
        with pytest.raises(ProgrammingError):
            await connection.execute(
                text("UPDATE enquiry_submissions SET primary_goal = 'Rewritten' WHERE id = :id"),
                {"id": submission_id},
            )

    # ...and the response columns are permitted, or FR-M2-011 cannot work.
    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_id)
        await connection.execute(
            text(
                "UPDATE enquiry_submissions "
                "SET responded_at = now(), responded_by_user_id = :u WHERE id = :id"
            ),
            {"id": submission_id, "u": owner},
        )

    # 🔒 DELETE stays refused — erasure runs as the migrator role (FR-M0-027).
    async with app_engine.begin() as connection:
        deletable = (
            await connection.execute(
                text("SELECT has_table_privilege('app_user', 'enquiry_submissions', 'DELETE')")
            )
        ).scalar_one()
    assert deletable is False, "app_user can DELETE an enquiry submission (DB §6.2)"


# ─── API §11.1 — the public read, and only the public read ───────────────


async def test_an_anonymous_connection_reads_the_form_and_nothing_else(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
    clean_leads: None,
) -> None:
    """🔒 Migration 0015's ``enquiry_forms__public_read``, discharged.

    The policy admits a **tenant-less** SELECT on active forms so that
    ``GET /public/forms/{slug}`` can resolve a slug before any tenant is known.
    That is a deliberate hole in an otherwise total isolation model, and this is
    the only place it can be shown to be exactly the size intended: the form is
    readable, and nothing else is.
    """
    tenant_id, _ = seeded_tenants
    await _promote_to_owner(app_engine, tenant_id=tenant_id)
    await _form_for(app_engine, tenant_id)
    await _submit(
        app_engine,
        tenant_id=tenant_id,
        form_id=await _form_for(app_engine, tenant_id),
        notice_id=seeded_notice,
    )

    async with app_engine.begin() as connection:
        slug = (
            await connection.execute(
                text("SELECT slug FROM tenants WHERE id = :t"), {"t": tenant_id}
            )
        ).scalar_one()

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        # 🔒 No `scope_to` — this is the anonymous public path.
        form = await load_public_form(session, tenant_slug=slug)

    assert form is not None, (
        "the public form is unreachable without a tenant scope, so every "
        "enquiry link 404s (FR-M2-001)"
    )
    assert form.tenant_id == tenant_id

    # 🔒 And the hole is exactly one table wide.
    async with app_engine.begin() as connection:
        for table in ("clients", "enquiry_submissions"):
            leaked = (await connection.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
            assert leaked == 0, (
                f"an unscoped connection can read `{table}` — the public-read "
                "policy is wider than the form it was written for"
            )


async def test_a_deactivated_form_is_invisible_to_the_public(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_leads: None,
) -> None:
    """🔒 EC-M2-07 — the practitioner's own off switch, and a suspended account.

    Both must 404 identically. Telling a stranger that a practice exists but has
    stopped accepting enquiries publishes a fact about that business nobody asked
    us to publish.
    """
    tenant_id, _ = seeded_tenants
    form_id = await _form_for(app_engine, tenant_id)

    async with app_engine.begin() as connection:
        slug = (
            await connection.execute(
                text("SELECT slug FROM tenants WHERE id = :t"), {"t": tenant_id}
            )
        ).scalar_one()

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        await scope_to(await session.connection(), tenant_id)
        await update_form(session, tenant_id=tenant_id, form_id=form_id, is_active=False)

    async with sessions() as session, session.begin():
        assert await load_public_form(session, tenant_slug=slug) is None


async def test_a_suspended_tenant_stops_accepting_enquiries(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_leads: None,
) -> None:
    """🔒 EC-M2-07 — "public form disabled with a neutral message; no data loss".

    ⚠️ Independent of the form's own ``is_active`` flag. A practitioner who has
    not paid has not chosen to stop taking enquiries, so the account's status has
    to override the switch rather than depend on someone remembering to flip it.
    """
    tenant_id, _ = seeded_tenants
    await _form_for(app_engine, tenant_id)

    async with migrator_engine.begin() as connection:
        slug = (
            await connection.execute(
                text("SELECT slug FROM tenants WHERE id = :t"), {"t": tenant_id}
            )
        ).scalar_one()
        await connection.execute(
            text("UPDATE tenants SET status = 'suspended' WHERE id = :t"), {"t": tenant_id}
        )

    sessions = _sessions(app_engine)
    async with sessions() as session, session.begin():
        assert await load_public_form(session, tenant_slug=slug) is None

    async with migrator_engine.begin() as connection:
        await connection.execute(
            text("UPDATE tenants SET status = 'active' WHERE id = :t"), {"t": tenant_id}
        )


async def test_only_one_active_form_per_tenant(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    clean_leads: None,
) -> None:
    """🔒 FR-M2-001 — "a publicly accessible enquiry form", singular.

    The partial unique index is what makes ``ensure_form`` idempotent under
    concurrency. Without it, two first-time reads would race and the practitioner
    would have two forms with no way to tell which link they had shared.
    """
    tenant_id, _ = seeded_tenants
    first = await _form_for(app_engine, tenant_id)
    second = await _form_for(app_engine, tenant_id)
    assert first == second

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_id)
        with pytest.raises((ProgrammingError, Exception)):
            await connection.execute(
                text(
                    "INSERT INTO enquiry_forms (tenant_id, title, is_active) "
                    "VALUES (:t, 'Second form', true)"
                ),
                {"t": tenant_id},
            )


# ─── FR-M2-011's index is actually used ──────────────────────────────────


async def test_the_needs_response_query_seeks_its_index(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
    clean_leads: None,
) -> None:
    """🔒 The Slice E lesson, applied to Slice F's own index.

    Migration 0013 shipped an index that was valid, built, and never seeked: under
    FORCE RLS PostgreSQL pushes only *leakproof* quals into an Index Cond, and
    `reverse()` and `LIKE` are not. The planner silently scanned instead.

    ``ix_enquiry_submissions__needs_response`` is partial on ``responded_at IS
    NULL`` over plain columns, so it should be immune — but "should be" is what
    0013 assumed too. This asks the cluster.

    ⚠️ **Filtered to `Index Cond` lines.** A bitmap scan *names* the index while
    doing none of its work, so grepping the plan for the index name proves
    nothing (the exact mistake the Slice E note records).

    ⚠️ Needs volume, distinct values, and an ANALYZE run **as the owner** —
    `app_user` cannot analyse, and its `permission denied` is a WARNING, so it
    silently no-ops and the planner works from empty-table statistics.
    """
    tenant_id, _ = seeded_tenants
    await _promote_to_owner(app_engine, tenant_id=tenant_id)
    form_id = await _form_for(app_engine, tenant_id)
    owner = await _owner_of(app_engine, tenant_id)

    # Enough rows that a sequential scan is the more expensive plan, and mostly
    # answered so the partial index is genuinely selective.
    async with migrator_engine.begin() as connection:
        await scope_to(connection, tenant_id)
        client = (
            await connection.execute(
                text(
                    "INSERT INTO clients (tenant_id, full_name, mobile, owner_user_id, stage) "
                    "VALUES (:t, 'Bulk', '+919000000000', :o, 'lead') RETURNING id"
                ),
                {"t": tenant_id, "o": owner},
            )
        ).scalar_one()
        await connection.execute(
            text(
                "INSERT INTO enquiry_submissions "
                "  (tenant_id, form_id, client_id, submitted_name, submitted_mobile, "
                "   primary_goal, submitted_at, responded_at, responded_by_user_id) "
                "SELECT :t, :f, :c, 'Bulk ' || i, '+9190000' || lpad(i::text, 5, '0'), "
                "       'goal', now() - (i || ' minutes')::interval, "
                "       CASE WHEN i % 50 = 0 THEN NULL ELSE now() END, "
                "       CASE WHEN i % 50 = 0 THEN NULL ELSE :o END "
                "FROM generate_series(1, 3000) AS i"
            ),
            {"t": tenant_id, "f": form_id, "c": client, "o": owner},
        )

    async with migrator_engine.begin() as connection:
        await connection.execute(text("ANALYZE enquiry_submissions"))

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_id)
        plan = "\n".join(
            str(row[0])
            for row in (
                await connection.execute(
                    text(
                        "EXPLAIN SELECT id FROM enquiry_submissions "
                        "WHERE tenant_id = :t AND responded_at IS NULL "
                        "ORDER BY submitted_at ASC, id ASC LIMIT 25"
                    ),
                    {"t": tenant_id},
                )
            ).all()
        )

    index_conditions = [line for line in plan.splitlines() if "Index Cond" in line]
    assert "ix_enquiry_submissions__needs_response" in plan, (
        "the planner did not choose the needs-response index:\n" + plan
    )
    assert index_conditions or "Index Scan" in plan or "Index Only Scan" in plan, (
        "the index is named but produced no Index Cond — a bitmap scan does that "
        "while reading the whole table (the Slice E failure mode):\n" + plan
    )


# ─── FR-M2-009 — source attribution survives the round trip ─────────────


async def test_an_unrecognised_source_is_bucketed_without_losing_the_original(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
    clean_leads: None,
) -> None:
    """🔒 FR-M2-009 / US-M2-04 — the reporting axis stays clean.

    A campaign parameter nobody in the practice controls becomes ``other``, so
    "which channel produces enquiries" is still answerable, while the raw value
    survives in ``source_detail`` for the practitioner who wants it.
    """
    tenant_id, _ = seeded_tenants
    await _promote_to_owner(app_engine, tenant_id=tenant_id)
    form_id = await _form_for(app_engine, tenant_id)

    result = await _submit(
        app_engine,
        tenant_id=tenant_id,
        form_id=form_id,
        notice_id=seeded_notice,
        source="spring-campaign-2026",
    )

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_id)
        row = (
            await connection.execute(
                text("SELECT source, source_detail FROM enquiry_submissions WHERE id = :id"),
                {"id": result.submission_id},  # type: ignore[attr-defined]
            )
        ).one()

    assert row.source == LeadSource.OTHER.value
    assert row.source_detail is not None
    assert "spring" in row.source_detail


async def test_a_known_source_is_recorded_as_itself(
    app_engine: AsyncEngine,
    seeded_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
    clean_leads: None,
) -> None:
    """FR-M2-009 — the Instagram-bio link, which is US-M2-01's whole scenario."""
    tenant_id, _ = seeded_tenants
    await _promote_to_owner(app_engine, tenant_id=tenant_id)
    form_id = await _form_for(app_engine, tenant_id)

    result = await _submit(
        app_engine,
        tenant_id=tenant_id,
        form_id=form_id,
        notice_id=seeded_notice,
        source="Instagram",
    )

    async with app_engine.begin() as connection:
        await scope_to(connection, tenant_id)
        source = (
            await connection.execute(
                text("SELECT source FROM enquiry_submissions WHERE id = :id"),
                {"id": result.submission_id},  # type: ignore[attr-defined]
            )
        ).scalar_one()

    assert source == LeadSource.INSTAGRAM.value


# ─── EC-M2-06 — leads are never metered ─────────────────────────────────


async def test_a_tenant_at_its_client_limit_still_accepts_enquiries(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    subscribed_tenants: tuple[uuid.UUID, ...],
    seeded_notice: uuid.UUID,
    clean_leads: None,
) -> None:
    """🔒 EC-M2-06 / FR-M1-003 — "leads are unmetered; the limit applies at
    conversion to `Active`".

    ⚠️ The failure this prevents is commercially specific: a practitioner on the
    ₹799 tier who hits their client limit would stop receiving enquiries, and the
    prospects would never know. M2.2 prices each one at ₹2,500–4,000/month.
    """
    tenant_id, _ = subscribed_tenants
    await _promote_to_owner(app_engine, tenant_id=tenant_id)
    form_id = await _form_for(app_engine, tenant_id)

    # Drop the plan's allowance to zero — no headroom at all.
    async with migrator_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE plan_definitions SET limits = jsonb_set(limits, "
                "'{active_clients}', '0') WHERE code = 'starter'"
            )
        )

    try:
        result = await _submit(
            app_engine, tenant_id=tenant_id, form_id=form_id, notice_id=seeded_notice
        )
        assert result.client_id is not None  # type: ignore[attr-defined]
    finally:
        async with migrator_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE plan_definitions SET limits = jsonb_set(limits, "
                    "'{active_clients}', '30') WHERE code = 'starter'"
                )
            )
