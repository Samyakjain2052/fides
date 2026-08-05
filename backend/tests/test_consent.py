"""Phase 3 — the invariants that make a consent record evidence.

These tests are the specification. If one of them starts failing, the product
has stopped being able to prove what it claims to prove.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.errors import Conflict, NotFound
from app.db.session import set_tenant_context
from app.models.audit import AuditAction
from app.models.consent import Consent, DataPrincipal, Notice, Purpose
from app.services import consent_service, notice_service
from app.services.audit_service import Actor


def _actor(tenant: dict) -> Actor:
    return Actor(type="user", id=tenant["admin_id"], label="test")


@asynccontextmanager
async def scoped(factory, tenant_id):
    """A session with tenant context set, inside a transaction.

    A context manager rather than a bare factory so a failing assertion cannot
    leak a checked-out connection — which exhausts the pool and turns one real
    failure into a suite that appears to hang.

    Note that the context ends at commit: `set_tenant_context` uses SET LOCAL, so
    committing discards it and any query after that runs with NO tenant context
    and matches nothing. Re-enter the block to read back what you committed. That
    is RLS behaving correctly, and it is easy to mistake for a bug.
    """
    async with factory() as session:
        await session.begin()
        await set_tenant_context(session, tenant_id)
        try:
            yield session
        finally:
            if session.in_transaction():
                await session.rollback()


async def _seed_purpose_and_notice(
    factory, tenant, *, key="marketing_email", mandatory=False,
    legal_basis="consent", retention_days=None, publish=True, language="English",
):
    async with scoped(factory, tenant["id"]) as session:
        purpose = await notice_service.create_purpose(
            session, tenant_id=tenant["id"], actor=_actor(tenant),
            key=key, name=key.replace("_", " ").title(), category="Contact Data",
            is_mandatory=mandatory, legal_basis=legal_basis,
            retention_days=retention_days,
        )
        notice = await notice_service.draft_notice(
            session, tenant_id=tenant["id"], actor=_actor(tenant),
            purpose_id=purpose.id, language=language,
            content="We use your email to send product updates.",
            data_collected="Email address",
            user_rights="You may withdraw at any time.",
            withdrawal_policy="Marketing stops within 24 hours.",
        )
        if publish:
            await notice_service.publish_notice(
                session, tenant_id=tenant["id"], actor=_actor(tenant),
                notice_id=notice.id,
            )
        principal = DataPrincipal(
            tenant_id=tenant["id"], external_id="cust-001",
            email="person@example.com",
        )
        session.add(principal)
        await session.flush()
        ids = (purpose.id, notice.id, principal.id)
        await session.commit()
    return ids



# --------------------------------------------------------------------------- #
# N4 — consent binds to a notice version
# --------------------------------------------------------------------------- #

async def test_consent_requires_a_published_notice(app_session_factory, tenant_a):
    """A purpose with only a draft cannot collect consent.

    Consenting to a draft means consenting to text that can still change, which
    is precisely what a consent record is supposed to rule out.
    """
    purpose_id, _, principal_id = await _seed_purpose_and_notice(
        app_session_factory, tenant_a, publish=False
    )
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(Conflict, match="no published notice"):
            await consent_service.grant(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                principal_id=principal_id, purpose_id=purpose_id,
            )


async def test_consent_records_the_notice_version_shown(app_session_factory, tenant_a):
    purpose_id, notice_id, principal_id = await _seed_purpose_and_notice(
        app_session_factory, tenant_a
    )
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        consent = await consent_service.grant(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            principal_id=principal_id, purpose_id=purpose_id, method="checkbox",
        )
        assert consent.notice_id == notice_id
        assert consent.status == "active"
        assert consent.given_at is not None
        await s.commit()


async def test_publishing_v2_does_not_move_an_existing_consent(
    app_session_factory, tenant_a
):
    """The evidence must name the words the person actually read.

    Someone who agreed to v1 did not agree to v2. If publishing a new version
    silently re-pointed old consents, every historical record would quietly
    become a claim about text its signatory never saw.
    """
    purpose_id, v1_id, principal_id = await _seed_purpose_and_notice(
        app_session_factory, tenant_a
    )
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await consent_service.grant(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            principal_id=principal_id, purpose_id=purpose_id,
        )
        v2 = await notice_service.revise_notice(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            notice_id=v1_id, content="We now also send partner offers by email.",
        )
        await notice_service.publish_notice(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), notice_id=v2.id
        )
        v2_version = v2.version
        await s.commit()

    # Fresh context: SET LOCAL is discarded by the commit above.
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        stored = await s.scalar(select(Consent).where(Consent.purpose_id == purpose_id))
        assert v2_version == 2
        assert stored is not None
        assert stored.notice_id == v1_id, "consent moved to a version its signatory never saw"


# --------------------------------------------------------------------------- #
# A published notice is immutable — enforced by the database
# --------------------------------------------------------------------------- #

async def test_published_notice_cannot_be_edited(app_session_factory, tenant_a):
    """Not "the service refuses" — the database refuses.

    A service-level check protects only the paths that remember to call it. This
    is the wording people agreed to; it has to survive a data fix, a migration,
    and a future code path nobody has written yet.
    """
    _, notice_id, _ = await _seed_purpose_and_notice(app_session_factory, tenant_a)
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(DBAPIError, match="cannot change"):
            await s.execute(
                text("UPDATE notices SET content = :c WHERE id = :i"),
                {"c": "Quietly different wording.", "i": str(notice_id)},
            )


async def test_published_notice_cannot_be_unpublished(app_session_factory, tenant_a):
    _, notice_id, _ = await _seed_purpose_and_notice(app_session_factory, tenant_a)
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(DBAPIError, match="cannot be un-published"):
            await s.execute(
                text("UPDATE notices SET published_at = NULL WHERE id = :i"),
                {"i": str(notice_id)},
            )


async def test_a_draft_is_freely_editable(app_session_factory, tenant_a):
    """The freeze applies to published text only — drafts exist to be changed."""
    _, notice_id, _ = await _seed_purpose_and_notice(
        app_session_factory, tenant_a, publish=False
    )
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        revised = await notice_service.revise_notice(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            notice_id=notice_id, content="Reworded while still a draft, which is fine.",
        )
        assert revised.id == notice_id, "editing a draft should not create a version"
        assert revised.version == 1
        await s.commit()


async def test_revising_published_creates_the_next_version(app_session_factory, tenant_a):
    _, notice_id, _ = await _seed_purpose_and_notice(app_session_factory, tenant_a)
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        v2 = await notice_service.revise_notice(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            notice_id=notice_id, content="Superseding wording.",
        )
        assert v2.id != notice_id
        assert v2.version == 2
        assert v2.published_at is None, "a new version starts as a draft"
        await s.commit()


# --------------------------------------------------------------------------- #
# Granting and withdrawing
# --------------------------------------------------------------------------- #

async def test_nothing_is_consented_by_default(app_session_factory, tenant_a):
    """Creating a purpose and a principal must not create a consent."""
    _, _, principal_id = await _seed_purpose_and_notice(app_session_factory, tenant_a)
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        assert (await s.execute(select(Consent))).scalars().all() == []
        result = await consent_service.check(
            s, tenant_id=tenant_a["id"], principal_id=principal_id,
            purpose_key="marketing_email",
        )
        assert result["allowed"] is False
        assert result["status"] == "never_given"


async def test_withdraw_is_one_call_and_idempotent(app_session_factory, tenant_a):
    """DPDP §6(4): withdrawing must be as easy as giving."""
    purpose_id, _, principal_id = await _seed_purpose_and_notice(
        app_session_factory, tenant_a
    )
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await consent_service.grant(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            principal_id=principal_id, purpose_id=purpose_id,
        )
        c = await consent_service.withdraw(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            principal_id=principal_id, purpose_id=purpose_id,
        )
        assert c.status == "withdrawn" and c.withdrawn_at is not None

        again = await consent_service.withdraw(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            principal_id=principal_id, purpose_id=purpose_id,
        )
        assert again.status == "withdrawn", "asking twice is not an error"

        check = await consent_service.check(
            s, tenant_id=tenant_a["id"], principal_id=principal_id,
            purpose_key="marketing_email",
        )
        assert check["allowed"] is False and check["status"] == "withdrawn"
        await s.commit()


async def test_mandatory_purpose_refuses_withdrawal_with_a_reason(
    app_session_factory, tenant_a
):
    """Refusing is correct; refusing silently is not.

    The person is entitled to know the processing continues and on what basis, so
    they can act on it. A dead control with no explanation is the version of this
    that gets a company fined.
    """
    purpose_id, _, principal_id = await _seed_purpose_and_notice(
        app_session_factory, tenant_a, key="kyc_verification",
        mandatory=True, legal_basis="legal_obligation",
    )
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await consent_service.grant(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            principal_id=principal_id, purpose_id=purpose_id,
        )
        with pytest.raises(Conflict, match="legal obligation"):
            await consent_service.withdraw(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                principal_id=principal_id, purpose_id=purpose_id,
            )


async def test_mandatory_purpose_cannot_rest_on_consent(app_session_factory, tenant_a):
    """Consent that cannot be refused is not consent, so the shape is rejected."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(Conflict, match="not consent"):
            await notice_service.create_purpose(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                key="sneaky", name="Sneaky", category="Other",
                is_mandatory=True, legal_basis="consent",
            )


