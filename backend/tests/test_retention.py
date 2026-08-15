"""Retention — the only module that destroys data.

A false pass here does not produce a wrong record somebody can correct. It loses
a customer's data irreversibly. These tests are written accordingly: they assert
row counts before and after, they assert the dry run and the live run select
*identically*, and they assert that every hold actually holds.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.errors import Conflict
from app.db.session import set_tenant_context
from app.models.consent import Consent, DataPrincipal
from app.models.dsar import DsarRequest
from app.models.retention import PurgeRun, PurgeRunItem, RetentionPolicy
from app.services import consent_service, notice_service, retention_service
from app.services.audit_service import Actor

CATEGORY = "Contact Data"


def _actor(tenant: dict) -> Actor:
    return Actor(type="user", id=tenant["admin_id"], label="dpo@test")


@asynccontextmanager
async def scoped(factory, tenant_id):
    async with factory() as session:
        await session.begin()
        await set_tenant_context(session, tenant_id)
        try:
            yield session
        finally:
            if session.in_transaction():
                await session.rollback()


async def _world(session, tenant, *, days_ago=400, status="withdrawn"):
    """A purpose in CATEGORY, a published notice, a principal, and a stale consent."""
    purpose = await notice_service.create_purpose(
        session, tenant_id=tenant["id"], actor=_actor(tenant),
        key=f"p{uuid.uuid4().hex[:8]}", name="Marketing", category=CATEGORY,
    )
    notice = await notice_service.draft_notice(
        session, tenant_id=tenant["id"], actor=_actor(tenant), purpose_id=purpose.id,
        content="We use your email.", data_collected="Email",
        user_rights="Withdraw anytime.", withdrawal_policy="Stops in 24h.",
    )
    await notice_service.publish_notice(
        session, tenant_id=tenant["id"], actor=_actor(tenant), notice_id=notice.id
    )
    principal = DataPrincipal(
        tenant_id=tenant["id"], external_id=f"cust-{uuid.uuid4().hex[:8]}",
        email="person@example.com", phone="+91 90000 00000",
    )
    session.add(principal)
    await session.flush()

    consent = await consent_service.grant(
        session, tenant_id=tenant["id"], actor=_actor(tenant),
        principal_id=principal.id, purpose_id=purpose.id,
    )
    if status == "withdrawn":
        consent.status = "withdrawn"
        consent.withdrawn_at = datetime.now(UTC) - timedelta(days=days_ago)
        consent.given_at = datetime.now(UTC) - timedelta(days=days_ago + 30)
    await session.flush()
    return purpose, principal, consent


async def _policy(session, tenant, **kw):
    return await retention_service.create_policy(
        session, tenant_id=tenant["id"], actor=_actor(tenant),
        **{"name": f"Policy {uuid.uuid4().hex[:6]}", "data_category": CATEGORY,
           "retention_days": 90, **kw},
    )


# --------------------------------------------------------------------------- #
# A dry run destroys nothing. This is the one that must never regress.
# --------------------------------------------------------------------------- #

async def test_a_dry_run_changes_absolutely_nothing(app_session_factory, tenant_a):
    """Asserted on the data, not on the return value.

    A preview that reports correctly while quietly mutating would be the worst
    possible bug in this module, and a test that only checks the summary would
    not catch it.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        _p, principal, _c = await _world(s, tenant_a)
        policy = await _policy(s, tenant_a)
        before = (principal.email, principal.phone, principal.external_id,
                  principal.purged_at)
        consents_before = await s.scalar(select(func.count()).select_from(Consent))

        run = await retention_service.preview(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), policy_id=policy.id
        )

        await s.refresh(principal)
        assert (principal.email, principal.phone, principal.external_id,
                principal.purged_at) == before, "a dry run mutated a principal"
        assert await s.scalar(select(func.count()).select_from(Consent)) == consents_before
        assert run.mode == "dry_run"
        assert run.rows_affected == 0
        assert run.candidates_found >= 1, "it should still have found the candidate"
        await s.commit()


