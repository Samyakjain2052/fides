"""Outbound alerts to Data Fiduciaries and Processors, and the log that proves it.

The thing this table makes true: **a withdrawal that stops processing.**

Until now a withdrawn consent changed a row here and nothing else. The person was
told, the audit chain recorded it, and the customer's CRM carried on mailing them
— because nobody had told the CRM. §6(6) gives the Data Principal the right to
withdraw "at any time", and the Act's consequence is that processing *ceases*. A
consent ledger that cannot reach the systems doing the processing records the
withdrawal without effecting it, which is the difference between compliance and a
tidy record of non-compliance.

MeitY's BRD for Consent Management names this directly in §4.4.2 (Data Fiduciary
and Processor Alerts) and repeats it as "Real-Time Synchronization" in §4.1.1,
§4.1.3 and §4.1.5. One mechanism, four requirements.

DELIVERED IS NOT ACTED, and they are stored separately
A 2xx means the receiver's server accepted the bytes. It does not mean anything
stopped. The BRD asks for "Action Confirmation" — the fiduciary confirming it
halted processing — and for unacknowledged alerts to be escalated. Collapsing
those into one column would make "we told them" indistinguishable from "they did
it", and the second is the one a Board would ask about. So `delivered_at` is set
by our sender and `acknowledged_at` only by the receiver calling back.

WHY A QUEUE TABLE AND NOT AN INLINE POST
Emitting inside the request that withdrew the consent would make a customer's
slow endpoint into our slow endpoint, and a customer's outage into a failed
withdrawal. Withdrawal must succeed even when every subscriber is down; the
alert is a promise to keep trying, not a precondition. Same Postgres queue as
`notifications`, claimed with `FOR UPDATE SKIP LOCKED`, for the same reason
stated there: no broker until something needs one.

THE PAYLOAD CARRIES AN IDENTIFIER, NOT A PROFILE
Enough to act on — who, which purpose, what changed — and nothing more. A webhook
body is a copy of personal data leaving our boundary for a host the customer
named, landing in their logs with a retention policy we do not control. "Stop
processing for principal X, purpose Y" needs no email address to be actionable.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDMixin

#: Every event a subscriber may ask for.
#:
#: Deliberately a closed set. An endpoint subscribing to `consent.*` by wildcard
#: would silently start receiving a new event the day one is added — including
#: one carrying a field their parser was never written for. Adding an event here
#: is a decision; expanding a wildcard is an accident.
WEBHOOK_EVENTS: dict[str, str] = {
    # §4.1.5. The one that matters most, and the reason this module exists.
    "consent.withdrawn": "A person withdrew consent for a purpose. Stop processing.",
    # §4.1.1 — so a fiduciary can begin processing the moment consent exists,
    # rather than polling the check endpoint on a timer.
    "consent.granted": "A person granted consent for a purpose.",
    # §4.1.3.
    "consent.updated": "An existing consent changed — scope, or its expiry.",
    # §4.1.4. Expiry is not withdrawal, and a receiver may well treat them
    # differently (a renewal prompt versus an immediate stop), so it is its own
    # event rather than a flag on `withdrawn`.
    "consent.expired": "A consent reached its expiry and is no longer valid.",
    # Rights requests. A fiduciary whose systems hold the data usually has to do
    # something itself, and the deadline is statutory.
    "dsar.received": "A rights request was raised and is now running its clock.",
    "dsar.completed": "A rights request was fulfilled.",
}

DELIVERY_STATUSES = (
    "queued",        # accepted, not yet attempted
    "sending",       # claimed by a worker
    "delivered",     # the receiver returned 2xx — see the module docstring
    "acknowledged",  # the receiver called back to confirm it acted
    "failed",        # permanently — attempts exhausted, or a hard rejection
)

#: Consecutive failures after which an endpoint stops being tried.
#:
#: An endpoint that has refused twenty deliveries in a row is not experiencing a
#: blip; it has been decommissioned and nobody told us. Continuing to queue for
#: it buries the live subscribers behind a backlog that will never drain, and
#: hides the fact that this customer is no longer receiving withdrawal alerts at
#: all — which is precisely the thing somebody needs to be told about.
FAILURE_BUDGET = 20


class WebhookEndpoint(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One subscriber: a URL, what it wants, and the secret it verifies us with."""

    __tablename__ = "webhook_endpoints"
    __table_args__ = (
        Index("ix_webhook_endpoints_tenant_id_active", "tenant_id", "active"),
    )

    #: Https only, and re-checked against the SSRF guard on every send rather
    #: than only when it was saved. A hostname that resolved publicly at creation
    #: can be repointed at 169.254.169.254 afterwards, and the check that matters
    #: is the one immediately before the request.
    url: Mapped[str] = mapped_column(String(2048), nullable=False)

    label: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text())

    #: Which of WEBHOOK_EVENTS this endpoint receives. Never empty — an endpoint
    #: subscribed to nothing is a row that looks configured and delivers nothing.
    events: Mapped[list[str]] = mapped_column(JSONB, nullable=False)

    #: Sealed with the application key, like connection credentials. It is a
    #: signing secret, not a password, but it is still a shared secret whose
    #: disclosure lets somebody forge "stop processing" alerts into a customer's
    #: systems — which is a denial-of-service against their business.
    secret_sealed: Mapped[str] = mapped_column(Text(), nullable=False)

    #: Shown once at creation and on rotation, then never again. Stored so the
    #: console can say *which* secret is live without being able to reveal it.
    secret_hint: Mapped[str] = mapped_column(String(16), nullable=False)
    secret_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    consecutive_failures: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    #: Set when FAILURE_BUDGET is spent. An admin re-enables it deliberately,
    #: after fixing whatever was wrong — auto-healing would just resume the
    #: silence.
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_reason: Mapped[str | None] = mapped_column(Text())

    #: How long the receiver has to acknowledge before the alert is escalated,
    #: per BRD §4.4.2. Per-endpoint because a batch system that reconciles hourly
    #: is not misbehaving at minute ten, and a real-time marketing suppression
    #: list is misbehaving at hour one.
    ack_deadline_hours: Mapped[int] = mapped_column(
        Integer, default=24, nullable=False
    )


