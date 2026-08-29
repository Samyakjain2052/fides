"""Breaches — where a false record of compliance is the worst outcome.

Most modules fail by not doing something. This one can fail by producing a
confident, well-formatted claim that a statutory duty was discharged when half of
it was not — and a regulator can disprove that in one question. So the tests
concentrate on the places the product could lie:

* marking a breach `notified` having told only the Board,
* closing with affected people un-notified and no reason,
* a bulk run that double-notifies, or reports optimistic progress,
* a quiet change to `discovered_at`, the field every deadline hangs on,
* and the affected list leaking to a role that has no business reading it.
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
from app.models.audit import AuditEvent
from app.models.breach import (
    BOARD_NOTIFICATION_HOURS,
    Breach,
    BreachAffectedPrincipal,
    BreachEvent,
)
from app.models.consent import DataPrincipal
from app.models.notification import Notification
from app.services import breach_service, consent_service, notice_service
from app.services.audit_service import Actor
from app.services.notification_providers import SendResult

CATEGORY = "Contact Data"


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
    name = "capture"

    def __init__(self, fail_after: int | None = None):
        self.sent: list[dict] = []
        self.fail_after = fail_after

    async def send(self, *, to, subject, body, channel, html_body=None):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            return SendResult(ok=False, error="provider rate limit", retryable=True)
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
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _record(session, tenant, **kw):
    defaults = {
        "title": "Misconfigured storage bucket",
        "description": "A marketing export was readable without credentials for ~6h.",
        "severity": "high",
        "discovered_at": datetime.now(UTC) - timedelta(hours=2),
        "categories_affected": [CATEGORY],
    }
    return await breach_service.record(
        session, tenant_id=tenant["id"], actor=_actor(tenant), **{**defaults, **kw}
    )


async def _affected_people(session, tenant, count: int):
    """`count` principals holding a consent in CATEGORY, so the query finds them."""
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
    people = []
    for i in range(count):
        p = DataPrincipal(
            tenant_id=tenant["id"], external_id=f"cust-{uuid.uuid4().hex[:8]}",
            email=f"affected{i}@example.com",
        )
        session.add(p)
        await session.flush()
        await consent_service.grant(
            session, tenant_id=tenant["id"], actor=_actor(tenant),
            principal_id=p.id, purpose_id=purpose.id,
        )
        people.append(p)
    await session.flush()
    return people


# --------------------------------------------------------------------------- #
# Half a notification is not a notification
# --------------------------------------------------------------------------- #

async def test_status_notified_cannot_be_set_directly(app_session_factory, tenant_a):
    """The status follows the work.

    A status the UI can assert independently of the work is a status that will
    eventually be wrong, and this one is a claim about a statutory duty.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach = await _record(s, tenant_a)
        with pytest.raises(Conflict) as exc:
            await breach_service.change_status(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
                to_status="notified",
            )
        assert "by notifying" in str(exc.value)