async def test_the_database_refuses_a_dry_run_that_changed_things(
    app_session_factory, tenant_a
):
    """A CHECK backing the same promise.

    If this constraint ever fires in production, the preview and the executor
    have diverged — which is exactly the failure that makes a dry run report four
    rows and the live run destroy four hundred.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        policy = await _policy(s, tenant_a)
        s.add(
            PurgeRun(
                tenant_id=tenant_a["id"], policy_id=policy.id, mode="dry_run",
                status="completed", started_at=datetime.now(UTC), rows_affected=7,
            )
        )
        with pytest.raises((IntegrityError, DBAPIError)):
            await s.flush()


async def test_dry_and_live_select_the_same_candidates(app_session_factory, tenant_a):
    """One selection path, called by both.

    Not "the two implementations agree" — there is only one implementation, and
    this test exists to keep it that way.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        purpose, principal, _c = await _world(s, tenant_a)
        policy = await _policy(s, tenant_a)

        from_selection = await retention_service.select_candidates(
            s, tenant_id=tenant_a["id"], policy=policy
        )
        dry = await retention_service.preview(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), policy_id=policy.id
        )
        assert dry.candidates_found == len([c for c in from_selection if c.purgeable])

        live = await retention_service.execute(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            policy_id=policy.id, confirm=policy.name,
        )
        assert live.rows_affected == dry.candidates_found, (
            "the live run touched a different number of rows than the preview "
            "promised"
        )
        await s.commit()


# --------------------------------------------------------------------------- #
# Everything that must stop a purge
# --------------------------------------------------------------------------- #

async def test_a_legal_hold_is_never_purged(app_session_factory, tenant_a):
    """A hold outranks every policy, and the skip says so on the receipt."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        _p, principal, _c = await _world(s, tenant_a)
        principal.legal_hold = True
        principal.legal_hold_reason = "Litigation hold — matter 2026/114"
        await s.flush()
        policy = await _policy(s, tenant_a)

        run = await retention_service.execute(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            policy_id=policy.id, confirm=policy.name,
        )
        await s.refresh(principal)
        assert principal.email == "person@example.com", "a held principal was purged"
        assert run.rows_affected == 0

        items = await retention_service.run_items(s, tenant_a["id"], run.id)
        assert any("legal hold" in (i.skip_reason or "") for i in items)
        assert any("2026/114" in (i.skip_reason or "") for i in items)
        await s.commit()


async def test_a_hold_must_say_why(app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        _p, principal, _c = await _world(s, tenant_a)
        await s.flush()
        with pytest.raises((IntegrityError, DBAPIError)):
            await s.execute(
                text("UPDATE data_principals SET legal_hold = true WHERE id = :i"),
                {"i": str(principal.id)},
            )


async def test_an_open_rights_request_blocks_a_purge(app_session_factory, tenant_a):
    """Erasing somebody mid-request would destroy the data they just asked to
    see, and make the request impossible to answer."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        _p, principal, _c = await _world(s, tenant_a)
        now = datetime.now(UTC)
        s.add(
            DsarRequest(
                tenant_id=tenant_a["id"], principal_id=principal.id,
                reference="DSAR-2026-9001", type="access", status="in_progress",
                submitted_at=now, deadline_at=now + timedelta(days=30),
            )
        )
        await s.flush()
        policy = await _policy(s, tenant_a)

        run = await retention_service.execute(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            policy_id=policy.id, confirm=policy.name,
        )
        await s.refresh(principal)
        assert principal.email == "person@example.com"
        items = await retention_service.run_items(s, tenant_a["id"], run.id)
        assert any("open rights request" in (i.skip_reason or "") for i in items)
        await s.commit()


