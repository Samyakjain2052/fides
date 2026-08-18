"""Grievances — the module that stands between a complaint and a regulator.

A person must exhaust this route before approaching the Data Protection Board.
That makes the failure mode specific and severe: a grievance that quietly falls
out of the queue, or resolves itself without redress, is how somebody's statutory
right is extinguished without anybody deciding to extinguish it.

So these tests concentrate on:

* the clocks coming from the tenant rather than from constants,
* the escalation being idempotent AND visible before any job has run,
* resolution and rejection being impossible without saying what and why,
* a Grievance Officer being unable to read anything but grievances,
* one person being unable to see or rate another person's complaint.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.errors import Conflict, NotFound
from app.db.session import set_tenant_context
from app.models.consent import DataPrincipal
from app.models.grievance import Grievance, GrievanceEvent
from app.models.notification import Notification
from app.models.tenant import Tenant
from app.models.user import User
from app.services import grievance_service, notification_service
from app.services.audit_service import Actor
from app.services.notification_providers import SendResult


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


def _actor(tenant: dict) -> Actor:
    return Actor(type="user", id=tenant["admin_id"], label="dpo@test")


class _Sends:
    """A provider that always succeeds, and remembers what it was asked to send."""

    name = "capture"

    def __init__(self):
        self.sent = []

    async def send(self, *, to, subject, body, channel):
        self.sent.append({"to": to, "subject": subject, "body": body})
        return SendResult(ok=True, provider_message_id=f"cap-{len(self.sent)}")


@pytest.fixture
def provider(monkeypatch):
    def _install(impl=None):
        impl = impl or _Sends()
        monkeypatch.setattr(
            "app.services.notification_providers.get_provider", lambda: impl
        )
        return impl
    return _install


@pytest.fixture
async def client():
    """The real app over ASGI. No network, but every dependency runs."""
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _principal(session, tenant, email="person@example.com") -> DataPrincipal:
    row = DataPrincipal(
        tenant_id=tenant["id"], external_id=f"cust-{uuid.uuid4().hex[:8]}", email=email
    )
    session.add(row)
    await session.flush()
    return row


async def _file(session, tenant, **kw):
    defaults = {
        "category": "consent_violation",
        "description": "You kept sending me marketing email after I withdrew consent.",
    }
    grievance, token = await grievance_service.file(
        session, tenant_id=tenant["id"], actor=_actor(tenant), **{**defaults, **kw}
    )
    return grievance, token


# --------------------------------------------------------------------------- #
# The clocks come from the tenant
# --------------------------------------------------------------------------- #

async def test_the_deadline_and_threshold_come_from_the_tenant_not_a_constant(
    app_session_factory, tenant_a, provider
):
    """The statutory window is a floor, and a customer may promise faster.

    A constant here would be wrong for every such customer, and silently wrong —
    the deadline would look authoritative while being someone else's number.
    """
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        tenant = await s.scalar(select(Tenant).where(Tenant.id == tenant_a["id"]))
        tenant.grievance_sla_days = 7
        tenant.grievance_escalation_days = 3
        await s.flush()

        principal = await _principal(s, tenant_a)
        grievance, _ = await _file(s, tenant_a, principal_id=principal.id)

        assert (grievance.deadline_at - grievance.submitted_at).days == 7
        assert (grievance.escalate_at - grievance.submitted_at).days == 3


async def test_changing_the_tenant_clock_does_not_rewrite_existing_grievances(
    app_session_factory, tenant_a, provider
):
    """Retroactively moving somebody's statutory deadline would make the record
    unreliable in exactly the direction that flatters the fiduciary."""
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        grievance, _ = await _file(s, tenant_a, principal_id=principal.id)
        original = grievance.deadline_at

        await grievance_service.set_officer(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            name="New Officer", email="officer@example.com",
            sla_days=60, escalation_days=30,
        )
        await s.refresh(grievance)
        assert grievance.deadline_at == original


async def test_escalation_must_come_before_the_deadline(app_session_factory, tenant_a):
    """An escalation with no time left to act on it is a formality."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(Conflict) as exc:
            await grievance_service.set_officer(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                name="O", email="o@example.com", sla_days=15, escalation_days=15,
            )
        assert "before the deadline" in str(exc.value)


# --------------------------------------------------------------------------- #
# Nothing closes without saying what happened
# --------------------------------------------------------------------------- #

async def test_resolving_without_notes_fails_at_the_service(
    app_session_factory, tenant_a, provider
):
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        grievance, _ = await _file(s, tenant_a, principal_id=principal.id)
        await grievance_service.change_status(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            grievance=grievance, to_status="acknowledged",
        )
        with pytest.raises(Conflict) as exc:
            await grievance_service.change_status(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                grievance=grievance, to_status="resolved",
            )
        assert "how it was resolved" in str(exc.value)


async def test_resolving_without_notes_also_fails_at_the_database(
    app_session_factory, tenant_a, provider
):
    """The service message is for humans; the CHECK is the guarantee.

    A service-level rule can be bypassed by the next code path somebody writes.
    This asserts the database itself will not hold a resolution with no record of
    the redress.
    """
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        grievance, _ = await _file(s, tenant_a, principal_id=principal.id)
        await s.flush()

        # Straight past the service, the way a bad migration or a data fix would.
        with pytest.raises(IntegrityError):
            await s.execute(
                text(
                    "UPDATE grievances SET status='resolved', resolved_at=now(), "
                    "resolution_notes=NULL WHERE id=:i"
                ),
                {"i": str(grievance.id)},
            )