async def test_expiry_is_evaluated_at_read_time(app_session_factory, tenant_a):
    """No nightly sweep: a sweep leaves a window in which an expired consent
    still reads as active, and processing in that window is unlawful."""
    purpose_id, _, principal_id = await _seed_purpose_and_notice(
        app_session_factory, tenant_a, retention_days=30
    )
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        consent = await consent_service.grant(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            principal_id=principal_id, purpose_id=purpose_id,
        )
        assert consent.expires_at is not None

        # Backdate past expiry. The row still says "active"; the answer must not.
        consent.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.flush()

        result = await consent_service.check(
            s, tenant_id=tenant_a["id"], principal_id=principal_id,
            purpose_key="marketing_email",
        )
        assert result["allowed"] is False
        assert result["status"] == "expired"


# --------------------------------------------------------------------------- #
# History comes from the audit chain
# --------------------------------------------------------------------------- #

async def test_history_reads_from_the_audit_chain(app_session_factory, tenant_a):
    purpose_id, _, principal_id = await _seed_purpose_and_notice(
        app_session_factory, tenant_a
    )
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await consent_service.grant(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            principal_id=principal_id, purpose_id=purpose_id,
        )
        await consent_service.withdraw(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            principal_id=principal_id, purpose_id=purpose_id,
        )
        await s.commit()

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        events = await consent_service.history(s, tenant_a["id"], principal_id)
        actions = [e.action for e in events]
        assert AuditAction.CONSENT_WITHDRAWN in actions
        assert AuditAction.CONSENT_GRANTED in actions
        # Every entry carries its chain hash: the history shown to a regulator is
        # the same evidence the integrity check verifies.
        assert all(e.hash for e in events)


