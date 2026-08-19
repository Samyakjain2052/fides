"""Liveness for the scheduler process.

The scheduler serves no HTTP, so it cannot answer the image's default
healthcheck — that one asks the API's `/health` on port 8100 and the worker was
therefore permanently `unhealthy` while running perfectly. A container stuck in
that state is exactly the false alarm the run log was built to avoid, and on
Container Apps it fails a readiness gate.

What this checks instead is the thing that actually matters: **has any job
succeeded recently.** A process that is alive but whose jobs all fail is not a
working scheduler, and a check that only proved the process existed would say it
was fine.

Tolerant on purpose:

* A job is late only after `STALE_AFTER_INTERVALS` of its own interval, so one
  slow sweep is not an outage.
* The longest interval is a day, so during the first day after a deploy the
  daily job has no success to show. Fresh containers are given a grace period
  rather than being failed for not having run yet — otherwise every deployment
  would look broken until tomorrow.
* A database it cannot reach is reported as unhealthy, which is correct: a
  scheduler that cannot read its own queue is not doing its work.

Exits 0 for healthy, 1 for not.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import UTC, datetime

# How long after start-up to accept "nothing has run yet". Long enough for every
# job to have had a turn, including the daily one.
GRACE_SECONDS = int(os.environ.get("DS_WORKER_HEALTH_GRACE_SECONDS", 90))

# Written by the worker on each loop pass. Its mtime proves the loop is turning
# even when there is no work to do, which is the common case.
HEARTBEAT = os.environ.get("DS_WORKER_HEARTBEAT_FILE", "/tmp/ds-scheduler-heartbeat")

# If the loop has not ticked in this long, it is wedged — the loop sleeps 5s.
HEARTBEAT_MAX_AGE = int(os.environ.get("DS_WORKER_HEARTBEAT_MAX_AGE", 120))


async def _check() -> tuple[bool, str]:
    from app.db.session import dispose_engine, unscoped_session
    from app.services import scheduler

    # 1. Is the loop turning at all? Cheap, and catches a wedged process that a
    #    query-based check would miss entirely if the wedge is in the sleep.
    try:
        age = time.time() - os.path.getmtime(HEARTBEAT)
    except OSError:
        age = None
    if age is None:
        # No heartbeat yet. Only acceptable while still starting up.
        pass
    elif age > HEARTBEAT_MAX_AGE:
        return False, f"the scheduler loop has not ticked for {int(age)}s"

    # 2. Has anything actually succeeded? This is the part that distinguishes
    #    "running" from "working".
    try:
        async with unscoped_session() as session:
            rows = await scheduler.status(session)
    except Exception as exc:  # noqa: BLE001
        return False, f"cannot reach the database: {type(exc).__name__}"
    finally:
        await dispose_engine()

    never_run = [r["job"] for r in rows if r["last_success_at"] is None]
    stale = [r["job"] for r in rows if r["stale"] and r["last_success_at"] is not None]

    if stale:
        return False, f"jobs have stopped succeeding: {', '.join(stale)}"

    if never_run:
        # Nothing has succeeded yet. Fine only while inside the grace window —
        # after that, a job that has never once run is a real failure.
        if age is not None and age < GRACE_SECONDS:
            return True, f"starting up; {len(never_run)} job(s) not yet run"
        return False, f"jobs have never succeeded: {', '.join(never_run)}"

    return True, "every job has succeeded within its window"


def main() -> None:
    try:
        ok, detail = asyncio.run(_check())
    except Exception as exc:  # noqa: BLE001
        # An unexpected failure in the check is itself a reason to report
        # unhealthy — silently passing would defeat the point.
        print(f"healthcheck error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
    print(detail)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
