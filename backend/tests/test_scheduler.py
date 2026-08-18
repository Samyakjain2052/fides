"""The scheduler.

The risk this module introduces is specific and worth naming: four modules
previously disclosed "there is no scheduler", which was honest and visible. A
scheduler that stops running replaces that with a product whose screens claim
escalation and retry are automatic while nothing happens. Nobody notices, because
nothing looks broken.

So the tests concentrate on:

* the advisory lock actually preventing a double-run,
* a failure being recorded rather than swallowed,
* one tenant's bad data not stopping the sweep for everybody,
* staleness being computed against the clock, so a dead scheduler is detectable,
* and the deliberate absence of an automatic purge.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.session import set_tenant_context, unscoped_session
from app.models.consent import DataPrincipal
from app.models.job_run import JobRun
from app.models.notification import Notification
from app.services import grievance_service, notification_service, scheduler
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
    name = "capture"

    def __init__(self, fail: bool = False):
        self.sent: list[dict] = []
        self.fail = fail

    async def send(self, *, to, subject, body, channel):
        if self.fail:
            return SendResult(ok=False, error="provider unavailable", retryable=True)
        self.sent.append({"to": to, "subject": subject})
        return SendResult(ok=True, provider_message_id=f"x{len(self.sent)}")


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


# --------------------------------------------------------------------------- #
# The lock
# --------------------------------------------------------------------------- #

async def test_a_job_takes_a_lock_and_a_second_attempt_is_recorded_as_skipped(
    app_session_factory, tenant_a, provider
):
    """Two scheduler replicas must not do the same work twice.

    Recorded as `skipped_locked` rather than silently returning, because two
    schedulers fighting is a deployment problem somebody should be able to see in
    the log.
    """
    provider()
    # Hold the lock as though another worker had it.
    async with unscoped_session() as holder:
        got = await scheduler._try_lock(holder, "grievance.escalate")  # noqa: SLF001
        assert got is True

        run = await scheduler.run_job("grievance.escalate")
        assert run.status == "skipped_locked"
        assert run.finished_at is not None, "a skipped run still finished"

        await scheduler._unlock(holder, "grievance.escalate")  # noqa: SLF001

    # With the lock free it runs normally.
    run = await scheduler.run_job("grievance.escalate")
    assert run.status == "succeeded"


async def test_the_lock_is_released_even_when_the_job_fails(
    app_session_factory, tenant_a, monkeypatch
):
    """A job that dies holding its lock would stop that job forever."""
    async def boom() -> tuple[int, int]:
        raise RuntimeError("something went wrong mid-sweep")

    monkeypatch.setattr(scheduler.JOBS["grievance.escalate"], "run", boom)
    failed = await scheduler.run_job("grievance.escalate")
    assert failed.status == "failed"

    # The lock is free, so the next attempt is not skipped.
    monkeypatch.undo()
    nxt = await scheduler.run_job("grievance.escalate")
    assert nxt.status == "succeeded", "the lock outlived the failure"


async def test_a_failing_job_records_the_reason(app_session_factory, monkeypatch):
    """Swallowed failures are how a scheduler dies quietly."""
    async def boom() -> tuple[int, int]:
        raise ValueError("the provider is misconfigured")

    monkeypatch.setattr(scheduler.JOBS["notifications.drain"], "run", boom)
    run = await scheduler.run_job("notifications.drain")
    assert run.status == "failed"
    assert "ValueError" in run.error
    assert "misconfigured" in run.error
    assert run.finished_at is not None


async def test_the_database_refuses_a_finished_run_with_no_finish_time(
    app_session_factory,
):
    """The constraint that makes a crash distinguishable from a completion.

    Without it, a run that died and a run that succeeded look the same — and
    telling them apart is the entire reason the table exists.
    """
    async with unscoped_session() as s:
        s.add(JobRun(job="probe", status="succeeded",
                     started_at=datetime.now(UTC), finished_at=None))
        with pytest.raises(IntegrityError):
            await s.flush()


async def test_a_failed_run_must_say_why(app_session_factory):
    async with unscoped_session() as s:
        now = datetime.now(UTC)
        s.add(JobRun(job="probe", status="failed", started_at=now,
                     finished_at=now, error=None))
        with pytest.raises(IntegrityError):
            await s.flush()


async def test_the_application_role_cannot_delete_the_run_log(app_session_factory):
    """A scheduler's history is how you notice it stopped."""
    async with unscoped_session() as s:
        await scheduler.run_job("grievance.escalate")
        with pytest.raises(DBAPIError):
            await s.execute(text("DELETE FROM job_runs"))


