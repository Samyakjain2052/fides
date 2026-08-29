"""Notifications — the module whose failures are silent.

Every other module fails loudly: a rejected request returns an error, a purge run
writes a receipt. This one fails by *not sending something*, and nobody notices
until a regulator asks why a data principal was never told their request was
refused. So these tests are mostly about the failure modes:

* a message that goes out twice,
* a message that never goes out and says nothing about why,
* a retry loop that never terminates,
* a template that renders a statutory deadline as an empty string,
* an English message recorded as if it had been sent in Hindi.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from app.core.errors import Conflict
from app.db.session import set_tenant_context
from app.models.consent import DataPrincipal
from app.models.notification import TEMPLATE_KEYS, Notification, NotificationTemplate
from app.services import notification_providers, notification_service
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


async def _seed(session, tenant) -> int:
    return await notification_service.seed_default_templates(
        session, tenant_id=tenant["id"]
    )


async def _principal(session, tenant, email="person@example.com") -> DataPrincipal:
    row = DataPrincipal(
        tenant_id=tenant["id"], external_id=f"cust-{uuid.uuid4().hex[:8]}", email=email
    )
    session.add(row)
    await session.flush()
    return row


# --------------------------------------------------------------------------- #
# Templates: caught at save time, not at 2am
# --------------------------------------------------------------------------- #

def test_every_template_key_has_a_default_and_every_default_validates():
    """The seed data and the placeholder allow-list must not drift apart.

    A key added to TEMPLATE_KEYS with no default template means every call site
    for it silently suppresses with "no active template" — honest, but nobody is
    notified and nothing looks broken.
    """
    assert set(notification_service.DEFAULT_TEMPLATES) == set(TEMPLATE_KEYS)
    for key, (subject, body) in notification_service.DEFAULT_TEMPLATES.items():
        notification_service.validate_template(key, subject, body)


def test_an_unknown_placeholder_is_refused_at_save_time():
    """The whole point of validating on save.

    `{{due_date}}` when the real name is `{{deadline}}` renders as nothing, and
    "We must respond by ." goes out to everyone who files a request.
    """
    with pytest.raises(notification_service.TemplateInvalid) as exc:
        notification_service.validate_template(
            "dsar.received",
            "Your request {{reference}}",
            "We must respond by {{due_date}}.",
        )
    # The error has to name the alternative, or whoever hit it has to go reading
    # source to find out what they meant.
    assert "due_date" in str(exc.value)
    assert "deadline" in str(exc.value)


def test_an_unknown_template_key_is_refused():
    with pytest.raises(notification_service.TemplateInvalid):
        notification_service.validate_template("dsar.maybe", "Hi", "There")


def test_values_are_escaped_but_the_template_is_not():
    """Values come from members of the public; templates come from an admin.

    A grievance description or a rejection reason is attacker-controlled text
    landing inside an email body. The template itself is trusted — escaping it
    would mangle an admin's own punctuation for no benefit.
    """
    out = notification_service.render(
        "Reason: {{reason}}",
        {"reason": "<script>alert('x')</script> & more"},
    )
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&amp;" in out


def test_a_missing_value_renders_empty_rather_than_leaving_the_placeholder():
    """An unsupplied value must not leak `{{deadline}}` into somebody's inbox.

    Rendering empty is not good, which is exactly why validate_template exists to
    stop it happening for placeholders that are never supplied. This asserts the
    fallback is at least not gibberish.
    """
    assert notification_service.render("Due {{deadline}}.", {}) == "Due ."


# --------------------------------------------------------------------------- #
# Queueing
# --------------------------------------------------------------------------- #

async def test_the_same_thing_is_never_sent_twice(app_session_factory, tenant_a):
    """Idempotency is a database constraint, not a code path.

    Two attempts to acknowledge the same request must produce one message. This
    is enforced by a unique index precisely so a retried job, a double-clicked
    button, and a redelivered webhook all land on the same answer.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _seed(s, tenant_a)
        principal = await _principal(s, tenant_a)
        entity = uuid.uuid4()

        first = await notification_service.enqueue(
            s, tenant_id=tenant_a["id"], key="dsar.received",
            to_address=principal.email,
            context={"reference": "DSAR-1", "type": "access", "deadline": "2026-09-14"},
            entity_type="dsar_request", entity_id=entity, principal_id=principal.id,
        )
        assert first is not None

        second = await notification_service.enqueue(
            s, tenant_id=tenant_a["id"], key="dsar.received",
            to_address=principal.email,
            context={"reference": "DSAR-1", "type": "access", "deadline": "2026-09-14"},
            entity_type="dsar_request", entity_id=entity, principal_id=principal.id,
        )
        assert second is None, "the second enqueue must be refused, not duplicated"

        count = await s.scalar(
            select(func.count()).select_from(Notification)
            .where(Notification.template_key == "dsar.received")
        )
        assert count == 1