async def test_an_active_consent_blocks_a_purge(app_session_factory, tenant_a):
    """Retention does not apply to data somebody is currently permitted to hold."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        _p, principal, _c = await _world(s, tenant_a, status="active")
        policy = await _policy(s, tenant_a)
        run = await retention_service.execute(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            policy_id=policy.id, confirm=policy.name,
        )
        await s.refresh(principal)
        assert principal.email == "person@example.com"
        items = await retention_service.run_items(s, tenant_a["id"], run.id)
        assert any("active consent" in (i.skip_reason or "") for i in items)
        await s.commit()


async def test_data_inside_the_retention_window_is_left_alone(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        _p, principal, _c = await _world(s, tenant_a, days_ago=10)
        policy = await _policy(s, tenant_a, retention_days=90)
        run = await retention_service.execute(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            policy_id=policy.id, confirm=policy.name,
        )
        await s.refresh(principal)
        assert principal.email == "person@example.com"
        items = await retention_service.run_items(s, tenant_a["id"], run.id)
        assert any("within the retention period" in (i.skip_reason or "") for i in items)
        await s.commit()


# --------------------------------------------------------------------------- #
# What a purge actually does
# --------------------------------------------------------------------------- #

async def test_a_purge_masks_identifiers_and_keeps_the_evidence(
    app_session_factory, tenant_a
):
    """Mirrors the DSAR erasure path: null the identifiers, keep the row.

    The consent record is NOT destroyed — it is the evidence that holding the
    data was permitted, and a fiduciary who cannot produce it has lost the only
    thing that made the processing lawful.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        _p, principal, consent = await _world(s, tenant_a)
        policy = await _policy(s, tenant_a)
        consent_id = consent.id

        await retention_service.execute(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            policy_id=policy.id, confirm=policy.name,
        )
        await s.refresh(principal)

        assert principal.email is None
        assert principal.phone is None
        assert principal.purged_at is not None
        assert principal.external_id.startswith("purged:")

        # The consent survives and still resolves.
        assert await s.scalar(select(Consent).where(Consent.id == consent_id)) is not None
        await s.commit()


async def test_a_second_run_does_not_re_purge(app_session_factory, tenant_a):
    """Idempotent. An already-purged principal is not a candidate, so a re-run
    does not churn the receipt with changes that did not happen."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        _p, _principal, _c = await _world(s, tenant_a)
        policy = await _policy(s, tenant_a)
        first = await retention_service.execute(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            policy_id=policy.id, confirm=policy.name,
        )
        second = await retention_service.execute(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            policy_id=policy.id, confirm=policy.name,
        )
        assert first.rows_affected == 1
        assert second.rows_affected == 0
        await s.commit()


# --------------------------------------------------------------------------- #
# Guardrails around running at all
# --------------------------------------------------------------------------- #

async def test_a_live_run_needs_the_policy_name_back(app_session_factory, tenant_a):
    """The same reason `rm -rf` prompts."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _world(s, tenant_a)
        policy = await _policy(s, tenant_a)
        with pytest.raises(Conflict, match="exactly as confirmation"):
            await retention_service.execute(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                policy_id=policy.id, confirm="yes",
            )
        with pytest.raises(Conflict):
            await retention_service.execute(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                policy_id=policy.id, confirm="",
            )


async def test_an_exempt_policy_will_not_run(app_session_factory, tenant_a):
    """Honouring a decision somebody already made beats relying on whoever
    presses the button to remember why it was set."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _world(s, tenant_a)
        policy = await _policy(
            s, tenant_a, exemption_code="statutory",
            exemption_reference="RBI KYC Master Direction 2016 §12",
        )
        with pytest.raises(Conflict, match="exempt"):
            await retention_service.execute(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                policy_id=policy.id, confirm=policy.name,
            )


async def test_auto_delete_cannot_be_set_without_a_notice_period(
    app_session_factory, tenant_a
):
    """Automatic destruction with no warning is not something the schema permits,
    however somebody arrived at it."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(Conflict, match="warn first"):
            await _policy(s, tenant_a, auto_delete=True, notify_days=0)

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        s.add(
            RetentionPolicy(
                tenant_id=tenant_a["id"], name="sneaky", data_category=CATEGORY,
                retention_days=30, auto_delete=True, notify_days=0,
            )
        )
        with pytest.raises((IntegrityError, DBAPIError)):
            await s.flush()


