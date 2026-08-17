"""Grievance redressal — DPDP §13, and the statutory escalation clock.

Not a support ticketing system that happens to be about privacy. §13 gives every
Data Principal the right to a redressal mechanism and requires the Fiduciary to
publish a Grievance Officer; a person must exhaust this route *before* they can
approach the Data Protection Board. That makes this queue the last thing standing
between a complaint and a regulator.

Four decisions carry the weight:

* **The deadline and the escalation threshold come from the tenant**, not from
  constants. `grievance_sla_days` and `grievance_escalation_days` already exist
  and are per-customer, because the statutory window is a floor and a company may
  contractually promise faster.

* **A resolved grievance must say how it was resolved.** A CHECK constraint, not
  service-level politeness: "resolved" with no record of the resolution is the
  precise shape of a redressal mechanism that isn't one.

* **Somebody must be reachable.** A principal id or a contact email, enforced by
  CHECK. A complaint nobody can be answered at is not actionable, and accepting
  one anyway lets a queue look busy while nothing is being redressed.

* **`description` is hostile input.** It is free text from a member of the public.
  Stored raw and escaped at every rendering path — the queue, the email, any
  future export. It will also routinely contain personal data about *third
  parties* ("your agent Ravi told me…"), which is unavoidable, and which is why
  it needs saying out loud: that text is inside the retention and export story
  whether anyone planned for it or not.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDMixin

GRIEVANCE_CATEGORIES = (
    "consent_violation",
    "data_breach",
    "dsar_delay",
    "inaccurate_data",
    "other",
)

GRIEVANCE_STATUSES = (
    "open",          # filed, nobody has looked at it
    "acknowledged",  # a human has seen it and said so
    "in_progress",   # being worked
    "resolved",
    "rejected",
    "reopened",      # the person was not satisfied — see the service
)

# Deliberately narrow. A grievance is not a support ticket with a fluid
# lifecycle; every one of these transitions is a fact somebody may have to
# defend to a regulator, so the set of legal moves is small and explicit.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "open": {"acknowledged", "in_progress", "rejected"},
    "acknowledged": {"in_progress", "resolved", "rejected"},
    "in_progress": {"resolved", "rejected"},
    # A resolved grievance can be reopened — by the person who filed it, if the
    # resolution did not satisfy them. That is the whole point of collecting a
    # satisfaction rating.
    "resolved": {"reopened"},
    "reopened": {"in_progress", "resolved", "rejected"},
    # Terminal. A rejected grievance is not reopened; the person's next step is
    # the Board, and quietly reopening it here would obscure that they were
    # refused.
    "rejected": set(),
}

TERMINAL_STATUSES = ("resolved", "rejected")

# A note on constraint names below: `NAMING_CONVENTION` in db/base.py already
# prepends `ck_<table>_`, so these are named bare (`status`, not
# `ck_grievances_status`) and come out as `ck_grievances_status`. Some earlier
# tables passed the prefix explicitly and ended up double-prefixed and truncated
# — `ck_retention_policies_ck_retention_policies_auto_delete_7b49`. Renaming those
# means rewriting applied migrations, so they stay as they are; new tables do it
# the intended way.


class Grievance(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "grievances"
    __table_args__ = (
        UniqueConstraint("tenant_id", "reference", name="uq_grievances_tenant_reference"),
        Index("ix_grievances_tenant_status", "tenant_id", "status"),
        Index("ix_grievances_tenant_deadline", "tenant_id", "deadline_at"),
        Index("ix_grievances_principal", "tenant_id", "principal_id"),
        Index("ix_grievances_assigned", "tenant_id", "assigned_to"),
        CheckConstraint(
            "status IN ('open','acknowledged','in_progress','resolved',"
            "'rejected','reopened')",
            name="status",
        ),
        CheckConstraint(
            "category IN ('consent_violation','data_breach','dsar_delay',"
            "'inaccurate_data','other')",
            name="category",
        ),
        # Somebody has to be reachable. A complaint nobody can be answered at is
        # not actionable, and accepting one anyway lets a queue look busy while
        # nothing is being redressed.
        CheckConstraint(
            "principal_id IS NOT NULL OR contact_email IS NOT NULL",
            name="reachable",
        ),
        # A resolution with no record of the resolution is not one.
        CheckConstraint(
            "status <> 'resolved' OR "
            "(resolution_notes IS NOT NULL AND resolved_at IS NOT NULL)",
            name="resolved_has_notes",
        ),
        # Nor is a refusal with no reason — and refusal is the point at which the
        # person's next stop is the Board.
        CheckConstraint(
            "status <> 'rejected' OR rejection_reason IS NOT NULL",
            name="rejected_has_reason",
        ),
        CheckConstraint(
            "NOT escalated OR escalated_at IS NOT NULL",
            name="escalated_has_timestamp",
        ),
        CheckConstraint(
            "deadline_at > submitted_at", name="deadline_after_submit"
        ),
        CheckConstraint(
            "satisfaction_rating IS NULL OR satisfaction_rating BETWEEN 1 AND 5",
            name="rating_range",
        ),
        # A rating is the filer's verdict on a resolution. One recorded against a
        # grievance that was never resolved is a data-entry bug at best.
        CheckConstraint(
            "satisfaction_rating IS NULL OR status IN ('resolved','reopened')",
            name="rating_needs_resolution",
        ),
        CheckConstraint(
            "NOT contact_verified OR verified_at IS NOT NULL",
            name="verified_has_timestamp",
        ),
    )

    # Nullable: a person whose data you hold may have no account with you.
    # Requiring one would make an account a precondition for exercising §13,
    # which arguably defeats §13.
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("data_principals.id", ondelete="RESTRICT")
    )

    reference: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)

    # Raw, as written. Never pre-escaped on the way in: storing escaped text means
    # every consumer has to know whether it was, and one that guesses wrong shows
    # `&amp;lt;` to a DPO or renders markup to a browser.
    description: Mapped[str] = mapped_column(Text, nullable=False)

    contact_email: Mapped[str | None] = mapped_column(String(320))

    # Whether that address has been confirmed by someone clicking a link.
    #
    # Filing is NOT gated on it — a barrier in front of a statutory right is a
    # barrier, and a genuine complaint from someone who never opens the email is
    # still a complaint. What it gates is escalation: waking a Grievance Officer
    # over an address nobody has confirmed turns the statutory alarm into noise.
    contact_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verification_token_hash: Mapped[str | None] = mapped_column(String(64))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")

    assigned_to: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    related_dsar_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("dsar_requests.id", ondelete="SET NULL")
    )

    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # submitted_at + tenants.grievance_sla_days, computed server-side. A
    # client-supplied deadline is not a statutory deadline.
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # submitted_at + tenants.grievance_escalation_days. Stored rather than
    # recomputed so that changing the tenant's threshold tomorrow does not
    # retroactively rewrite whether yesterday's grievance was late.
    escalate_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    escalated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    resolution_notes: Mapped[str | None] = mapped_column(Text)
    rejection_reason: Mapped[str | None] = mapped_column(Text)

    satisfaction_rating: Mapped[int | None] = mapped_column(Integer)
    satisfaction_comment: Mapped[str | None] = mapped_column(Text)

    @property
    def is_open(self) -> bool:
        return self.status not in TERMINAL_STATUSES

    @property
    def is_overdue(self) -> bool:
        """Past the statutory deadline and still unresolved.

        Computed, never stored. A stored flag is only as fresh as the last job
        that ran, and the moment it is stale is precisely the moment a DPO is
        looking at the screen.
        """
        return self.is_open and datetime.now(UTC) > self.deadline_at

    @property
    def escalation_due(self) -> bool:
        """Past the escalation threshold, unresolved, and not yet escalated.

        Excludes unconfirmed contact addresses. See `contact_verified`: an
        unverifiable complaint is still recorded and still counted, but it does
        not get to page a Grievance Officer.
        """
        if self.escalated or not self.is_open:
            return False
        if self.principal_id is None and not self.contact_verified:
            return False
        return datetime.now(UTC) > self.escalate_at

    @property
    def days_open(self) -> int:
        end = self.resolved_at or datetime.now(UTC)
        return max(0, (end - self.submitted_at).days)


class GrievanceEvent(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """The per-grievance timeline. Append-only.

    Same shape and same reasoning as `dsar_events`: the audit chain is
    tamper-evident evidence, this is the timeline a screen renders. Written
    together, and a divergence between them is a bug worth catching.
    """

    __tablename__ = "grievance_events"
    __table_args__ = (
        Index("ix_grievance_events_grievance_at", "grievance_id", "created_at"),
    )

    grievance_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("grievances.id", ondelete="CASCADE"), nullable=False
    )

    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    actor_label: Mapped[str | None] = mapped_column(String(255))

    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str | None] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text)

    # True when the escalation clock moved it rather than a person. "The system
    # escalated this because nobody answered" and "a human escalated this" are
    # different facts, and conflating them would let inaction read as judgement.
    automated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
