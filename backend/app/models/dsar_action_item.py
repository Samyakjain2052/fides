"""One unit of work on a rights request: one system, one named owner.

The data map answers "where is this person's data" in a single sweep. That is a
scan, and a scan is not a workflow. It leaves two things unanswered that a
regulator will ask about directly:

  who was responsible for this system, and
  what did they find?

WHY A NEGATIVE RESULT NEEDS A ROW

"We searched the payroll database and it held nothing about this person" and
"nobody ever looked at payroll" are completely different facts, and a scan that
returns no findings expresses both identically. `data_map_service` already draws
this distinction for connections it could not reach — reporting them as *unknown*
rather than empty — and this extends it to the human case: an action item is
closed by an explicit attestation, either "found and handled" or "searched,
nothing matched". Silence closes nothing.

WHY ASSIGNMENT MATTERS MORE THAN IT LOOKS

A rights request against fifteen systems is fifteen pieces of work, and in any
real organisation they belong to different people — the CRM to sales ops, the
warehouse to finance, the mailing list to marketing. A single queue that says
"this request is in progress" gives a DPO no way to find out which of the
fifteen is the one nobody has touched. Assignment plus a per-item status does,
and the statutory deadline is the reason it matters: the clock runs on the
request, not on the item, so the slowest system is the whole obligation.

THIRD PARTIES

Some systems cannot be reached by API and never will be — a processor's own
database, an offline archive, a payroll bureau. For those the work is to tell
somebody, and the record we need is that we told them, whom, and when. §8(2)
keeps the fiduciary responsible for its processors, so "we asked our vendor to
delete it" is a fact worth being able to produce.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDMixin

#: Where an item is in its own small lifecycle.
#:
#:   pending    created by the fan-out, nobody has taken it
#:   claimed    somebody has said they are doing it
#:   completed  closed with an outcome and an attestation
#:   failed     attempted and could not be finished; needs a human
#:   skipped    deliberately not done, with a recorded reason (a statutory
#:              retention obligation, most often)
ACTION_ITEM_STATUSES = ("pending", "claimed", "completed", "failed", "skipped")

#: What was concluded. Required to close an item, because "completed" on its own
#: does not say whether anything was found, let alone done.
#:
#:   data_found          records matched and were disclosed or handled
#:   no_records_matched  searched; this system holds nothing about this person
#:   erased              records matched and were masked or deleted
#:   retained            records matched and are being kept, lawfully — the
#:                       reason goes in `skip_reason`
#:   third_party_asked   we cannot reach this system; somebody was told to act
ACTION_ITEM_OUTCOMES = (
    "data_found",
    "no_records_matched",
    "erased",
    "retained",
    "third_party_asked",
)


class DsarActionItem(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "dsar_action_items"
    __table_args__ = (
        Index("ix_dsar_action_items_request", "dsar_request_id"),
        Index("ix_dsar_action_items_assignee", "tenant_id", "assignee_user_id"),
        Index("ix_dsar_action_items_status", "tenant_id", "status"),
        # One item per (request, connection). A re-run of the fan-out must not
        # duplicate work somebody has already claimed. NULL connection_id is
        # exempt from this by SQL's NULL semantics, which is what lets a request
        # carry several manual items for systems that have no connection row.
        UniqueConstraint(
            "dsar_request_id", "connection_id",
            name="uq_dsar_action_items_request_connection",
        ),
        CheckConstraint(
            "status IN ('pending','claimed','completed','failed','skipped')",
            name="status",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('data_found','no_records_matched',"
            "'erased','retained','third_party_asked')",
            name="outcome",
        ),
        # Closing an item requires saying what was concluded. This is the whole
        # point of the table: a completed item with no outcome is exactly the
        # silence it exists to prevent.
        CheckConstraint(
            "status <> 'completed' OR "
            "(outcome IS NOT NULL AND completed_at IS NOT NULL)",
            name="completed_has_outcome",
        ),
        # As is refusing to do one.
        CheckConstraint(
            "status <> 'skipped' OR skip_reason IS NOT NULL",
            name="skipped_has_reason",
        ),
        CheckConstraint(
            "status <> 'failed' OR failure_reason IS NOT NULL",
            name="failed_has_reason",
        ),
        # Retention is a legal claim. It needs a stated basis, not a shrug.
        CheckConstraint(
            "outcome <> 'retained' OR skip_reason IS NOT NULL",
            name="retained_has_basis",
        ),
        CheckConstraint("records_found >= 0", name="records_not_negative"),
    )

    dsar_request_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("dsar_requests.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: NULL for a system with no connection — a processor's own database, an
    #: offline archive. SET NULL rather than CASCADE on delete: removing a
    #: connection must not erase the record of what was done about a rights
    #: request through it.
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("connections.id", ondelete="SET NULL")
    )

    #: Captured at creation, so the item still names its system after the
    #: connection is renamed or deleted.
    system_label: Mapped[str] = mapped_column(String(160), nullable=False)

    #: Who owns this piece of work. NULL means unassigned, which is a state a
    #: DPO needs to be able to see and chase rather than a default that hides.
    assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    #: True when the connector can do this without a human. Distinguished from
    #: an unassigned human item for the same reason `DsarEvent.automated` is:
    #: an item nobody has picked up and an item that needs nobody are opposite
    #: situations, and conflating them makes a queue look stalled or look done.
    automated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    outcome: Mapped[str | None] = mapped_column(String(24))

    #: How many rows matched. Count only — never a value, the same rule the data
    #: map and `PurgeRunItem` already follow.
    records_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: The human statement closing the item. Free text on purpose: what an
    #: auditor wants here is a sentence somebody was willing to write.
    attestation: Mapped[str | None] = mapped_column(Text)

    #: Why it was skipped, or the lawful basis for retaining.
    skip_reason: Mapped[str | None] = mapped_column(Text)
    failure_reason: Mapped[str | None] = mapped_column(Text)

    #: Working notes. Not disclosed to the data principal — this is our record
    #: of handling their request, not part of the response to it.
    internal_notes: Mapped[str | None] = mapped_column(Text)

    #: `[{"address": "...", "notified_at": "...", "note": "..."}]`.
    #:
    #: A list rather than one address, because a system frequently has more than
    #: one party to tell, and each needs its own timestamp — a single
    #: `notified_at` cannot express "we told the vendor in March and their
    #: sub-processor in April".
    third_parties_notified: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )

    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    @property
    def is_open(self) -> bool:
        return self.status in ("pending", "claimed", "failed")