# --------------------------------------------------------------------------- #
# The jobs actually do the work
# --------------------------------------------------------------------------- #

async def _overdue_grievance(session, tenant):
    """A complaint past its escalation threshold, from an account holder."""
    principal = DataPrincipal(
        tenant_id=tenant["id"], external_id=f"c-{uuid.uuid4().hex[:8]}",
        email="person@example.com",
    )
    session.add(principal)
    await session.flush()
    grievance, _ = await grievance_service.file(
        session, tenant_id=tenant["id"], actor=_actor(tenant),
        category="consent_violation",
        description="You kept emailing me after I withdrew consent.",
        principal_id=principal.id,
    )
    # Past the 10-day default threshold. All three timestamps move together.
    await session.execute(
        text("UPDATE grievances SET submitted_at = submitted_at - interval '12 days', "
             "escalate_at = escalate_at - interval '12 days', "
             "deadline_at = deadline_at - interval '12 days' WHERE id = :i"),
        {"i": str(grievance.id)},
    )
    return grievance


async def test_the_escalation_job_escalates_without_anybody_opening_the_queue(
    app_session_factory, tenant_a, provider
):
    """The gap this closes.

    The queue screen already swept on read, which fixed the display. What it could
    not do is escalate a complaint nobody looked at — and a workspace where the DPO
    is on leave is exactly where the statutory clock matters most.
    """
    provider()
    async with app_session_factory() as s:
        await s.begin()
        await set_tenant_context(s, tenant_a["id"])
        grievance = await _overdue_grievance(s, tenant_a)
        gid = grievance.id
        await s.commit()

    run = await scheduler.run_job("grievance.escalate")
    assert run.status == "succeeded"
    assert run.items_processed == 1
    assert run.tenants_processed >= 1

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        row = await grievance_service.get(s, tenant_a["id"], gid)
        assert row.escalated is True
        assert row.escalated_at is not None


async def test_the_escalation_job_is_idempotent(
    app_session_factory, tenant_a, provider
):
    """It runs every 15 minutes. A second pass must find nothing to do."""
    provider()
    async with app_session_factory() as s:
        await s.begin()
        await set_tenant_context(s, tenant_a["id"])
        await _overdue_grievance(s, tenant_a)
        await s.commit()

    first = await scheduler.run_job("grievance.escalate")
    second = await scheduler.run_job("grievance.escalate")
    assert first.items_processed == 1
    assert second.items_processed == 0

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        sent = await s.scalar(
            select(func.count()).select_from(Notification)
            .where(Notification.template_key == "grievance.escalated")
        )
        assert sent == 1, "the officer was notified twice"


async def test_the_drain_job_retries_a_transiently_failed_message(
    app_session_factory, tenant_a, provider
):
    """What the "Process queue now" button did by hand.

    A message that hit a provider blip previously waited for somebody to notice.
    """
    failing = provider(_Sends(fail=True))
    async with app_session_factory() as s:
        await s.begin()
        await set_tenant_context(s, tenant_a["id"])
        queued = await notification_service.enqueue(
            s, tenant_id=tenant_a["id"], key="dsar.received",
            to_address="person@example.com",
            context={"reference": "DSAR-1", "type": "access", "deadline": "2026-09-14"},
            entity_type="dsar_request", entity_id=uuid.uuid4(),
        )
        await notification_service.send_now(s, notification=queued)
        assert queued.status == "queued", "back in the queue after a retryable failure"
        nid = queued.id
        # Make it due now rather than after the backoff.
        await s.execute(
            text("UPDATE notifications SET next_attempt_at = now() WHERE id = :i"),
            {"i": str(nid)},
        )
        await s.commit()

    # The provider recovers.
    failing.fail = False
    run = await scheduler.run_job("notifications.drain")
    assert run.status == "succeeded"
    assert run.items_processed == 1

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        row = await s.scalar(select(Notification).where(Notification.id == nid))
        assert row.status == "delivered"


async def test_the_drain_job_does_nothing_when_nothing_is_due(
    app_session_factory, tenant_a, provider
):
    """It ticks every minute, so the empty case has to be cheap and silent."""
    provider()
    run = await scheduler.run_job("notifications.drain")
    assert run.status == "succeeded"
    assert run.items_processed == 0


