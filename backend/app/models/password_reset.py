"""Password reset tokens.

Added because there was no server side to "Forgot password" at all. The screen
existed and `sendResetLink` in the frontend waited half a second and returned
`{sent: true}` without making a network call, so a person who forgot their
password was shown a confirmation and never received anything.

THE TENANT TRAVELS IN THE TOKEN, as `<tenant-hex>.<secret>`.

Not a stylistic choice. A reset is redeemed by somebody who is not signed in, so
no tenant context exists when the lookup runs — and this table is under FORCEd
RLS, so a query without `app.tenant_id` set matches zero rows and every valid
token would be rejected. That exact bug has shipped four times in this codebase
already: refresh tokens, `ds_live_` API keys, `pk_live_` publishable keys, and
user invitations. This is the fifth place it would have happened, and the
convention exists so it does not.

Two hashes, for the same reason API keys have two: Argon2 is salted, so a token
cannot be *found* by its Argon2 hash. A keyed SHA-256 gives a stable index and
Argon2 still does the verifying.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDMixin

#: How long a link works. Short on purpose — a reset link is a bearer credential
#: for somebody's account sitting in their inbox, and the longer it lives the
#: longer a compromised mailbox is a compromised account.
RESET_TTL_MINUTES = 60


class PasswordReset(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "password_resets"
    __table_args__ = (
        UniqueConstraint("lookup_hash", name="uq_password_resets_lookup_hash"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    #: Argon2 of the secret half. Verifies.
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Keyed SHA-256 of the secret half. Finds.
    lookup_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    #: Single use. Set the moment a password is changed with it.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Superseded when a newer link is requested, so an old email in an inbox
    #: stops working the moment somebody asks for another one.
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    #: Kept for the audit trail: a reset request is a security event, and where
    #: it came from is the useful part when somebody disputes it.
    requested_ip: Mapped[str | None] = mapped_column(String(64))
    requested_user_agent: Mapped[str | None] = mapped_column(Text)
