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

# The rights the Act actually grants, kept distinct because §12(1) names them
# separately and a person is entitled to ask for the one they mean.
#
#   access      §11 — a summary of their personal data and the processing of it
#   correction  §12(1) — a value is wrong
#   completion  §12(1) — a value is absent and should not be
#   updating    §12(1) — a value was right and has changed
#   erasure     §12(3)
#
# Collapsing the middle three into "correction" was the previous shape, and it
# loses information the fiduciary needs: correcting a misspelled name, adding a
# missing middle name, and changing an address after a move are three different
# operations on the source system, with different evidence and different risk of
# getting it wrong. A screen that offers only "correction" also makes somebody
# choose the closest wrong word for what they want, which then has to be
# guessed at by whoever picks the request up.
#
# access and erasure execute against the Fides engine. The §12(1) three do not —
# the engine has no correction action — so they are tracked manual workflows
# with the same deadline and the same audit trail. A right the product hides is
# worse than one it handles by hand.
DSAR_TYPES = ("access", "correction", "completion", "updating", "erasure")

#: The §12(1) family. Grouped because they share a workflow: no engine action,
#: a payload describing the change, and a human making it in the source system.
CORRECTION_TYPES = ("correction", "completion", "updating")

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

    #: Keyed hash of the emailed confirmation token, never the token. Set
    #: only for a request that arrived through the unauthenticated public
    #: form, where the address is a claim until somebody proves they control
    #: the mailbox.
    verification_token_hash: Mapped[str | None] = mapped_column(String(64))

    #: True when it came in through the public form rather than the portal.
    #: It matters for triage: a portal request has a session behind it, and
    #: a public one has an unproven address until it is confirmed.
    arrived_publicly: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    requested_by_actor: Mapped[str] = mapped_column(
        String(16), nullable=False, default="principal"
    )

    #: Set when a NOMINEE raised this rather than the person themselves —
    #: §14. The link matters for the audit trail: "who asked for this
    #: erasure" has to be answerable, and "the deceased" is not the answer.
    nomination_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("nominations.id", ondelete="SET NULL")
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text)

    # Correction only: what they say is wrong and what it should be.
    correction_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # An access package is one person's complete personal data in a single file.
    # It expires; see the service for why that is not optional.
    package_available_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ------------------------------------------------ identity verification --
    #
    # `verification_method` and `verified_at` above record that verification
    # happened. These record the DECISION: which document was examined, who
    # examined it, when, and — if it was refused — why. A rejection on identity
    # grounds is the one refusal a person is most likely to challenge, and
    # "verified_at IS NULL" is not an answer to "why was I turned down".
    identity_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("stored_files.id", ondelete="SET NULL")
    )
    identity_reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    identity_reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    identity_rejection_reason: Mapped[str | None] = mapped_column(Text)

    # ---------------------------------------------------- assembled package --
    #
    # The stored artifact, as opposed to the live engine passthrough that
    # preceded it. Assembled once, hashed, and delivered through the message
    # thread — so a request fulfilled by hand through the connections path
    # produces a real deliverable, and the engine is not required to retain
    # somebody's data forever just so a download keeps working.
    package_file_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("stored_files.id", ondelete="SET NULL")
    )
    package_assembled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    package_delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    @property
    def is_open(self) -> bool:
        return self.status not in ("completed", "rejected", "cancelled")

    @property
    def identity_verified(self) -> bool:
        """Whether identity was affirmatively confirmed.

        Not the same as `verified_at is not None`: a request can arrive already
        verified by another route (an authenticated portal session, a staff
        member acting on a known caller), and this asks specifically whether a
        document was reviewed and accepted.
        """
        return (
            self.identity_reviewed_at is not None
            and self.identity_rejection_reason is None
        )


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