async def test_the_database_refuses_notified_with_only_the_board(
    app_session_factory, tenant_a
):
    """The constraint behind the service rule.

    A service rule can be bypassed by the next code path somebody writes; this
    asserts the database itself will not hold a half-notification.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach = await _record(s, tenant_a)
        await s.flush()
        with pytest.raises(IntegrityError):
            await s.execute(
                text(
                    "UPDATE breaches SET status='notified', board_notified_at=now(), "
                    "board_submitted_by='someone', principals_notified_at=NULL "
                    "WHERE id=:i"
                ),
                {"i": str(breach.id)},
            )


async def test_the_database_refuses_notified_with_only_the_principals(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach = await _record(s, tenant_a)
        await s.flush()
        with pytest.raises(IntegrityError):
            await s.execute(
                text(
                    "UPDATE breaches SET status='notified', "
                    "principals_notified_at=now(), board_notified_at=NULL "
                    "WHERE id=:i"
                ),
                {"i": str(breach.id)},
            )


async def test_notified_is_reached_only_when_both_halves_are_done(
    app_session_factory, tenant_a, provider
):
    """The happy path, asserted at each step rather than only at the end."""
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        people = await _affected_people(s, tenant_a, 3)
        breach = await _record(s, tenant_a)
        await breach_service.change_status(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
            to_status="investigating",
        )
        await breach_service.update(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
            remediation="Bucket policy corrected and access logs reviewed.",
        )
        await breach_service.attach_affected(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
            principal_ids=[p.id for p in people],
        )

        # Board only.
        await breach_service.notify_board(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
            submitted_by="Meena Patel", reference="DPB/2026/00412",
        )
        assert breach.status == "investigating", "one half is not the duty"

        # Then the people.
        progress = await breach_service.notify_principals(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
        )
        assert progress.complete
        assert breach.status == "notified"
        assert breach.principals_notified_at is not None


# --------------------------------------------------------------------------- #
# The awareness date
# --------------------------------------------------------------------------- #

async def test_a_breach_cannot_leave_draft_without_an_awareness_date(
    app_session_factory, tenant_a
):
    """Every deadline in §8(6) is measured from it."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach = await _record(s, tenant_a, discovered_at=None)
        with pytest.raises(Conflict) as exc:
            await breach_service.change_status(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
                to_status="investigating",
            )
        assert "became aware" in str(exc.value)


