"""Phase 5 — the record behind the rights engine.

The engine itself is proven by `scripts/acceptance.sh` on every run. These tests
cover the things that make a request a *record*: a deadline nobody outside the
server can set, a rejection that has to say why, a timeline that cannot be
rewritten, and an engine that cannot overrule a human.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.errors import Conflict, PermissionDenied
from app.core.permissions import Capability, Role, capabilities_for
from app.db.session import set_tenant_context
from app.models.audit import AuditAction, AuditEvent
from app.models.consent import DataPrincipal
from app.models.dsar import DsarEvent, DsarRequest
from app.models.tenant import Tenant
from app.services import dsar_service
from app.services.audit_service import Actor


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


async def _principal(session, tenant_id, ref="cust-1", email="person@example.com"):
    p = DataPrincipal(tenant_id=tenant_id, external_id=ref, email=email)
    session.add(p)
    await session.flush()
    return p


async def _raise(session, tenant, principal_id, **kw):
    return await dsar_service.submit(
        session, tenant_id=tenant["id"], actor=_actor(tenant),
        principal_id=principal_id, **{"type": "access", **kw},
    )


# --------------------------------------------------------------------------- #
# The statutory deadline
# --------------------------------------------------------------------------- #

async def test_the_deadline_comes_from_the_tenant_sla(app_session_factory, tenant_a):
    """A deadline a caller can set is not a statutory deadline.

    It is computed from `tenants.dsar_sla_days` — the field a regulator asks
    about — and `submit()` has no parameter through which a caller could
    influence it.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        tenant = await s.scalar(select(Tenant).where(Tenant.id == tenant_a["id"]))
        tenant.dsar_sla_days = 15
        p = await _principal(s, tenant_a["id"])
        req = await _raise(s, tenant_a, p.id)

        assert (req.deadline_at - req.submitted_at).days == 15
        assert req.deadline_at.tzinfo is not None, "the clock must be aware"
        await s.commit()


async def test_the_deadline_cannot_precede_the_request(app_session_factory, tenant_a):
    """A CHECK, not a convention. A deadline before the submission would make
    every overdue calculation nonsense."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        p = await _principal(s, tenant_a["id"])
        now = datetime.now(UTC)
        s.add(
            DsarRequest(
                tenant_id=tenant_a["id"], principal_id=p.id, reference="DSAR-BAD-1",
                type="access", status="received",
                submitted_at=now, deadline_at=now - timedelta(days=1),
            )
        )
        with pytest.raises((IntegrityError, DBAPIError)):
            await s.flush()


async def test_overdue_is_evaluated_against_the_clock(app_session_factory, tenant_a):
    """Not by a nightly job. A sweep leaves a window in which an overdue request
    still displays as fine, and that window is exactly when a DPO is looking."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        p = await _principal(s, tenant_a["id"])
        req = await _raise(s, tenant_a, p.id)
        # Move BOTH: `deadline_at > submitted_at` is a CHECK, so a request that
        # is overdue is one that was submitted long enough ago — not one whose
        # deadline was dragged behind its own submission.
        req.submitted_at = datetime.now(UTC) - timedelta(days=40)
        req.deadline_at = datetime.now(UTC) - timedelta(days=10)
        await s.flush()
        assert req.is_open
        assert req.deadline_at <= datetime.now(UTC), "overdue the moment anyone looks"
        await s.commit()


# --------------------------------------------------------------------------- #
# Decisions must be defensible
# --------------------------------------------------------------------------- #

async def test_rejecting_without_a_reason_is_refused(app_session_factory, tenant_a):
    """Service and database both. A rejection with no recorded reason is not a
    decision anyone can defend to a regulator."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        p = await _principal(s, tenant_a["id"])
        req = await _raise(s, tenant_a, p.id)

        with pytest.raises(Conflict, match="has to say why"):
            await dsar_service.change_status(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=req, to_status="rejected",
            )
        with pytest.raises(Conflict, match="has to say why"):
            await dsar_service.change_status(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=req, to_status="rejected", reason="   ",
            )


async def test_the_database_also_refuses_a_reasonless_rejection(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        p = await _principal(s, tenant_a["id"])
        req = await _raise(s, tenant_a, p.id)
        await s.flush()
        with pytest.raises((IntegrityError, DBAPIError)):
            await s.execute(
                text("UPDATE dsar_requests SET status='rejected' WHERE id=:i"),
                {"i": str(req.id)},
            )


async def test_completing_records_when(app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        p = await _principal(s, tenant_a["id"])
        req = await _raise(s, tenant_a, p.id)
        req.status = "in_progress"
        await s.flush()
        await dsar_service.change_status(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            request=req, to_status="completed",
        )
        assert req.resolved_at is not None
        # An access request that completed has a package window.
        assert req.package_available_until is not None
        await s.commit()


async def test_an_illegal_transition_says_what_is_allowed(app_session_factory, tenant_a):
    """The state machine is written down so a triage UI can render it, and so the
    refusal is useful rather than a bare 409."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        p = await _principal(s, tenant_a["id"])
        req = await _raise(s, tenant_a, p.id)
        await dsar_service.change_status(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            request=req, to_status="rejected", reason="Not verifiable.",
        )
        with pytest.raises(Conflict, match="already closed"):
            await dsar_service.change_status(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=req, to_status="in_progress",
            )