async def test_rejecting_without_a_reason_fails(app_session_factory, tenant_a, provider):
    """Rejection is the point at which the person's next step is the Board.

    They are entitled to know why, and a refusal with no recorded reason is not a
    decision anybody can defend.
    """
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        grievance, _ = await _file(s, tenant_a, principal_id=principal.id)
        with pytest.raises(Conflict) as exc:
            await grievance_service.change_status(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                grievance=grievance, to_status="rejected",
            )
        assert "Data Protection Board" in str(exc.value)


async def test_a_grievance_nobody_can_be_answered_at_is_refused(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(Conflict) as exc:
            await _file(s, tenant_a)  # no principal, no contact email
        assert "no way to answer it" in str(exc.value)


async def test_the_database_refuses_an_unreachable_grievance_too(
    app_session_factory, tenant_a
):
    """The CHECK behind the service rule."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        now = datetime.now(UTC)
        s.add(Grievance(
            tenant_id=tenant_a["id"], reference="GRV-X-0001",
            category="other", description="x" * 20,
            principal_id=None, contact_email=None,
            submitted_at=now, deadline_at=now + timedelta(days=15),
            escalate_at=now + timedelta(days=10),
        ))
        with pytest.raises(IntegrityError):
            await s.flush()


async def test_a_rejected_grievance_is_terminal(app_session_factory, tenant_a, provider):
    """Quietly reopening a refusal would obscure that the person was refused —
    and that their next stop is the Board."""
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        grievance, _ = await _file(s, tenant_a, principal_id=principal.id)
        await grievance_service.change_status(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), grievance=grievance,
            to_status="rejected", rejection_reason="Outside our processing.",
        )
        for target in ("reopened", "in_progress", "resolved", "acknowledged"):
            with pytest.raises(Conflict):
                await grievance_service.change_status(
                    s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    grievance=grievance, to_status=target,
                    resolution_notes="x", rejection_reason="x",
                )


# --------------------------------------------------------------------------- #
# The escalation clock
# --------------------------------------------------------------------------- #

async def _age(grievance, days: int) -> None:
    """Backdate a whole grievance by `days`, keeping its timeline coherent.

    Every timestamp moves together. Moving `escalate_at` alone would put it before
    `submitted_at` and produce a row that no real clock could have produced — and
    `ck_grievances_deadline_after_submit` exists to stop exactly that.
    """
    back = timedelta(days=days)
    grievance.submitted_at -= back
    grievance.escalate_at -= back
    grievance.deadline_at -= back


async def _overdue(session, tenant, principal_id, *, days_past=12):
    """A grievance whose escalation threshold has already passed.

    12 days, not 5: the default `grievance_escalation_days` is 10, so a smaller
    number leaves `escalate_at` in the future and the sweep correctly does
    nothing. That is the test lying, not the code.
    """
    grievance, _ = await _file(session, tenant, principal_id=principal_id)
    await _age(grievance, days_past)
    await session.flush()
    return grievance


async def test_an_overdue_grievance_reads_as_due_before_any_job_has_run(
    app_session_factory, tenant_a, provider
):
    """The window a nightly job leaves open is exactly when a DPO is looking.

    `escalation_due` is computed against the clock, so the row is correct the
    instant it becomes true rather than the next time a sweep happens to run.
    """
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        grievance = await _overdue(s, tenant_a, principal.id)

        assert grievance.escalated is False, "no job has run"
        assert grievance.escalation_due is True, "but the clock says it is due"


async def test_the_sweep_escalates_and_notifies_the_officer(
    app_session_factory, tenant_a, provider
):
    impl = provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        grievance = await _overdue(s, tenant_a, principal.id)

        count = await grievance_service.sweep_escalations(s, tenant_id=tenant_a["id"])

        assert count == 1
        assert grievance.escalated is True
        assert grievance.escalated_at is not None

        rows = await notification_service.log_for_tenant(s, tenant_a["id"])
        escalations = [r for r in rows if r.template_key == "grievance.escalated"]
        assert len(escalations) == 1
        tenant = await s.scalar(select(Tenant).where(Tenant.id == tenant_a["id"]))
        assert escalations[0].to_address == tenant.grievance_officer_email


async def test_the_sweep_is_idempotent(app_session_factory, tenant_a, provider):
    """Running twice must not notify twice.

    Guarded three times over — the flag filters the query, `escalate` re-checks
    it, and the notification's unique constraint would refuse a duplicate anyway.
    This asserts the observable consequence rather than any one of the guards.
    """
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        await _overdue(s, tenant_a, principal.id)

        first = await grievance_service.sweep_escalations(s, tenant_id=tenant_a["id"])
        second = await grievance_service.sweep_escalations(s, tenant_id=tenant_a["id"])

        assert (first, second) == (1, 0)
        sent = await s.scalar(
            select(func.count()).select_from(Notification)
            .where(Notification.template_key == "grievance.escalated")
        )
        assert sent == 1


async def test_a_resolved_grievance_is_never_escalated(
    app_session_factory, tenant_a, provider
):
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        grievance = await _overdue(s, tenant_a, principal.id)
        await grievance_service.change_status(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), grievance=grievance,
            to_status="acknowledged",
        )
        await grievance_service.change_status(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), grievance=grievance,
            to_status="resolved", resolution_notes="Suppression list corrected.",
        )

        assert await grievance_service.sweep_escalations(s, tenant_id=tenant_a["id"]) == 0
        assert grievance.escalated is False


async def test_an_unconfirmed_address_does_not_page_the_officer(
    app_session_factory, tenant_a, provider
):
    """The whole reason `contact_verified` exists.

    An anonymous filing is recorded, counted and visible — but escalating to a
    Grievance Officer on the strength of an address nobody has proven they own
    turns the statutory alarm into noise, and noise is how a real escalation gets
    ignored.
    """
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        grievance, token = await _file(
            s, tenant_a, contact_email="anon@example.com", require_verification=True
        )
        assert token, "a public filing mints a confirmation token"
        await _age(grievance, 12)
        await s.flush()

        assert grievance.escalation_due is False
        assert await grievance_service.sweep_escalations(s, tenant_id=tenant_a["id"]) == 0


async def test_a_confirmed_address_does_page_the_officer(
    app_session_factory, tenant_a, provider
):
    """The other half: confirming is what arms the escalation.

    Confirmed first and aged afterwards, on purpose. The confirmation window
    (7 days) is deliberately shorter than the default escalation threshold
    (10 days), so a grievance old enough to escalate is already too old to
    confirm — which is correct behaviour and makes "age it, then confirm it" an
    impossible sequence rather than a test worth writing.
    """
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        grievance, token = await _file(
            s, tenant_a, contact_email="anon@example.com", require_verification=True
        )
        await grievance_service.confirm_contact(
            s, tenant_id=tenant_a["id"], reference=grievance.reference, token=token
        )
        await _age(grievance, 12)
        await s.flush()

        assert grievance.escalation_due is True
        assert await grievance_service.sweep_escalations(s, tenant_id=tenant_a["id"]) == 1


async def test_an_unconfirmed_grievance_is_still_recorded_and_counted(
    app_session_factory, tenant_a, provider
):
    """Not escalating is not the same as not existing.

    A DPO must be able to see the pile — that is how they notice they are being
    spammed, or that their confirmation emails are not arriving.
    """
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _file(s, tenant_a, contact_email="anon@example.com",
                    require_verification=True)
        counts = await grievance_service.counts(s, tenant_a["id"])
        assert counts["total"] == 1
        assert counts["open"] == 1
        assert counts["awaiting_confirmation"] == 1


# --------------------------------------------------------------------------- #
# Confirmation tokens
# --------------------------------------------------------------------------- #

async def test_the_raw_token_is_never_stored(app_session_factory, tenant_a, provider):
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        grievance, token = await _file(
            s, tenant_a, contact_email="anon@example.com", require_verification=True
        )
        assert grievance.verification_token_hash != token
        assert len(grievance.verification_token_hash) == 64  # sha256 hex


async def test_a_confirmation_token_is_single_use(
    app_session_factory, tenant_a, provider
):
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        grievance, token = await _file(
            s, tenant_a, contact_email="anon@example.com", require_verification=True
        )
        await grievance_service.confirm_contact(
            s, tenant_id=tenant_a["id"], reference=grievance.reference, token=token
        )
        with pytest.raises(Conflict):
            await grievance_service.confirm_contact(
                s, tenant_id=tenant_a["id"], reference=grievance.reference, token=token
            )


async def test_a_wrong_token_and_a_missing_grievance_fail_identically(
    app_session_factory, tenant_a, provider
):
    """Distinguishing them would be a way to test whether a given reference
    exists — which is to say, whether a given person complained."""
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        grievance, _token = await _file(
            s, tenant_a, contact_email="anon@example.com", require_verification=True
        )
        with pytest.raises(Conflict) as wrong:
            await grievance_service.confirm_contact(
                s, tenant_id=tenant_a["id"], reference=grievance.reference,
                token="not-the-token",
            )
        with pytest.raises(Conflict) as absent:
            await grievance_service.confirm_contact(
                s, tenant_id=tenant_a["id"], reference="GRV-2026-9999",
                token="not-the-token",
            )
        assert str(wrong.value) == str(absent.value)


async def test_an_expired_token_is_refused(app_session_factory, tenant_a, provider):
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        grievance, token = await _file(
            s, tenant_a, contact_email="anon@example.com", require_verification=True
        )
        await _age(grievance, grievance_service.VERIFICATION_TTL.days + 1)
        await s.flush()

        with pytest.raises(Conflict):
            await grievance_service.confirm_contact(
                s, tenant_id=tenant_a["id"], reference=grievance.reference, token=token
            )


# --------------------------------------------------------------------------- #
# Anonymous filing throttles
# --------------------------------------------------------------------------- #

async def test_one_unconfirmed_complaint_per_address_at_a_time(
    app_session_factory, tenant_a, provider
):
    """Stops somebody being buried under complaints filed in their name."""
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await grievance_service.throttle_anonymous_filing(
            s, tenant_id=tenant_a["id"], contact_email="anon@example.com"
        )
        await _file(s, tenant_a, contact_email="anon@example.com",
                    require_verification=True)

        with pytest.raises(Conflict) as exc:
            await grievance_service.throttle_anonymous_filing(
                s, tenant_id=tenant_a["id"], contact_email="anon@example.com"
            )
        assert "waiting to be confirmed" in str(exc.value)


async def test_confirming_frees_the_address_to_file_again(
    app_session_factory, tenant_a, provider
):
    """The throttle is on *unconfirmed* filings. A person with a genuine second
    complaint must not be locked out by their first."""
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        grievance, token = await _file(
            s, tenant_a, contact_email="anon@example.com", require_verification=True
        )
        await grievance_service.confirm_contact(
            s, tenant_id=tenant_a["id"], reference=grievance.reference, token=token
        )
        # Does not raise.
        await grievance_service.throttle_anonymous_filing(
            s, tenant_id=tenant_a["id"], contact_email="anon@example.com"
        )


# --------------------------------------------------------------------------- #
# Ratings
# --------------------------------------------------------------------------- #

async def test_an_unsatisfied_rating_reopens_the_grievance(
    app_session_factory, tenant_a, provider
):
    """A satisfaction score that feeds a dashboard and changes nothing is a
    metric. One that reopens an inadequate resolution is redress."""
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        grievance, _ = await _file(s, tenant_a, principal_id=principal.id)
        await grievance_service.change_status(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), grievance=grievance,
            to_status="acknowledged",
        )
        await grievance_service.change_status(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), grievance=grievance,
            to_status="resolved", resolution_notes="We say it is fine.",
        )

        await grievance_service.rate(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), grievance=grievance,
            rating=1, comment="It is not fine.",
        )
        assert grievance.status == "reopened"
        assert grievance.resolved_at is None


async def test_a_satisfied_rating_leaves_it_resolved(
    app_session_factory, tenant_a, provider
):
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        grievance, _ = await _file(s, tenant_a, principal_id=principal.id)
        await grievance_service.change_status(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), grievance=grievance,
            to_status="acknowledged",
        )
        await grievance_service.change_status(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), grievance=grievance,
            to_status="resolved", resolution_notes="Suppression list corrected.",
        )
        await grievance_service.rate(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), grievance=grievance,
            rating=5,
        )
        assert grievance.status == "resolved"


async def test_reopening_does_not_restart_the_clock(
    app_session_factory, tenant_a, provider
):
    """An unsatisfactory resolution must not buy another full statutory window."""
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        grievance, _ = await _file(s, tenant_a, principal_id=principal.id)
        original_deadline = grievance.deadline_at

        await grievance_service.change_status(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), grievance=grievance,
            to_status="acknowledged",
        )
        await grievance_service.change_status(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), grievance=grievance,
            to_status="resolved", resolution_notes="Closed.",
        )
        await grievance_service.rate(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), grievance=grievance,
            rating=1,
        )
        assert grievance.status == "reopened"
        assert grievance.deadline_at == original_deadline


async def test_an_unresolved_grievance_cannot_be_rated(
    app_session_factory, tenant_a, provider
):
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        grievance, _ = await _file(s, tenant_a, principal_id=principal.id)
        with pytest.raises(Conflict):
            await grievance_service.rate(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                grievance=grievance, rating=5,
            )


async def test_the_database_refuses_a_rating_on_an_unresolved_grievance(
    app_session_factory, tenant_a, provider
):
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        grievance, _ = await _file(s, tenant_a, principal_id=principal.id)
        await s.flush()
        with pytest.raises(IntegrityError):
            await s.execute(
                text("UPDATE grievances SET satisfaction_rating=5 WHERE id=:i"),
                {"i": str(grievance.id)},
            )


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #

async def test_filing_acknowledges_the_person(app_session_factory, tenant_a, provider):
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        await _file(s, tenant_a, principal_id=principal.id)

        rows = await notification_service.log_for_tenant(s, tenant_a["id"])
        received = [r for r in rows if r.template_key == "grievance.received"]
        assert len(received) == 1
        assert received[0].to_address == principal.email


async def test_a_public_filing_sends_the_confirmation_and_not_an_acknowledgement(
    app_session_factory, tenant_a, provider
):
    """One email, not two.

    The confirmation already carries the reference and the deadline. Sending an
    acknowledgement as well means the message that asks the person to act is the
    second one — and the second one is the one that gets ignored.
    """
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _file(s, tenant_a, contact_email="anon@example.com",
                    require_verification=True)
        rows = await notification_service.log_for_tenant(s, tenant_a["id"])
        keys = sorted(r.template_key for r in rows)
        assert keys == ["grievance.confirm"]


async def test_a_rejection_does_not_say_the_complaint_was_resolved(
    app_session_factory, tenant_a, provider
):
    """Two templates, not one with a conditional sentence.

    "Your complaint is resolved" arriving about a complaint that was refused is a
    wording that reads as a resolution and is not one — and the person's next step
    depends on them understanding which happened.
    """
    impl = provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        grievance, _ = await _file(s, tenant_a, principal_id=principal.id)
        await grievance_service.change_status(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), grievance=grievance,
            to_status="rejected", rejection_reason="We do not process that data.",
        )
        rows = await notification_service.log_for_tenant(s, tenant_a["id"])
        outcome = [r for r in rows if r.template_key == "grievance.rejected"]
        assert len(outcome) == 1
        assert "resolved" not in outcome[0].subject_rendered.lower()
        # And the reason reaches the person.
        bodies = " ".join(m["body"] for m in impl.sent)
        assert "We do not process that data." in bodies


async def test_internal_transitions_are_silent(app_session_factory, tenant_a, provider):
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        grievance, _ = await _file(s, tenant_a, principal_id=principal.id)
        before = len(await notification_service.log_for_tenant(s, tenant_a["id"]))
        for to in ("acknowledged", "in_progress"):
            await grievance_service.change_status(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                grievance=grievance, to_status=to,
            )
        after = len(await notification_service.log_for_tenant(s, tenant_a["id"]))
        assert after == before


# --------------------------------------------------------------------------- #
# Hostile text
# --------------------------------------------------------------------------- #

MARKUP = "<script>alert('x')</script> & <img onerror=1>"


async def test_the_description_is_stored_raw(app_session_factory, tenant_a, provider):
    """Stored raw, escaped at each sink.

    Storing escaped text means every consumer has to know whether it was, and one
    that guesses wrong either double-escapes (`&amp;lt;`, shown to a DPO) or
    renders markup.
    """
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        grievance, _ = await _file(
            s, tenant_a, principal_id=principal.id,
            description=f"They did this: {MARKUP}",
        )
        assert MARKUP in grievance.description


async def test_the_description_is_inert_in_a_non_html_sink(
    app_session_factory, tenant_a, provider
):
    """`safe_text` is for the paths where nothing escapes on the way out — a PDF,
    a CSV, a plain-text email — where the absence of escaping is silent."""
    assert "<script>" not in grievance_service.safe_text(MARKUP)
    assert "&lt;script&gt;" in grievance_service.safe_text(MARKUP)


async def test_a_resolution_note_is_escaped_into_the_email(
    app_session_factory, tenant_a, provider
):
    """The note is written by staff and may quote the complainant verbatim, so it
    is not trusted on the way into a message body."""
    impl = provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        grievance, _ = await _file(s, tenant_a, principal_id=principal.id)
        await grievance_service.change_status(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), grievance=grievance,
            to_status="acknowledged",
        )
        await grievance_service.change_status(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), grievance=grievance,
            to_status="resolved", resolution_notes=f"Fixed. {MARKUP}",
        )
        bodies = " ".join(m["body"] for m in impl.sent)
        assert "<script>" not in bodies
        assert "&lt;script&gt;" in bodies


async def test_the_description_is_capped_at_the_service(app_session_factory, tenant_a):
    """The form is not the only way in — the public endpoint is reachable by
    anything that can make an HTTP request."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        with pytest.raises(Conflict):
            await _file(
                s, tenant_a, principal_id=principal.id,
                description="x" * (grievance_service.MAX_DESCRIPTION + 1),
            )


async def test_the_complaint_text_is_not_copied_into_the_audit_chain(
    app_session_factory, tenant_a, provider
):
    """The chain is read by people investigating the platform.

    Copying a member of the public's complaint — with whatever third parties they
    named in it — into a second immutable table multiplies the exposure and buys
    nothing.
    """
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        secret = "my neighbour Ravi told me you sold my number"
        await _file(s, tenant_a, principal_id=principal.id,
                    description=f"Please explain. {secret}")

        rows = await s.execute(text("SELECT payload::text FROM audit_events"))
        blob = " ".join(r[0] or "" for r in rows)
        assert secret not in blob


# --------------------------------------------------------------------------- #
# The timeline
# --------------------------------------------------------------------------- #

async def test_the_timeline_is_append_only(app_session_factory, tenant_a, provider):
    """Evidence the application can rewrite is not evidence."""
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        await _file(s, tenant_a, principal_id=principal.id)
        await s.flush()

        with pytest.raises(DBAPIError):
            await s.execute(text("UPDATE grievance_events SET note='rewritten'"))


async def test_the_timeline_cannot_be_deleted(app_session_factory, tenant_a, provider):
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        await _file(s, tenant_a, principal_id=principal.id)
        await s.flush()

        with pytest.raises(DBAPIError):
            await s.execute(text("DELETE FROM grievance_events"))


async def test_an_automated_escalation_is_marked_as_such(
    app_session_factory, tenant_a, provider
):
    """"The system escalated this because nobody answered" and "a human decided
    to escalate this" are different facts. Conflating them lets inaction read as
    judgement."""
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        grievance = await _overdue(s, tenant_a, principal.id)
        await grievance_service.sweep_escalations(s, tenant_id=tenant_a["id"])

        events = await grievance_service.timeline(s, tenant_a["id"], grievance.id)
        automated = [e for e in events if e.automated]
        assert len(automated) == 1
        assert automated[0].actor_type == "system"


# --------------------------------------------------------------------------- #
# Assignment
# --------------------------------------------------------------------------- #

async def test_a_deactivated_user_cannot_be_assigned(
    app_session_factory, tenant_a, provider
):
    """Assigning a statutory complaint to somebody who cannot sign in is how a
    deadline passes unnoticed."""
    from app.services import tenant_service

    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        grievance, _ = await _file(s, tenant_a, principal_id=principal.id)

        # A second, non-admin user. Deactivating the tenant's only admin is now
        # refused by a trigger on `users` — correctly, since a workspace with no
        # administrator cannot be recovered — so this uses the realistic case: a
        # colleague who has left.
        leaver = await tenant_service.create_user(
            s, tenant_id=tenant_a["id"], email="leaver@tenant-a.example.com",
            full_name="Has Left", role="grievance_officer",
            password="correct-horse-battery-staple-leaver", actor=_actor(tenant_a),
        )
        leaver.is_active = False
        await s.flush()

        with pytest.raises(Conflict) as exc:
            await grievance_service.assign(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                grievance=grievance, user_id=leaver.id,
            )
        assert "deactivated" in str(exc.value)


async def test_a_user_from_another_tenant_cannot_be_assigned(
    app_session_factory, tenant_a, tenant_b, provider
):
    """RLS makes this impossible rather than merely discouraged."""
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        principal = await _principal(s, tenant_a)
        grievance, _ = await _file(s, tenant_a, principal_id=principal.id)
        with pytest.raises(NotFound):
            await grievance_service.assign(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                grievance=grievance, user_id=tenant_b["admin_id"],
            )


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #

async def test_grievances_are_tenant_isolated(
    app_session_factory, tenant_a, tenant_b, provider
):
    provider()
    async with app_session_factory() as s:
        await s.begin()
        await set_tenant_context(s, tenant_b["id"])
        principal = await _principal(s, tenant_b)
        await _file(s, tenant_b, principal_id=principal.id)
        await s.commit()

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        assert await grievance_service.list_for_tenant(s, tenant_a["id"]) == []
        counts = await grievance_service.counts(s, tenant_a["id"])
        assert counts["total"] == 0


async def test_a_principal_sees_only_their_own(app_session_factory, tenant_a, provider):
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        mine = await _principal(s, tenant_a, email="mine@example.com")
        theirs = await _principal(s, tenant_a, email="theirs@example.com")
        await _file(s, tenant_a, principal_id=mine.id)
        await _file(s, tenant_a, principal_id=theirs.id)

        rows = await grievance_service.list_for_principal(s, tenant_a["id"], mine.id)
        assert len(rows) == 1
        assert rows[0].principal_id == mine.id


# --------------------------------------------------------------------------- #
# The published officer
# --------------------------------------------------------------------------- #

async def test_a_new_workspace_publishes_an_officer_by_default(
    app_session_factory, tenant_a
):
    """§13 requires a published contact. A workspace that is non-compliant the
    moment it is created would be a trap; the first admin is the default."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        officer = await grievance_service.officer(s, tenant_a["id"])
        assert officer["published"] is True
        assert officer["email"] == tenant_a["admin_email"]


async def test_a_cleared_officer_reports_unpublished_rather_than_blank(
    app_session_factory, tenant_a
):
    """An empty field on a public page is a compliance gap that looks like a
    design choice. This is what lets the screen say so instead."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        tenant = await s.scalar(select(Tenant).where(Tenant.id == tenant_a["id"]))
        tenant.grievance_officer_name = None
        tenant.grievance_officer_email = None
        await s.flush()

        officer = await grievance_service.officer(s, tenant_a["id"])
        assert officer["published"] is False


async def test_escalation_with_no_published_officer_suppresses_with_a_reason(
    app_session_factory, tenant_a, provider
):
    """The escalation still happens and is still on the record.

    Silently dropping it because nobody is configured to receive it would hide
    the one fact a DPO most needs: that a statutory alarm fired into the void.
    """
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        tenant = await s.scalar(select(Tenant).where(Tenant.id == tenant_a["id"]))
        tenant.grievance_officer_email = None
        await s.flush()

        principal = await _principal(s, tenant_a)
        grievance = await _overdue(s, tenant_a, principal.id)
        assert await grievance_service.sweep_escalations(s, tenant_id=tenant_a["id"]) == 1
        assert grievance.escalated is True

        rows = await notification_service.log_for_tenant(s, tenant_a["id"])
        esc = [r for r in rows if r.template_key == "grievance.escalated"]
        assert len(esc) == 1
        assert esc[0].status == "suppressed"
        assert esc[0].suppression_reason


# --------------------------------------------------------------------------- #
# The Grievance Officer's restricted view — over real HTTP
# --------------------------------------------------------------------------- #
#
# The role's nav is already restricted, but nav is presentation. These go through
# the actual app so the assertion is about the routes, not about what a sidebar
# chooses to render. A role whose limits exist only in the UI has no limits.

async def _sign_in(client, email: str, password: str, workspace: str) -> str:
    r = await client.post(
        "/v1/auth/login",
        json={"tenant_slug": workspace, "email": email, "password": password},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture
async def officer_login(app_session_factory, tenant_a, client):
    """A real grievance_officer account, signed in over HTTP."""
    from app.services import tenant_service

    email = "officer@tenant-a.example.com"
    password = "correct-horse-battery-staple-2"
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await tenant_service.create_user(
            s, tenant_id=tenant_a["id"], email=email, full_name="Grievance Officer",
            role="grievance_officer", password=password, actor=_actor(tenant_a),
        )
        await s.commit()
    token = await _sign_in(client, email, password, tenant_a["slug"])
    return {"token": token, "headers": {"Authorization": f"Bearer {token}"}}


async def test_a_grievance_officer_can_reach_the_queue(client, officer_login):
    r = await client.get("/v1/grievances", headers=officer_login["headers"])
    assert r.status_code == 200, r.text
    assert "items" in r.json() and "counts" in r.json()


@pytest.mark.parametrize(
    "path",
    [
        "/v1/consents",
        "/v1/dsar",
        "/v1/audit",
        "/v1/admin/users",
        "/v1/retention/policies",
        "/v1/notifications/templates",
    ],
)
async def test_a_grievance_officer_is_refused_everything_else(
    client, officer_login, path
):
    """403, not 404 and not 200.

    Enforced by the routes' own capability checks. If any of these ever returns
    200 it means a capability was widened somewhere far away from this file, and
    somebody hired to handle complaints can read every consent record in the
    workspace.
    """
    r = await client.get(path, headers=officer_login["headers"])
    assert r.status_code == 403, f"{path} returned {r.status_code}: {r.text[:200]}"


async def test_a_grievance_officer_cannot_change_the_published_officer(
    client, officer_login
):
    """Changing who complaints escalate to is a tenant-administration act.

    An officer who could redirect their own escalations elsewhere would make the
    escalation clock unenforceable against them.
    """
    r = await client.put(
        "/v1/grievances/officer",
        headers=officer_login["headers"],
        json={"name": "Someone Else", "email": "elsewhere@example.com"},
    )
    assert r.status_code == 403, r.text


async def test_one_person_cannot_rate_anothers_grievance(
    app_session_factory, tenant_a, client, provider
):
    """A rating of 1 reopens a grievance, so this is a write dressed as feedback.

    Answered with 404 rather than 403: confirming that a grievance exists but
    belongs to somebody else is itself a disclosure.
    """
    provider()
    from app.services import tenant_service

    # A grievance belonging to somebody else entirely.
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        victim = await _principal(s, tenant_a, email="victim@example.com")
        grievance, _ = await _file(s, tenant_a, principal_id=victim.id)
        await grievance_service.change_status(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), grievance=grievance,
            to_status="acknowledged",
        )
        await grievance_service.change_status(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), grievance=grievance,
            to_status="resolved", resolution_notes="Handled.",
        )
        grievance_id = grievance.id

        # An unrelated account with a login.
        await tenant_service.create_user(
            s, tenant_id=tenant_a["id"], email="nosy@tenant-a.example.com",
            full_name="Nosy Person", role="data_principal",
            password="correct-horse-battery-staple-3", actor=_actor(tenant_a),
        )
        await s.commit()

    token = await _sign_in(
        client, "nosy@tenant-a.example.com", "correct-horse-battery-staple-3",
        tenant_a["slug"],
    )
    r = await client.post(
        f"/v1/grievances/{grievance_id}/feedback",
        headers={"Authorization": f"Bearer {token}"},
        json={"rating": 1, "comment": "reopening someone else's complaint"},
    )
    assert r.status_code == 404, r.text

    # And it is still resolved.
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        row = await grievance_service.get(s, tenant_a["id"], grievance_id)
        assert row.status == "resolved"
        assert row.satisfaction_rating is None