async def test_the_database_refuses_a_non_draft_without_an_awareness_date(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach = await _record(s, tenant_a, discovered_at=None)
        await s.flush()
        with pytest.raises(IntegrityError):
            await s.execute(
                text("UPDATE breaches SET status='investigating' WHERE id=:i"),
                {"i": str(breach.id)},
            )


async def test_changing_the_awareness_date_requires_a_reason(
    app_session_factory, tenant_a
):
    """Backdating awareness is the most consequential edit anybody can make here.

    It moves the deadline the fiduciary is judged against, so it must not be
    possible to do it quietly.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach = await _record(s, tenant_a)
        with pytest.raises(Conflict) as exc:
            await breach_service.update(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
                discovered_at=datetime.now(UTC) - timedelta(days=5),
            )
        assert "needs a reason" in str(exc.value)


async def test_the_old_and_new_awareness_dates_both_reach_the_audit_chain(
    app_session_factory, tenant_a
):
    """The chain must show the movement, not just the destination.

    "It was always this date" is exactly the claim this entry exists to disprove.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        original = datetime.now(UTC) - timedelta(hours=2)
        breach = await _record(s, tenant_a, discovered_at=original)
        moved = datetime.now(UTC) - timedelta(days=5)

        await breach_service.update(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
            discovered_at=moved,
            discovered_at_reason="Log review showed the alert was seen on the 12th.",
        )

        entry = await s.scalar(
            select(AuditEvent)
            .where(AuditEvent.action == "breach.discovery_changed")
            .order_by(AuditEvent.seq.desc())
        )
        assert entry is not None
        assert entry.payload["from"] == original.isoformat()
        assert entry.payload["to"] == moved.isoformat()
        assert "Log review" in entry.payload["reason"]

        # And on the timeline a human reads.
        events = await breach_service.timeline(s, tenant_a["id"], breach.id)
        assert any("Awareness date changed" in (e.note or "") for e in events)


async def test_discovery_cannot_precede_occurrence(app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(Conflict):
            await _record(
                s, tenant_a,
                occurred_at=datetime.now(UTC),
                discovered_at=datetime.now(UTC) - timedelta(days=1),
            )


async def test_the_board_deadline_is_null_without_an_awareness_date(
    app_session_factory, tenant_a
):
    """A deadline computed from a missing start is a fabricated number."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach = await _record(s, tenant_a, discovered_at=None)
        assert breach.board_deadline_at is None
        assert breach.board_overdue is False
        assert breach.hours_since_discovery is None


async def test_an_un_notified_breach_past_the_threshold_reads_as_overdue(
    app_session_factory, tenant_a
):
    """Computed against the clock, so it is true the moment it becomes true."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach = await _record(
            s, tenant_a,
            discovered_at=datetime.now(UTC)
            - timedelta(hours=BOARD_NOTIFICATION_HOURS + 1),
        )
        assert breach.board_overdue is True

        counts = await breach_service.counts(s, tenant_a["id"])
        assert counts["board_overdue"] == 1


# --------------------------------------------------------------------------- #
# The Board submission is a human's action
# --------------------------------------------------------------------------- #

async def test_notifying_the_board_requires_naming_the_submitter(
    app_session_factory, tenant_a
):
    """The product does not submit anything, so the record has to say who did.

    "The system reported it" would be false, and a compliance record whose most
    load-bearing claim is false is worse than no record.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach = await _record(s, tenant_a)
        with pytest.raises(Conflict) as exc:
            await breach_service.notify_board(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
                submitted_by="  ",
            )
        assert "who did" in str(exc.value)


async def test_the_board_notification_cannot_predate_awareness(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach = await _record(s, tenant_a)
        with pytest.raises(Conflict):
            await breach_service.notify_board(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
                submitted_by="Meena Patel",
                submitted_at=breach.discovered_at - timedelta(hours=1),
            )


async def test_the_board_cannot_be_recorded_as_notified_twice(
    app_session_factory, tenant_a
):
    """Overwriting the first submission would erase when it actually happened."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach = await _record(s, tenant_a)
        await breach_service.notify_board(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
            submitted_by="Meena Patel", reference="DPB/1",
        )
        with pytest.raises(Conflict) as exc:
            await breach_service.notify_board(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
                submitted_by="Someone Else", reference="DPB/2",
            )
        assert "already notified" in str(exc.value)
        assert breach.board_reference == "DPB/1"


async def test_lateness_is_recorded_at_the_time_rather_than_recomputed(
    app_session_factory, tenant_a
):
    """The threshold is our reading, not a statutory figure, and it may change.

    Recording the judgement with the threshold it was made against means a later
    change to that number does not retroactively make somebody late.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach = await _record(
            s, tenant_a,
            discovered_at=datetime.now(UTC)
            - timedelta(hours=BOARD_NOTIFICATION_HOURS + 5),
        )
        await breach_service.notify_board(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
            submitted_by="Meena Patel",
        )
        entry = await s.scalar(
            select(AuditEvent)
            .where(AuditEvent.action == "breach.board_notified")
            .order_by(AuditEvent.seq.desc())
        )
        assert entry.payload["late"] is True
        assert entry.payload["within_threshold_hours"] == BOARD_NOTIFICATION_HOURS
        assert entry.payload["hours_after_discovery"] > BOARD_NOTIFICATION_HOURS


async def test_the_board_notice_names_the_product_as_not_submitting(
    app_session_factory, tenant_a
):
    from app.models.tenant import Tenant

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach = await _record(s, tenant_a)
        tenant = await s.scalar(select(Tenant).where(Tenant.id == tenant_a["id"]))
        content = breach_service.board_notification_content(breach, tenant)
        assert "section 8(6)" in content
        assert breach.reference in content
        assert "does not transmit anything to the Board" in content


# --------------------------------------------------------------------------- #
# The affected list
# --------------------------------------------------------------------------- #

async def test_the_category_query_finds_people_with_a_consent_in_that_category(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        people = await _affected_people(s, tenant_a, 4)
        found = await breach_service.find_by_categories(
            s, tenant_id=tenant_a["id"], categories=[CATEGORY]
        )
        assert {p.id for p in found} == {p.id for p in people}


async def test_the_query_excludes_already_purged_principals(
    app_session_factory, tenant_a
):
    """Their identifiers are masked, so there is nobody left to write to.

    Including them would inflate the affected count a regulator is shown with
    people who cannot be notified even in principle.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        people = await _affected_people(s, tenant_a, 3)
        people[0].purged_at = datetime.now(UTC)
        await s.flush()

        found = await breach_service.find_by_categories(
            s, tenant_id=tenant_a["id"], categories=[CATEGORY]
        )
        assert people[0].id not in {p.id for p in found}
        assert len(found) == 2


async def test_attaching_the_same_person_twice_adds_them_once(
    app_session_factory, tenant_a
):
    """Both the notification count and the figure shown to a regulator come off
    this table, so a duplicate would corrupt both."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        people = await _affected_people(s, tenant_a, 3)
        breach = await _record(s, tenant_a)
        ids = [p.id for p in people]

        first = await breach_service.attach_affected(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
            principal_ids=ids,
        )
        second = await breach_service.attach_affected(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
            principal_ids=ids,
        )
        assert first["added"] == 3
        assert second["added"] == 0 and second["already_listed"] == 3
        assert second["total"] == 3


async def test_the_database_refuses_a_duplicate_attachment(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        people = await _affected_people(s, tenant_a, 1)
        breach = await _record(s, tenant_a)
        for _ in range(1):
            s.add(BreachAffectedPrincipal(
                tenant_id=tenant_a["id"], breach_id=breach.id,
                principal_id=people[0].id,
            ))
        await s.flush()
        s.add(BreachAffectedPrincipal(
            tenant_id=tenant_a["id"], breach_id=breach.id, principal_id=people[0].id,
        ))
        with pytest.raises(IntegrityError):
            await s.flush()


# --------------------------------------------------------------------------- #
# The bulk notification
# --------------------------------------------------------------------------- #

async def _ready_to_notify(session, tenant, *, people: int = 5):
    folk = await _affected_people(session, tenant, people)
    breach = await _record(session, tenant)
    await breach_service.update(
        session, tenant_id=tenant["id"], actor=_actor(tenant), breach=breach,
        remediation="Bucket policy corrected; access logs reviewed.",
    )
    await breach_service.attach_affected(
        session, tenant_id=tenant["id"], actor=_actor(tenant), breach=breach,
        principal_ids=[p.id for p in folk],
    )
    return breach, folk


async def test_a_bulk_run_is_resumable_and_never_double_notifies(
    app_session_factory, tenant_a, provider
):
    """Ten thousand people and a rate limit means the first run will not finish.

    This is the property that makes the second attempt safe rather than harmful.
    """
    impl = provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach, folk = await _ready_to_notify(s, tenant_a, people=5)

        first = await breach_service.notify_principals(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach, batch=2,
        )
        assert (first.notified, first.remaining, first.complete) == (2, 3, False)

        second = await breach_service.notify_principals(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach, batch=2,
        )
        assert (second.notified, second.remaining) == (4, 1)

        third = await breach_service.notify_principals(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach, batch=10,
        )
        assert (third.notified, third.remaining, third.complete) == (5, 0, True)

        # Exactly one message per person, across three runs.
        assert len(impl.sent) == 5
        assert len({m["to"] for m in impl.sent}) == 5

        rows = await s.scalar(
            select(func.count()).select_from(Notification)
            .where(Notification.template_key == "breach.principal_notice")
        )
        assert rows == 5


async def test_a_further_run_after_completion_does_nothing(
    app_session_factory, tenant_a, provider
):
    impl = provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach, _folk = await _ready_to_notify(s, tenant_a, people=3)
        await breach_service.notify_principals(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
        )
        sent_after_first = len(impl.sent)

        again = await breach_service.notify_principals(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
        )
        assert again.complete
        assert len(impl.sent) == sent_after_first, "nobody was told twice"


async def test_progress_is_counted_from_rows_not_remembered(
    app_session_factory, tenant_a, provider
):
    """The figure a DPO uses to decide whether the duty is discharged.

    Counted from the table, so it survives a crash and cannot be optimistic.
    """
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach, _folk = await _ready_to_notify(s, tenant_a, people=6)
        await breach_service.notify_principals(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach, batch=4,
        )
        progress = await breach_service.notify_progress(
            s, tenant_id=tenant_a["id"], breach=breach
        )
        handled = await s.scalar(
            select(func.count()).select_from(BreachAffectedPrincipal).where(
                BreachAffectedPrincipal.breach_id == breach.id,
                BreachAffectedPrincipal.notified_at.isnot(None),
            )
        )
        assert progress.notified + progress.suppressed == handled
        assert progress.summary == "4 of 6 notified"


async def test_somebody_with_no_address_is_recorded_as_unreachable_not_skipped(
    app_session_factory, tenant_a, provider
):
    """Otherwise the run never completes and `remaining` is permanently wrong.

    "We could not tell this person, because we hold no address" is an answer a
    fiduciary needs to be able to give — and it counts differently from delivered.
    """
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach, folk = await _ready_to_notify(s, tenant_a, people=3)
        folk[0].email = None
        await s.flush()

        progress = await breach_service.notify_principals(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
        )
        assert progress.complete, "the run finishes"
        assert progress.suppressed == 1
        assert progress.notified == 2, "the unreachable person is not counted as told"

        rows = await breach_service.affected_list(s, tenant_a["id"], breach.id)
        unreachable = [link for link, _p in rows if link.suppressed_reason]
        assert len(unreachable) == 1
        assert "address" in unreachable[0].suppressed_reason


async def test_notifying_requires_saying_what_was_done_about_it(
    app_session_factory, tenant_a, provider
):
    """A notice describing a breach with no remedy tells somebody they have a
    problem and nothing they can do about it."""
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        folk = await _affected_people(s, tenant_a, 2)
        breach = await _record(s, tenant_a)
        await breach_service.attach_affected(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
            principal_ids=[p.id for p in folk],
        )
        with pytest.raises(Conflict) as exc:
            await breach_service.notify_principals(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
            )
        assert "what you have done" in str(exc.value)


async def test_notifying_an_empty_affected_list_is_refused(
    app_session_factory, tenant_a, provider
):
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach = await _record(s, tenant_a)
        await breach_service.update(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
            remediation="Fixed.",
        )
        with pytest.raises(Conflict) as exc:
            await breach_service.notify_principals(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
            )
        assert "affected list" in str(exc.value)


async def test_the_notice_tells_the_person_what_and_when(
    app_session_factory, tenant_a, provider
):
    impl = provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach, _folk = await _ready_to_notify(s, tenant_a, people=1)
        await breach_service.notify_principals(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
        )
        body = impl.sent[0]["body"]
        assert breach.reference in body
        assert CATEGORY in body
        assert breach.discovered_at.date().isoformat() in body
        # And where to go next.
        assert "Grievance Officer" in body
        assert "Data Protection Board" in body


# --------------------------------------------------------------------------- #
# Closing and voiding
# --------------------------------------------------------------------------- #

async def test_closing_requires_a_root_cause_and_a_remediation(
    app_session_factory, tenant_a, provider
):
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach, _folk = await _ready_to_notify(s, tenant_a, people=1)
        await breach_service.notify_board(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
            submitted_by="Meena Patel",
        )
        await breach_service.notify_principals(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
        )
        with pytest.raises(Conflict) as exc:
            await breach_service.close(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
                root_cause="", remediation="",
            )
        assert "teaches nobody anything" in str(exc.value)


async def test_the_database_refuses_a_closed_breach_with_no_cause(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach = await _record(s, tenant_a)
        await s.flush()
        with pytest.raises(IntegrityError):
            await s.execute(
                text("UPDATE breaches SET status='closed', closed_at=now() WHERE id=:i"),
                {"i": str(breach.id)},
            )


async def test_a_breach_cannot_close_without_notifying_the_board(
    app_session_factory, tenant_a, provider
):
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach, _folk = await _ready_to_notify(s, tenant_a, people=1)
        await breach_service.notify_principals(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
        )
        with pytest.raises(Conflict) as exc:
            await breach_service.close(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
                root_cause="Misconfigured bucket policy.", remediation="Corrected.",
            )
        assert "statutory obligation" in str(exc.value)


async def test_a_breach_cannot_close_with_people_un_notified_and_no_exemption(
    app_session_factory, tenant_a, provider
):
    """The check the whole module is built around.

    Notifying the Board and notifying the people are separate obligations, and
    closing on the strength of the first would record compliance that did not
    happen.
    """
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach, _folk = await _ready_to_notify(s, tenant_a, people=5)
        await breach_service.notify_board(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
            submitted_by="Meena Patel",
        )
        await breach_service.notify_principals(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach, batch=2,
        )
        with pytest.raises(Conflict) as exc:
            await breach_service.close(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
                root_cause="Misconfigured bucket policy.", remediation="Corrected.",
            )
        assert "3 affected data principal(s) have not been notified" in str(exc.value)


async def test_a_written_exemption_allows_closing_and_is_kept(
    app_session_factory, tenant_a, provider
):
    """Text, not a flag: "we decided not to tell them" needs a sentence attached."""
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach, _folk = await _ready_to_notify(s, tenant_a, people=4)
        await breach_service.notify_board(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
            submitted_by="Meena Patel",
        )
        await breach_service.notify_principals(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach, batch=1,
        )
        await breach_service.close(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
            root_cause="Misconfigured bucket policy.",
            remediation="Corrected and reviewed.",
            notification_exemption=(
                "Remaining records were pseudonymised exports with no recoverable "
                "contact address; DPO decision recorded 18 Aug."
            ),
        )
        assert breach.status == "closed"
        assert "pseudonymised" in breach.notification_exemption

        entry = await s.scalar(
            select(AuditEvent).where(AuditEvent.action == "breach.closed")
            .order_by(AuditEvent.seq.desc())
        )
        assert entry.payload["unnotified"] == 3
        assert entry.payload["exemption"]


async def test_a_closed_breach_is_terminal(app_session_factory, tenant_a, provider):
    """Reopening would let the record of what was learned be rewritten."""
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach, _folk = await _ready_to_notify(s, tenant_a, people=1)
        await breach_service.notify_board(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
            submitted_by="Meena Patel",
        )
        await breach_service.notify_principals(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
        )
        await breach_service.close(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
            root_cause="Cause.", remediation="Fix.",
        )
        for call in (
            lambda: breach_service.change_status(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
                to_status="investigating",
            ),
            lambda: breach_service.void(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
                reason="changed my mind",
            ),
        ):
            with pytest.raises(Conflict):
                await call()


async def test_voiding_requires_a_reason_and_keeps_the_entry(
    app_session_factory, tenant_a
):
    """There is no delete. A register whose entries can vanish is not a register."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach = await _record(s, tenant_a)
        with pytest.raises(Conflict):
            await breach_service.void(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
                reason="  ",
            )
        await breach_service.void(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
            reason="Duplicate of BRE-2026-0001; same bucket, same window.",
        )
        assert breach.status == "void"
        assert "Duplicate" in breach.void_reason

        still_there = await s.scalar(
            select(func.count()).select_from(Breach).where(Breach.id == breach.id)
        )
        assert still_there == 1


async def test_the_application_role_cannot_delete_a_breach(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _record(s, tenant_a)
        await s.flush()
        with pytest.raises(DBAPIError):
            await s.execute(text("DELETE FROM breaches"))


async def test_a_voided_breach_cannot_be_worked_on(app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach = await _record(s, tenant_a)
        await breach_service.void(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
            reason="Recorded in error.",
        )
        with pytest.raises(Conflict) as exc:
            await breach_service.notify_board(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
                submitted_by="Meena Patel",
            )
        assert "voided" in str(exc.value)


# --------------------------------------------------------------------------- #
# The timeline and isolation
# --------------------------------------------------------------------------- #

async def test_the_timeline_is_append_only(app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _record(s, tenant_a)
        await s.flush()
        with pytest.raises(DBAPIError):
            await s.execute(text("UPDATE breach_events SET note='rewritten'"))
        with pytest.raises(DBAPIError):
            await s.execute(text("DELETE FROM breach_events"))


async def test_the_automatic_advance_to_notified_is_marked_automated(
    app_session_factory, tenant_a, provider
):
    """"The system concluded both halves were done" is not "a human decided"."""
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        breach, _folk = await _ready_to_notify(s, tenant_a, people=1)
        await breach_service.notify_board(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
            submitted_by="Meena Patel",
        )
        await breach_service.notify_principals(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), breach=breach,
        )
        events = await breach_service.timeline(s, tenant_a["id"], breach.id)
        auto = [e for e in events if e.automated and e.to_status == "notified"]
        assert len(auto) == 1


async def test_breaches_are_tenant_isolated(app_session_factory, tenant_a, tenant_b):
    async with app_session_factory() as s:
        await s.begin()
        await set_tenant_context(s, tenant_b["id"])
        await _record(s, tenant_b)
        await s.commit()

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        assert await breach_service.list_for_tenant(s, tenant_a["id"]) == []
        assert (await breach_service.counts(s, tenant_a["id"]))["total"] == 0

    async with scoped(app_session_factory, tenant_b["id"]) as s:
        assert (await breach_service.counts(s, tenant_b["id"]))["total"] == 1


async def test_the_affected_list_is_tenant_isolated(
    app_session_factory, tenant_a, tenant_b, provider
):
    """The most sensitive join in the product — who was affected by what."""
    provider()
    async with app_session_factory() as s:
        await s.begin()
        await set_tenant_context(s, tenant_b["id"])
        breach, _folk = await _ready_to_notify(s, tenant_b, people=3)
        breach_id = breach.id
        await s.commit()

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        rows = await breach_service.affected_list(s, tenant_a["id"], breach_id)
        assert rows == []


# --------------------------------------------------------------------------- #
# Over HTTP, and the permission boundary
# --------------------------------------------------------------------------- #

async def _sign_in(client, email: str, password: str, workspace: str) -> str:
    r = await client.post(
        "/v1/auth/login",
        json={"tenant_slug": workspace, "email": email, "password": password},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture
async def admin_headers(app_session_factory, tenant_a, client):
    token = await _sign_in(
        client, tenant_a["admin_email"], tenant_a["password"], tenant_a["slug"]
    )
    return {"Authorization": f"Bearer {token}"}


async def _headers_for_role(app_session_factory, tenant, client, role: str):
    from app.services import tenant_service

    email = f"{role}@tenant-a.example.com"
    password = f"correct-horse-battery-staple-{role}"
    async with scoped(app_session_factory, tenant["id"]) as s:
        await tenant_service.create_user(
            s, tenant_id=tenant["id"], email=email, full_name=role.title(),
            role=role, password=password, actor=_actor(tenant),
        )
        await s.commit()
    token = await _sign_in(client, email, password, tenant["slug"])
    return {"Authorization": f"Bearer {token}"}


async def test_the_whole_lifecycle_over_http(
    app_session_factory, tenant_a, client, admin_headers, provider
):
    """Record → investigate → attach → notify Board → notify people → close.

    Over HTTP because the routes build their responses differently from the
    service, and that difference has already produced one 500 in this codebase.
    """
    provider()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _affected_people(s, tenant_a, 3)
        await s.commit()

    created = await client.post(
        "/v1/breaches",
        headers=admin_headers,
        json={
            "title": "Misconfigured storage bucket",
            "description": "A marketing export was readable without credentials.",
            "severity": "high",
            "discovered_at": datetime.now(UTC).isoformat(),
            "categories_affected": [CATEGORY],
            "estimated_affected_count": 5,
        },
    )
    assert created.status_code == 201, created.text
    bid = created.json()["id"]
    assert created.json()["status"] == "draft"
    assert created.json()["reference"].startswith("BRE-")

    moved = await client.post(
        f"/v1/breaches/{bid}/status", headers=admin_headers,
        json={"to_status": "investigating"},
    )
    assert moved.status_code == 200, moved.text

    # Review who would be attached, before attaching.
    prev = await client.post(
        f"/v1/breaches/{bid}/affected/preview", headers=admin_headers,
        json={"categories": [CATEGORY]},
    )
    assert prev.status_code == 200, prev.text
    assert prev.json()["matched"] == 3

    attached = await client.post(
        f"/v1/breaches/{bid}/affected", headers=admin_headers,
        json={"categories": [CATEGORY]},
    )
    assert attached.status_code == 200, attached.text
    assert attached.json()["affected_count"] == 3

    # The Board notice is generated for a human to submit.
    notice = await client.get(f"/v1/breaches/{bid}/board-notice", headers=admin_headers)
    assert notice.status_code == 200, notice.text
    assert notice.json()["submitted"] is False
    assert "does not transmit" in notice.json()["note"]

    board = await client.post(
        f"/v1/breaches/{bid}/notify-board", headers=admin_headers,
        json={"submitted_by": "Meena Patel", "board_reference": "DPB/2026/00412"},
    )
    assert board.status_code == 200, board.text
    assert board.json()["status"] == "investigating", "half the duty"

    # Remediation is required before writing to people.
    early = await client.post(
        f"/v1/breaches/{bid}/notify-principals", headers=admin_headers
    )
    assert early.status_code == 409, early.text

    await client.patch(
        f"/v1/breaches/{bid}", headers=admin_headers,
        json={"remediation": "Bucket policy corrected; access logs reviewed."},
    )

    run = await client.post(
        f"/v1/breaches/{bid}/notify-principals?batch=2", headers=admin_headers
    )
    assert run.status_code == 200, run.text
    assert run.json()["progress"]["summary"] == "2 of 3 notified"
    assert run.json()["status"] == "investigating"

    finish = await client.post(
        f"/v1/breaches/{bid}/notify-principals", headers=admin_headers
    )
    assert finish.status_code == 200, finish.text
    assert finish.json()["progress"]["complete"] is True
    assert finish.json()["status"] == "notified"

    closed = await client.post(
        f"/v1/breaches/{bid}/close", headers=admin_headers,
        json={
            "root_cause": "A deploy removed the bucket policy.",
            "remediation": "Policy restored and pinned in Terraform.",
        },
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "closed"


async def test_the_register_states_that_the_threshold_is_ours_not_the_statutes(
    client, admin_headers
):
    """The Rules say "without delay", which is not a number.

    Encoding 72 hours without saying that would be inventing a legal deadline.
    """
    r = await client.get("/v1/breaches", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["board_threshold_hours"] == BOARD_NOTIFICATION_HOURS
    assert "not a statutory figure" in r.json()["board_threshold_note"]


@pytest.mark.parametrize("role", ["auditor", "grievance_officer", "data_principal"])
async def test_no_other_role_reaches_the_register(
    app_session_factory, tenant_a, client, role
):
    """`breach:manage` on every route, including the reads.

    Breach detail is the most sensitive combination in the product. An auditor
    checking that the obligation was discharged is served by counts and
    timestamps — the names add nothing and would spread the
    who-was-affected-by-what join to a second role.
    """
    headers = await _headers_for_role(app_session_factory, tenant_a, client, role)
    for path in ("/v1/breaches", f"/v1/breaches/{uuid.uuid4()}",
                 f"/v1/breaches/{uuid.uuid4()}/affected"):
        r = await client.get(path, headers=headers)
        assert r.status_code == 403, f"{role} reached {path}: {r.status_code}"

    r = await client.post(
        "/v1/breaches", headers=headers,
        json={"title": "Probe attempt", "description": "Should never be recorded."},
    )
    assert r.status_code == 403, r.text
