"""Transactional compliance messaging — templates, and the delivery log.

Not a marketing system. Every message here exists because a statutory or
contractual obligation says somebody must be told something: a rights request was
received, a consent was withdrawn, a grievance passed its escalation clock, data
is about to be purged.

Two design points worth stating:

* **The delivery log is evidence, not telemetry.** "We notified you on the 14th"
  is a claim a fiduciary will have to defend, so the log is append-mostly: rows
  are inserted, and only the sender's own status columns may be updated. A freely
  mutable log is not evidence that anything was sent.

* **It deliberately does not store rendered bodies.** A log of message bodies is
  a second copy of everyone's personal data, sitting outside the consent
  machinery, with its own retention problem. The address and the subject are
  enough to support the claim being made.
"""

from __future__ import annotations

import uuid
from datetime import datetime

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
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDMixin

CHANNELS = ("email", "sms")

NOTIFICATION_STATUSES = (
    "queued",       # accepted, not yet attempted
    "sending",      # claimed by a worker
    "delivered",    # the provider accepted it
    "failed",       # permanently — retries exhausted or a hard rejection
    "suppressed",   # deliberately not sent; see suppression_reason
)

# Every template the product sends, and the ONLY placeholders each may use.
#
# Validated when a template is saved, not when it is sent. Discovering a typo
# because a statutory notification silently failed at 2am is too late, and a
# placeholder that renders as an empty string is worse than one that was rejected.
TEMPLATE_KEYS: dict[str, tuple[str, ...]] = {
    "dsar.received": ("reference", "type", "deadline", "organisation"),
    "dsar.completed": ("reference", "type", "organisation"),
    "dsar.rejected": ("reference", "type", "reason", "organisation"),
    "consent.withdrawn": ("purpose", "organisation", "effective_from"),
    "grievance.received": ("reference", "category", "deadline", "organisation"),
    "grievance.escalated": ("reference", "category", "days_open", "organisation"),
    "grievance.resolved": ("reference", "resolution", "organisation"),
    # Separate from `resolved` because the subject line differs and the difference
    # matters: telling somebody their complaint "is resolved" when it was in fact
    # refused is the kind of wording that reads as a resolution and is not one.
    "grievance.rejected": ("reference", "reason", "organisation"),
    # Public filing only. Separate from `received` because it asks the person to
    # DO something, and burying a required action inside an acknowledgement is how
    # it gets ignored.
    "grievance.confirm": ("reference", "code", "deadline", "organisation"),
    "retention.pre_purge": ("category", "purge_date", "organisation"),
    # DPDP §8(6). Notifying the Board is only half the duty — the affected people
    # are a separate, mandatory obligation, and this is how that half is done.
    # Console users, not data principals. An invitation is how somebody gets an
    # account without an administrator ever knowing their password.
    "user.invitation": ("role", "accept_url", "expires_in", "organisation"),
    "breach.principal_notice": (
        "reference", "categories", "discovered_on", "remediation", "organisation",
    ),
    # Goes to the DPO, not to a data principal — the only key here that does.
    # A connection to a customer's own system that has stopped working shrinks
    # their DSAR reach silently, and the moment they would otherwise find out is
    # while a statutory deadline is running.
    # Goes to a console user who cannot sign in. `expires_in` is stated because
    # a reset link with an unclear lifetime gets opened an hour too late.
    "user.password_reset": ("reset_url", "expires_in", "organisation"),
    "connection.failing": (
        "connection", "system", "failures", "since", "reason", "organisation",
    ),
}


class NotificationTemplate(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "notification_templates"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "key", "channel", "language",
            name="uq_notification_templates_tenant_key_channel_language",
        ),
        Index("ix_notification_templates_lookup", "tenant_id", "key", "channel"),
    )

    key: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(8), nullable=False, default="email")
    language: Mapped[str] = mapped_column(String(32), nullable=False, default="English")

    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Notification(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """One message. The row IS the evidence that it was sent."""

    __tablename__ = "notifications"
    __table_args__ = (
        # Idempotency: one message per (template, entity). A DPO refreshing a
        # queue, or a retried job, must not re-notify a data principal about
        # something they were already told.
        UniqueConstraint(
            "tenant_id", "template_key", "entity_type", "entity_id",
            name="uq_notifications_tenant_template_entity",
        ),
        # The worker's claim query: oldest queued row whose next attempt is due.
        Index("ix_notifications_claim", "status", "next_attempt_at"),
        Index("ix_notifications_tenant_created", "tenant_id", "created_at"),
        Index("ix_notifications_principal", "tenant_id", "principal_id"),
    )

    template_key: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(8), nullable=False, default="email")

    # The language ACTUALLY used, not the one requested. When a template is
    # missing for someone's language we fall back to English — and "we notified
    # them in their language" has to be checkable, so the fallback is recorded
    # rather than invisible.
    language: Mapped[str] = mapped_column(String(32), nullable=False)
    language_requested: Mapped[str | None] = mapped_column(String(32))

    to_address: Mapped[str] = mapped_column(String(320), nullable=False)
    subject_rendered: Mapped[str] = mapped_column(String(255), nullable=False)

    # The rendered body, held ONLY while the message is still in flight, and
    # dropped the moment it reaches a terminal status. A database CHECK enforces
    # that — see the migration.
    #
    # This is the compromise between two things that both matter. A retry has to
    # send the same words as the first attempt, and the values that produced them
    # (a deadline, a rejection reason) are not recoverable from the template
    # alone. But a permanent log of message bodies is a second copy of everyone's
    # personal data with its own retention problem. So the body lives exactly as
    # long as the send does. What survives is the address, the subject and the
    # outcome — enough to support the claim that a notice was given.
    pending_body: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    provider: Mapped[str | None] = mapped_column(String(32))
    provider_message_id: Mapped[str | None] = mapped_column(String(255))

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    suppression_reason: Mapped[str | None] = mapped_column(String(255))

    # What it was about, so "when did you tell this person about that request?"
    # is answerable without storing the message itself.
    entity_type: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("data_principals.id", ondelete="SET NULL")
    )

    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
