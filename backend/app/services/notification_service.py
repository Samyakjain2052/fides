"""Rendering, queueing and sending.

The queue is a Postgres table claimed with `FOR UPDATE SKIP LOCKED`, not a
broker. ARCHITECTURE.md already says a broker is deferred until something needs
one, and a compliance mailer sending tens of messages a minute is not that thing.
One less moving part to deploy, monitor and lose messages in.

Three properties this module exists to guarantee:

* **Nobody is told the same thing twice.** A unique constraint on
  (template, entity) means a retried job or a refreshed queue cannot re-notify.
* **A permanent failure stops.** Retrying a nonexistent mailbox forever starves
  the real messages behind it.
* **Placeholders are validated when a template is saved.** Finding a typo because
  a statutory notification failed at 2am is too late.
"""

from __future__ import annotations

import html
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.models.notification import (
    CHANNELS,
    TEMPLATE_KEYS,
    Notification,
    NotificationTemplate,
)
from app.models.tenant import Tenant
from app.services import notification_providers

logger = logging.getLogger("app.notifications")

# Capped, and short. Five attempts over ~15 minutes is enough to ride out a
# provider blip; beyond that the problem is not transient and a human should see
# a failed row rather than a queue that never settles.
MAX_ATTEMPTS = 5
BACKOFF = (timedelta(seconds=30), timedelta(minutes=1), timedelta(minutes=3),
           timedelta(minutes=10))

_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


class TemplateInvalid(Conflict):
    """A template that would fail, or mislead, at send time."""


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #

def placeholders_in(value: str) -> set[str]:
    return set(_PLACEHOLDER.findall(value or ""))


def validate_template(key: str, subject: str, body: str) -> None:
    """Reject at SAVE time what would otherwise fail at send time.

    An unknown placeholder renders as nothing, which turns "your request
    DSAR-2026-0007 is due on 14 September" into "your request  is due on ",
    quietly, in a statutory notification. Rejecting the template is the only
    place this can be caught while somebody is still looking at it.
    """
    if key not in TEMPLATE_KEYS:
        raise TemplateInvalid(
            f"Unknown template key {key!r}. Known keys: {', '.join(sorted(TEMPLATE_KEYS))}."
        )
    allowed = set(TEMPLATE_KEYS[key])
    used = placeholders_in(subject) | placeholders_in(body)
    unknown = used - allowed
    if unknown:
        raise TemplateInvalid(
            f"Template {key!r} uses placeholders that will never be supplied: "
            f"{', '.join(sorted(unknown))}. Available: {', '.join(sorted(allowed))}."
        )


def render(template: str, context: dict[str, Any]) -> str:
    """Substitute placeholders, escaping every value.

    Escaping is not optional. A grievance description is written by a member of
    the public and ends up inside an email body; unescaped, that is an injection
    into whatever renders it. The template itself is trusted (an admin wrote it);
    the values never are.
    """
    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        value = context.get(name)
        return html.escape("" if value is None else str(value), quote=False)

    return _PLACEHOLDER.sub(_sub, template or "")


async def upsert_template(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    key: str,
    channel: str,
    language: str,
    subject: str,
    body: str,
) -> NotificationTemplate:
    if channel not in CHANNELS:
        raise TemplateInvalid(f"Unknown channel {channel!r}.")
    validate_template(key, subject, body)

    existing = await session.scalar(
        select(NotificationTemplate).where(
            NotificationTemplate.tenant_id == tenant_id,
            NotificationTemplate.key == key,
            NotificationTemplate.channel == channel,
            NotificationTemplate.language == language,
        )
    )
    if existing is not None:
        existing.subject = subject
        existing.body = body
        await session.flush()
        return existing

    row = NotificationTemplate(
        tenant_id=tenant_id, key=key, channel=channel, language=language,
        subject=subject, body=body,
    )
    session.add(row)
    await session.flush()
    return row


async def resolve_template(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    key: str,
    channel: str,
    language: str,
) -> tuple[NotificationTemplate, str] | None:
    """The template to use, and the language actually used.

    Falls back to English when the requested language has none. The fallback is
    RETURNED rather than hidden, because "we notified them in their language" has
    to be checkable — and silently sending English under a Hindi label is the
    kind of thing that reads fine in a demo and fails an audit.
    """
    for candidate in (language, "English"):
        row = await session.scalar(
            select(NotificationTemplate).where(
                NotificationTemplate.tenant_id == tenant_id,
                NotificationTemplate.key == key,
                NotificationTemplate.channel == channel,
                NotificationTemplate.language == candidate,
                NotificationTemplate.is_active.is_(True),
            )
        )
        if row is not None:
            return row, candidate
    return None


# --------------------------------------------------------------------------- #
# Queueing
# --------------------------------------------------------------------------- #