async def test_one_tenants_failure_does_not_stop_the_sweep(
    app_session_factory, tenant_a, tenant_b, monkeypatch, provider
):
    """The alternative is one bad row halting escalation for every customer."""
    provider()
    async with app_session_factory() as s:
        await s.begin()
        await set_tenant_context(s, tenant_b["id"])
        await _overdue_grievance(s, tenant_b)
        await s.commit()

    real_sweep = grievance_service.sweep_escalations
    calls: list[uuid.UUID] = []

    async def flaky(session, *, tenant_id, **kw):
        calls.append(tenant_id)
        if tenant_id == tenant_a["id"]:
            raise RuntimeError("this tenant's data is in a state we cannot handle")
        return await real_sweep(session, tenant_id=tenant_id, **kw)

    monkeypatch.setattr(grievance_service, "sweep_escalations", flaky)

    run = await scheduler.run_job("grievance.escalate")
    # The job as a whole succeeded, and tenant B's work was done.
    assert run.status == "succeeded"
    assert tenant_a["id"] in calls and tenant_b["id"] in calls
    assert run.items_processed == 1
    # Tenant A was attempted and failed, so it is not counted as processed.
    assert run.tenants_processed >= 1


async def test_the_sweep_binds_each_tenants_own_context(
    app_session_factory, tenant_a, tenant_b, provider
):
    """The worker holds the application role, which is NOBYPASSRLS.

    So it iterates tenants and binds context per tenant rather than reading across
    all of them at once. A background process with cross-tenant read access is
    exactly what RLS exists to prevent.
    """
    provider()
    async with app_session_factory() as s:
        await s.begin()
        await set_tenant_context(s, tenant_a["id"])
        ga = await _overdue_grievance(s, tenant_a)
        ga_id = ga.id
        await s.commit()
    async with app_session_factory() as s:
        await s.begin()
        await set_tenant_context(s, tenant_b["id"])
        gb = await _overdue_grievance(s, tenant_b)
        gb_id = gb.id
        await s.commit()

    run = await scheduler.run_job("grievance.escalate")
    assert run.items_processed == 2, "both tenants swept"

    # And each escalation notification went to its own tenant's officer.
    for tenant, gid in ((tenant_a, ga_id), (tenant_b, gb_id)):
        async with scoped(app_session_factory, tenant["id"]) as s:
            row = await grievance_service.get(s, tenant["id"], gid)
            assert row.escalated is True
            rows = await notification_service.log_for_tenant(s, tenant["id"])
            esc = [r for r in rows if r.template_key == "grievance.escalated"]
            assert len(esc) == 1, "one escalation notice per tenant, in that tenant"


# --------------------------------------------------------------------------- #
# What is deliberately NOT scheduled
# --------------------------------------------------------------------------- #

def test_there_is_no_job_that_destroys_data():
    """A decision, not an omission.

    Retention policies carry `auto_delete` and it would be easy to act on it here.
    Unattended destruction on a timer is a different class of risk from sending a
    warning: the warning is recoverable, the purge is not, and the only witness
    would be a row in a job log. The notice period exists so a human can decide
    informed and on time — so the scheduler sends the notice and the destruction
    stays a human action.
    """
    assert set(scheduler.JOBS) == {
        "notifications.drain",
        "grievance.escalate",
        "retention.prepurge_warn",
    }
    for name in scheduler.JOBS:
        assert "purge" not in name or "warn" in name


async def test_the_prepurge_job_warns_and_does_not_purge(
    app_session_factory, tenant_a, provider
):
    """The strongest form of the previous test: assert on the data.

    A job that reported "warned" while masking identifiers would pass a
    name-checking test and be catastrophic.
    """
    provider()
    from tests.test_retention import _policy, _world

    async with app_session_factory() as s:
        await s.begin()
        await set_tenant_context(s, tenant_a["id"])
        _p, principal, _c = await _world(s, tenant_a, days_ago=400)
        # auto_delete, so the scheduler picks it up.
        await _policy(s, tenant_a, retention_days=90, notify_days=14, auto_delete=True)
        before = (principal.email, principal.phone, principal.external_id,
                  principal.purged_at)
        pid = principal.id
        await s.commit()

    run = await scheduler.run_job("retention.prepurge_warn")
    assert run.status == "succeeded"
    assert run.items_processed == 1, "one person warned"

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        after = await s.scalar(select(DataPrincipal).where(DataPrincipal.id == pid))
        assert (after.email, after.phone, after.external_id, after.purged_at) == before, \
            "the scheduler destroyed data"
        rows = await notification_service.log_for_tenant(s, tenant_a["id"])
        warned = [r for r in rows if r.template_key == "retention.pre_purge"]
        assert len(warned) == 1