async def test_a_duplicate_does_not_roll_back_the_callers_work(
    app_session_factory, tenant_a
):
    """The savepoint. This is the bug that would have hurt most.

    Every call site is a state change that has already happened — a request was
    submitted, a consent withdrawn. If the duplicate-key error rolled back the
    whole transaction, the *rights request* would vanish because its
    acknowledgement was already queued. Losing the request is catastrophic;
    skipping the duplicate email is the intended behaviour.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _seed(s, tenant_a)
        principal = await _principal(s, tenant_a)
        entity = uuid.uuid4()
        ctx = {"reference": "DSAR-2", "type": "erasure", "deadline": "2026-09-20"}

        await notification_service.enqueue(
            s, tenant_id=tenant_a["id"], key="dsar.received",
            to_address=principal.email, context=ctx,
            entity_type="dsar_request", entity_id=entity, principal_id=principal.id,
        )
        # Something the caller did before the duplicate attempt.
        marker = await _principal(s, tenant_a, email="marker@example.com")
        marker_id = marker.id

        assert await notification_service.enqueue(
            s, tenant_id=tenant_a["id"], key="dsar.received",
            to_address=principal.email, context=ctx,
            entity_type="dsar_request", entity_id=entity, principal_id=principal.id,
        ) is None

        # The caller's row is still there and the session is still usable.
        survived = await s.scalar(
            select(DataPrincipal).where(DataPrincipal.id == marker_id)
        )
        assert survived is not None, "the duplicate rolled back the caller's work"


async def test_no_address_records_a_suppression_instead_of_raising(
    app_session_factory, tenant_a
):
    """"We never told them" needs a reason on the record.

    Raising would fail the DSAR submission because we hold no email for somebody.
    Returning silently would leave no trace. A suppressed row with a reason is the
    only defensible option.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _seed(s, tenant_a)
        row = await notification_service.enqueue(
            s, tenant_id=tenant_a["id"], key="dsar.received", to_address=None,
            context={"reference": "DSAR-3", "type": "access", "deadline": "x"},
            entity_type="dsar_request", entity_id=uuid.uuid4(),
        )
        assert row is not None
        assert row.status == "suppressed"
        assert row.suppression_reason
        assert "address" in row.suppression_reason


async def test_a_missing_template_suppresses_with_a_reason(app_session_factory, tenant_a):
    """A deactivated template must suppress with a reason, not send blank.

    Every workspace is seeded with the full set now — creating one without them
    was a trap — so this deactivates the one under test rather than relying on an
    unseeded tenant.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await s.execute(
            text("UPDATE notification_templates SET is_active = false "
                 "WHERE key = 'dsar.received'")
        )
        row = await notification_service.enqueue(
            s, tenant_id=tenant_a["id"], key="dsar.received",
            to_address="person@example.com",
            context={"reference": "DSAR-4", "type": "access", "deadline": "x"},
            entity_type="dsar_request", entity_id=uuid.uuid4(),
        )
        assert row is not None and row.status == "suppressed"
        assert "template" in row.suppression_reason


async def test_the_language_actually_used_is_recorded_not_the_one_requested(
    app_session_factory, tenant_a
):
    """The fallback must be visible.

    "We notified them in their language" is a claim somebody will check. Sending
    English under a Hindi label reads fine in a demo and fails an audit, so the
    row records what was used AND what was asked for.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _seed(s, tenant_a)  # English only
        row = await notification_service.enqueue(
            s, tenant_id=tenant_a["id"], key="dsar.received",
            to_address="person@example.com",
            context={"reference": "DSAR-5", "type": "access", "deadline": "x"},
            entity_type="dsar_request", entity_id=uuid.uuid4(),
            language="Hindi",
        )
        assert row.language == "English", "fell back, as expected"
        assert row.language_requested == "Hindi", "but the fallback must be on the record"


