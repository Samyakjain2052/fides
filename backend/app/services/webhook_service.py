"""Registering subscribers, signing alerts, delivering them, and chasing the silence.

The module that turns a withdrawal into a stop. See `app.models.webhook` for why
it exists at all; this file is the machinery.

FOUR PROPERTIES THIS MODULE GUARANTEES

* **Emitting cannot fail the act that caused it.** `emit()` only inserts rows.
  A subscriber that is down, slow, or misconfigured must never be the reason a
  person's withdrawal returns 500 — the right is exercised the moment they ask,
  and telling their processors is our problem, not theirs.

* **A receiver can prove the alert came from us.** Every request carries an
  HMAC-SHA256 signature over `timestamp.body` with the endpoint's own secret.
  Without it a "stop processing user X for every purpose" alert is a request
  anybody on the internet can forge into a customer's systems, which is a
  denial-of-service against their business dressed as compliance.

* **The signature is over a timestamp too, so it cannot be replayed.** A
  captured `consent.granted` alert replayed a month after the person withdrew
  would restart processing that had lawfully stopped. The receiver is told to
  reject anything older than five minutes.

* **The SSRF guard runs immediately before every send, not at save time.** A
  hostname that resolved publicly when the endpoint was created can be repointed
  at 169.254.169.254 the next day, and the only check that means anything is the
  one taken just before the socket opens. Redirects are refused for the same
  reason: following one re-targets a request that has already been cleared.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.hosts import HostNotAllowed, resolve_and_check
from app.core.crypto import open_sealed, seal
from app.core.errors import Conflict, NotFound, ValidationProblem
from app.core.security import generate_signing_secret
from app.models.audit import AuditAction
from app.models.webhook import (
    FAILURE_BUDGET,
    WEBHOOK_EVENTS,
    WebhookDelivery,
    WebhookEndpoint,
)
from app.services import audit_service
from app.services.audit_service import Actor

logger = logging.getLogger("app.webhooks")

#: Capped and short, like the notification queue's. Six attempts spread over
#: roughly two hours rides out a deploy or a certificate renewal; past that the
#: problem is not transient and an admin should be looking at a failed row rather
#: than a queue that never settles.
MAX_ATTEMPTS = 6
BACKOFF = (
    timedelta(seconds=30),
    timedelta(minutes=2),
    timedelta(minutes=10),
    timedelta(minutes=30),
    timedelta(hours=1),
)

#: One send. Deliberately tight: a receiver that needs longer than this to say
#: "got it" is doing its work inline, and the answer to that is for them to
#: queue and return, not for us to hold a connection open.
SEND_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)

#: How stale a signature may be before a receiver should reject it. Published in
#: the integration docs and enforced by the receiver, not by us — we can only
#: state the window we sign.
SIGNATURE_TOLERANCE_SECONDS = 300

SIGNATURE_HEADER = "X-DataShield-Signature"
EVENT_HEADER = "X-DataShield-Event"
DELIVERY_HEADER = "X-DataShield-Delivery"


class EndpointUnusable(Conflict):
    """The URL cannot be delivered to, and saying so now beats failing later."""


# --------------------------------------------------------------------------- #
# Signing
# --------------------------------------------------------------------------- #

def sign(secret: str, *, body: bytes, timestamp: int) -> str:
    """The value of the signature header.

    `t=<unix>,v1=<hex>` — the same shape Stripe and GitHub use, chosen because a
    receiver's developer has almost certainly implemented it before and will not
    invent a subtly wrong verification for a format they have never seen.

    The timestamp is inside the signed material, not merely alongside it. A
    timestamp a caller could edit without invalidating the signature would make
    the replay window unbounded, which is the entire thing it exists to bound.
    """
    signed = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify(secret: str, *, body: bytes, header: str, tolerance: int = SIGNATURE_TOLERANCE_SECONDS) -> bool:
    """The receiver's half, shipped so integration docs can point at real code.

    Not used by the sender. It lives here because a verification routine written
    from prose is where the `compare_digest` gets replaced by `==` — and a
    timing-safe comparison is not optional when the thing being compared decides
    whether to trust a "stop processing" instruction.
    """
    parts = dict(
        piece.split("=", 1) for piece in header.split(",") if "=" in piece
    )
    try:
        timestamp = int(parts.get("t", ""))
    except ValueError:
        return False
    if abs(int(datetime.now(UTC).timestamp()) - timestamp) > tolerance:
        return False
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, parts.get("v1", ""))


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

def _check_url(url: str) -> None:
    """Refuse a URL we should not be making requests to, with a reason.

    Checked here so a mistake surfaces while somebody is looking at the form,
    and checked again before every send because this one goes stale.
    """
    parsed = urlparse((url or "").strip())

    if parsed.scheme != "https":
        raise EndpointUnusable(
            "A webhook URL must be https. These alerts carry the identifier of a "
            "person and an instruction about their data; plain http would put "
            "both on the wire in clear."
        )
    if not parsed.hostname:
        raise EndpointUnusable("That URL has no host.")

    try:
        resolve_and_check(parsed.hostname, parsed.port or 443)
    except HostNotAllowed as exc:
        raise EndpointUnusable(str(exc)) from exc


async def create_endpoint(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    url: str,
    label: str,
    events: list[str],
    description: str | None = None,
    ack_deadline_hours: int = 24,
) -> tuple[WebhookEndpoint, str]:
    """Register a subscriber. Returns the endpoint and its secret, once.

    The secret is returned here and never again — the same discipline as an API
    key. A secret that can be read back is a secret that will be read back by
    something other than its owner.
    """
    unknown = sorted(set(events) - set(WEBHOOK_EVENTS))
    if unknown:
        raise ValidationProblem(
            f"Unknown event(s): {', '.join(unknown)}. "
            f"Valid events are: {', '.join(sorted(WEBHOOK_EVENTS))}."
        )
    if not events:
        raise ValidationProblem(
            "Subscribe to at least one event. An endpoint subscribed to nothing "
            "looks configured and delivers nothing, which is worse than absent."
        )

    _check_url(url)

    secret = generate_signing_secret()
    endpoint = WebhookEndpoint(
        tenant_id=tenant_id,
        url=url.strip(),
        label=label.strip(),
        description=(description or "").strip() or None,
        events=sorted(set(events)),
        secret_sealed=seal({"secret": secret}),
        secret_hint=secret[:8],
        ack_deadline_hours=ack_deadline_hours,
    )
    session.add(endpoint)
    await session.flush()

    await audit_service.record(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action=AuditAction.WEBHOOK_ENDPOINT_CREATED,
        entity_type="webhook_endpoint",
        entity_id=endpoint.id,
        # The URL, not the secret. An audit entry is readable by an auditor, and
        # an auditor's remit does not include the ability to forge our alerts.
        payload={"url": endpoint.url, "events": endpoint.events},
    )
    return endpoint, secret


async def rotate_secret(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    endpoint_id: uuid.UUID,
) -> str:
    """Issue a new signing secret. The old one stops working immediately.

    No overlap window, deliberately. Two valid secrets means a leaked one stays
    valid for the length of the window, and rotation usually happens *because*
    one leaked. A receiver updating a config value can tolerate a minute of
    rejected alerts; those alerts retry.
    """
    endpoint = await get_endpoint(session, tenant_id=tenant_id, endpoint_id=endpoint_id)
    secret = generate_signing_secret()
    endpoint.secret_sealed = seal({"secret": secret})
    endpoint.secret_hint = secret[:8]
    endpoint.secret_rotated_at = datetime.now(UTC)

    await audit_service.record(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action=AuditAction.WEBHOOK_SECRET_ROTATED,
        entity_type="webhook_endpoint",
        entity_id=endpoint.id,
        payload={"url": endpoint.url},
    )
    return secret


async def get_endpoint(
    session: AsyncSession, *, tenant_id: uuid.UUID, endpoint_id: uuid.UUID
) -> WebhookEndpoint:
    endpoint = await session.scalar(
        select(WebhookEndpoint).where(WebhookEndpoint.id == endpoint_id)
    )
    if endpoint is None:
        raise NotFound("No such webhook endpoint.")
    return endpoint


async def list_endpoints(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> list[WebhookEndpoint]:
    rows = await session.execute(
        select(WebhookEndpoint).order_by(WebhookEndpoint.created_at.desc())
    )
    return list(rows.scalars().all())


async def set_active(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    endpoint_id: uuid.UUID,
    active: bool,
) -> WebhookEndpoint:
    """Enable or disable. Re-enabling clears the failure budget and the reason.

    Clearing them is the point: an endpoint disabled after twenty failures and
    then re-enabled with its counter still at twenty would disable itself again
    on the next single failure, and the admin who just fixed it would have no way
    to tell that from the original fault.
    """
    endpoint = await get_endpoint(session, tenant_id=tenant_id, endpoint_id=endpoint_id)
    endpoint.active = active
    if active:
        endpoint.consecutive_failures = 0
        endpoint.disabled_at = None
        endpoint.disabled_reason = None

    await audit_service.record(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action=(
            AuditAction.WEBHOOK_ENDPOINT_ENABLED if active
            else AuditAction.WEBHOOK_ENDPOINT_DISABLED
        ),
        entity_type="webhook_endpoint",
        entity_id=endpoint.id,
        payload={"url": endpoint.url},
    )
    return endpoint


async def delete_endpoint(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    endpoint_id: uuid.UUID,
) -> None:
    """Remove a subscriber, and with it every delivery record it owned.

    The cascade is deliberate and is the reason `set_active(False)` exists
    alongside this: disabling keeps the evidence that alerts were sent and
    acknowledged, deleting discards it. An admin decommissioning a processor
    usually wants the first.
    """
    endpoint = await get_endpoint(session, tenant_id=tenant_id, endpoint_id=endpoint_id)
    url = endpoint.url
    await session.delete(endpoint)
    await audit_service.record(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action=AuditAction.WEBHOOK_ENDPOINT_DELETED,
        entity_type="webhook_endpoint",
        entity_id=endpoint_id,
        payload={"url": url},
    )


# --------------------------------------------------------------------------- #
# Serialisation
#
# Shaped here rather than in the router, for the reason `connection_service`
# gives: a response model that CANNOT express the secret is a stronger guarantee
# than a router that remembers not to include it.
# --------------------------------------------------------------------------- #

def endpoint_out(endpoint: WebhookEndpoint) -> dict[str, Any]:
    return {
        "id": str(endpoint.id),
        "label": endpoint.label,
        "url": endpoint.url,
        "description": endpoint.description,
        "events": endpoint.events,
        "active": endpoint.active,
        # The first eight characters, so an admin can tell which secret is live
        # when rotating without being able to reconstruct it.
        "secret_hint": f"{endpoint.secret_hint}…",
        "secret_rotated_at": (
            endpoint.secret_rotated_at.isoformat()
            if endpoint.secret_rotated_at else None
        ),
        "consecutive_failures": endpoint.consecutive_failures,
        "disabled_at": (
            endpoint.disabled_at.isoformat() if endpoint.disabled_at else None
        ),
        "disabled_reason": endpoint.disabled_reason,
        "ack_deadline_hours": endpoint.ack_deadline_hours,
        "created_at": endpoint.created_at.isoformat(),
    }


def delivery_out(delivery: WebhookDelivery) -> dict[str, Any]:
    return {
        "id": str(delivery.id),
        "endpoint_id": str(delivery.endpoint_id),
        "event": delivery.event,
        "status": delivery.status,
        "attempts": delivery.attempts,
        "next_attempt_at": delivery.next_attempt_at.isoformat(),
        "last_status_code": delivery.last_status_code,
        "last_error": delivery.last_error,
        "delivered_at": (
            delivery.delivered_at.isoformat() if delivery.delivered_at else None
        ),
        "acknowledged_at": (
            delivery.acknowledged_at.isoformat() if delivery.acknowledged_at else None
        ),
        "acknowledgement_note": delivery.acknowledgement_note,
        "escalated_at": (
            delivery.escalated_at.isoformat() if delivery.escalated_at else None
        ),
        "entity_type": delivery.entity_type,
        "entity_id": str(delivery.entity_id) if delivery.entity_id else None,
        "created_at": delivery.created_at.isoformat(),
    }


async def list_deliveries(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    endpoint_id: uuid.UUID | None = None,
    status: str | None = None,
    unacknowledged_only: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """The delivery log, newest first.

    `unacknowledged_only` is the view that matters in an inquiry: alerts the
    receiver accepted and never confirmed acting on. It is deliberately not the
    same as `status='delivered'` in the UI's vocabulary, even though it is today
    — an acknowledged row moves to its own status, and a future third state
    should not silently fall out of this filter.
    """
    query = select(WebhookDelivery).order_by(WebhookDelivery.created_at.desc())
    if endpoint_id is not None:
        query = query.where(WebhookDelivery.endpoint_id == endpoint_id)
    if status is not None:
        query = query.where(WebhookDelivery.status == status)
    if unacknowledged_only:
        query = query.where(
            WebhookDelivery.delivered_at.isnot(None),
            WebhookDelivery.acknowledged_at.is_(None),
        )
    rows = await session.execute(query.limit(min(limit, 500)))
    return [delivery_out(row) for row in rows.scalars().all()]


async def replay(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    delivery_id: uuid.UUID,
) -> WebhookDelivery:
    """Queue a fresh attempt of a delivery that failed.

    A NEW row, not a reset of the old one. The failed attempt is evidence that a
    processor was unreachable when it mattered, and rewinding its counters would
    erase the only record that there was a gap.
    """
    original = await session.scalar(
        select(WebhookDelivery).where(WebhookDelivery.id == delivery_id)
    )
    if original is None:
        raise NotFound("No such delivery.")

    replayed = WebhookDelivery(
        tenant_id=tenant_id,
        endpoint_id=original.endpoint_id,
        event=original.event,
        # The original bytes, deliberately. A payload rebuilt from current state
        # would describe today's consent rather than the change the receiver
        # missed — which for a withdrawal since superseded would tell them to
        # resume processing.
        payload=original.payload,
        entity_type=original.entity_type,
        entity_id=original.entity_id,
        next_attempt_at=datetime.now(UTC),
    )
    session.add(replayed)
    await session.flush()
    return replayed


# --------------------------------------------------------------------------- #
# Emitting
# --------------------------------------------------------------------------- #

async def emit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    event: str,
    data: dict[str, Any],
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
) -> int:
    """Queue one delivery per subscribed, active endpoint. Returns how many.

    INSERTS ONLY. No network call, no commit, no raise for a business reason —
    this runs inside the transaction that withdrew the consent, and the
    withdrawal must not acquire a new way to fail. A tenant with no subscribers
    returns 0 and that is a normal outcome, not a warning.
    """
    if event not in WEBHOOK_EVENTS:
        # A programming error, not a user one: the event name is a constant at
        # every call site. Loud, because a typo here means an alert that silently
        # never fires.
        raise ValueError(f"Unknown webhook event {event!r}.")

    rows = await session.execute(
        select(WebhookEndpoint).where(
            WebhookEndpoint.active.is_(True),
            WebhookEndpoint.events.contains([event]),
        )
    )
    endpoints = list(rows.scalars().all())
    if not endpoints:
        return 0

    now = datetime.now(UTC)
    for endpoint in endpoints:
        session.add(
            WebhookDelivery(
                tenant_id=tenant_id,
                endpoint_id=endpoint.id,
                event=event,
                payload={
                    "event": event,
                    # Not the delivery id: this identifies the OCCURRENCE, and
                    # every endpoint subscribed to it sees the same value. A
                    # receiver that also subscribes a second endpoint for
                    # failover can dedupe on it.
                    "occurred_at": now.isoformat(),
                    "data": data,
                },
                entity_type=entity_type,
                entity_id=entity_id,
                next_attempt_at=now,
            )
        )
    await session.flush()
    return len(endpoints)


# --------------------------------------------------------------------------- #
# Delivering
# --------------------------------------------------------------------------- #

async def claim_due(
    session: AsyncSession, *, limit: int = 20
) -> list[WebhookDelivery]:
    """Take up to `limit` due deliveries, invisible to any other worker.

    `FOR UPDATE SKIP LOCKED`, for the reason the notification queue states: two
    workers running this query take disjoint sets instead of both sending the
    same alert. "Stop processing" delivered twice is harmless; `consent.granted`
    delivered twice out of order is not.
    """
    now = datetime.now(UTC)
    rows = await session.execute(
        select(WebhookDelivery)
        .where(
            WebhookDelivery.status == "queued",
            WebhookDelivery.next_attempt_at <= now,
        )
        .order_by(WebhookDelivery.next_attempt_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    claimed = list(rows.scalars().all())
    for row in claimed:
        row.status = "sending"
    await session.flush()
    return claimed


def _endpoint_secret(endpoint: WebhookEndpoint) -> str:
    return open_sealed(endpoint.secret_sealed)["secret"]


async def attempt(
    session: AsyncSession,
    *,
    delivery: WebhookDelivery,
    endpoint: WebhookEndpoint,
    client: httpx.AsyncClient,
) -> bool:
    """One POST. Records the outcome on both rows. Returns whether it landed.

    Never raises for a delivery failure — a failed send is data, recorded and
    retried. It raises only if the database refuses the write, which is a real
    fault and should stop the batch.
    """
    delivery.attempts += 1
    body = json.dumps(
        {**delivery.payload, "delivery_id": str(delivery.id)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    timestamp = int(datetime.now(UTC).timestamp())

    try:
        # Re-checked here, on every attempt. See the module docstring.
        _check_url(endpoint.url)

        response = await client.post(
            endpoint.url,
            content=body,
            headers={
                "Content-Type": "application/json",
                SIGNATURE_HEADER: sign(
                    _endpoint_secret(endpoint), body=body, timestamp=timestamp
                ),
                EVENT_HEADER: delivery.event,
                DELIVERY_HEADER: str(delivery.id),
                "User-Agent": "DataShield-Webhooks/1.0",
            },
            timeout=SEND_TIMEOUT,
            # A redirect would re-target a request the SSRF guard has already
            # cleared, at a host it never saw. Refused outright.
            follow_redirects=False,
        )
        delivery.last_status_code = response.status_code
        landed = 200 <= response.status_code < 300
        if not landed:
            delivery.last_error = f"HTTP {response.status_code}"[:500]
    except EndpointUnusable as exc:
        delivery.last_status_code = None
        delivery.last_error = str(exc)[:500]
        landed = False
    except httpx.HTTPError as exc:
        delivery.last_status_code = None
        delivery.last_error = f"{type(exc).__name__}: {exc}"[:500]
        landed = False

    now = datetime.now(UTC)
    if landed:
        delivery.status = "delivered"
        delivery.delivered_at = now
        delivery.last_error = None
        endpoint.consecutive_failures = 0
        return True

    endpoint.consecutive_failures += 1
    if delivery.attempts >= MAX_ATTEMPTS:
        delivery.status = "failed"
    else:
        delivery.status = "queued"
        delivery.next_attempt_at = now + BACKOFF[
            min(delivery.attempts - 1, len(BACKOFF) - 1)
        ]

    if endpoint.consecutive_failures >= FAILURE_BUDGET and endpoint.active:
        endpoint.active = False
        endpoint.disabled_at = now
        endpoint.disabled_reason = (
            f"{FAILURE_BUDGET} consecutive failures. Last error: "
            f"{delivery.last_error}"
        )[:500]
        logger.warning(
            "webhook endpoint disabled",
            extra={"context": {"endpoint": str(endpoint.id), "url": endpoint.url}},
        )
    return False


async def drain_tenant(
    session: AsyncSession, *, tenant_id: uuid.UUID, limit: int = 20
) -> dict[str, int]:
    """Attempt every due delivery for one tenant, up to `limit`."""
    claimed = await claim_due(session, limit=limit)
    if not claimed:
        return {"claimed": 0, "delivered": 0, "failed": 0}

    endpoints = {
        endpoint.id: endpoint
        for endpoint in (
            await session.execute(
                select(WebhookEndpoint).where(
                    WebhookEndpoint.id.in_({row.endpoint_id for row in claimed})
                )
            )
        ).scalars()
    }

    delivered = 0
    async with httpx.AsyncClient() as client:
        for row in claimed:
            endpoint = endpoints.get(row.endpoint_id)
            if endpoint is None:
                # The endpoint was deleted between the claim and now. The cascade
                # will remove this row; marking it failed keeps it out of the
                # queue in the meantime.
                row.status = "failed"
                row.last_error = "Endpoint no longer exists."
                continue
            if await attempt(session, delivery=row, endpoint=endpoint, client=client):
                delivered += 1

    await session.flush()
    return {
        "claimed": len(claimed),
        "delivered": delivered,
        "failed": len(claimed) - delivered,
    }


# --------------------------------------------------------------------------- #
# Acknowledgement and escalation — BRD §4.4.2
# --------------------------------------------------------------------------- #

async def acknowledge(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    delivery_id: uuid.UUID,
    note: str | None = None,
) -> WebhookDelivery:
    """The receiver confirming it acted — not merely that it received.

    Idempotent: a receiver whose retry logic acknowledges twice gets the same
    answer, and the FIRST timestamp is kept. Overwriting it would move the
    evidence of when processing actually stopped to whenever their retry
    happened to fire.
    """
    delivery = await session.scalar(
        select(WebhookDelivery).where(WebhookDelivery.id == delivery_id)
    )
    if delivery is None:
        raise NotFound("No such delivery.")

    if delivery.acknowledged_at is not None:
        return delivery

    if delivery.status not in ("delivered", "acknowledged"):
        raise Conflict(
            "That alert has not been delivered yet, so it cannot be acknowledged."
        )

    delivery.acknowledged_at = datetime.now(UTC)
    delivery.acknowledgement_note = (note or "").strip()[:500] or None
    delivery.status = "acknowledged"
    return delivery


async def sweep_escalations(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> int:
    """Escalate alerts delivered but never acknowledged past the endpoint's window.

    Runs on a timer rather than on read, for the reason the grievance sweep
    gives: the case that matters most is the one nobody is looking at. A
    processor that quietly stopped acting on withdrawal alerts produces exactly
    no signal until somebody asks why a person who withdrew in March is still
    being mailed in June.

    Escalating means raising a notification for the DPO and stamping
    `escalated_at` so it happens once. It does not disable the endpoint: the
    receiver IS accepting our alerts, which is a different fault from being
    unreachable and has a different fix.
    """
    from app.models.user import User
    from app.services import notification_service

    now = datetime.now(UTC)
    rows = await session.execute(
        select(WebhookDelivery, WebhookEndpoint)
        .join(WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id)
        .where(
            WebhookDelivery.status == "delivered",
            WebhookDelivery.acknowledged_at.is_(None),
            WebhookDelivery.escalated_at.is_(None),
        )
    )
    overdue = [
        (delivery, endpoint)
        for delivery, endpoint in rows.all()
        if delivery.delivered_at is not None
        and delivery.delivered_at
        + timedelta(hours=endpoint.ack_deadline_hours) <= now
    ]
    if not overdue:
        return 0

    # To an admin of this workspace, not to the person the alert concerned.
    # Resolved once for the batch rather than per row: the same mailbox, and a
    # sweep that escalates forty deliveries should not run forty identical
    # lookups. Mirrors `connection_service`, the only other operator-facing
    # notification in the product.
    to_address = await session.scalar(
        select(User.email)
        .where(User.role == "admin", User.is_active.is_(True))
        .order_by(User.created_at)
        .limit(1)
    )

    for delivery, endpoint in overdue:
        delivery.escalated_at = now
        await audit_service.record(
            session,
            tenant_id=tenant_id,
            actor=Actor(type="system", id=None, label="webhook-escalation"),
            action=AuditAction.WEBHOOK_ESCALATED,
            entity_type="webhook_delivery",
            entity_id=delivery.id,
            payload={
                "url": endpoint.url,
                "event": delivery.event,
                "hours_unacknowledged": endpoint.ack_deadline_hours,
            },
        )
        # `enqueue` suppresses rather than raises when there is no address or no
        # template, so a workspace with neither still gets the audit entry above
        # — which is the part that has to exist.
        await notification_service.enqueue(
            session,
            tenant_id=tenant_id,
            key="webhook.escalated",
            to_address=to_address,
            entity_type="webhook_delivery",
            entity_id=delivery.id,
            context={
                "endpoint": endpoint.label,
                "event": delivery.event,
                "sent_at": delivery.delivered_at.strftime("%d %b %Y at %H:%M UTC"),
                "hours": str(endpoint.ack_deadline_hours),
            },
        )

    return len(overdue)
