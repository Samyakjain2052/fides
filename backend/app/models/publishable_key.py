"""Publishable keys — credentials that ship inside a browser bundle.

Deliberately a different table, a different prefix and a different lookup path
from `api_keys`. Conflating them is how a secret key ends up in a bundle or a
publishable key ends up holding a withdraw scope.

**The security model does not rest on the key being secret.** It ships in
JavaScript; anyone can read it. Protection comes from three things instead:

1. **The key is incapable of harm.** `consent:collect` only, enforced as a
   ceiling when the key is created (`PUBLISHABLE_SCOPES`). It cannot withdraw,
   cannot read anyone's consent, cannot touch a DSAR.
2. **Provenance on every record.** Origin, hashed IP, user agent, notice version
   and a server-minted receipt id are stamped by the server on each collection.
   A forged record is still attributable, so honest records can be told apart
   from junk after the fact.
3. **Origin pinning**, which is defence-in-depth and explicitly *not* the
   boundary — see the dependency that checks it.

Storage note: the key is stored in plaintext, unlike an API key's Argon2 hash.
Hashing a value that is published in a bundle protects nothing, and it would
stop the console from ever showing the key again — which customers need, because
they have to paste it into their site. A `lookup_hash` still exists for a
constant-shape indexed lookup consistent with `api_keys`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDMixin

# How consent arrived. Stamped by the server, never supplied by the client.
COLLECTION_METHODS = ("publishable_key", "signed_token", "session", "api", "import")


class PublishableKey(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "publishable_keys"
    __table_args__ = (
        UniqueConstraint("lookup_hash", name="uq_publishable_keys_lookup_hash"),
        Index("ix_publishable_keys_tenant_id_revoked_at", "tenant_id", "revoked_at"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # `pk_live_…` / `pk_test_…`. Visibly different from `ds_live_…` so that seeing
    # one in a bundle, a log or a public repo is immediately readable as "this is
    # supposed to be here" rather than an incident.
    prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(8), nullable=False, default="live")

    # Plaintext, on purpose — see the module docstring.
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    lookup_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Capped at creation to PUBLISHABLE_SCOPES. Stored rather than assumed so the
    # console can display exactly what a key can do, and so widening the ceiling
    # later does not silently widen every key already issued.
    capabilities: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)

    # Explicit allowlist. Empty means no browser origin is accepted, which is a
    # safer default than "any": a key created without thinking about origins
    # cannot be used from a page at all until someone decides where it runs.
    allowed_origins: Mapped[list[str]] = mapped_column(
        ARRAY(String(255)), nullable=False, default=list
    )

    # Low by default. This is an unauthenticated public write path, so the ceiling
    # should be sized for "a real site's signup traffic", not for an API client.
    rate_limit_per_minute: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60
    )
    rate_limit_per_ip_per_minute: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10
    )

    require_signed_token: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="When true, collect requires a signed token — for sensitive "
                "purposes where an asserted principal_ref is not good enough.",
    )

    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    @property
    def is_usable(self) -> bool:
        return self.revoked_at is None


class ConsentProvenance(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """Where one act of consent came from, as observed by the server.

    A separate row per collection event rather than columns on `consents`,
    because `consents` holds the *current* answer (one row per principal+purpose)
    and re-granting overwrites it. Provenance is a history, and losing the
    circumstances of an earlier grant would lose exactly the evidence this table
    exists to keep.

    Every field here is derived server-side. A client that could set its own
    provenance would be supplying its own alibi.
    """

    __tablename__ = "consent_provenance"
    __table_args__ = (
        UniqueConstraint("server_receipt_id", name="uq_consent_provenance_receipt"),
        Index("ix_consent_provenance_consent_id", "consent_id"),
        Index("ix_consent_provenance_tenant_received", "tenant_id", "received_at"),
    )

    consent_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("consents.id", ondelete="CASCADE"), nullable=False
    )

    # Server-minted, returned to the caller. A customer disputing a record can
    # quote this and we can find the exact request.
    server_receipt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    collection_method: Mapped[str] = mapped_column(String(32), nullable=False)

    # True only when a signed token bound the principal. False means the
    # principal_ref was *asserted* by the page and is trusted on provenance
    # alone — a real distinction a customer's auditor is entitled to see.
    strongly_bound: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    origin: Mapped[str | None] = mapped_column(String(255))
    user_agent: Mapped[str | None] = mapped_column(String(1000))

    # Hashed, not raw. An IP is personal data under DPDP, and a consent-collection
    # log is the wrong place to accumulate a second identifier for everyone who
    # ever saw a banner. Keyed HMAC rather than plain SHA-256 so the value cannot
    # be reversed by hashing the whole IPv4 space.
    ip_hash: Mapped[str | None] = mapped_column(String(64))

    # The wording in force at the moment of collection. Denormalised from the
    # notice on purpose: this row must remain readable as evidence even if the
    # notice chain is later archived elsewhere.
    notice_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("notices.id", ondelete="RESTRICT")
    )
    notice_version: Mapped[int | None] = mapped_column(Integer)

    publishable_key_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("publishable_keys.id", ondelete="SET NULL")
    )
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="SET NULL")
    )