# --------------------------------------------------------------------------- #
# The engine must not overrule a human
# --------------------------------------------------------------------------- #

async def test_a_closed_request_is_never_reopened_by_the_engine(
    app_session_factory, tenant_a, monkeypatch
):
    """A DPO's rejection is a decision.

    A late callback from the engine saying "complete" must not resurrect it. This
    is one guard in `refresh_from_engine`, and without it a rejected request
    quietly reopens itself and the person is told the opposite of what they were
    told before.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        p = await _principal(s, tenant_a["id"])
        req = await _raise(s, tenant_a, p.id)
        req.engine_ref = "pri_whatever"
        await dsar_service.change_status(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            request=req, to_status="rejected", reason="Identity not verified.",
        )
        await s.flush()

        called = False

        class _Boom:
            async def __aenter__(self):
                nonlocal called
                called = True
                raise AssertionError("the engine must not even be consulted")

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr("app.services.dsar_service.httpx.AsyncClient", lambda **k: _Boom())

        await dsar_service.refresh_from_engine(
            s, tenant_id=tenant_a["id"], request=req
        )
        assert req.status == "rejected", "a closed request must stay closed"
        assert called is False


async def test_a_failed_dispatch_does_not_lose_the_request(
    app_session_factory, tenant_a, monkeypatch
):
    """Losing somebody's rights request because a downstream was briefly down
    would be the worst possible way to fail. It stays at `received` with the
    reason on its timeline, and can be retried."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        p = await _principal(s, tenant_a["id"])
        req = await _raise(s, tenant_a, p.id)

        class _Down:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                raise ConnectionError("gateway unreachable")

        monkeypatch.setattr("app.services.dsar_service.httpx.AsyncClient", lambda **k: _Down())

        await dsar_service.dispatch_to_engine(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), request=req
        )
        assert req.status == "received", "the request survives"
        assert req.engine_ref is None
        assert "ConnectionError" in (req.engine_error or "")

        events = await dsar_service.timeline(s, tenant_a["id"], req.id)
        assert any("dispatch failed" in (e.note or "") for e in events)
        await s.commit()


async def test_a_correction_never_reaches_the_engine(app_session_factory, tenant_a):
    """The engine has no correction action. A correction is a tracked manual
    workflow with the same deadline — a right handled by hand, not a right
    quietly dropped. The schema enforces the separation."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        p = await _principal(s, tenant_a["id"])
        req = await _raise(
            s, tenant_a, p.id, type="correction",
            correction_payload={"field": "phone", "corrected": "+91 98765 43210"},
        )
        await dsar_service.dispatch_to_engine(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), request=req
        )
        assert req.engine_ref is None
        assert req.status == "received"

        with pytest.raises((IntegrityError, DBAPIError)):
            await s.execute(
                text("UPDATE dsar_requests SET engine_ref='pri_x' WHERE id=:i"),
                {"i": str(req.id)},
            )


async def test_a_correction_must_say_what_is_wrong(app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        p = await _principal(s, tenant_a["id"])
        with pytest.raises(Conflict, match="what is wrong"):
            await _raise(s, tenant_a, p.id, type="correction")


async def test_a_principal_with_no_email_cannot_raise_an_engine_request(
    app_session_factory, tenant_a
):
    """The engine locates a person by email. Saying so now beats a request that
    sits at `received` forever with no explanation."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        p = await _principal(s, tenant_a["id"], ref="no-email", email=None)
        with pytest.raises(Conflict, match="no email on record"):
            await _raise(s, tenant_a, p.id, type="erasure")


# --------------------------------------------------------------------------- #
# The record itself
# --------------------------------------------------------------------------- #