async def enqueue(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    key: str,
    to_address: str | None,
    context: dict[str, Any],
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    principal_id: uuid.UUID | None = None,
    language: str = "English",
    channel: str = "email",
) -> Notification | None:
    """Queue one message. Returns None when there is nothing to send.

    Deliberately forgiving: a missing address or a missing template records a
    *suppressed* row rather than raising. The caller is usually a state change
    that has already happened — a DSAR was rejected, a grievance escalated — and
    failing that operation because a template is absent would be the tail wagging
    the dog. The suppression is on the record either way.
    """
    now = datetime.now(UTC)

    if not to_address:
        return await _suppress(
            session, tenant_id=tenant_id, key=key, channel=channel, language=language,
            entity_type=entity_type, entity_id=entity_id, principal_id=principal_id,
            reason="no contact address on record",
        )

    resolved = await resolve_template(
        session, tenant_id=tenant_id, key=key, channel=channel, language=language
    )
    if resolved is None:
        return await _suppress(
            session, tenant_id=tenant_id, key=key, channel=channel, language=language,
            entity_type=entity_type, entity_id=entity_id, principal_id=principal_id,
            reason=f"no active {channel} template for {key}",
            to_address=to_address,
        )
    template, language_used = resolved

    tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
    full_context = {"organisation": tenant.name if tenant else "", **context}

    row = Notification(
        tenant_id=tenant_id,
        template_key=key,
        channel=channel,
        language=language_used,
        language_requested=language if language_used != language else None,
        to_address=to_address,
        subject_rendered=render(template.subject, full_context)[:255],
        # Rendered now and stored until the message settles. A retry three
        # minutes from now cannot re-derive "your request is due on 14 September"
        # from the template — the values are gone — and a retry that sends
        # different words than the first attempt is not a retry.
        pending_body=render(template.body, full_context),
        status="queued",
        entity_type=entity_type,
        entity_id=entity_id,
        principal_id=principal_id,
        queued_at=now,
        next_attempt_at=now,
    )
    # A SAVEPOINT, not a plain flush. The caller is almost always a state change
    # that has already happened in this transaction — a DSAR was submitted, a
    # consent withdrawn — and a bare `session.rollback()` on the duplicate-key
    # error would take that change down with it. Losing a rights request because
    # its acknowledgement was already queued is a far worse bug than the one the
    # constraint is preventing.
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        # Already queued for this entity. That is the idempotency constraint
        # doing its job, not an error — the caller asked twice.
        logger.info(
            "notification already queued for this entity; not duplicating",
            extra={"context": {"key": key, "entity_id": str(entity_id)}},
        )
        return None

    return row


