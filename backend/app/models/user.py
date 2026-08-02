"""Console and portal users, scoped to a tenant."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
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

    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mfa_secret: Mapped[str | None] = mapped_column(String(255))

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
