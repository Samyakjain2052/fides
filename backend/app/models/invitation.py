"""Invitations — how somebody gets an account without anybody knowing their password.

The thing this replaces matters more than the thing it is. Creating a user by
typing a password for them means an administrator knows a colleague's credential,
which defeats non-repudiation: every audit entry attributed to that person is
arguable, and the audit chain is the product's central claim. So an invitation is
not a convenience feature. It is what makes the rest of the trail worth anything.

**The invitation token is a credential** and is treated exactly like a refresh
token, because it grants the ability to create an account with a chosen role
inside somebody else's workspace:

* Argon2-hashed at rest; the raw value is shown once and never stored.
* A deterministic `lookup_hash` alongside it, because Argon2 is salted and cannot
  be queried by.
* Short-lived — 72 hours.
* Single use: accepting it stamps `accepted_at`, and nothing else can be done
  with it.

**The tenant travels inside the token**, formatted `<tenant-hex>.<secret>`.
Acceptance happens before any tenant context exists and this table is under RLS,
so a lookup without the tenant matches zero rows and every valid invitation would
be rejected. This codebase has hit that exact bug three times — refresh tokens,
`ds_live_` API keys, `pk_live_` publishable keys — and the brief for this module
says do not make it four.
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

# Deliberately short. An invitation is a standing offer to create a privileged
# account; one that sits in a mailbox for a month is a credential nobody is
# watching.
INVITATION_TTL_HOURS = 72


class UserInvitation(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "user_invitations"
    __table_args__ = (
        # One live invitation per address. Enforced as a partial unique index in
        # the migration — accepted and revoked rows are kept as history, so a
        # plain UNIQUE would refuse a legitimate re-invitation after a revoke.
        Index(
            "uq_user_invitations_live_email",
            "tenant_id",
            "email",
            unique=True,
            postgresql_where=(
                "accepted_at IS NULL AND revoked_at IS NULL"
            ),
        ),
        Index("ix_user_invitations_lookup_hash", "lookup_hash"),
        Index("ix_user_invitations_tenant_created", "tenant_id", "created_at"),
        # An invitation cannot be both accepted and revoked. Whichever happened
        # first is the fact; allowing both would make the history unreadable.
        CheckConstraint(
            "accepted_at IS NULL OR revoked_at IS NULL", name="not_both_accepted_and_revoked"
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_reason IS NOT NULL",
            name="revoked_needs_reason",
        ),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)

    # Argon2id, like a refresh token. Never the raw value.
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # SHA-256 of the secret, for the lookup. Argon2 is salted, so the hash above
    # cannot be used in a WHERE clause.
    lookup_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set on acceptance so "who did this invitation become?" is answerable without
    # matching on an email that the person may later change.
    accepted_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(Text)

    @property
    def status(self) -> str:
        """Computed, never stored — expiry is a fact about the clock.

        A stored status would be stale the moment the expiry passed, and the
        window in which an expired invitation still reads as pending is exactly
        the window in which somebody would use it.
        """
        from datetime import UTC

        if self.accepted_at is not None:
            return "accepted"
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at <= datetime.now(UTC):
            return "expired"
        return "pending"

    @property
    def is_usable(self) -> bool:
        return self.status == "pending"