async def test_a_data_principal_cannot_read_the_queue(
    app_session_factory, tenant_a, client
):
    from app.services import tenant_service

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await tenant_service.create_user(
            s, tenant_id=tenant_a["id"], email="dp@tenant-a.example.com",
            full_name="A Person", role="data_principal",
            password="correct-horse-battery-staple-4", actor=_actor(tenant_a),
        )
        await s.commit()

    token = await _sign_in(
        client, "dp@tenant-a.example.com", "correct-horse-battery-staple-4",
        tenant_a["slug"],
    )
    r = await client.get("/v1/grievances", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403, r.text

    # But they can see their own, and the published officer.
    for path in ("/v1/grievances/mine", "/v1/grievances/officer"):
        ok = await client.get(path, headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200, f"{path}: {ok.text[:200]}"


# --------------------------------------------------------------------------- #
# Public filing, over HTTP
# --------------------------------------------------------------------------- #

async def test_anyone_can_file_without_an_account(
    app_session_factory, tenant_a, client, provider
):
    """The point of the endpoint.

    Somebody whose number a company bought from a broker has no account and never
    will. Requiring one would put a barrier in front of a statutory right.
    """
    impl = provider()
    r = await client.post(
        "/public/v1/grievance",
        json={
            "workspace": tenant_a["slug"],
            "category": "consent_violation",
            "description": "I never gave you my number and you keep texting me.",
            "contact_email": "stranger@example.com",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["reference"].startswith("GRV-")
    assert body["confirmation_required"] is True

    # The confirmation code went out, and the response did NOT contain it.
    assert "code" not in r.text
    assert impl.sent, "a confirmation email must have been sent"
    assert impl.sent[0]["to"] == "stranger@example.com"


async def test_an_unknown_workspace_does_not_confirm_which_ones_exist(client):
    """A vague 404. Telling an anonymous caller which workspaces are real would
    turn this into a customer-list oracle."""
    r = await client.post(
        "/public/v1/grievance",
        json={
            "workspace": "no-such-company",
            "category": "other",
            "description": "Something went wrong with my data.",
            "contact_email": "a@example.com",
        },
    )
    assert r.status_code == 404
    assert "no-such-company" not in r.text


async def test_the_public_confirmation_round_trip_works(
    app_session_factory, tenant_a, client, provider
):
    """File anonymously, read the code out of the email, confirm it."""
    impl = provider()
    filed = await client.post(
        "/public/v1/grievance",
        json={
            "workspace": tenant_a["slug"],
            "category": "dsar_delay",
            "description": "I asked for my data a month ago and heard nothing.",
            "contact_email": "waiting@example.com",
        },
    )
    assert filed.status_code == 201, filed.text
    reference = filed.json()["reference"]

    # The token is only in the message — never stored, never echoed.
    body = impl.sent[0]["body"]
    code = next(
        line.strip() for line in body.splitlines()
        if line.strip() and " " not in line.strip() and len(line.strip()) > 20
    )

    confirmed = await client.post(
        "/public/v1/grievance/confirm",
        json={"workspace": tenant_a["slug"], "reference": reference, "token": code},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["confirmed"] is True

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        row = await s.scalar(
            select(Grievance).where(Grievance.reference == reference)
        )
        assert row.contact_verified is True
        assert row.verification_token_hash is None, "single use"


async def test_the_public_endpoint_throttles_a_repeat_filer(
    app_session_factory, tenant_a, client, provider
):
    provider()
    payload = {
        "workspace": tenant_a["slug"],
        "category": "other",
        "description": "The same complaint, filed twice in a row.",
        "contact_email": "repeat@example.com",
    }
    first = await client.post("/public/v1/grievance", json=payload)
    assert first.status_code == 201, first.text

    second = await client.post("/public/v1/grievance", json=payload)
    assert second.status_code == 409, second.text
    assert "confirmed" in second.text


async def test_the_public_endpoint_cannot_read_anything(client, tenant_a):
    """It accepts and it confirms. Nothing else.

    A status endpoint keyed on a reference somebody could guess would leak
    complaints; tracking requires the account portal.
    """
    for method, path in (
        ("get", "/public/v1/grievance"),
        ("get", f"/public/v1/grievance/{tenant_a['slug']}"),
    ):
        r = await getattr(client, method)(path)
        assert r.status_code in (404, 405), f"{path} → {r.status_code}"


async def test_a_public_filing_lands_in_the_queue_awaiting_confirmation(
    app_session_factory, tenant_a, client, provider
):
    """Not escalatable is not the same as invisible.

    A DPO must be able to see the pile — that is how they notice they are being
    spammed, or that their confirmation emails are not being delivered.
    """
    provider()
    await client.post(
        "/public/v1/grievance",
        json={
            "workspace": tenant_a["slug"],
            "category": "inaccurate_data",
            "description": "My address on your records has been wrong for a year.",
            "contact_email": "wrong-address@example.com",
        },
    )
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        counts = await grievance_service.counts(s, tenant_a["id"])
        assert counts["open"] == 1
        assert counts["awaiting_confirmation"] == 1
        assert counts["escalated"] == 0


# --------------------------------------------------------------------------- #
# The full lifecycle over HTTP
# --------------------------------------------------------------------------- #
#
# Added because every test above calls the service directly, and that turned out
# to be a real blind spot: the routes build their response by reading every column
# off the ORM object, which behaves differently from a service call inside an open
# transaction. A 500 on "acknowledge" got all the way to a manual check.

@pytest.fixture
async def admin_login(app_session_factory, tenant_a, client):
    token = await _sign_in(
        client, tenant_a["admin_email"], tenant_a["password"], tenant_a["slug"]
    )
    return {"Authorization": f"Bearer {token}"}


async def test_the_whole_lifecycle_over_http(
    app_session_factory, tenant_a, client, admin_login, provider
):
    """File → acknowledge → in progress → resolve → rate → reopened.

    Every step asserted on the HTTP response, because that is what the browser
    sees and it is not the same code path as the service.
    """
    provider()

    filed = await client.post(
        "/v1/grievances",
        headers=admin_login,
        json={
            "category": "inaccurate_data",
            "description": "My postal address has been wrong for over a year.",
        },
    )
    assert filed.status_code == 201, filed.text
    gid = filed.json()["id"]
    assert filed.json()["status"] == "open"

    for to_status in ("acknowledged", "in_progress"):
        r = await client.patch(
            f"/v1/grievances/{gid}", headers=admin_login,
            json={"to_status": to_status},
        )
        assert r.status_code == 200, f"{to_status}: {r.text[:400]}"
        assert r.json()["status"] == to_status

    # Resolving with no notes must be refused, over HTTP too.
    refused = await client.patch(
        f"/v1/grievances/{gid}", headers=admin_login, json={"to_status": "resolved"}
    )
    assert refused.status_code == 409, refused.text

    resolved = await client.patch(
        f"/v1/grievances/{gid}", headers=admin_login,
        json={"to_status": "resolved", "resolution_notes": "Corrected in the CRM."},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["resolution_notes"] == "Corrected in the CRM."
    assert resolved.json()["resolved_at"]

    rated = await client.post(
        f"/v1/grievances/{gid}/feedback", headers=admin_login,
        json={"rating": 1, "comment": "Still wrong on my statements."},
    )
    assert rated.status_code == 200, rated.text
    assert rated.json()["status"] == "reopened"
    assert rated.json()["satisfaction_rating"] == 1
    # The timeline records the automatic reopen as automatic.
    notes = " ".join(e["note"] or "" for e in rated.json()["timeline"])
    assert "Reopened automatically" in notes


async def test_assign_and_escalate_over_http(
    app_session_factory, tenant_a, client, admin_login, provider
):
    provider()
    filed = await client.post(
        "/v1/grievances",
        headers=admin_login,
        json={"category": "other", "description": "Something is wrong with my data."},
    )
    gid = filed.json()["id"]

    assigned = await client.post(
        f"/v1/grievances/{gid}/assign", headers=admin_login,
        json={"user_id": str(tenant_a["admin_id"])},
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["assigned_to"] == str(tenant_a["admin_id"])

    escalated = await client.post(
        f"/v1/grievances/{gid}/escalate", headers=admin_login,
        json={"reason": "Customer called twice."},
    )
    assert escalated.status_code == 200, escalated.text
    assert escalated.json()["escalated"] is True

    # Idempotent over HTTP as well — not an error the second time.
    again = await client.post(
        f"/v1/grievances/{gid}/escalate", headers=admin_login, json={}
    )
    assert again.status_code == 200, again.text


async def test_the_queue_endpoint_returns_counts_and_computed_fields(
    app_session_factory, tenant_a, client, admin_login, provider
):
    provider()
    await client.post(
        "/v1/grievances", headers=admin_login,
        json={"category": "dsar_delay", "description": "No answer to my request."},
    )
    r = await client.get("/v1/grievances", headers=admin_login)
    assert r.status_code == 200, r.text
    page = r.json()
    assert page["counts"]["open"] == 1
    row = page["items"][0]
    # Computed against the clock, not stored.
    assert row["is_overdue"] is False
    assert row["days_open"] == 0
