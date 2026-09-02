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
# A paragraph that is nothing but a link — see render_html_email()'s handling
# of these below.
_BARE_URL = re.compile(r"https?://\S+")


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


# One shared shell around every rendered body — a subject and a card is the whole
# design. Every template, present and future (grievance confirmation, breach
# notice, an eventual user invitation), goes through this so a person who gets
# three different emails from the product does not have to guess it is the same
# sender each time.
_BRAND_NAME = "AuditTrace"

# The mark, inline. An `<img src="https://...">` breaks the moment that host is
# down, renamed, or simply not reachable from wherever a mail client fetches
# images from (many strip remote images by default); a `data:` URI has no
# network dependency at send time or read time. It costs a few hundred bytes per
# email — a fair trade for a logo that cannot go missing.
#
# This is a raster PNG rather than an inline SVG: Outlook desktop/Windows Mail
# (still Word-engine rendered) and several other mail clients do not render
# SVG at all, inline or as a data URI, so a chunk of readers would simply see
# no logo. A small PNG data URI renders everywhere.
_LOGO_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAKAAAACgCAYAAACLz2ctAAAEe0lEQVR4nO3d623bMBRAYTvoBN"
    "ZQzQrNgOkK6VDUCgmIQq3jpyRe8r7O9zMJbAM+uKSchDoeFJ0+3j81nx9/za9vx4OSYU9MbL7M"
    "g6Ls+iREF8PcMcYuD0x4Mc0dQhR9QMLLYRYMUeSBCC+nWSDEl9YHIL68TgKfYjQFSHw4NUa4a4"
    "QSHqSW5M0TkPgg2camAIkP0o2sDpD40KOVVQESH7Za28zTAIkPe61pp/lzQKDFwwCZfmj1rKG7"
    "ARIfpDxq6WaAxAdp95piDwhVVwEy/dDLrbaYgFD1LUCmH3q7bIwJCFX/AmT6YZTz1piAUEWA0A"
    "+Q5RejLc0xAaGKAKGKAKGKAKHqyAUINDEBoYoAoYoAoYoAoYoAoYoAoYoAoeqH7tOjl/Lz19XX"
    "pj+/D9YQYILwLr9nKUSW4CTx7fm5EQgwiLIxKisREiBUEWAAZec0szAFCRCqCNC5YmCKtSBAx4"
    "rz+CoCdKoEiK8iwMTxTQY+kCZAqCJAZ0qg6VcRoCMlWHwVATpRAsZXEWAik7H4KgJMMv0mg/FV"
    "BGhcCRxfRYCGleDxVQRoVAnym45nCDCwyfj0qwjQoJJg6V0QoDElUXwVARpSkuz7zhGgESXob"
    "zqeIcBAJmfxVQRoQEm27ztHgMpK4vgqAlRUEl50XCJA5ybH068iQCXZl94FASogvv8IcDDi+4"
    "4AB+Ki4xoBOjMF2PedI8BBWHpvI8ABiO8+AuyMfd9jBNhR1r9w2YIAjZsCx+fiNg1e7ndxiX2"
    "f8wC93e/iHPE5X4I93u/C8muyzFyAXu93UXHRESDA7Caj24oUAXq+3wX7vgABttCMkPj2CxOg"
    "VoQWpq9noQL0GsSUbN8XOsCREbL0BgtQchL0jpD4AgYorVeExBc4QOn9kHSEHveYlpkL0EOEr"
    "TJfdLgIcHmTrO0JWXoTBbiwEiHxJQ3QQoTWlvBIXASoGSF/4dKXmwAtTMK9uOgIEuDoCNn39e"
    "cuwFEREt8YLgPsHSEXHeO4DdD6npB93zrH08f758E5axOL+JJMQItvuKXX4kGIAK288RZegzd"
    "hAqwIwJ9QAWpGSPz7hAtQIwbi2y9kgCOjIL42YQOsiMO+0AH2jpDA24UPsFcoxCcjRYDSwRCf"
    "nDQBSoVDfLJSBVgRkC3pAmyJkHjlpQwQdqQNcOs0Y/r1kTbALVERX8LbNIyyxOX1fiTepQ9wQ"
    "Ww6Ui/B0EeAUEWAUEWAUEWAUEWAUEWAUEWAUPUyv74ddV8CsqrtMQGhigChigChigChHyAXIh"
    "htaY4JCFUECBsBsgxjlPPWmIBQ9S1ApiB6u2yMCQhVVwEyBdHLrbaYgFB1M0CmIKTda+ruBCR"
    "CSHnU0sMlmAjR6llD7AGh6mmATEHstaadVROQCLHV2mZWL8FEiB6tbNoDEiGkG9l8EUKEkGyj"
    "6V8yI9xtHe1ahlLTxzBMQ8yN/1fe/DkgEeY1CxxqIHoqAktyDrPgaRpdjuUgxJjmDse4dD0Xh"
    "hBjmDueHzTsYCJi9GUedGiV6slYRHkwQfNC8gvpg61YOU5e4wAAAABJRU5ErkJggg=="
)
_LOGO_DATA_URI = "data:image/png;base64," + _LOGO_PNG_BASE64


