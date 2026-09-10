"""§14 — the right to nominate.

"A Data Principal shall have the right to nominate any other individual, who
shall, in the event of death or incapacity of the Data Principal, exercise the
rights of the Data Principal."

This right has no equivalent in the GDPR or in any US state statute, which is
why no comparable product implements it. It is also the one right that has to
be exercised *before* it is needed: a nomination made after somebody has died
is not a nomination.

WHAT IT IS NOT

Not a rights request. A request is a one-off act with a statutory deadline; a
nomination is a standing arrangement that sits dormant, possibly for decades,
and then confers authority on somebody else. It therefore has no deadline, no
engine action, and a completely different lifecycle — which is why it is its
own table rather than a sixth `DSAR_TYPES` value.

THE HARD PART IS ACTIVATION, AND THE HONEST ANSWER IS THAT A HUMAN DOES IT

A nomination becomes live on the death or incapacity of the person who made it,
and no software can establish either fact. What this models is therefore:

  * the nomination itself, recorded and acknowledged while the principal is
    able to make it
  * an explicit ACTIVATION, performed by a named member of staff, who records
    what evidence they saw
  * every subsequent request made under it, attributed to the nominee and
    linked back to the nomination

Automatically believing a claim of death would be the single most dangerous
feature this product could ship: it hands a stranger the right to extract and
erase somebody's entire record. So activation is a deliberate human act with a
name and an evidence note against it, and the audit chain keeps both.

REVOCATION IS UNILATERAL AND IMMEDIATE

The principal can withdraw a nomination at any time while they are able to, and
does not have to say why — §14 gives the right to nominate, not a right for the
nominee to remain nominated.
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

#: Where a nomination stands.
#:
#:   pending    recorded; the nominee has not confirmed they accept
#:   active     in force, dormant, waiting for an event nobody wants
#:   invoked    the principal has died or become incapacitated, a human has
#:              recorded the evidence, and the nominee may now act
#:   revoked    withdrawn by the principal
#:   lapsed     the nominee themselves is gone, or declined
NOMINATION_STATUSES = ("pending", "active", "invoked", "revoked", "lapsed")

#: What the nominee may do once invoked.
#:
#: Offered as a choice because the two are genuinely different in consequence:
#: a family member who needs to obtain records to settle an estate does not
#: necessarily need the power to destroy them, and a principal who thinks about
#: it for a moment will often want to grant one and not the other.
NOMINATION_SCOPES = ("access_only", "access_and_erasure", "all_rights")


class Nomination(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "nominations"
    __table_args__ = (
        Index("ix_nominations_principal", "tenant_id", "principal_id"),
        Index("ix_nominations_status", "tenant_id", "status"),
        CheckConstraint(
            "status IN ('pending','active','invoked','revoked','lapsed')",
            name="status",
        ),
        CheckConstraint(
            "scope IN ('access_only','access_and_erasure','all_rights')",
            name="scope",
        ),
        # Invoking a nomination hands somebody else the right to extract or
        # destroy a person's entire record. It requires a named member of staff
        # and a note saying what they saw — the single most consequential
        # constraint in this table.
        CheckConstraint(
            "status <> 'invoked' OR ("
            "invoked_at IS NOT NULL AND invoked_by IS NOT NULL "
            "AND evidence_note IS NOT NULL)",
            name="invoked_has_evidence",
        ),
        CheckConstraint(
            "status <> 'revoked' OR revoked_at IS NOT NULL",
            name="revoked_has_timestamp",
        ),
        # A nominee has to be reachable, or the nomination cannot be acted on
        # when it matters. An email address is the minimum.
        CheckConstraint(
            "length(btrim(nominee_email)) > 0", name="nominee_reachable"
        ),
    )

    #: Whose rights are being nominated away. RESTRICT rather than CASCADE:
    #: purging a principal must not silently drop the record that somebody was
    #: authorised to act for them.
    principal_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("data_principals.id", ondelete="RESTRICT"),
        nullable=False,
    )

    nominee_name: Mapped[str] = mapped_column(String(200), nullable=False)
    nominee_email: Mapped[str] = mapped_column(String(320), nullable=False)
    nominee_phone: Mapped[str | None] = mapped_column(String(32))
    #: "daughter", "solicitor", "executor". Free text, because the set of
    #: relationships people actually nominate is not enumerable and a dropdown
    #: would force somebody to pick the nearest wrong one.
    nominee_relationship: Mapped[str | None] = mapped_column(String(120))

    scope: Mapped[str] = mapped_column(
        String(24), nullable=False, default="access_only"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )

    #: Anything the principal wants recorded about their intent — "only the
    #: medical records", "after probate". Not machine-enforced, and the field
    #: says so: a condition a human wrote is for a human to read.
    instructions: Mapped[str | None] = mapped_column(Text)

    #: The nominee confirming they know and accept. Not required to make the
    #: nomination valid — §14 gives the principal the right, and it does not
    #: depend on the nominee agreeing in advance — but worth having, because a
    #: nominee who has never heard of the arrangement is unlikely to act on it.
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Hash of the acceptance token, never the token. Same two-hash discipline
    #: as invitations and password resets.
    acceptance_token_hash: Mapped[str | None] = mapped_column(String(64))

    # --- activation --------------------------------------------------------
    invoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invoked_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: What the staff member actually saw. A death certificate, a court order
    #: appointing a guardian, a medical certificate. Required to invoke, and
    #: kept verbatim: this is the evidence for the most dangerous state change
    #: in the product.
    evidence_note: Mapped[str | None] = mapped_column(Text)

    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Deliberately nullable and never required. §14 gives a right to nominate,
    #: not a right for the nominee to be told why they were unnominated.
    revocation_note: Mapped[str | None] = mapped_column(Text)

    @property
    def is_live(self) -> bool:
        """In force and waiting. Not the same as usable — see `is_exercisable`."""
        return self.status in ("pending", "active")

    @property
    def is_exercisable(self) -> bool:
        """Whether the nominee may act right now."""
        return self.status == "invoked"

    def permits(self, request_type: str) -> bool:
        """Whether this nomination's scope covers a given kind of request.

        Defaults closed: an unknown request type is not permitted, so adding a
        new right without extending this refuses rather than quietly granting.
        """
        if self.scope == "all_rights":
            return request_type in (
                "access", "correction", "completion", "updating", "erasure",
            )
        if self.scope == "access_and_erasure":
            return request_type in ("access", "erasure")
        if self.scope == "access_only":
            return request_type == "access"
        return False