# --------------------------------------------------------------------------- #
# Isolation — as for every other table
# --------------------------------------------------------------------------- #

async def test_one_tenant_cannot_see_anothers_consent_domain(
    app_session_factory, tenant_a, tenant_b
):
    purpose_id, _, principal_id = await _seed_purpose_and_notice(
        app_session_factory, tenant_a
    )
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await consent_service.grant(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            principal_id=principal_id, purpose_id=purpose_id,
        )
        await s.commit()

    async with scoped(app_session_factory, tenant_b["id"]) as s:
        assert (await s.execute(select(Purpose))).scalars().all() == []
        assert (await s.execute(select(Notice))).scalars().all() == []
        assert (await s.execute(select(DataPrincipal))).scalars().all() == []
        assert (await s.execute(select(Consent))).scalars().all() == []
        # And cannot reach one by id either — RLS filters, it does not merely
        # hide a listing.
        assert await s.scalar(
            select(Consent).where(Consent.purpose_id == purpose_id)
        ) is None


async def test_a_tenant_cannot_write_into_another_tenant(
    app_session_factory, tenant_a, tenant_b
):
    """WITH CHECK, not just USING: inserting a row for someone else must fail at
    write time rather than succeed and become invisible."""
    async with scoped(app_session_factory, tenant_b["id"]) as s:
        s.add(
            Purpose(tenant_id=tenant_a["id"], key="planted", name="Planted",
                    category="Other", legal_basis="consent")
        )
        with pytest.raises((IntegrityError, DBAPIError)):
            await s.flush()


async def test_every_consent_table_has_an_rls_policy(app_session_factory, tenant_a):
    """A table holding customer data without a policy is the one mistake this
    codebase is arranged to make hard. Assert it rather than trust the review."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        rows = await s.execute(
            text(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                       (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid)
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname IN ('purposes','notices','data_principals','consents')
                """
            )
        )
        found = {r[0]: (r[1], r[2], r[3]) for r in rows.all()}
        assert len(found) == 4, f"missing tables: {found.keys()}"
        for table, (enabled, forced, policies) in found.items():
            assert enabled, f"{table} has RLS disabled"
            assert forced, f"{table} does not FORCE RLS — the owner would bypass it"
            assert policies >= 1, f"{table} has no policy"