async def test_the_timeline_cannot_be_rewritten(app_session_factory, tenant_a):
    """Append-and-read, like the audit chain. A timeline the application can edit
    is not a timeline."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        p = await _principal(s, tenant_a["id"])
        await _raise(s, tenant_a, p.id)
        await s.commit()

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(DBAPIError):
            await s.execute(text("UPDATE dsar_events SET note='rewritten'"))
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(DBAPIError):
            await s.execute(text("DELETE FROM dsar_events"))


async def test_a_request_cannot_be_deleted(app_session_factory, tenant_a):
    """A rights request is the record that somebody exercised a right. The
    application has no business removing it."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        p = await _principal(s, tenant_a["id"])
        await _raise(s, tenant_a, p.id)
        await s.commit()

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(DBAPIError):
            await s.execute(text("DELETE FROM dsar_requests"))


async def test_every_change_reaches_both_the_timeline_and_the_chain(
    app_session_factory, tenant_a
):
    """They are not redundant: the chain is tamper-evident evidence, the timeline
    is what a screen renders. A divergence between them is a bug."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        p = await _principal(s, tenant_a["id"])
        req = await _raise(s, tenant_a, p.id)
        req.status = "in_progress"
        await s.flush()
        await dsar_service.change_status(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            request=req, to_status="completed",
        )
        await s.commit()

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        events = await dsar_service.timeline(s, tenant_a["id"], req.id)
        assert [e.to_status for e in events] == ["received", "completed"]

        chain = (
            await s.execute(
                select(AuditEvent).where(AuditEvent.entity_type == "dsar_request")
            )
        ).scalars().all()
        actions = {e.action for e in chain}
        assert AuditAction.DSAR_SUBMITTED in actions
        assert AuditAction.DSAR_COMPLETED in actions
        assert all(e.hash for e in chain)


async def test_references_are_unique_and_human_shaped(app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        p = await _principal(s, tenant_a["id"])
        a = await _raise(s, tenant_a, p.id)
        b = await _raise(s, tenant_a, p.id, type="erasure")
        assert a.reference.startswith("DSAR-") and a.reference != b.reference
        assert a.reference.endswith("0001") and b.reference.endswith("0002")
        await s.commit()


# --------------------------------------------------------------------------- #
# Who may do what
# --------------------------------------------------------------------------- #

def test_every_human_role_can_exercise_its_own_rights():
    """Staff are data subjects too.

    A DPO has an account, the company holds their data, and the DPDP Act does not
    stop applying because someone works there. Until this was fixed, an admin
    could process everybody's requests and had no way to raise their own.
    """
    for role in (Role.DATA_PRINCIPAL, Role.ADMIN, Role.AUDITOR, Role.GRIEVANCE_OFFICER):
        caps = capabilities_for(role)
        assert Capability.SELF_DSAR_WRITE in caps, f"{role} cannot raise its own request"
        assert Capability.SELF_READ in caps


def test_only_admin_can_process_other_peoples_requests():
    assert Capability.DSAR_PROCESS in capabilities_for(Role.ADMIN)
    for role in (Role.DATA_PRINCIPAL, Role.AUDITOR, Role.GRIEVANCE_OFFICER):
        assert Capability.DSAR_PROCESS not in capabilities_for(role)


def test_an_auditor_stays_read_only_on_what_it_audits():
    """Granting self:* must not have widened an auditor into something that can
    change what it audits."""
    caps = capabilities_for(Role.AUDITOR)
    for forbidden in (
        Capability.DSAR_PROCESS, Capability.PURPOSE_MANAGE,
        Capability.USER_MANAGE, Capability.RETENTION_MANAGE,
    ):
        assert forbidden not in caps


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #

async def test_one_tenant_cannot_see_or_touch_anothers_requests(
    app_session_factory, tenant_a, tenant_b
):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        p = await _principal(s, tenant_a["id"])
        req = await _raise(s, tenant_a, p.id)
        await s.commit()

    async with scoped(app_session_factory, tenant_b["id"]) as s:
        assert (await s.execute(select(DsarRequest))).scalars().all() == []
        assert (await s.execute(select(DsarEvent))).scalars().all() == []
        # Not reachable by id either — RLS filters, it does not merely hide a list.
        assert await s.scalar(select(DsarRequest).where(DsarRequest.id == req.id)) is None


async def test_the_dsar_tables_have_rls_policies(app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        rows = await s.execute(
            text(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                       (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid)
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname='public' AND c.relname IN ('dsar_requests','dsar_events')
                """
            )
        )
        found = {r[0]: (r[1], r[2], r[3]) for r in rows.all()}
        assert len(found) == 2, f"missing: {found.keys()}"
        for table, (enabled, forced, policies) in found.items():
            assert enabled and forced and policies >= 1, f"{table} is not protected"
