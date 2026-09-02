"""A customer's connection to one of their own systems.

The row holds an encrypted credential and, separately, the answer to "did it
actually work". Those are deliberately different columns: a stored credential
proves nothing, and conflating "we have a password" with "we can connect" is how
a compliance product ends up promising a DSAR reach it does not have.

WHAT IS AND IS NOT IN HERE

`config_sealed` is one AES-256-GCM blob covering the whole credential dict — see
core/crypto.py for why it is encrypted rather than hashed like every other secret
in this schema. `config_public` holds the non-secret fields in clear (host, port,
region) so the list screen can be rendered, and `hints` holds a last-4 for each
secret field so an admin can recognise which key they pasted.

Nothing here is ever returned in plaintext by any endpoint, including to the
admin who typed it. If they lose the credential, they replace it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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

#: Lifecycle of a connection, which is about *verification*, not about storage.
#:
#:   unverified  credentials stored, never successfully tested
#:   connected   a probe succeeded; the timestamp says when
#:   failing     a probe has failed since the last success
#:   disabled    switched off by an admin; kept for the audit trail
CONNECTION_STATUSES = ("unverified", "connected", "failing", "disabled")


class Connection(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "connections"
    __table_args__ = (
        # Named bare: NAMING_CONVENTION prepends `ck_connections_`, and passing
        # the prefix here is what produced the double-prefixed, truncated names
        # on some older tables.
        CheckConstraint(
            "status IN ('unverified','connected','failing','disabled')",
            name="status",
        ),
        # One connection per connector per label, so a customer can hold two
        # PostgreSQL connections ("billing", "crm-replica") without ambiguity,
        # and cannot silently create a duplicate of one.
        UniqueConstraint("tenant_id", "connector_id", "label",
                         name="uq_connections_tenant_connector_label"),
    )

    #: Registry id — `postgresql`, `razorpay`. Not a foreign key: the catalogue
    #: lives in code (connectors/registry.py) because it is a deployment fact,
    #: not tenant data, and a table would let the two disagree.
    connector_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    #: An admin's own name for it. Distinguishes two connections to the same kind
    #: of system, which is the normal case rather than the exception.
    label: Mapped[str] = mapped_column(String(120), nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default="unverified")

    #: The encrypted credential. Opaque, and prefixed with its scheme so a future
    #: key rotation can migrate old rows rather than orphan them.
    config_sealed: Mapped[str] = mapped_column(Text, nullable=False)

    #: Non-secret fields, in clear, for rendering the list.
    config_public: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )

    #: Per-secret-field last-4, e.g. {"password": "••••f3a1"}. Enough to
    #: recognise, not enough to use.
    hints: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False,
                                                  default=dict)

    # --- verification -----------------------------------------------------
    last_tested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    #: The probe's message, kept verbatim so an admin sees what the vendor said
    #: rather than a paraphrase. Probes scrub credentials out of driver errors
    #: before they reach here.
    last_test_message: Mapped[str | None] = mapped_column(Text)

    #: A streak, not a boolean. One failed check is a blip — a failover, a
    #: restart, a DNS hiccup. Three in a row is a broken integration, and only
    #: the second is worth waking somebody for.
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    #: When it last actually worked. Separate from `last_tested_at` so
    #: "failing since Tuesday" is answerable — a single last-tested timestamp
    #: cannot express it.
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Whether the background job checks this one. Lets an admin silence a system
    #: they know is down for maintenance without deleting the credential.
    monitor: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: Set when a failure notice is sent, so crossing the threshold notifies once
    #: instead of every fifteen minutes until somebody fixes it.
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