class WebhookDelivery(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One attempt to tell one endpoint about one event — and what came back.

    Append-mostly, for the same reason the notification log is: "we told your
    processor to stop on the 14th and they confirmed on the 14th" is a claim that
    has to survive somebody's interest in it being untrue.
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        # The drain query: due rows, oldest first. Partial, because delivered and
        # acknowledged rows are the overwhelming majority and never match it.
        Index(
            "ix_webhook_deliveries_due",
            "next_attempt_at",
            postgresql_where="status IN ('queued','sending')",
        ),
        # The escalation sweep, and the console's "who has not confirmed" view.
        Index(
            "ix_webhook_deliveries_unacked",
            "tenant_id",
            "delivered_at",
            postgresql_where="status = 'delivered'",
        ),
        Index("ix_webhook_deliveries_endpoint_id_created_at",
              "endpoint_id", "created_at"),
    )

    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
        nullable=False,
    )

    event: Mapped[str] = mapped_column(String(64), nullable=False)

    #: The exact body that was signed and sent. Stored rather than re-rendered,
    #: because a replay must reproduce the original bytes — a payload rebuilt
    #: from current state would describe today's consent, not the withdrawal the
    #: receiver missed.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), default="queued", nullable=False, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    last_status_code: Mapped[int | None] = mapped_column(Integer)
    #: Truncated. A receiver returning an HTML error page would otherwise put a
    #: few hundred KB of somebody's stack trace into our database, per attempt.
    last_error: Mapped[str | None] = mapped_column(String(500))

    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: What the receiver says it did. Free text, theirs, capped — evidence of a
    #: claim they made, not a fact we verified.
    acknowledgement_note: Mapped[str | None] = mapped_column(String(500))

    #: Set once, by the escalation sweep, so a delivery is escalated at most
    #: once no matter how often the sweep runs. Same discipline as the grievance
    #: escalation flag.
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: What this delivery is about, for the console and for replay. Not a foreign
    #: key: the consent row a withdrawal alert refers to may be purged under a
    #: retention policy while the evidence that we sent the alert must remain.
    entity_type: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
