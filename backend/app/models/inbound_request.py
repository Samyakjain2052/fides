"""Rights requests that arrived as ordinary email.

Most rights requests in the real world are an email to whatever address a
person can find — support@, info@, the last human who replied to them. Until
this existed those were invisible to the product: the clock a regulator cares
about had started, and nothing here knew.

WHY THIS DOES NOT CREATE A REQUEST AUTOMATICALLY

The obvious design is to parse the email, guess which right it is asking for,
and open a request. That guess is the problem. "Please stop emailing me and
remove my details" could be a withdrawal of marketing consent or an erasure
request under §12(3), and those have wildly different consequences — one
unsubscribes somebody, the other destroys their record. A classifier that is
right 95% of the time deletes somebody's data wrongly one time in twenty.

So an inbound email is RECORDED and ANSWERED, not interpreted. The reply points
the person at the hosted form, where they choose the right themselves in words
the statute uses. A human in the queue can also link an inbound email to a
request once one exists.

WHY THE ARRIVAL DATE IS KEPT ANYWAY

Because it is the date that matters. If somebody emails on the 1st and
completes the form on the 20th, the fiduciary has arguably been on notice since
the 1st — and a product that silently restarted the clock at the form
submission would be helping a customer understate how late they are.
`received_at` is preserved and shown against any request it is linked to, so
the honest date is the visible one.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDMixin

#: Where an inbound email has got to.
#:
#:   received   recorded, nothing sent back yet
#:   replied    the person was pointed at the form
#:   linked     a request exists and this email is attached to it
#:   dismissed  not a rights request — a supplier invoice, a newsletter — with
#:              a reason, because "we decided this was not a rights request" is
#:              a decision somebody may have to defend
INBOUND_STATUSES = ("received", "replied", "linked", "dismissed")


class InboundRightsEmail(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "inbound_rights_emails"
    __table_args__ = (
        Index("ix_inbound_rights_tenant_status", "tenant_id", "status"),
        Index("ix_inbound_rights_from", "tenant_id", "from_address"),
        # Deduplicates a provider retrying the same delivery. Providers retry
        # on any non-2xx, and without this a transient timeout on our side
        # produces three copies of one person's email in the queue.
        Index(
            "ix_inbound_rights_message_id",
            "tenant_id", "provider_message_id",
            unique=True,
            postgresql_where="provider_message_id IS NOT NULL",
        ),
        CheckConstraint(
            "status IN ('received','replied','linked','dismissed')",
            name="status",
        ),
        CheckConstraint(
            "status <> 'dismissed' OR dismissal_reason IS NOT NULL",
            name="dismissed_has_reason",
        ),
    )

    #: Who wrote it. The single most important field: everything else is
    #: context, and this is what a request eventually gets matched to.
    from_address: Mapped[str] = mapped_column(String(320), nullable=False)
    from_name: Mapped[str | None] = mapped_column(String(200))

    #: Which of the customer's addresses it arrived at. Worth keeping because
    #: "these all came to support@, nobody was watching it" is a finding.
    to_address: Mapped[str | None] = mapped_column(String(320))

    subject: Mapped[str | None] = mapped_column(String(500))

    #: The body as received. Hostile input from a member of the public, stored
    #: raw and escaped at every rendering path — the same rule
    #: `Grievance.description` already follows, and for the same reason: it will
    #: routinely contain personal data about third parties.
    body: Mapped[str | None] = mapped_column(Text)

    #: When the EMAIL arrived, not when we recorded it. See the module
    #: docstring: this is the date a regulator would count from.
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    #: The provider's own id for the message, for deduplicating retries.
    provider_message_id: Mapped[str | None] = mapped_column(String(255))

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="received"
    )
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Set once somebody connects this email to an actual request.
    linked_request_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("dsar_requests.id", ondelete="SET NULL")
    )

    dismissal_reason: Mapped[str | None] = mapped_column(Text)
    dismissed_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    #: What a keyword scan noticed, recorded as a HINT and never acted on.
    #:
    #: Stored so a DPO can sort a busy inbox by "probably an erasure", and
    #: explicitly not used to create anything: "please stop emailing me and
    #: remove my details" is either a consent withdrawal or an erasure request,
    #: and a classifier that is right 95% of the time destroys somebody's record
    #: wrongly one time in twenty.
    suspected_type: Mapped[str | None] = mapped_column(String(16))

    @property
    def is_open(self) -> bool:
        return self.status in ("received", "replied")