async def _suppress(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    key: str,
    channel: str,
    language: str,
    reason: str,
    entity_type: str | None,
    entity_id: uuid.UUID | None,
    principal_id: uuid.UUID | None,
    to_address: str = "(none)",
) -> Notification | None:
    now = datetime.now(UTC)
    row = Notification(
        tenant_id=tenant_id, template_key=key, channel=channel, language=language,
        to_address=to_address, subject_rendered="(not sent)",
        status="suppressed", suppression_reason=reason[:255],
        entity_type=entity_type, entity_id=entity_id, principal_id=principal_id,
        queued_at=now, next_attempt_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        # Same reasoning as enqueue: the suppression is not worth losing the
        # state change that prompted it.
        return None
    return row


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #

async def claim_due(
    session: AsyncSession, *, limit: int = 10
) -> list[Notification]:
    """Take up to `limit` due messages, invisible to any other worker.

    `FOR UPDATE SKIP LOCKED` is the whole trick: two workers running the same
    query take disjoint sets rather than fighting over the head of the queue or
    both sending the same message. Without it, "one message, two emails" is a
    matter of timing.

    Unscoped by tenant on purpose — this is the platform's worker, not a
    request. It must run with the owner role or with RLS context per row.
    """
    now = datetime.now(UTC)
    rows = await session.execute(
        select(Notification)
        .where(
            Notification.status == "queued",
            Notification.next_attempt_at <= now,
        )
        .order_by(Notification.next_attempt_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    claimed = list(rows.scalars().all())
    for row in claimed:
        row.status = "sending"
    await session.flush()
    return claimed


async def deliver(
    session: AsyncSession, *, notification: Notification, body: str | None = None
) -> Notification:
    """Attempt one send and record the outcome.

    A retryable failure goes back to `queued` with a later `next_attempt_at`; a
    permanent one, or an exhausted budget, lands at `failed` with the reason. A
    row that keeps retrying forever is how a queue fills with garbage while real
    messages wait behind it.
    """
    provider = notification_providers.get_provider()
    notification.attempts += 1
    notification.provider = provider.name

    result = await provider.send(
        to=notification.to_address,
        subject=notification.subject_rendered,
        # The stored body is the default. `body` is an override for the rare
        # caller that has just rendered one; a worker claiming a row off the queue
        # has nothing but what the row carries.
        body=body if body is not None else (notification.pending_body or ""),
        channel=notification.channel,
    )

    now = datetime.now(UTC)
    if result.ok:
        notification.status = "delivered"
        notification.sent_at = now
        notification.delivered_at = now
        notification.provider_message_id = result.provider_message_id
        notification.last_error = None
        # Settled: the body goes. The database CHECK would refuse the row
        # otherwise, which is the point — this cannot be forgotten.
        notification.pending_body = None
    elif not result.retryable or notification.attempts >= MAX_ATTEMPTS:
        notification.status = "failed"
        notification.failed_at = now
        notification.last_error = (result.error or "send failed")[:2000]
        # A permanently failed message keeps no body either. If somebody requeues
        # it, it is re-rendered from the template with fresh values — which is
        # honest: we cannot claim to resend words we no longer hold.
        notification.pending_body = None
    else:
        notification.status = "queued"
        delay = BACKOFF[min(notification.attempts - 1, len(BACKOFF) - 1)]
        notification.next_attempt_at = now + delay
        notification.last_error = (result.error or "send failed")[:2000]

    await session.flush()
    return notification


async def send_now(
    session: AsyncSession, *, notification: Notification | None
) -> Notification | None:
    """Deliver a freshly-enqueued message in the same request.

    Used by the call sites, which are state changes a person is waiting on. The
    background worker exists for retries and for anything queued while a provider
    was down; making the first attempt inline means the common case is immediate
    rather than up to a poll interval late.
    """
    if notification is None or notification.status != "queued":
        return notification
    return await deliver(session, notification=notification)


async def retry_failed(
    session: AsyncSession, *, tenant_id: uuid.UUID, notification_id: uuid.UUID
) -> Notification:
    """Requeue one failed message and attempt it immediately.

    `attempts` is NOT reset. The count is the record of how many times we tried
    to reach somebody; a counter a human can zero is not a record, and the retry
    budget exists to stop a dead mailbox being hammered forever — including by a
    person clicking a button.

    A suppressed message cannot be retried. Suppression is a decision ("we hold
    no address for this person"), not a failure, and re-attempting it would send
    nothing while making the log look like we tried.
    """
    row = await session.scalar(
        select(Notification).where(
            Notification.tenant_id == tenant_id,
            Notification.id == notification_id,
        )
    )
    if row is None:
        raise NotFound("No such notification.")
    if row.status == "suppressed":
        raise Conflict(
            f"This message was not sent deliberately: {row.suppression_reason}. "
            "Fix the underlying reason rather than retrying."
        )
    if row.status == "delivered":
        raise Conflict("This message was already delivered.")
    if row.attempts >= MAX_ATTEMPTS:
        raise Conflict(
            f"This message has already been attempted {row.attempts} times. "
            "The address or the provider is the problem, not the timing."
        )

    # Re-render from the current template. The original body is gone by design,
    # so what goes out now is what the template says now — and the values we can
    # still recover. Anything we cannot recover renders empty rather than wrong.
    resolved = await resolve_template(
        session, tenant_id=tenant_id, key=row.template_key,
        channel=row.channel, language=row.language,
    )
    if resolved is None:
        raise Conflict(
            f"There is no active {row.channel} template for {row.template_key} "
            "any more, so this message cannot be re-rendered."
        )
    template, _ = resolved
    tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
    context = {"organisation": tenant.name if tenant else ""}

    row.status = "queued"
    row.failed_at = None
    row.next_attempt_at = datetime.now(UTC)
    row.pending_body = render(template.body, context)
    await session.flush()
    return await deliver(session, notification=row)


async def drain_tenant(
    session: AsyncSession, *, tenant_id: uuid.UUID, limit: int = 25
) -> dict[str, int]:
    """One pass of the worker loop, scoped to a single tenant.

    Scoped because this is reachable from the API: an admin draining their own
    backlog must not be able to send another tenant's messages. The real
    background worker uses `claim_due`, which is deliberately unscoped and runs
    outside any request.
    """
    now = datetime.now(UTC)
    rows = await session.execute(
        select(Notification)
        .where(
            Notification.tenant_id == tenant_id,
            Notification.status == "queued",
            Notification.next_attempt_at <= now,
        )
        .order_by(Notification.next_attempt_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    claimed = list(rows.scalars().all())

    tally = {"claimed": len(claimed), "delivered": 0, "failed": 0, "requeued": 0}
    for row in claimed:
        await deliver(session, notification=row)
        if row.status == "delivered":
            tally["delivered"] += 1
        elif row.status == "failed":
            tally["failed"] += 1
        else:
            tally["requeued"] += 1
    return tally


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

async def log_for_tenant(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    principal_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[Notification]:
    stmt = select(Notification).where(Notification.tenant_id == tenant_id)
    if principal_id:
        stmt = stmt.where(Notification.principal_id == principal_id)
    if status:
        stmt = stmt.where(Notification.status == status)
    rows = await session.execute(stmt.order_by(Notification.created_at.desc()).limit(limit))
    return list(rows.scalars().all())


async def templates_for_tenant(
    session: AsyncSession, tenant_id: uuid.UUID
) -> list[NotificationTemplate]:
    rows = await session.execute(
        select(NotificationTemplate)
        .where(NotificationTemplate.tenant_id == tenant_id)
        .order_by(NotificationTemplate.key, NotificationTemplate.language)
    )
    return list(rows.scalars().all())


# --------------------------------------------------------------------------- #
# Starter templates
# --------------------------------------------------------------------------- #

DEFAULT_TEMPLATES: dict[str, tuple[str, str]] = {
    "dsar.received": (
        "We received your data request {{reference}}",
        "We have received your {{type}} request ({{reference}}).\n\n"
        "We must respond by {{deadline}}. You can track it in your account at any "
        "time.\n\n{{organisation}}",
    ),
    "dsar.completed": (
        "Your data request {{reference}} is complete",
        "Your {{type}} request ({{reference}}) is complete.\n\n{{organisation}}",
    ),
    "dsar.rejected": (
        "About your data request {{reference}}",
        "We could not action your {{type}} request ({{reference}}).\n\n"
        "Reason: {{reason}}\n\n"
        "If you disagree, you may raise a grievance with our Grievance Officer.\n\n"
        "{{organisation}}",
    ),
    "consent.withdrawn": (
        "You withdrew consent for {{purpose}}",
        "We have recorded that you withdrew your consent for {{purpose}}.\n\n"
        "This takes effect from {{effective_from}}.\n\n{{organisation}}",
    ),
    "grievance.received": (
        "We received your complaint {{reference}}",
        "We have received your complaint about {{category}} ({{reference}}).\n\n"
        "We will respond by {{deadline}}.\n\n{{organisation}}",
    ),
    "grievance.escalated": (
        "Your complaint {{reference}} has been escalated",
        "Your complaint ({{reference}}) about {{category}} has been open for "
        "{{days_open}} days and has been escalated to our Grievance Officer.\n\n"
        "{{organisation}}",
    ),
    "grievance.resolved": (
        "Your complaint {{reference}} is resolved",
        "Your complaint ({{reference}}) has been resolved.\n\n{{resolution}}\n\n"
        "{{organisation}}",
    ),
    "grievance.rejected": (
        "About your complaint {{reference}}",
        "We have looked into your complaint ({{reference}}) and were not able to "
        "uphold it.\n\nReason: {{reason}}\n\n"
        "If you disagree with this outcome, you may approach the Data Protection "
        "Board of India.\n\n{{organisation}}",
    ),
    "grievance.confirm": (
        "Confirm your complaint {{reference}}",
        "We have recorded your complaint ({{reference}}) and must respond by "
        "{{deadline}}.\n\n"
        "Because you filed without an account, please confirm this email address "
        "is yours by entering this code on the confirmation page:\n\n"
        "    {{code}}\n\n"
        "Your complaint is logged either way. Confirming it means we will escalate "
        "it to our Grievance Officer if it is not resolved in time.\n\n"
        "{{organisation}}",
    ),
    "retention.pre_purge": (
        "Scheduled deletion of your {{category}} data",
        "Under our retention policy, your {{category}} data is scheduled for "
        "deletion on {{purge_date}}.\n\nNo action is needed from you.\n\n"
        "{{organisation}}",
    ),
}


async def seed_default_templates(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> int:
    """English email templates for every key the product sends.

    Seeded so a new workspace notifies people from day one. Without these every
    call site would suppress with "no active template", which is honest but
    useless — and a fiduciary who does not know they need to write eight templates
    before anyone is told anything has been handed a trap.
    """
    made = 0
    for key, (subject, body) in DEFAULT_TEMPLATES.items():
        existing = await session.scalar(
            select(NotificationTemplate).where(
                NotificationTemplate.tenant_id == tenant_id,
                NotificationTemplate.key == key,
                NotificationTemplate.channel == "email",
                NotificationTemplate.language == "English",
            )
        )
        if existing is not None:
            continue
        validate_template(key, subject, body)
        session.add(
            NotificationTemplate(
                tenant_id=tenant_id, key=key, channel="email", language="English",
                subject=subject, body=body,
            )
        )
        made += 1
    await session.flush()
    return made
