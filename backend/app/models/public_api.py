"""Phase 4 — what the public API needs beyond the consent core.

Two tables, both of which exist because customers integrate over an unreliable
network and we cannot make their retries our correctness problem.

* **idempotency_keys** — a POST that times out client-side may well have
  succeeded server-side. Without this, the customer's retry records a second
  consent, and the audit trail now shows a person consenting twice at almost the
  same instant. Storing the first response and replaying it makes the retry safe.

* **api_request_log** — per-key request accounting. It answers "did you call us?"
  during an integration argument, and it is the rate limiter's counter, so the
  limit survives a restart and holds across replicas instead of living in one
  process's memory.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class IdempotencyKey(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """One customer-supplied key, and the response we gave it."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        # Scoped per API key, not per tenant: two services at one customer may
        # legitimately generate the same UUID, and one blocking the other would
        # be a confusing outage with no cause visible from either side.
        UniqueConstraint(
            "tenant_id", "api_key_id", "publishable_key_id", "key",
            name="uq_idempotency_keys_tenant_key",
        ),
        Index("ix_idempotency_keys_expires_at", "expires_at"),
    )

    # Exactly one of these is set. A publishable key and a secret key are
    # different callers with different key spaces; sharing one column would let
    # one credential's key collide with another's.
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="CASCADE")
    )
    publishable_key_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("publishable_keys.id", ondelete="CASCADE")
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)

    # The request fingerprint. A retry that reuses a key with a *different* body
    # is a client bug, and replaying the old response would hide it — so we
    # compare and reject instead.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # Kept for a bounded window. Forever would grow without limit; too short and
    # a customer's overnight retry queue stops being protected.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApiRequestLog(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """One row per public-API request. Also the rate limiter's counter."""

    __tablename__ = "api_request_log"
    __table_args__ = (
        # The rate-limit query is "count for this key since T", so the index has
        # to lead with the key and then time.
        Index("ix_api_request_log_key_created", "api_key_id", "created_at"),
        Index("ix_api_request_log_tenant_created", "tenant_id", "created_at"),
    )

    api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="CASCADE")
    )
    publishable_key_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("publishable_keys.id", ondelete="CASCADE")
    )

    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(255), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)

    ip_address: Mapped[str | None] = mapped_column(String(64))
    # Hashed as well as raw: the per-IP rate limiter counts on the hash, which is
    # also what provenance stores, so the two agree on what "same client" means.
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(Text)

    # Which principal and purpose the call was about, where that applies. Enough
    # to answer "show me every time you checked consent for this person" without
    # storing the bodies — which would put personal data in a log table.
    principal_ref: Mapped[str | None] = mapped_column(String(255))
    purpose_key: Mapped[str | None] = mapped_column(String(64))
