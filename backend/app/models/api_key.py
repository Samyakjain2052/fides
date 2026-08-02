"""API keys — how a customer's own systems authenticate to us."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ARRAY, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class ApiKey(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "api_keys"

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # `ds_live` / `ds_test`. Stored so the console can identify a key without
    # holding it, and so a leaked key is greppable in logs and public repos.
    prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(8), nullable=False, default="live")

    # Argon2id over the full key. The plaintext is shown exactly once, at
    # creation, and never persisted — we cannot reveal a key again, only rotate.
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    lookup_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    # Least privilege: a key in a marketing service gets consent:read and nothing
    # that could erase a person.
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)

    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
