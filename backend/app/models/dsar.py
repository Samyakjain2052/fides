"""Data subject rights requests — the record, not the engine.

The engine already works: one privacy request fans out across four datastores
and `scripts/acceptance.sh` proves it on every run. What was missing was the
*record*. Until this table existed a submitted request lived in the browser's
`localStorage`, which meant it was invisible to the DPO, invisible on another
device, and gone if the person cleared their browser — while the erasure it
triggered had genuinely happened.

Two ideas carry the compliance weight here:

* **The deadline is computed by the server** from `tenants.dsar_sla_days`. A
  client-supplied deadline is not a statutory deadline, and this is the field a
  regulator asks about.
* **A rejected request must say why, and a completed one must say when.** Both
  are CHECK constraints rather than service-level politeness: a rejection with no
  recorded reason is not a decision anyone can defend.
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
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDMixin

# access and erasure execute against the Fides engine. `correction` does not —
# the engine has no correction action — so it is a tracked manual workflow with
# the same deadline and the same audit trail. A right the product hides is worse
# than one it handles by hand.
DSAR_TYPES = ("access", "erasure", "correction")

DSAR_STATUSES = (
    "received",      # recorded, nothing started
    "verifying",     # identity check in progress
    "in_progress",   # the engine is executing, or a human is working it
    "completed",
    "rejected",
    "cancelled",     # withdrawn by the person who raised it
)

# Who raised it. A DPO acting on a phone call is a real workflow; "staff can
# erase anyone" is also how someone gets erased maliciously. Recording which
# is what makes the difference auditable.
REQUESTED_BY = ("principal", "staff")


class DsarRequest(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "dsar_requests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "reference", name="uq_dsar_requests_tenant_reference"),
        Index("ix_dsar_requests_tenant_status", "tenant_id", "status"),
        Index("ix_dsar_requests_tenant_deadline", "tenant_id", "deadline_at"),
        Index("ix_dsar_requests_principal", "tenant_id", "principal_id"),
        # The engine's id, when there is one. Indexed because reconciliation
        # looks requests up by it.
        Index("ix_dsar_requests_engine_ref", "engine_ref"),
    )

    principal_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("data_principals.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # The handle a person quotes on the phone. Unique per tenant, human-shaped.
    reference: Mapped[str] = mapped_column(String(32), nullable=False)

    type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="received")

    # The Fides privacy request. NULL for correction, which the engine cannot do.
    engine_ref: Mapped[str | None] = mapped_column(String(128))
    engine_status: Mapped[str | None] = mapped_column(String(32))
    engine_error: Mapped[str | None] = mapped_column(Text)

    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # submitted_at + tenants.dsar_sla_days, computed server-side. See the module
    # docstring: this is the statutory clock, not a client hint.
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    verification_method: Mapped[str | None] = mapped_column(String(32))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    requested_by_actor: Mapped[str] = mapped_column(
        String(16), nullable=False, default="principal"
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text)

    # Correction only: what they say is wrong and what it should be.
    correction_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # An access package is one person's complete personal data in a single file.
    # It expires; see the service for why that is not optional.
    package_available_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_open(self) -> bool:
        return self.status not in ("completed", "rejected", "cancelled")


class DsarEvent(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """The per-request timeline. Append-only.

    Not redundant with the audit chain: the chain is tamper-evident *evidence*,
    this is the queryable timeline a screen renders. They are written together
    and a divergence between them is a bug worth catching.
    """

    __tablename__ = "dsar_events"
    __table_args__ = (
        Index("ix_dsar_events_request_at", "dsar_request_id", "created_at"),
    )

    dsar_request_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("dsar_requests.id", ondelete="CASCADE"), nullable=False
    )

    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    actor_label: Mapped[str | None] = mapped_column(String(255))

    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str | None] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text)

    # True when the engine moved it rather than a person. Kept distinct because
    # "the system did this" and "a human decided this" are different facts, and
    # an engine callback must never look like a human decision.
    automated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
