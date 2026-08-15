"""Retention policies and the purge executor's records.

**This is the only module in the product that destroys data.** Every other one
records things; a bug there produces a wrong record somebody can correct. A bug
here deletes a customer's data irreversibly, on a schedule, with no human in the
loop, and there is nothing to correct it from.

The design follows from that:

* `auto_delete` defaults to **false**. Automatic destruction is opt-in per
  policy, never a default anyone can arrive at by not thinking about it.
* A policy that destroys automatically **must** warn first — a CHECK, not a
  convention.
* Purge runs and the rows they touched are **append-and-read**. A receipt the
  application can rewrite proves nothing about what was destroyed.
* Exemptions are structured, not free text: a reason code, a statute reference
  and an expiry, so "why is this still here?" has an answer a regulator accepts.

What "purge" means here mirrors the DSAR erasure path deliberately: **mask the
identifiers, keep the row**. Two different meanings of "erased" in one product
would be a support nightmare and an audit contradiction. The consent records
themselves are never destroyed — they are the evidence that holding the data was
permitted in the first place.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDMixin

# mask  — null the identifying fields, keep the row. Same as a DSAR erasure.
# delete — remove the row entirely. Only where a policy explicitly asks.
PURGE_ACTIONS = ("mask", "delete")

# Why something is kept past its retention period. Structured because
# "why is this still here?" needs an answer a regulator accepts, and free text
# is not one.
EXEMPTION_CODES = ("none", "statutory", "legal_hold", "dispute")

PURGE_MODES = ("dry_run", "live")
PURGE_STATUSES = ("running", "completed", "failed", "cancelled")


class RetentionPolicy(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "retention_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_retention_policies_tenant_name"),
        Index("ix_retention_policies_tenant_active", "tenant_id", "is_active"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Matches the category vocabulary used by purposes and notices, so a policy
    # is expressed in the same words as the thing it governs.
    data_category: Mapped[str] = mapped_column(String(128), nullable=False)

    retention_days: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False, default="mask")

    # FALSE by default, and the schema refuses to pair it with no warning period.
    auto_delete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notify_days: Mapped[int] = mapped_column(Integer, nullable=False, default=14)

    exemption_code: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    exemption_reference: Mapped[str | None] = mapped_column(String(255))
    exemption_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PurgeRun(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """The receipt. Append-and-read.

    "We deleted it because the policy said so" is only defensible if the policy,
    the run, and what it touched are all on record — and if none of the three can
    be edited afterwards.
    """

    __tablename__ = "purge_runs"
    __table_args__ = (
        Index("ix_purge_runs_tenant_started", "tenant_id", "started_at"),
        Index("ix_purge_runs_policy", "policy_id"),
    )

    policy_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("retention_policies.id", ondelete="RESTRICT"),
        nullable=False,
    )

    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # NULL for a scheduled run. Present means a human pressed the button, which
    # is a different fact and worth keeping distinct.
    initiated_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    candidates_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_affected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Per-table counts and the skip reasons. What a DPO reads before deciding.
    scope_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)


class PurgeRunItem(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """One row the run touched — or deliberately did not, and why.

    Recording the skips matters as much as recording the deletions: "this person
    was not purged because they have an open grievance" is the answer to a
    question somebody will eventually ask.
    """

    __tablename__ = "purge_run_items"
    __table_args__ = (
        Index("ix_purge_run_items_run", "purge_run_id"),
    )

    purge_run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("purge_runs.id", ondelete="CASCADE"), nullable=False
    )

    table_name: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)

    # masked | deleted | skipped
    action_taken: Mapped[str] = mapped_column(String(16), nullable=False)
    skip_reason: Mapped[str | None] = mapped_column(String(255))
