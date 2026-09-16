"""Console and portal users, scoped to a tenant."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class User(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        # Email is unique WITHIN a tenant, not globally: the same person may
        # legitimately be a user of two different customers of ours.
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_id_email"),
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    # Argon2id. Nullable because an SSO-only user has no local password.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # MFA (BRD §4.6.1 for admin accounts, §4.7 for audit-log access).
    #
    # `mfa_enabled` flips only after a code has been verified, never at
    # enrolment. A user who scans the QR, never finishes, and is marked enabled
    # is locked out of their own account by a secret they do not have.
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    #: SEALED, not plaintext and not hashed. A TOTP secret is the one credential
    #: here that must be recoverable — verifying a code means recomputing the
    #: HMAC from it — so it takes the same AES-GCM treatment as a connector
    #: credential. Widened from String(255) when it stopped being a bare base32
    #: string: the ciphertext is longer than the plaintext.
    mfa_secret: Mapped[str | None] = mapped_column(Text())

    #: The highest TOTP counter already accepted. A code is valid for up to
    #: ninety seconds across the skew window, which is ample time for somebody
    #: who read it over a shoulder to reuse it — so a counter at or below this
    #: is refused even when the digest is correct.
    mfa_last_counter: Mapped[int | None] = mapped_column(Integer)

    #: Argon2 hashes of single-use recovery codes. Hashed, because unlike the
    #: TOTP secret these only ever need verifying — and a readable list of them
    #: in the database is a readable list of ways past MFA.
    mfa_recovery_hashes: Mapped[list[str] | None] = mapped_column(JSONB)

    mfa_enrolled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Reserved now so adding OIDC later is not a migration on a hot table.
    external_idp: Mapped[str | None] = mapped_column(String(64))
    external_idp_subject: Mapped[str | None] = mapped_column(String(255))

    # Brute-force protection state.
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RefreshToken(UUIDMixin, TimestampMixin, Base):
    """One row per issued refresh token.

    Stored hashed, single-use, and grouped into a `family_id`. Presenting a token
    that has already been used means it leaked, so the whole family is revoked —
    which logs the real user out too, deliberately: a forced re-login is the
    correct response to a stolen token.
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)

    family_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Deterministic index for lookup; the Argon2 hash above still does the
    # verifying. Argon2 is salted, so you cannot query by it.
    lookup_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(64))

    # Context for the session list a user can review, and for incident forensics.
    user_agent: Mapped[str | None] = mapped_column(String(512))
    ip_address: Mapped[str | None] = mapped_column(String(64))