async def test_the_prepurge_job_ignores_manual_policies(
    app_session_factory, tenant_a, provider
):
    """A policy run by hand does not need a notice sent on a timer.

    A human is choosing when it happens and can send it. The policies that need
    this are the ones destroying data on a schedule.
    """
    provider()
    from tests.test_retention import _policy, _world

    async with app_session_factory() as s:
        await s.begin()
        await set_tenant_context(s, tenant_a["id"])
        await _world(s, tenant_a, days_ago=400)
        await _policy(s, tenant_a, retention_days=90, notify_days=14,
                      auto_delete=False)
        await s.commit()

    run = await scheduler.run_job("retention.prepurge_warn")
    assert run.items_processed == 0


# --------------------------------------------------------------------------- #
# Staleness — the thing that makes a dead scheduler visible
# --------------------------------------------------------------------------- #

async def test_a_job_that_has_never_run_reads_as_stale(app_session_factory, tenant_a):
    """"We have no evidence this works" and "this stopped working" warrant the
    same response, so they get the same flag."""
    async with unscoped_session() as s:
        rows = await scheduler.status(s)
        assert rows, "every job is reported"
        assert all(r["stale"] for r in rows)
        assert all(r["last_success_at"] is None for r in rows)


async def test_a_recent_success_reads_as_healthy(
    app_session_factory, tenant_a, provider
):
    provider()
    await scheduler.run_job("grievance.escalate")
    async with unscoped_session() as s:
        rows = {r["job"]: r for r in await scheduler.status(s)}
        assert rows["grievance.escalate"]["stale"] is False
        assert rows["grievance.escalate"]["last_status"] == "succeeded"
        # And the others, which have not run, are still flagged.
        assert rows["notifications.drain"]["stale"] is True


async def test_an_old_success_reads_as_stale(app_session_factory, tenant_a, provider):
    """Computed against the clock, not stored.

    A stored freshness flag is only as fresh as whatever last wrote it — and this
    field exists precisely to detect that nothing is writing.
    """
    provider()
    await scheduler.run_job("grievance.escalate")
    interval = scheduler.JOBS["grievance.escalate"].interval_seconds
    async with unscoped_session() as s:
        await s.execute(
            text("UPDATE job_runs SET started_at = started_at - "
                 "make_interval(secs => :s) WHERE job = 'grievance.escalate'"),
            {"s": interval * (scheduler.STALE_AFTER_INTERVALS + 1)},
        )
        rows = {r["job"]: r for r in await scheduler.status(s)}
        assert rows["grievance.escalate"]["stale"] is True
        assert rows["grievance.escalate"]["seconds_since_success"] > interval


async def test_a_single_missed_tick_is_not_an_alarm(
    app_session_factory, tenant_a, provider
):
    """An alarm that fires on normal variation is one people learn to ignore."""
    provider()
    await scheduler.run_job("grievance.escalate")
    interval = scheduler.JOBS["grievance.escalate"].interval_seconds
    async with unscoped_session() as s:
        await s.execute(
            text("UPDATE job_runs SET started_at = started_at - "
                 "make_interval(secs => :s) WHERE job = 'grievance.escalate'"),
            {"s": int(interval * 1.5)},
        )
        rows = {r["job"]: r for r in await scheduler.status(s)}
        assert rows["grievance.escalate"]["stale"] is False


async def test_a_failure_does_not_count_as_a_success(
    app_session_factory, monkeypatch, provider
):
    """The point of tracking last SUCCESS rather than last run.

    A job failing every minute is not a working job, and reporting it as fresh
    because it ran would be the exact reassurance this endpoint must not give.
    """
    provider()
    async def boom() -> tuple[int, int]:
        raise RuntimeError("still broken")

    monkeypatch.setattr(scheduler.JOBS["grievance.escalate"], "run", boom)
    await scheduler.run_job("grievance.escalate")

    async with unscoped_session() as s:
        rows = {r["job"]: r for r in await scheduler.status(s)}
        assert rows["grievance.escalate"]["last_status"] == "failed"
        assert rows["grievance.escalate"]["last_success_at"] is None
        assert rows["grievance.escalate"]["stale"] is True


# --------------------------------------------------------------------------- #
# The loop and the API
# --------------------------------------------------------------------------- #

async def test_every_job_runs_once_on_startup(app_session_factory, provider):
    """Otherwise a deployment that restarts daily would never run the daily job."""
    provider()
    await scheduler.run_forever(once=True)
    async with unscoped_session() as s:
        ran = await s.execute(select(JobRun.job).distinct())
        assert {r[0] for r in ran.all()} == set(scheduler.JOBS)