def render_html_email(*, subject: str, body: str) -> str:
    """Wrap an already-rendered subject and body in the shared HTML shell.

    `body` must already have come out of `render()` — its placeholder values are
    escaped, but the surrounding template text is admin-authored and trusted, the
    same trust boundary `render()` itself relies on. This function only turns
    blank-line breaks into paragraphs and single newlines into `<br>`; it does not
    escape again, because doing so would double-escape every substituted value
    (an "&" a person typed would come back as "&amp;amp;"). `subject` is treated
    the same way — it is what `enqueue()` already rendered and truncated.

    A paragraph that is *only* a URL (every template's `{{accept_url}}` /
    `{{invite_url}}`-style link is written on its own blank-line-separated line,
    see DEFAULT_TEMPLATES) is a link a person is meant to click, not a sentence
    to read — rendered as plain text a long, token-bearing URL wraps across
    several lines at body size and reads as noise. Those paragraphs become a
    button instead, with the raw URL kept underneath in small muted text as a
    fallback for clients that strip links, matching how every real product
    invitation email (e.g. the reference this design was built from) does it.
    """
    paragraphs = [p for p in re.split(r"\n\s*\n", (body or "").strip()) if p]
    parts = []
    for p in paragraphs:
        stripped = p.strip()
        url_match = _BARE_URL.fullmatch(stripped)
        if url_match:
            url = url_match.group(0)
            # `render()` escapes `&`/`<`/`>` in placeholder values but not quotes
            # (`quote=False`) — safe inside the `<p>` text context it was written
            # for, but this is now going inside an `href="..."` attribute, where an
            # unescaped `"` could break out of it. Escape it here, for this context.
            href = url.replace('"', "&quot;")
            parts.append(
                '<table role="presentation" cellpadding="0" cellspacing="0" '
                'style="margin:4px 0 20px;"><tr><td '
                'style="background-color:#12B8A6;border-radius:6px;">'
                f'<a href="{href}" style="display:inline-block;padding:12px 24px;'
                'font-size:15px;font-weight:600;color:#FFFFFF;'
                f'text-decoration:none;">Open secure link →</a>'
                "</td></tr></table>"
                '<p style="margin:0 0 16px;font-size:12px;color:#8A94A6;">'
                "Or paste this link into your browser:<br>"
                f'<a href="{href}" style="color:#12B8A6;word-break:break-all;'
                f'font-size:12px;">{url}</a></p>'
            )
        else:
            parts.append(
                f'<p style="margin:0 0 16px;">{p.replace(chr(10), "<br>")}</p>'
            )
    body_html = "".join(parts)
    return f"""<!DOCTYPE html>
<html>
  <body style="margin:0;padding:0;background-color:#F1F3F5;
      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,
      Helvetica,Arial,sans-serif;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
        style="background-color:#F1F3F5;padding:32px 0;">
      <tr><td align="center">
        <table role="presentation" width="560" cellpadding="0" cellspacing="0"
            style="background-color:#FFFFFF;border-radius:8px;overflow:hidden;
            max-width:560px;width:100%;box-shadow:0 1px 3px rgba(11,31,58,0.08);">
          <!-- Header / Logo -->
          <tr><td style="background-color:#0B1F3A;padding:28px 40px;">
            <table role="presentation" cellpadding="0" cellspacing="0">
              <tr>
                <td style="vertical-align:middle;">
                  <img src="{_LOGO_DATA_URI}" width="32" height="32" alt=""
                      style="display:block;border-radius:8px;">
                </td>
                <td style="vertical-align:middle;padding-left:10px;">
                  <span style="color:#FFFFFF;font-size:18px;font-weight:600;
                      letter-spacing:0.2px;">{_BRAND_NAME}</span>
                </td>
              </tr>
            </table>
          </td></tr>
          <!-- Body -->
          <tr><td style="padding:40px;">
            <p style="margin:0 0 4px 0;font-size:13px;font-weight:600;
                color:#12B8A6;text-transform:uppercase;letter-spacing:0.6px;">
              Notification
            </p>
            <h1 style="margin:0 0 20px 0;font-size:22px;line-height:1.35;
                color:#0B1F3A;font-weight:700;">{subject}</h1>
            <div style="font-size:15px;line-height:1.6;color:#3C4858;">
              {body_html}
            </div>
          </td></tr>
          <!-- Footer -->
          <tr><td style="background-color:#F8F9FB;padding:24px 40px;
              border-top:1px solid #E8EBEF;">
            <p style="margin:0;font-size:12px;color:#8A94A6;">
              This is an automated message from {_BRAND_NAME}. Please do not
              reply to this email.
            </p>
          </td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""


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

    # Nothing found. Before giving up, seed the shipped default for this key if
    # this workspace has never had one.
    #
    # This exists because of a real gap: `seed_default_templates` runs once, when a
    # workspace is created. Adding a new template key later — as the breach module
    # did — left every existing workspace unable to send that message, silently and
    # forever. They suppressed with "no active template", which is honest and
    # useless: nobody was told about a breach and nothing looked broken.
    #
    # Only when NO row exists for the key at all. A template an administrator
    # deliberately deactivated stays deactivated; resurrecting it would override a
    # decision somebody made.
    if channel == "email" and key in DEFAULT_TEMPLATES:
        any_existing = await session.scalar(
            select(NotificationTemplate.id).where(
                NotificationTemplate.tenant_id == tenant_id,
                NotificationTemplate.key == key,
                NotificationTemplate.channel == channel,
            )
        )
        if any_existing is None:
            subject, body = DEFAULT_TEMPLATES[key]
            validate_template(key, subject, body)
            seeded = NotificationTemplate(
                tenant_id=tenant_id, key=key, channel="email", language="English",
                subject=subject, body=body,
            )
            try:
                async with session.begin_nested():
                    session.add(seeded)
                    await session.flush()
            except IntegrityError:
                # Another request seeded it first. Read it back rather than failing.
                return await resolve_template(
                    session, tenant_id=tenant_id, key=key, channel=channel,
                    language=language,
                )
            logger.info(
                "seeded a missing default template on first use",
                extra={"context": {"key": key, "tenant_id": str(tenant_id)}},
            )
            return seeded, "English"

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

    plain_body = body if body is not None else (notification.pending_body or "")
    result = await provider.send(
        to=notification.to_address,
        subject=notification.subject_rendered,
        # The stored body is the default. `body` is an override for the rare
        # caller that has just rendered one; a worker claiming a row off the queue
        # has nothing but what the row carries.
        body=plain_body,
        # Every channel gets the same shell — one branded design, not a plain-text
        # message for some templates and a styled one for others. `None` for SMS,
        # where there is no HTML part.
        html_body=(
            render_html_email(subject=notification.subject_rendered, body=plain_body)
            if notification.channel == "email" else None
        ),
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
    "user.invitation": (
        "You have been invited to {{organisation}} on DataShield",
        "{{organisation}} has invited you to their DataShield workspace as "
        "{{role}}.\n\n"
        "Set up your account here:\n\n    {{accept_url}}\n\n"
        "The link is valid for {{expires_in}} and can be used once. You choose "
        "your own password — nobody at {{organisation}} sets it or can see it.\n\n"
        "If you were not expecting this, you can ignore this email and the "
        "invitation will lapse.\n\n{{organisation}}",
    ),
    "user.password_reset": (
        "Reset your {{organisation}} password",
        "Somebody asked to reset the password for your {{organisation}} "
        "account.\n\n"
        "Set a new one here:\n\n    {{reset_url}}\n\n"
        "The link works once and expires in {{expires_in}}. Asking for another "
        "one immediately invalidates this.\n\n"
        "If this was not you, nothing has changed and you can ignore this "
        "email — but somebody knows your address, so it is worth checking "
        "your account.\n\n{{organisation}}",
    ),
    "connection.failing": (
        "A connection has stopped working: {{connection}}",
        "The {{system}} connection {{connection}} has failed its last "
        "{{failures}} checks. It last worked {{since}}.\n\n"
        "What the system reported:\n\n    {{reason}}\n\n"
        "Why this matters: while it is failing, a rights request cannot reach "
        "the data held in that system. If a request is already open, its "
        "statutory deadline is still running.\n\n"
        "Check the credentials and the network path in Connections, then use "
        "Test to confirm.\n\n{{organisation}}",
    ),
    "breach.principal_notice": (
        "Important: a data security incident affecting your information",
        "We are writing to tell you about a personal data breach that affected "
        "information we hold about you.\n\n"
        "What happened: we became aware of this on {{discovered_on}}. Our internal "
        "reference is {{reference}}.\n\n"
        "What was affected: {{categories}}\n\n"
        "What we have done: {{remediation}}\n\n"
        "You may wish to be alert to unexpected messages referring to this "
        "information. If you have questions, or wish to raise a complaint, you can "
        "contact our Grievance Officer. You may also approach the Data Protection "
        "Board of India.\n\n{{organisation}}",
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
