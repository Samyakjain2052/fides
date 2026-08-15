"""The consent domain — purposes, versioned notices, principals, consents.

This is the product. Everything else in the backend exists so that these four
tables can be trusted.

Three rules are enforced here and in the migration rather than left to callers:

* **A consent points at a notice version, never at a purpose alone** (N4).
  "She consented to marketing" is not evidence. "She consented to v3 of the
  marketing notice, in Hindi, by checkbox, on this date" is. `notice_id` is
  therefore NOT NULL, and the FK is RESTRICT — a notice that someone agreed to
  can never be deleted out from under the record of them agreeing.

* **A published notice is immutable.** Editing the words people agreed to is
  the most damaging thing this system could allow, so it is blocked by a
  database trigger, not by a service method someone can forget to call. A
  change produces version N+1; existing consents keep pointing at the version
  actually shown to them.

* **Nothing is granted implicitly.** There is no default that produces an
  `active` consent. DPDP consent must be a free, specific, informed,
  unconditional and unambiguous *act*.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDMixin

# --------------------------------------------------------------------------- #
# Vocabularies. Kept as plain tuples rather than SQL enums: adding a value to a
# Postgres enum needs a migration and a lock, and these will grow.
# --------------------------------------------------------------------------- #

CONSENT_STATUSES = ("active", "withdrawn", "expired")

# How the consent was captured. Recorded because DPDP requires consent to be
# demonstrable, and "how" is part of demonstrating it.
CONSENT_METHODS = ("checkbox", "banner", "api", "import", "guardian", "verbal_logged")

# Article 6-equivalent grounds under the DPDP Act. `consent` is the default;
# `legitimate_use` covers the Section 7 exemptions.
LEGAL_BASES = ("consent", "legitimate_use", "legal_obligation", "vital_interest")


class Purpose(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """*Why* a fiduciary processes something. Stable across notice versions.

    The purpose is the durable concept ("Marketing communications"); the notice
    is the wording shown to a person at a point in time. Separating them is what
    lets the wording be re-issued without invalidating the concept, and what
    makes "consent to marketing, v2" expressible at all.
    """

    __tablename__ = "purposes"
    __table_args__ = (
        # Stable machine key. This is what a customer's systems pass to
        # /consent/check, so it must be unique per tenant and must not change.
        UniqueConstraint("tenant_id", "key", name="uq_purposes_tenant_id_key"),
        Index("ix_purposes_tenant_id_is_active", "tenant_id", "is_active"),
    )

    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False)

    # Mandatory purposes are shown with a locked toggle and the reason, never
    # hidden. Hiding a mandatory purpose to avoid the conversation is the
    # dark-pattern version of this feature.
    is_mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    legal_basis: Mapped[str] = mapped_column(
        String(32), nullable=False, default="consent"
    )

    # Drives the retention module (Phase 7) and is shown to the principal, so
    # they can see how long a "yes" lasts before they give it.
    retention_days: Mapped[int | None] = mapped_column(Integer)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Notice(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """The exact words shown to a person, in one language, at one version.

    Immutable once published — see the module docstring and the migration's
    trigger. A draft (published_at IS NULL) may still be edited freely; that is
    the whole point of having a draft state.
    """

    __tablename__ = "notices"
    __table_args__ = (
        # N4, as a constraint rather than a convention.
        UniqueConstraint(
            "tenant_id",
            "purpose_id",
            "version",
            "language",
            name="uq_notices_tenant_id_purpose_id_version_language",
        ),
        Index("ix_notices_tenant_id_purpose_id_language", "tenant_id", "purpose_id", "language"),
    )

    purpose_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        # RESTRICT, not CASCADE: deleting a purpose must not silently delete the
        # notices that people consented to.
        ForeignKey("purposes.id", ondelete="RESTRICT"),
        nullable=False,
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # One of the Eighth Schedule languages. Stored per notice because DPDP
    # §5(3) gives the principal the right to the notice in their language, and
    # which language they actually read is part of the evidence.
    language: Mapped[str] = mapped_column(String(32), nullable=False, default="English")

    content: Mapped[str] = mapped_column(Text, nullable=False)
    data_collected: Mapped[str] = mapped_column(Text, nullable=False)
    user_rights: Mapped[str] = mapped_column(Text, nullable=False)
    withdrawal_policy: Mapped[str] = mapped_column(Text, nullable=False)

    # NULL = draft, editable. Non-NULL = published, frozen.
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    published_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class DataPrincipal(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A person whose data the fiduciary holds.

    Deliberately not a `users` row. A Data Principal is a subject of processing,
    not an operator of this console; conflating them would put customers' end
    users in our authentication tables.
    """

    __tablename__ = "data_principals"
    __table_args__ = (
        # The customer's own identifier for this person, so their systems can
        # ask about someone without us inventing an id exchange.
        UniqueConstraint(
            "tenant_id", "external_id", name="uq_data_principals_tenant_id_external_id"
        ),
        Index("ix_data_principals_tenant_id_email", "tenant_id", "email"),
    )

    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(32))

    # DPDP §9: a child's data needs verifiable parental consent. This flag is
    # what routes the UI to the guardian flow, so it is part of the data model
    # rather than a UI-only concern.
    is_minor: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    guardian_email: Mapped[str | None] = mapped_column(String(320))

    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # A legal hold outranks every retention policy. A policy-level exemption
    # covers a category; this covers an individual under litigation or
    # investigation, and nothing may sweep them up while it is set.
    legal_hold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    legal_hold_reason: Mapped[str | None] = mapped_column(String(255))

    # Set when retention has masked this person's identifiers. Queryable state
    # rather than something inferred from the absence of an email.
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Consent(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """One person's answer, for one purpose, against one notice version.

    There is no history table. Consent history is a query over `audit_events`:
    one source of truth, because a history that can disagree with the audit
    trail is worse than no history at all.
    """

    __tablename__ = "consents"
    __table_args__ = (
        # One live row per (principal, purpose). Re-granting updates this row and
        # writes a new audit entry, so the current answer is a single lookup
        # rather than a max-by-timestamp over a log — which is what makes
        # /consent/check fast enough to sit in a customer's request path.
        UniqueConstraint(
            "tenant_id",
            "principal_id",
            "purpose_id",
            name="uq_consents_tenant_id_principal_id_purpose_id",
        ),
        Index("ix_consents_tenant_id_purpose_id_status", "tenant_id", "purpose_id", "status"),
        Index("ix_consents_tenant_id_expires_at", "tenant_id", "expires_at"),
    )

    principal_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("data_principals.id", ondelete="CASCADE"),
        nullable=False,
    )
    purpose_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("purposes.id", ondelete="RESTRICT"), nullable=False
    )

    # N4. NOT NULL, and RESTRICT: the evidence is the pairing of a person with
    # the exact text they were shown.
    notice_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("notices.id", ondelete="RESTRICT"), nullable=False
    )

    status: Mapped[str] = mapped_column(String(16), nullable=False)

    given_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # The language the notice was actually read in, and how the answer was
    # captured. Both are evidence, not metadata.
    language: Mapped[str] = mapped_column(String(32), nullable=False, default="English")
    method: Mapped[str] = mapped_column(String(32), nullable=False)

    # Where it came from: "preference-centre", "signup-banner", a service name
    # for API-collected consent. Answers "which of our systems asked?".
    source: Mapped[str | None] = mapped_column(String(128))
