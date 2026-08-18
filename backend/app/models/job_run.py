"""The scheduler's own record of what it did.

This table exists because of a specific failure mode. Four modules previously
disclosed "there is no scheduler" — which was honest and, being visible in the UI,
harmless. Replacing that with a scheduler creates a worse possibility: a scheduler
that stopped running weeks ago, while every screen quietly claims escalation and
retry are automatic. Nobody notices, because nothing appears broken.

So the run log is not telemetry. It is the thing that makes the claim checkable:

* **Every attempt is recorded, including failures.** A row is written when a job
  starts and updated when it ends, so a job that crashed mid-run leaves a `running`
  row with no finish — visibly stuck rather than absent.
* **It is append-and-update, never deleted** by the application. Trimming it is a
  scheduled task run by the owner role, like the notification log.
* **Platform-level, not tenant-scoped.** One row covers a sweep across every
  tenant, so it carries no tenant's data and has no RLS policy — the same
  arrangement as `tenants`. What it holds is counts and error strings, and a
  deliberate choice not to name the tenants involved: "which customers had
  escalations last night" is not a question this table needs to answer.

`GET /v1/admin/jobs` reads it, and the module caveats now quote it rather than
asserting the scheduler is running.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin

JOB_STATUSES = (
    "running",   # started, no outcome yet. A stale one means a crash.
    "succeeded",
    "failed",
    # Another process held the lock. Recorded rather than silent so a misbehaving
    # deployment — two schedulers fighting — is visible in the log.
    "skipped_locked",
)


class JobRun(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "job_runs"
    __table_args__ = (
        Index("ix_job_runs_job_started", "job", "started_at"),
        CheckConstraint(
            "status IN ('running','succeeded','failed','skipped_locked')",
            name="status",
        ),
        # A finished run has to say when. Without this a crashed run and a
        # completed one are indistinguishable, which is the whole point of the
        # table.
        CheckConstraint(
            "status = 'running' OR finished_at IS NOT NULL",
            name="finished_unless_running",
        ),
        CheckConstraint(
            "status <> 'failed' OR error IS NOT NULL", name="failed_has_error"
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="finished_after_started",
        ),
    )

    job: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # How many tenants the sweep covered, and how much work it found. Counts only:
    # naming the tenants would turn a platform table into one that discloses which
    # customers had overdue complaints.
    tenants_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    error: Mapped[str | None] = mapped_column(Text)

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()