async def test_a_present_language_is_not_recorded_as_a_fallback(
    app_session_factory, tenant_a
):
    """The converse: no false fallback flag when the language was honoured."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _seed(s, tenant_a)
        subject, body = notification_service.DEFAULT_TEMPLATES["dsar.received"]
        await notification_service.upsert_template(
            s, tenant_id=tenant_a["id"], key="dsar.received", channel="email",
            language="Hindi", subject=subject, body=body,
        )
        row = await notification_service.enqueue(
            s, tenant_id=tenant_a["id"], key="dsar.received",
            to_address="person@example.com",
            context={"reference": "DSAR-6", "type": "access", "deadline": "x"},
            entity_type="dsar_request", entity_id=uuid.uuid4(), language="Hindi",
        )
        assert row.language == "Hindi"
        assert row.language_requested is None


# --------------------------------------------------------------------------- #
# Sending, retrying, and stopping
# --------------------------------------------------------------------------- #

class _Failing:
    """A provider that fails a fixed number of times, then succeeds."""

    name = "flaky"

    def __init__(self, fail_times: int, retryable: bool = True):
        self.remaining = fail_times
        self.retryable = retryable
        self.calls = 0

    async def send(self, *, to, subject, body, channel, html_body=None):
        self.calls += 1
        self.bodies = getattr(self, "bodies", [])
        self.bodies.append(body)
        if self.remaining > 0:
            self.remaining -= 1
            return SendResult(ok=False, error="provider unavailable",
                              retryable=self.retryable)
        return SendResult(ok=True, provider_message_id="ok-1")


@pytest.fixture
def provider(monkeypatch):
    """Swap the provider without touching config."""
    def _install(impl):
        monkeypatch.setattr(notification_providers, "get_provider", lambda: impl)
        return impl
    return _install


async def _queued(session, tenant, *, key="dsar.received", entity=None):
    return await notification_service.enqueue(
        session, tenant_id=tenant["id"], key=key,
        to_address="person@example.com",
        context={"reference": "DSAR-9", "type": "access", "deadline": "2026-09-14"},
        entity_type="dsar_request", entity_id=entity or uuid.uuid4(),
    )


async def test_a_retryable_failure_goes_back_to_the_queue_with_a_delay(
    app_session_factory, tenant_a, provider
):
    impl = provider(_Failing(fail_times=1))
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _seed(s, tenant_a)
        row = await _queued(s, tenant_a)
        before = row.next_attempt_at

        await notification_service.send_now(s, notification=row)

        assert row.status == "queued", "a transient failure must not be terminal"
        assert row.attempts == 1
        assert row.next_attempt_at > before, "and it must not be retried immediately"
        assert row.last_error


async def test_a_permanent_failure_does_not_retry_at_all(
    app_session_factory, tenant_a, provider
):
    """A mailbox that does not exist will never exist.

    Retrying it five times over fifteen minutes is not diligence, it is a queue
    filling with garbage while real messages wait behind it.
    """
    impl = provider(_Failing(fail_times=1, retryable=False))
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _seed(s, tenant_a)
        row = await _queued(s, tenant_a)

        await notification_service.send_now(s, notification=row)

        assert row.status == "failed"
        assert row.attempts == 1, "a permanent failure must be attempted exactly once"
        assert row.failed_at is not None
        assert row.last_error


async def test_retries_are_capped_and_the_row_ends_failed(
    app_session_factory, tenant_a, provider
):
    """The termination guarantee. Without this the queue never settles."""
    impl = provider(_Failing(fail_times=99))
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _seed(s, tenant_a)
        row = await _queued(s, tenant_a)

        for _ in range(notification_service.MAX_ATTEMPTS + 3):
            row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
            row.status = "queued" if row.status != "failed" else row.status
            if row.status == "failed":
                break
            await notification_service.deliver(s, notification=row)

        assert row.status == "failed"
        assert row.attempts == notification_service.MAX_ATTEMPTS
        assert impl.calls == notification_service.MAX_ATTEMPTS, (
            "the provider must not be called after the budget is spent"
        )


async def test_a_settled_message_keeps_no_body(app_session_factory, tenant_a, provider):
    """"We do not keep message bodies" is a claim about the data at rest.

    A delivery log full of rendered bodies is a second copy of everyone's personal
    data, outside the consent machinery, with its own retention problem. The body
    exists only as long as the send does — and a CHECK constraint enforces it, so
    this cannot be quietly reintroduced.
    """
    provider(_Failing(fail_times=0))
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _seed(s, tenant_a)
        row = await _queued(s, tenant_a)
        assert row.pending_body, "in flight, the body is needed"

        await notification_service.send_now(s, notification=row)

        assert row.status == "delivered"
        assert row.pending_body is None, "settled, the body must be gone"


async def test_the_database_refuses_a_settled_row_that_still_holds_a_body(
    app_session_factory, tenant_a
):
    """The constraint itself, not the service's intention to honour it."""
    from sqlalchemy.exc import IntegrityError

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        now = datetime.now(UTC)
        s.add(Notification(
            tenant_id=tenant_a["id"], template_key="dsar.received", channel="email",
            language="English", to_address="a@b.com", subject_rendered="x",
            status="delivered", sent_at=now, pending_body="this must not persist",
            queued_at=now, next_attempt_at=now,
        ))
        with pytest.raises(IntegrityError):
            await s.flush()


