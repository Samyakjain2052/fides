"""The message thread on a rights request.

New, because there was no way to talk to the person who raised a request. The
product could email them a notification and nothing else — so an administrator
who needed to ask "which of these two accounts is yours?" had to leave the
system, and the answer came back to somebody's inbox where it is neither
audited nor attached to the request.

That gap has a compliance shape as well as a usability one. §11 and §12
responses are frequently a conversation: a request that names no account, an
erasure where some data must be retained and the reason has to be explained, a
correction where the corrected value has to be confirmed. Doing that over
ordinary email means the evidence of how a statutory request was handled lives
outside the audit trail.

WHY MESSAGES AND EVENTS ARE DIFFERENT TABLES

`dsar_events` is the timeline — what happened, largely written by the system,
never addressed to anybody. This is correspondence, with a direction, an author
who might not be a user of this product at all, and attachments. Folding
correspondence into the timeline would mean either a `direction` column that is
NULL for most rows or an internal note that reads as though it were sent to
somebody. Both have bitten products before; the second is worse, because
"we told them" is exactly the fact somebody will dispute.

ONE INVARIANT, ENFORCED IN THE DATABASE

A message has exactly one author: a staff user or a data principal, never both
and never neither. An unattributed message in a statutory correspondence record
is not evidence of anything.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
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

#: Who a message travelled towards. Stored rather than derived from which
#: author column is set, because a staff member can post on a principal's
#: behalf when a request arrives by post or over the phone — and the direction
#: of the correspondence is then still "from the principal".
MESSAGE_DIRECTIONS = ("to_principal", "from_principal")


class DsarMessage(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "dsar_messages"
    __table_args__ = (
        Index("ix_dsar_messages_request_at", "dsar_request_id", "created_at"),
        CheckConstraint(
            "direction IN ('to_principal','from_principal')", name="direction"
        ),
        # Exactly one author. See the module docstring.
        CheckConstraint(
            "(author_user_id IS NOT NULL) <> (author_principal_id IS NOT NULL)",
            name="one_author",
        ),
        CheckConstraint("length(btrim(body)) > 0", name="body_not_blank"),
    )

    dsar_request_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("dsar_requests.id", ondelete="CASCADE"),
        nullable=False,
    )

    direction: Mapped[str] = mapped_column(String(16), nullable=False)

    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    author_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("data_principals.id", ondelete="SET NULL")
    )

    #: Who it was from, captured at the time. The FK above goes SET NULL when a
    #: staff member leaves, and a correspondence record that loses the name of
    #: the person who wrote it is not much of a record.
    author_label: Mapped[str] = mapped_column(String(255), nullable=False)

    #: Raw text as written, escaped at every rendering path. Hostile input in
    #: both directions: a data principal's message is from a member of the
    #: public, and staff text ends up in an email.
    body: Mapped[str] = mapped_column(Text, nullable=False)

    #: True when the system wrote it — a package-delivery note, an
    #: acknowledgement. Distinguishing these from a person's own words matters
    #: for the same reason `DsarEvent.automated` does: silence should not read
    #: as judgement, and a template should not read as a human reply.
    automated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: When the other side first opened it. Nullable forever if they never do.
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Whether an email went out about it, so a resend is distinguishable from a
    #: first send and a delivery failure is visible rather than assumed.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