async def _sign_in(client, tenant, email=None, password=None):
    r = await client.post("/v1/auth/login", json={
        "tenant_slug": tenant["slug"],
        "email": email or tenant["admin_email"],
        "password": password or tenant["password"],
    })
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_the_jobs_endpoint_says_plainly_when_nothing_is_running(
    app_session_factory, tenant_a, client
):
    headers = await _sign_in(client, tenant_a)
    r = await client.get("/v1/admin/jobs", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["jobs"]) == len(scheduler.JOBS)
    assert all(j["stale"] for j in body["jobs"])
    # The note has to be blunt: the other screens imply this work is automatic.
    assert "not running" in body["note"]
    assert "regardless of what any other screen implies" in body["note"]


async def test_a_job_can_be_triggered_by_hand_over_http(
    app_session_factory, tenant_a, client, provider
):
    provider()
    headers = await _sign_in(client, tenant_a)
    r = await client.post("/v1/admin/jobs/grievance.escalate/run", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "succeeded"

    after = await client.get("/v1/admin/jobs", headers=headers)
    row = next(j for j in after.json()["jobs"] if j["job"] == "grievance.escalate")
    assert row["stale"] is False


async def test_a_manual_trigger_respects_the_scheduler_lock(
    app_session_factory, tenant_a, client, provider
):
    """So a hand-run cannot double up on a sweep already in progress."""
    provider()
    headers = await _sign_in(client, tenant_a)
    async with unscoped_session() as holder:
        await scheduler._try_lock(holder, "grievance.escalate")  # noqa: SLF001
        r = await client.post("/v1/admin/jobs/grievance.escalate/run", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "skipped_locked"
        await scheduler._unlock(holder, "grievance.escalate")  # noqa: SLF001


async def test_an_unknown_job_is_refused_by_name(app_session_factory, tenant_a, client):
    headers = await _sign_in(client, tenant_a)
    r = await client.post("/v1/admin/jobs/not.a.job/run", headers=headers)
    assert r.status_code == 409, r.text
    assert "grievance.escalate" in r.text, "the error names the alternatives"


@pytest.mark.parametrize("role", ["auditor", "grievance_officer", "data_principal"])
async def test_only_tenant_manage_reaches_the_jobs_endpoint(
    app_session_factory, tenant_a, client, role
):
    """Platform health, so it sits behind the workspace-administration capability.

    An auditor reading it would learn nothing about their own workspace's
    compliance — the log names no tenant — but this is deployment information and
    belongs with whoever runs the deployment.
    """
    from app.services import tenant_service

    pw = f"correct-horse-battery-staple-{role}"
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await tenant_service.create_user(
            s, tenant_id=tenant_a["id"], email=f"{role}@tenant-a.example.com",
            full_name=role, role=role, password=pw, actor=_actor(tenant_a),
        )
        await s.commit()
    headers = await _sign_in(
        client, tenant_a, email=f"{role}@tenant-a.example.com", password=pw
    )
    for method, path in (
        ("get", "/v1/admin/jobs"),
        ("post", "/v1/admin/jobs/grievance.escalate/run"),
    ):
        r = await getattr(client, method)(path, headers=headers)
        assert r.status_code == 403, f"{role} reached {path}"


async def test_the_run_log_names_no_tenant(app_session_factory, tenant_a, provider):
    """A platform table that answered "which customers had overdue complaints last
    night" would be a disclosure nobody asked for.

    Counts only — which is also why it needs no RLS policy.
    """
    provider()
    async with app_session_factory() as s:
        await s.begin()
        await set_tenant_context(s, tenant_a["id"])
        await _overdue_grievance(s, tenant_a)
        await s.commit()

    await scheduler.run_job("grievance.escalate")
    async with unscoped_session() as s:
        result = await s.execute(text("SELECT * FROM job_runs LIMIT 1"))
        columns = set(result.keys())
        # No tenant column at all, so there is nothing to leak by accident.
        assert not any(c.startswith("tenant_id") for c in columns)
        assert "tenants_processed" in columns, "a count, not an identity"

        # And nothing a tenant could be identified by leaked into the text fields.
        rows = await s.execute(text("SELECT job, status, error FROM job_runs"))
        blob = " ".join(str(v) for row in rows.all() for v in row if v)
        assert tenant_a["slug"] not in blob
        assert str(tenant_a["id"]) not in blob
