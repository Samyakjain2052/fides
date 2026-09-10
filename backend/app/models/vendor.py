"""Processors and third parties, and what we know about each.

§8(2) is the reason this exists: a Data Fiduciary stays responsible for
processing carried out by its processors. That makes every vendor holding
personal data an extension of the company's own compliance surface, and a
product that tracks only the systems it happens to hold credentials for is
tracking the smaller half.

WHAT THIS IS NOT

It is not a vendor risk *score*. Competing products publish a number per vendor
— 0-100, trended over time, derived from litigation feeds, breach databases and
diffed privacy policies. That number is the output of a research operation with
people employed to maintain it, not a feature, and generating one from what a
customer types into a form would be inventing an authority we do not have.

So this records what the customer knows and can evidence, and derives only what
follows arithmetically from it: whether a DPA exists, whether it has expired,
whether the review is overdue, whether the vendor can actually action a rights
request inside the statutory window. Those are facts with owners, and each of
them is individually actionable — which a composite score is not.

THE FIELD THAT MATTERS MOST

`dsar_contact` and `dsar_sla_days`. The statutory clock runs on the fiduciary,
not on the processor: a vendor who takes 45 days to action an erasure makes
their customer late, and a vendor with no route to ask at all makes the
obligation undischargeable. Those two columns are what turn "we use Mailchimp"
into "here is how we get somebody deleted from Mailchimp, and how long it
takes".
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
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

#: Where a vendor stands in the approval process.
#:
#:   prospective  under review, not yet sending data
#:   approved     cleared, in use
#:   conditional  in use with outstanding conditions — the honest state most
#:                real vendor relationships are actually in, and the one a
#:                two-value approved/rejected model forces people to lie about
#:   rejected     assessed and refused
#:   retired      no longer used; kept because a past relationship is part of
#:                the record when an old breach surfaces
VENDOR_STATUSES = (
    "prospective", "approved", "conditional", "rejected", "retired",
)

#: How much a failure here would matter. Set by a human, not computed.
#:
#: Deliberately not derived from the answers to a questionnaire: the impact of a
#: vendor failing depends on what the company uses them for, which the company
#: knows and an algorithm does not.
RISK_TIERS = ("low", "medium", "high", "critical")

#: How the vendor relates to the data.
#:
#:   processor      acts on our instructions (§2(k))
#:   sub_processor  a processor's own processor, recorded because §8(2) does not
#:                  stop at the first hop
#:   joint          determines purposes alongside us
#:   recipient      receives data as an independent fiduciary
VENDOR_ROLES = ("processor", "sub_processor", "joint", "recipient")


class Vendor(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "vendors"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_vendors_tenant_name"),
        Index("ix_vendors_tenant_status", "tenant_id", "status"),
        Index("ix_vendors_tenant_tier", "tenant_id", "risk_tier"),
        Index("ix_vendors_review", "tenant_id", "next_review_at"),
        CheckConstraint(
            "status IN ('prospective','approved','conditional','rejected',"
            "'retired')",
            name="status",
        ),
        CheckConstraint(
            "risk_tier IN ('low','medium','high','critical')", name="risk_tier"
        ),
        CheckConstraint(
            "role IN ('processor','sub_processor','joint','recipient')",
            name="role",
        ),
        # A refusal needs a reason, the same rule grievances and identity
        # decisions already follow. "We rejected this vendor" with nothing
        # behind it cannot be defended and cannot be revisited.
        CheckConstraint(
            "status <> 'rejected' OR decision_note IS NOT NULL",
            name="rejected_has_reason",
        ),
        # So does using one with outstanding conditions — that IS the record of
        # what is outstanding.
        CheckConstraint(
            "status <> 'conditional' OR decision_note IS NOT NULL",
            name="conditional_has_conditions",
        ),
        CheckConstraint(
            "dsar_sla_days IS NULL OR dsar_sla_days > 0",
            name="sla_positive",
        ),
        CheckConstraint(
            "review_every_days IS NULL OR review_every_days > 0",
            name="cadence_positive",
        ),
        # A signed DPA has a date. Without one there is no way to tell whether
        # it predates the processing it is supposed to cover.
        CheckConstraint(
            "NOT dpa_signed OR dpa_signed_on IS NOT NULL",
            name="dpa_has_date",
        ),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Their primary domain. Not unique — two vendors can share one, and a
    #: uniqueness constraint here would block recording both.
    domain: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="prospective"
    )
    risk_tier: Mapped[str] = mapped_column(
        String(8), nullable=False, default="medium"
    )
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default="processor"
    )

    #: Why the decision went the way it did, or what remains outstanding on a
    #: conditional approval.
    decision_note: Mapped[str | None] = mapped_column(Text)

    #: Who owns this relationship. The person a question about the vendor goes
    #: to, and the default assignee for its review.
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    # --- the contract ------------------------------------------------------
    dpa_signed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    dpa_signed_on: Mapped[date | None] = mapped_column(Date)
    #: When the agreement lapses, if it does. Surfaced as expiring rather than
    #: left to be noticed: a DPA that expired in March means every day since has
    #: been processing without one.
    dpa_expires_on: Mapped[date | None] = mapped_column(Date)

    # --- rights requests ---------------------------------------------------
    #: How a rights request reaches them. See the module docstring: this is the
    #: field that turns "we use this vendor" into "here is how somebody gets
    #: deleted from it".
    dsar_contact: Mapped[str | None] = mapped_column(String(320))
    #: How long they take. The statutory clock runs on us, so a slow processor
    #: is our problem and a 45-day one makes us late by default.
    dsar_sla_days: Mapped[int | None] = mapped_column(Integer)

    #: Their undertaking on breach notification. §8(6) requires us to tell the
    #: Board and the affected people; we cannot do it faster than we are told.
    breach_notice_hours: Mapped[int | None] = mapped_column(Integer)

    # --- what they hold ----------------------------------------------------
    #: Categories of personal data they process, from the same vocabulary the
    #: assessments use.
    data_categories: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    #: Where it lives. Free text because "AWS ap-south-1, with support access
    #: from Ireland" is the true answer and no enumeration holds it.
    data_location: Mapped[str | None] = mapped_column(Text)
    #: True when data leaves India — §16 lets the Government restrict transfers
    #: to notified countries, and cloud region alone decides this for most
    #: vendors.
    transfers_outside_india: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    #: `[{"name": "...", "purpose": "...", "location": "..."}]`.
    #:
    #: Recorded as the customer knows them, not discovered. §8(2) does not stop
    #: at the first hop, and a sub-processor nobody has written down is a gap in
    #: the chain rather than an absence of one.
    subprocessors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )

    #: Certifications claimed. Claimed, not verified — the distinction is in the
    #: word, and `VendorDocument` is where the evidence goes.
    certifications: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list
    )

    # --- review ------------------------------------------------------------
    last_reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    review_every_days: Mapped[int | None] = mapped_column(Integer)
    next_review_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    @property
    def in_use(self) -> bool:
        return self.status in ("approved", "conditional")

    @property
    def dpa_expired(self) -> bool:
        """Processing under a lapsed agreement.

        Computed on read rather than stored, so it is true the moment it becomes
        true rather than when a job last ran.
        """
        return bool(
            self.in_use
            and self.dpa_expires_on
            and self.dpa_expires_on < date.today()
        )

    @property
    def dpa_missing(self) -> bool:
        """In use, holding personal data, with no signed agreement.

        The single most consequential thing this register can tell somebody:
        §8(2) makes them responsible for this processor's processing, and
        without a contract there is nothing obliging the processor to act on a
        rights request passed to them.
        """
        return bool(self.in_use and not self.dpa_signed)

    @property
    def review_overdue(self) -> bool:
        return bool(
            self.in_use
            and self.next_review_at
            and self.next_review_at < datetime.now(UTC)
        )


class VendorDocument(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A document about a vendor, and when somebody last looked at it.

    Two kinds of row, deliberately in one table:

      * a URL somebody records — the vendor's privacy policy, their trust
        centre, their sub-processor list
      * an uploaded file — the signed DPA, a SOC 2 report, a penetration test
        summary. The bytes go through `stored_files`; this row is the index.

    `content_hash` is what makes the URL kind useful. Recording a policy's hash
    at review time means a later fetch can say *whether it changed* since
    somebody last read it — which is the honest, self-hosted version of the
    policy-diffing that commercial products sell, without pretending to a
    research operation we do not run. Nothing here fetches automatically; it
    holds what was seen and when.
    """

    __tablename__ = "vendor_documents"
    __table_args__ = (
        Index("ix_vendor_documents_vendor", "vendor_id"),
        CheckConstraint(
            "kind IN ('privacy_policy','dpa','subprocessor_list','certification',"
            "'security_report','breach_notice','other')",
            name="kind",
        ),
        # A row with neither a URL nor a file is a note about nothing.
        CheckConstraint(
            "url IS NOT NULL OR file_id IS NOT NULL",
            name="has_url_or_file",
        ),
    )

    vendor_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("vendors.id", ondelete="CASCADE"),
        nullable=False,
    )

    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)

    url: Mapped[str | None] = mapped_column(Text)
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("stored_files.id", ondelete="SET NULL")
    )

    #: sha256 of the content as last seen. See the class docstring.
    content_hash: Mapped[str | None] = mapped_column(String(64))
    #: When somebody last actually read it — not when the row was created.
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    #: True once a later fetch found different content than `content_hash`.
    changed_since_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    note: Mapped[str | None] = mapped_column(Text)


class VendorSystem(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """Which connections a vendor supplies.

    Joins the vendor register to the data map. Without it the two halves stay
    separate — a list of systems with credentials on one side, a list of
    companies on the other — and the question that matters is the join:
    "this vendor is retired, which of our systems does that affect?"
    """

    __tablename__ = "vendor_systems"
    __table_args__ = (
        UniqueConstraint(
            "vendor_id", "connection_id", name="uq_vendor_systems_pair"
        ),
        Index("ix_vendor_systems_connection", "connection_id"),
    )

    vendor_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("vendors.id", ondelete="CASCADE"),
        nullable=False,
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("connections.id", ondelete="CASCADE"),
        nullable=False,
    )