async def test_a_retry_reuses_the_attempt_count(app_session_factory, tenant_a, provider):
    """A human pressing retry does not get a fresh budget.

    `attempts` is the record of how hard we tried to reach somebody. A counter
    that a button resets is not a record, and it would let a dead address be
    hammered indefinitely one click at a time.
    """
    provider(_Failing(fail_times=99))
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _seed(s, tenant_a)
        row = await _queued(s, tenant_a)
        await notification_service.deliver(s, notification=row)
        assert row.attempts == 1

        row.status = "failed"
        row.failed_at = datetime.now(UTC)
        row.pending_body = None
        await s.flush()

        again = await notification_service.retry_failed(
            s, tenant_id=tenant_a["id"], notification_id=row.id
        )
        assert again.attempts == 2, "the retry continued the count rather than resetting"


async def test_a_suppressed_message_cannot_be_retried(app_session_factory, tenant_a):
    """Suppression is a decision, not a failure.

    Retrying it would send nothing while making the log look like we tried —
    which is worse than the honest "we hold no address for this person".
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _seed(s, tenant_a)
        row = await notification_service.enqueue(
            s, tenant_id=tenant_a["id"], key="dsar.received", to_address=None,
            context={}, entity_type="dsar_request", entity_id=uuid.uuid4(),
        )
        with pytest.raises(Conflict):
            await notification_service.retry_failed(
                s, tenant_id=tenant_a["id"], notification_id=row.id
            )


async def test_a_delivered_message_cannot_be_retried(
    app_session_factory, tenant_a, provider
):
    """The one that would actually double-send if it were allowed."""
    provider(_Failing(fail_times=0))
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _seed(s, tenant_a)
        row = await _queued(s, tenant_a)
        await notification_service.send_now(s, notification=row)
        assert row.status == "delivered"

        with pytest.raises(Conflict):
            await notification_service.retry_failed(
                s, tenant_id=tenant_a["id"], notification_id=row.id
            )


# --------------------------------------------------------------------------- #
# The queue claim
# --------------------------------------------------------------------------- #

async def test_two_workers_claim_disjoint_sets(app_session_factory, tenant_a, provider):
    """SKIP LOCKED is the whole reason this is a Postgres table and not a list.

    Without it, two workers running the same query both take the head of the queue
    and one message becomes two emails — a matter of timing, not of logic. This
    test holds the first worker's transaction open on purpose: that is the window
    in which the bug would occur.
    """
    provider(_Failing(fail_times=0))

    # Committed, so a second connection can see them at all.
    async with app_session_factory() as setup:
        await setup.begin()
        await set_tenant_context(setup, tenant_a["id"])
        await _seed(setup, tenant_a)
        for _ in range(4):
            await _queued(setup, tenant_a)
        await setup.commit()

    async with app_session_factory() as w1, app_session_factory() as w2:
        await w1.begin()
        await set_tenant_context(w1, tenant_a["id"])
        first = await notification_service.drain_tenant(
            w1, tenant_id=tenant_a["id"], limit=2
        )
        # w1's transaction is still open — its two rows are locked.

        await w2.begin()
        await set_tenant_context(w2, tenant_a["id"])
        second = await notification_service.drain_tenant(
            w2, tenant_id=tenant_a["id"], limit=10
        )

        assert first["claimed"] == 2
        assert second["claimed"] == 2, (
            "the second worker must skip the locked rows and take the rest, "
            f"not block and not re-take them (got {second['claimed']})"
        )
        await w1.rollback()
        await w2.rollback()


async def test_a_message_not_yet_due_is_not_claimed(app_session_factory, tenant_a, provider):
    """Backoff has to actually hold the message back."""
    provider(_Failing(fail_times=0))
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _seed(s, tenant_a)
        row = await _queued(s, tenant_a)
        row.next_attempt_at = datetime.now(UTC) + timedelta(minutes=5)
        await s.flush()

        result = await notification_service.drain_tenant(s, tenant_id=tenant_a["id"])
        assert result["claimed"] == 0
        assert row.status == "queued"


async def test_draining_is_scoped_to_one_tenant(
    app_session_factory, tenant_a, tenant_b, provider
):
    """Reachable from the API, so it must not touch another tenant's queue.

    RLS would already stop this, which is why the assertion is on the tally: a
    drain that silently claimed zero because RLS filtered everything would look
    identical to one that worked, and this pins the intended behaviour rather than
    the accident.
    """
    provider(_Failing(fail_times=0))
    async with app_session_factory() as s:
        await s.begin()
        await set_tenant_context(s, tenant_b["id"])
        await _seed(s, tenant_b)
        await _queued(s, tenant_b)
        await s.commit()

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _seed(s, tenant_a)
        result = await notification_service.drain_tenant(s, tenant_id=tenant_a["id"])
        assert result["claimed"] == 0, "tenant A's drain must not see tenant B's queue"

    async with scoped(app_session_factory, tenant_b["id"]) as s:
        result = await notification_service.drain_tenant(s, tenant_id=tenant_b["id"])
        assert result["claimed"] == 1, "tenant B's own message is still waiting for it"


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #

async def test_the_delivery_log_is_tenant_isolated(
    app_session_factory, tenant_a, tenant_b, provider
):
    provider(_Failing(fail_times=0))
    async with app_session_factory() as s:
        await s.begin()
        await set_tenant_context(s, tenant_b["id"])
        await _seed(s, tenant_b)
        await _queued(s, tenant_b)
        await s.commit()

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        rows = await notification_service.log_for_tenant(s, tenant_a["id"])
        assert rows == []


async def test_the_application_role_cannot_delete_from_the_delivery_log(
    app_session_factory, tenant_a, provider
):
    """The log is evidence. Evidence the application can erase is not evidence.

    Trimming it by retention is a scheduled job running as the owner, not
    something reachable from a request.
    """
    from sqlalchemy.exc import DBAPIError

    provider(_Failing(fail_times=0))
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _seed(s, tenant_a)
        await _queued(s, tenant_a)
        await s.flush()

        with pytest.raises(DBAPIError):
            await s.execute(text("DELETE FROM notifications"))


# --------------------------------------------------------------------------- #
# The seams — where a state change becomes a message
# --------------------------------------------------------------------------- #
#
# These are the tests that would actually catch a regression in production. The
# machinery above can be perfect while nothing is wired to it, and a notification
# module nobody calls is indistinguishable from one that does not exist.

async def _world_for_dsar(session, tenant):
    """A purpose, a published notice, a principal with an email, and a consent."""
    from app.services import consent_service, notice_service
    from app.services.audit_service import Actor as _A

    actor = _A(type="user", id=tenant["admin_id"], label="dpo@test")
    purpose = await notice_service.create_purpose(
        session, tenant_id=tenant["id"], actor=actor,
        key=f"p{uuid.uuid4().hex[:8]}", name="Marketing", category="Contact Data",
    )
    notice = await notice_service.draft_notice(
        session, tenant_id=tenant["id"], actor=actor, purpose_id=purpose.id,
        content="We use your email.", data_collected="Email",
        user_rights="Withdraw anytime.", withdrawal_policy="Stops in 24h.",
    )
    await notice_service.publish_notice(
        session, tenant_id=tenant["id"], actor=actor, notice_id=notice.id
    )
    principal = await _principal(session, tenant)
    await consent_service.grant(
        session, tenant_id=tenant["id"], actor=actor,
        principal_id=principal.id, purpose_id=purpose.id,
    )
    return actor, purpose, principal


async def test_withdrawing_consent_notifies_the_person(
    app_session_factory, tenant_a, provider
):
    """Withdrawal is the moment a person most needs a receipt.

    They have just told a company to stop doing something. A confirmation is the
    only evidence they have that it was heard.
    """
    from app.services import consent_service

    provider(_Failing(fail_times=0))
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _seed(s, tenant_a)
        actor, purpose, principal = await _world_for_dsar(s, tenant_a)

        await consent_service.withdraw(
            s, tenant_id=tenant_a["id"], actor=actor,
            principal_id=principal.id, purpose_id=purpose.id,
        )

        rows = await notification_service.log_for_tenant(s, tenant_a["id"])
        withdrawn = [r for r in rows if r.template_key == "consent.withdrawn"]
        assert len(withdrawn) == 1, "withdrawal must produce exactly one message"
        assert withdrawn[0].to_address == principal.email
        assert withdrawn[0].status == "delivered"


async def test_a_rejected_request_tells_the_person_why(
    app_session_factory, tenant_a, provider
):
    """A rejection nobody is told about is indistinguishable from being ignored.

    The reason travels into the message, because "we refused, and here is why" is
    the whole of what makes a refusal answerable.
    """
    from app.services import dsar_service

    provider(_Failing(fail_times=0))
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _seed(s, tenant_a)
        actor, _purpose, principal = await _world_for_dsar(s, tenant_a)

        request = await dsar_service.submit(
            s, tenant_id=tenant_a["id"], actor=actor,
            principal_id=principal.id, type="access",
        )
        await dsar_service.change_status(
            s, tenant_id=tenant_a["id"], actor=actor, request=request,
            to_status="rejected", reason="Identity could not be verified.",
        )

        rows = await notification_service.log_for_tenant(s, tenant_a["id"])
        rejected = [r for r in rows if r.template_key == "dsar.rejected"]
        assert len(rejected) == 1
        assert rejected[0].status == "delivered"


async def test_internal_transitions_do_not_generate_messages(
    app_session_factory, tenant_a, provider
):
    """Only outcomes are notified, on purpose.

    A message for every internal state change trains people to ignore these
    emails, and the one that matters — "your request was refused" — arrives in a
    stream of noise they have already learned to delete.
    """
    from app.services import dsar_service

    provider(_Failing(fail_times=0))
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _seed(s, tenant_a)
        actor, _purpose, principal = await _world_for_dsar(s, tenant_a)

        request = await dsar_service.submit(
            s, tenant_id=tenant_a["id"], actor=actor,
            principal_id=principal.id, type="access",
        )
        before = len(await notification_service.log_for_tenant(s, tenant_a["id"]))

        for to_status in ("verifying", "in_progress"):
            await dsar_service.change_status(
                s, tenant_id=tenant_a["id"], actor=actor, request=request,
                to_status=to_status,
            )

        after = len(await notification_service.log_for_tenant(s, tenant_a["id"]))
        assert after == before, (
            "intermediate transitions must be silent; only outcomes are notified"
        )


async def test_retention_warns_before_it_purges(app_session_factory, tenant_a, provider):
    """The seam retention was built around and could not use until now.

    A policy that destroys data on a timer must tell people first — which is why
    `auto_delete` cannot be enabled without a notice period. This asserts the
    notice actually goes out.
    """
    from app.services import retention_service

    provider(_Failing(fail_times=0))
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _seed(s, tenant_a)
        # Reuse retention's own world builder so the candidate selection under
        # test is the real one, not a hand-built approximation of it.
        from tests.test_retention import _policy, _world

        _p, principal, _c = await _world(s, tenant_a, days_ago=400)
        policy = await _policy(s, tenant_a, retention_days=90, notify_days=14)

        sent = await retention_service.warn_upcoming(
            s, tenant_id=tenant_a["id"], policy=policy
        )
        assert sent == 1, "the person about to be purged must be warned"

        rows = await notification_service.log_for_tenant(s, tenant_a["id"])
        warning = [r for r in rows if r.template_key == "retention.pre_purge"]
        assert len(warning) == 1
        assert warning[0].to_address == principal.email
        # Keyed on the principal so a daily run warns each person once, not once
        # per run — the constraint, not a flag we remember to check.
        assert warning[0].entity_id == principal.id


async def test_warning_twice_does_not_notify_twice(app_session_factory, tenant_a, provider):
    """A daily scheduled run must not email somebody every morning for two weeks."""
    from app.services import retention_service
    from tests.test_retention import _policy, _world

    provider(_Failing(fail_times=0))
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _seed(s, tenant_a)
        await _world(s, tenant_a, days_ago=400)
        policy = await _policy(s, tenant_a, retention_days=90, notify_days=14)

        assert await retention_service.warn_upcoming(
            s, tenant_id=tenant_a["id"], policy=policy
        ) == 1
        assert await retention_service.warn_upcoming(
            s, tenant_id=tenant_a["id"], policy=policy
        ) == 0, "the second run must be a no-op, not a second email"


async def test_a_template_key_added_later_is_seeded_on_first_use(
    app_session_factory, tenant_a, provider
):
    """A workspace created before a template key existed must still be able to send.

    `seed_default_templates` runs once, at workspace creation. When the breach
    module added `breach.principal_notice`, every existing workspace became unable
    to send it — silently and forever, suppressing with "no active template". That
    is honest and useless: nobody was told about a breach and nothing looked
    broken. So a missing default is seeded the first time it is needed.
    """
    provider(_Failing(fail_times=0))
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        # Simulate the older workspace: delete the template entirely, as though it
        # had never been seeded.
        await s.execute(
            text("DELETE FROM notification_templates WHERE key = 'consent.withdrawn'")
        )
        row = await notification_service.enqueue(
            s, tenant_id=tenant_a["id"], key="consent.withdrawn",
            to_address="person@example.com",
            context={"purpose": "Marketing", "effective_from": "2026-08-18"},
            entity_type="consent", entity_id=uuid.uuid4(),
        )
        assert row is not None
        assert row.status == "queued", "seeded and queued, not suppressed"
        assert "Marketing" in row.subject_rendered

        # And the template now exists for next time.
        seeded = await s.scalar(
            select(NotificationTemplate).where(
                NotificationTemplate.key == "consent.withdrawn"
            )
        )
        assert seeded is not None


async def test_a_deactivated_template_is_not_resurrected(
    app_session_factory, tenant_a, provider
):
    """Deactivating is a decision. Seeding around it would override a human.

    This is the distinction that makes the lazy seed safe: it fills a gap where
    nothing was ever created, and leaves alone anything somebody switched off.
    """
    provider(_Failing(fail_times=0))
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await s.execute(
            text("UPDATE notification_templates SET is_active = false "
                 "WHERE key = 'consent.withdrawn'")
        )
        row = await notification_service.enqueue(
            s, tenant_id=tenant_a["id"], key="consent.withdrawn",
            to_address="person@example.com",
            context={"purpose": "Marketing", "effective_from": "2026-08-18"},
            entity_type="consent", entity_id=uuid.uuid4(),
        )
        assert row.status == "suppressed"
        assert "template" in row.suppression_reason
