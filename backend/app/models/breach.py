"""The breach register — DPDP §8(6).

On becoming aware of a personal data breach, a Data Fiduciary must notify **both**
the Data Protection Board **and every affected Data Principal**. Two things in
that sentence shape this whole module.

**"Aware" is a timestamp somebody will litigate.** The clock does not start when
the breach happened, or when it was contained. It starts when the fiduciary became
aware, which is why `discovered_at` is its own column, required the moment a
breach leaves draft, changeable only with a recorded reason, and given its own
audit action rather than being folded into a generic update.

**Notifying only the Board is not compliance.** The affected individuals are a
separate, mandatory obligation. A product that let somebody mark a breach
"notified" having done half of it would be actively harmful — it would produce a
confident record of compliance that a regulator could disprove in one question. So
`status = 'notified'` requires both timestamps, enforced by CHECK, not by the UI
remembering to.

Two further decisions worth stating:

* **Nothing is ever deleted.** A mistaken entry becomes `void` with a reason, and
  stays. A register whose entries can vanish is not a register.
* **The product never submits to the Board.** There is no API for it — it is a
  portal process — and unattended software contacting a regulator is not something
  this should do. What is recorded is that a named human submitted it, and the
  reference they got back.

This table holds the most sensitive combination in the product: who was affected
by what. `breach_affected_principals` exists so that fact is queryable, and it is
why no read scope wider than `breach:manage` reaches it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    ARRAY,
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

SEVERITIES = ("low", "medium", "high", "critical")

BREACH_STATUSES = (
    "draft",          # being written up; discovered_at not yet required
    "investigating",  # real, under investigation
    "contained",      # stopped, not yet notified
    "notified",       # BOTH the Board and the affected principals
    "closed",         # root cause and remediation recorded
    "void",           # recorded in error; kept, with a reason
)

# Narrow on purpose. Every one of these transitions is a fact a fiduciary may have
# to defend, and `void` is reachable from anywhere because a mistake can be
# noticed at any point.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"investigating", "void"},
    "investigating": {"contained", "notified", "void"},
    # Notifying without containing is legal and sometimes necessary — the duty is
    # triggered by awareness, not by having fixed it.
    "contained": {"notified", "void"},
    "notified": {"closed", "void"},
    # Terminal. Reopening a closed breach would let the record of what was learned
    # be quietly rewritten; record a new breach instead.
    "closed": set(),
    "void": set(),
}

# How long after becoming aware the Board notification is treated as late.
#
# The Rules say "without delay", which is not a number. 72 hours is the widely
# used operational reading and is what the interface counts down to — but it is
# OUR interpretation, not a statutory figure, and the UI says so. Encoding it
# without saying that would be inventing a legal deadline.
BOARD_NOTIFICATION_HOURS = 72


class Breach(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "breaches"
    __table_args__ = (
        UniqueConstraint("tenant_id", "reference", name="uq_breaches_tenant_reference"),
        Index("ix_breaches_tenant_status", "tenant_id", "status"),
        Index("ix_breaches_tenant_discovered", "tenant_id", "discovered_at"),
        CheckConstraint(
            "severity IN ('low','medium','high','critical')", name="severity"
        ),
        CheckConstraint(
            "status IN ('draft','investigating','contained','notified','closed','void')",
            name="status",
        ),
        # The field the obligation hangs on. Optional only while drafting.
        CheckConstraint(
            "status = 'draft' OR discovered_at IS NOT NULL",
            name="discovered_at_required_outside_draft",
        ),
        # Half a notification is not a notification. This is the constraint that
        # stops the product producing a confident record of compliance that a
        # regulator can disprove in one question.
        CheckConstraint(
            "status <> 'notified' OR "
            "(board_notified_at IS NOT NULL AND principals_notified_at IS NOT NULL)",
            name="notified_means_both",
        ),
        # A closed breach with no recorded cause teaches nobody anything, and the
        # next one will be the same breach.
        CheckConstraint(
            "status <> 'closed' OR (root_cause IS NOT NULL AND remediation IS NOT NULL "
            "AND closed_at IS NOT NULL)",
            name="closed_needs_cause_and_fix",
        ),
        CheckConstraint(
            "status <> 'void' OR void_reason IS NOT NULL", name="void_needs_reason"
        ),
        # A Board reference without a timestamp, or the reverse, is a half-recorded
        # submission — and this is the evidence that the submission happened.
        CheckConstraint(
            "(board_notified_at IS NULL) = (board_submitted_by IS NULL)",
            name="board_submission_is_whole",
        ),
        CheckConstraint(
            "occurred_at IS NULL OR discovered_at IS NULL OR occurred_at <= discovered_at",
            name="occurred_before_discovered",
        ),
        CheckConstraint(
            "estimated_affected_count IS NULL OR estimated_affected_count >= 0",
            name="affected_count_not_negative",
        ),
    )

    reference: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")

    # Often genuinely unknown, and a guess dressed as a fact is worse than a null.
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The statutory clock. See the module docstring.
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    contained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Uses the same vocabulary as `purposes.category`, so "which breaches touched
    # Contact Data" is answerable against the consent records rather than by
    # matching free text.
    categories_affected: Mapped[list[str]] = mapped_column(
        ARRAY(String(128)), nullable=False, default=list
    )
    # The fiduciary's own estimate, kept alongside the exact attached count rather
    # than replaced by it: the gap between "we thought 5,000" and "it was 12,400"
    # is itself worth having on the record.
    estimated_affected_count: Mapped[int | None] = mapped_column(Integer)

    root_cause: Mapped[str | None] = mapped_column(Text)
    remediation: Mapped[str | None] = mapped_column(Text)

    board_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    board_reference: Mapped[str | None] = mapped_column(String(255))
    # Who filed it. The product does not submit to the Board, so the record has to
    # name the person who did — "the system reported it" would be false.
    board_submitted_by: Mapped[str | None] = mapped_column(String(255))

    principals_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # A documented reason for closing with people un-notified. Nullable, and the
    # only way past that check — so the exemption is a written decision rather
    # than a silent skip.
    notification_exemption: Mapped[str | None] = mapped_column(Text)

    void_reason: Mapped[str | None] = mapped_column(Text)

    @property
    def is_open(self) -> bool:
        return self.status not in ("closed", "void")

    @property
    def board_deadline_at(self) -> datetime | None:
        """When the Board notification becomes late, by our reading of the Rules.

        Null while `discovered_at` is unknown, because a deadline computed from a
        missing start is a fabricated number.
        """
        if self.discovered_at is None:
            return None
        from datetime import timedelta

        return self.discovered_at + timedelta(hours=BOARD_NOTIFICATION_HOURS)

    @property
    def board_overdue(self) -> bool:
        deadline = self.board_deadline_at
        return (
            deadline is not None
            and self.board_notified_at is None
            and self.is_open
            and datetime.now(UTC) > deadline
        )

    @property
    def hours_since_discovery(self) -> float | None:
        if self.discovered_at is None:
            return None
        return (datetime.now(UTC) - self.discovered_at).total_seconds() / 3600


class BreachAffectedPrincipal(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """One person affected by one breach, and whether they were told.

    This table is what makes the bulk notification resumable. Ten thousand people,
    a provider rate limit, and a half-finished run is the normal case — not the
    exception — so `notified_at` per row is the thing that makes the second attempt
    safe and the progress figure true.
    """

    __tablename__ = "breach_affected_principals"
    __table_args__ = (
        # Attaching the same person twice is the obvious bug, and it would both
        # double-notify them and inflate the count a regulator is shown.
        UniqueConstraint(
            "breach_id", "principal_id", name="uq_breach_affected_breach_principal"
        ),
        Index("ix_breach_affected_breach", "breach_id", "notified_at"),
        Index("ix_breach_affected_tenant", "tenant_id"),
        CheckConstraint(
            "notified_at IS NULL OR notification_id IS NOT NULL OR suppressed_reason "
            "IS NOT NULL",
            name="notified_has_a_trace",
        ),
    )

    breach_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("breaches.id", ondelete="CASCADE"),
        nullable=False,
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        # RESTRICT: a person attached to a breach must not be deletable out from
        # under the record that says they were affected.
        ForeignKey("data_principals.id", ondelete="RESTRICT"),
        nullable=False,
    )

    notification_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("notifications.id", ondelete="SET NULL")
    )
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # "We could not tell this person, because we hold no address for them" is an
    # answer a fiduciary needs to be able to give. It counts as handled, and it
    # counts differently from delivered.
    suppressed_reason: Mapped[str | None] = mapped_column(String(255))

    # How they came to be on the list. A DPO reviewing who is about to be told
    # needs to know which rows came from a query and which a human added.
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="query")


class BreachEvent(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """The per-breach timeline. Append-only.

    Same reasoning as the DSAR and grievance timelines: the audit chain is
    tamper-evident evidence, this is the timeline a screen renders.
    """

    __tablename__ = "breach_events"
    __table_args__ = (Index("ix_breach_events_breach_at", "breach_id", "created_at"),)

    breach_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("breaches.id", ondelete="CASCADE"),
        nullable=False,
    )

    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    actor_label: Mapped[str | None] = mapped_column(String(255))

    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str | None] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text)
    automated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
