"""The scheduled work, and the loop that runs it.

Four modules shipped saying "there is no scheduler", each with a consequence: a
transiently-failed notification waited for a human to press a button, grievance
escalation was only evaluated when somebody opened the queue, and a retention
policy's notice period was honoured only when a run was triggered by hand. This
closes all three.

## What runs, and what deliberately does not

Three jobs: drain the notification queue, escalate overdue grievances, warn people
before a retention purge.

**Purges are not scheduled, and that is a decision rather than an omission.**
Retention policies carry an `auto_delete` flag and a notice period, and it would be
straightforward to act on them here. It is not done because unattended data
destruction on a timer is a different class of risk from sending a warning email:
the warning is recoverable and the purge is not, and the only witness would be a
row in a job log. The notice period exists so a human can decide, informed and on
time — so the scheduler sends the notice and the destruction stays a human action.
The retention caveat says this.

## Two properties the design rests on

**Nothing double-runs.** Each job takes a Postgres session-level advisory lock
before doing anything. Two scheduler replicas, or a scheduler and a developer
running a job by hand, take disjoint work — the second one records
`skipped_locked` and moves on. `pg_try_advisory_lock` rather than the blocking
form, because a job that waits for a lock it will never get is a job that stops
running.

**A dead scheduler is visible.** Every attempt writes a `job_runs` row before it
starts and updates it when it ends, so a crash leaves a `running` row with no
finish — visibly stuck rather than absent. `GET /v1/admin/jobs` reads it, and the
module caveats quote it instead of asserting the scheduler is alive. Replacing
"there is no scheduler" with a scheduler that silently died would be strictly worse
than what was there before.

## Tenancy

The worker holds the application role, which is `NOBYPASSRLS`. It therefore
iterates tenants and binds context per tenant rather than running as the owner and
reading across all of them at once. That is slower and it is the right trade:
a background process with cross-tenant read access is exactly the thing RLS exists
to prevent, and it would be reading customer data with no request to attribute it
to.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job_run import JobRun
from app.models.tenant import Tenant
from app.services.audit_service import Actor

logger = logging.getLogger("app.scheduler")

# The actor every scheduled action is attributed to. Not a user, and never a
# tenant's admin: "the escalation clock did this" and "a person did this" are
# different facts, and the timelines record `automated=True` for the same reason.
SYSTEM_ACTOR = Actor(type="system", id=None, label="scheduler")


@dataclass
class Job:
    name: str
    # How often to attempt it. The loop is intentionally simple — a fixed interval
    # per job rather than cron expressions, because none of this work needs to
    # happen at a particular time of day, only regularly.
    interval_seconds: int
    run: Callable[[], Awaitable[tuple[int, int]]]
    description: str


# --------------------------------------------------------------------------- #
# Advisory locking
# --------------------------------------------------------------------------- #

def _lock_key(job: str) -> int:
    """Fold a job name into the signed 64-bit int the advisory lock functions want.

    The same technique the audit chain uses for its per-tenant lock. A collision
    between two job names would mean they serialise against each other, which is
    harmless — they are all idempotent — so a simple hash is enough.
    """
    return int.from_bytes(
        uuid.uuid5(uuid.NAMESPACE_OID, job).bytes[:8], "big", signed=True
    )


async def _try_lock(session: AsyncSession, job: str) -> bool:
    """Session-level, not transaction-level.

    A job's work spans several transactions — one per tenant — so a
    transaction-scoped lock would be released after the first tenant and let a
    second worker in halfway through.
    """
    got = await session.scalar(
        text("SELECT pg_try_advisory_lock(:k)"), {"k": _lock_key(job)}
    )
    return bool(got)


async def _unlock(session: AsyncSession, job: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_unlock(:k)"), {"k": _lock_key(job)}
    )


# --------------------------------------------------------------------------- #
# Tenant iteration
# --------------------------------------------------------------------------- #

async def _active_tenant_ids() -> list[uuid.UUID]:
    """Every active tenant, read without tenant context.

    `tenants` has no RLS policy — it is the one table that legitimately precedes
    context, which is also how login resolves a workspace by slug.
    """
    from app.db.session import unscoped_session

    async with unscoped_session() as session:
        rows = await session.execute(
            select(Tenant.id).where(Tenant.is_active.is_(True)).order_by(Tenant.created_at)
        )
        return [r[0] for r in rows.all()]


async def _for_each_tenant(
    work: Callable[[AsyncSession, uuid.UUID], Awaitable[int]],
) -> tuple[int, int]:
    """Run `work` under each tenant's own context. Returns (tenants, items).

    One transaction per tenant, so one customer's failure cannot roll back
    another's completed work — and a tenant whose data is in a state the job
    cannot handle does not stop the sweep. Its exception is logged and the loop
    continues, because the alternative is one bad row halting escalation for
    everybody.
    """
    from app.db.session import tenant_session

    tenants = 0
    items = 0
    for tenant_id in await _active_tenant_ids():
        try:
            async with tenant_session(tenant_id) as session:
                items += await work(session, tenant_id)
            tenants += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "scheduled work failed for one tenant; continuing",
                extra={"context": {"tenant_id": str(tenant_id)}},
            )
    return tenants, items


# --------------------------------------------------------------------------- #
# The jobs
# --------------------------------------------------------------------------- #

async def drain_notifications() -> tuple[int, int]:
    """Attempt every notification that is due.

    This is what the "Process queue now" button did by hand. A message that hit a
    transient failure now gets its next attempt on schedule instead of waiting for
    somebody to notice.

    Loops per tenant until nothing is due, bounded — a single tenant with a large
    backlog must not starve the others behind it.
    """
    from app.services import notification_service

    async def work(session: AsyncSession, tenant_id: uuid.UUID) -> int:
        sent = 0
        for _ in range(20):  # at most 20 batches per tenant per tick
            result = await notification_service.drain_tenant(
                session, tenant_id=tenant_id, limit=50
            )
            if not result["claimed"]:
                break
            sent += result["claimed"]
        return sent

    return await _for_each_tenant(work)


async def escalate_grievances() -> tuple[int, int]:
    """Escalate complaints past their threshold.

    The queue screen already sweeps on read, which closed the window where an
    overdue grievance displayed as fine. What that could not do is escalate a
    complaint nobody looked at — a workspace where the DPO is on leave was exactly
    where the statutory clock mattered most and nothing was watching it.
    """
    from app.services import grievance_service

    async def work(session: AsyncSession, tenant_id: uuid.UUID) -> int:
        return await grievance_service.sweep_escalations(session, tenant_id=tenant_id)

    return await _for_each_tenant(work)


async def warn_before_purge() -> tuple[int, int]:
    """Send pre-purge notices for policies that carry a notice period.

    Only policies with `auto_delete` set: a policy that is run by hand does not
    need a notice sent on a timer, because a human is choosing when it happens and
    can send it. A policy that destroys on a timer is the one whose notice period
    somebody is relying on — and until now that notice only went out if a run was
    triggered manually, which rather defeated it.

    This sends the notice. It does NOT run the purge. See the module docstring.
    """
    from app.models.retention import RetentionPolicy
    from app.services import retention_service

    async def work(session: AsyncSession, tenant_id: uuid.UUID) -> int:
        rows = await session.execute(
            select(RetentionPolicy).where(
                RetentionPolicy.tenant_id == tenant_id,
                RetentionPolicy.is_active.is_(True),
                RetentionPolicy.auto_delete.is_(True),
                RetentionPolicy.notify_days > 0,
            )
        )
        warned = 0
        for policy in rows.scalars().all():
            # Idempotent by the notification table's unique constraint on
            # (template, entity), so running this daily warns each person once
            # rather than once per day.
            warned += await retention_service.warn_upcoming(
                session, tenant_id=tenant_id, policy=policy
            )
        return warned

    return await _for_each_tenant(work)


JOBS: dict[str, Job] = {
    "notifications.drain": Job(
        name="notifications.drain",
        interval_seconds=60,
        run=drain_notifications,
        description=(
            "Attempts every notification that is due, including retries after a "
            "transient provider failure."
        ),
    ),
    "grievance.escalate": Job(
        name="grievance.escalate",
        interval_seconds=900,
        run=escalate_grievances,
        description=(
            "Escalates complaints past this workspace's escalation threshold to the "
            "published Grievance Officer. Idempotent."
        ),
    ),
    "retention.prepurge_warn": Job(
        name="retention.prepurge_warn",
        # Daily. More often would be pointless — the notice is idempotent, so a
        # second run the same day sends nothing.
        interval_seconds=86_400,
        run=warn_before_purge,
        description=(
            "Warns people whose data an auto-delete policy will purge inside its "
            "notice period. Sends the notice; does not run the purge."
        ),
    ),
}


# --------------------------------------------------------------------------- #
# Running one job
# --------------------------------------------------------------------------- #

async def run_job(job_name: str) -> JobRun:
    """Run one job under its lock, recording the attempt either way.

    Safe to call by hand — that is what the API's manual trigger does, and it is why
    the lock is checked rather than assumed. The `job_runs` row is written before
    the work starts, so a crash leaves it `running` with no finish rather than
    leaving no trace at all.
    """
    from app.db.session import unscoped_session

    job = JOBS[job_name]
    started = datetime.now(UTC)

    # The lock and the log live on their own session, held for the whole job, while
    # the work opens a session per tenant. A session-level lock released with the
    # first tenant's transaction would let a second worker in halfway through.
    async with unscoped_session() as ledger:
        locked = await _try_lock(ledger, job_name)
        row = JobRun(
            job=job_name,
            status="running" if locked else "skipped_locked",
            started_at=started,
            finished_at=None if locked else datetime.now(UTC),
        )
        ledger.add(row)
        await ledger.flush()

        if not locked:
            # Recorded rather than silent: two schedulers fighting is a deployment
            # problem worth being able to see in the log.
            logger.info("job already running elsewhere; skipped", extra={
                "context": {"job": job_name}
            })
            return row

        try:
            tenants, items = await job.run()
            row.status = "succeeded"
            row.tenants_processed = tenants
            row.items_processed = items
        except Exception as exc:  # noqa: BLE001
            row.status = "failed"
            row.error = f"{type(exc).__name__}: {exc}"[:4000]
            logger.exception("scheduled job failed", extra={
                "context": {"job": job_name}
            })
        finally:
            row.finished_at = datetime.now(UTC)
            await ledger.flush()
            await _unlock(ledger, job_name)

        if row.items_processed:
            logger.info("job finished with work done", extra={"context": {
                "job": job_name, "tenants": row.tenants_processed,
                "items": row.items_processed,
                "seconds": round(row.duration_seconds or 0, 2),
            }})
        return row


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #

async def run_forever(*, once: bool = False) -> None:
    """Tick every job on its own interval.

    Deliberately not a cron implementation. None of this work needs to happen at a
    particular time of day, only regularly, and a fixed interval per job is one
    fewer thing that can be misconfigured into never running.

    Every job runs once on startup rather than waiting out its first interval —
    otherwise a deployment that restarts daily would never run the daily job.
    """
    from app.db.session import dispose_engine

    next_due: dict[str, float] = {name: 0.0 for name in JOBS}
    logger.info("scheduler starting", extra={"context": {
        "jobs": {n: j.interval_seconds for n, j in JOBS.items()}
    }})

    try:
        while True:
            now = asyncio.get_event_loop().time()
            for name, job in JOBS.items():
                if now < next_due[name]:
                    continue
                await run_job(name)
                next_due[name] = asyncio.get_event_loop().time() + job.interval_seconds
            if once:
                return
            # A short tick, so a 60-second job is not up to a minute late. The jobs
            # themselves are cheap when there is nothing to do: one indexed count
            # per tenant.
            await asyncio.sleep(5)
    finally:
        await dispose_engine()


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

# A job that has not succeeded within this many multiples of its interval is
# reported as stale. Three, so a single missed tick or a slow run is not an alarm —
# an alarm that fires on normal variation is one people learn to ignore.
STALE_AFTER_INTERVALS = 3


async def status(session: AsyncSession) -> list[dict]:
    """Each job's last attempt and last success, and whether it looks alive.

    `stale` is computed against the clock rather than stored, for the same reason
    every other deadline in this product is: a stored flag is only as fresh as
    whatever last wrote it, and this one exists to detect that nothing is writing.
    """
    now = datetime.now(UTC)
    out = []
    for name, job in JOBS.items():
        last = await session.scalar(
            select(JobRun).where(JobRun.job == name)
            .order_by(JobRun.started_at.desc()).limit(1)
        )
        last_ok = await session.scalar(
            select(JobRun).where(JobRun.job == name, JobRun.status == "succeeded")
            .order_by(JobRun.started_at.desc()).limit(1)
        )
        age = (now - last_ok.started_at).total_seconds() if last_ok else None
        out.append({
            "job": name,
            "description": job.description,
            "interval_seconds": job.interval_seconds,
            "last_status": last.status if last else None,
            "last_started_at": last.started_at if last else None,
            "last_error": last.error if last else None,
            "last_success_at": last_ok.started_at if last_ok else None,
            "last_success_items": last_ok.items_processed if last_ok else None,
            # Never run at all counts as stale: "we have no evidence this works" and
            # "this stopped working" need the same response.
            "stale": age is None or age > job.interval_seconds * STALE_AFTER_INTERVALS,
            "seconds_since_success": round(age) if age is not None else None,
        })
    return out


async def recent_runs(
    session: AsyncSession, *, job: str | None = None, limit: int = 50
) -> list[JobRun]:
    stmt = select(JobRun)
    if job:
        stmt = stmt.where(JobRun.job == job)
    rows = await session.execute(stmt.order_by(JobRun.started_at.desc()).limit(limit))
    return list(rows.scalars().all())