async def test_auto_delete_is_off_by_default(app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        policy = await _policy(s, tenant_a)
        assert policy.auto_delete is False, "automatic destruction must be opt-in"
        assert policy.action == "mask", "the safer action is the default"
        await s.commit()


async def test_an_exemption_needs_a_reference(app_session_factory, tenant_a):
    """Without one it is an assertion, not a justification."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(Conflict, match="needs a reference"):
            await _policy(s, tenant_a, exemption_code="dispute")

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        s.add(
            RetentionPolicy(
                tenant_id=tenant_a["id"], name="unjustified", data_category=CATEGORY,
                retention_days=30, exemption_code="statutory",
            )
        )
        with pytest.raises((IntegrityError, DBAPIError)):
            await s.flush()


# --------------------------------------------------------------------------- #
# The receipt
# --------------------------------------------------------------------------- #

async def test_receipts_cannot_be_rewritten(app_session_factory, tenant_a):
    """A receipt the application can edit proves nothing about what was
    destroyed."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _world(s, tenant_a)
        policy = await _policy(s, tenant_a)
        await retention_service.execute(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            policy_id=policy.id, confirm=policy.name,
        )
        await s.commit()

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(DBAPIError):
            await s.execute(text("DELETE FROM purge_runs"))
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(DBAPIError):
            await s.execute(text("UPDATE purge_run_items SET action_taken='skipped'"))
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(DBAPIError):
            await s.execute(text("DELETE FROM purge_run_items"))


async def test_a_skip_must_say_why(app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _world(s, tenant_a)
        policy = await _policy(s, tenant_a)
        run = await retention_service.preview(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), policy_id=policy.id
        )
        s.add(
            PurgeRunItem(
                tenant_id=tenant_a["id"], purge_run_id=run.id,
                table_name="data_principals", entity_id=uuid.uuid4(),
                action_taken="skipped",
            )
        )
        with pytest.raises((IntegrityError, DBAPIError)):
            await s.flush()


async def test_the_receipt_names_a_capped_run(app_session_factory, tenant_a):
    """Silent truncation reads as 'everything eligible was handled'."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _world(s, tenant_a)
        policy = await _policy(s, tenant_a)
        run = await retention_service.preview(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), policy_id=policy.id
        )
        assert "batch_capped" in run.scope_summary
        assert run.scope_summary["batch_capped"] is False
        assert run.scope_summary["examined"] >= 1
        await s.commit()


# --------------------------------------------------------------------------- #
# Isolation — the worst possible failure in this module
# --------------------------------------------------------------------------- #

async def test_a_policy_can_never_reach_another_tenants_data(
    app_session_factory, tenant_a, tenant_b
):
    """The single most damaging bug this module could have.

    Tenant B is given an identical category and an identically stale principal,
    so nothing but RLS distinguishes them. A live run in tenant A must leave B
    untouched.
    """
    async with scoped(app_session_factory, tenant_b["id"]) as s:
        _p, b_principal, _c = await _world(s, tenant_b)
        b_id, b_email = b_principal.id, b_principal.email
        await s.commit()

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _world(s, tenant_a)
        policy = await _policy(s, tenant_a)
        run = await retention_service.execute(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            policy_id=policy.id, confirm=policy.name,
        )
        assert run.rows_affected == 1, "should have touched exactly its own one"
        await s.commit()

    async with scoped(app_session_factory, tenant_b["id"]) as s:
        untouched = await s.scalar(
            select(DataPrincipal).where(DataPrincipal.id == b_id)
        )
        assert untouched.email == b_email, "a purge crossed a tenant boundary"
        assert untouched.purged_at is None
        assert (await s.execute(select(PurgeRun))).scalars().all() == []


async def test_the_retention_tables_have_rls_policies(app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        rows = await s.execute(
            text(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                       (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid)
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname='public'
                  AND c.relname IN ('retention_policies','purge_runs','purge_run_items')
                """
            )
        )
        found = {r[0]: (r[1], r[2], r[3]) for r in rows.all()}
        assert len(found) == 3, f"missing: {found.keys()}"
        for table, (enabled, forced, policies) in found.items():
            assert enabled and forced and policies >= 1, f"{table} is not protected"
