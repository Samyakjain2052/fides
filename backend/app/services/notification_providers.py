"""Where a message actually goes.

One interface, three implementations, and the choice is configuration rather than
code — so picking an email vendor is a `.env` decision, not a refactor.

**`ConsoleProvider` is the default outside production, and that is a safety
property, not a convenience.** Local development and the test suite must not be
able to email a real person: a developer running a grievance-escalation test
should not be able to send a stranger a message about their data. The provider is
selected by config, and `Settings` refuses a `console` provider in `prod`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.core.config import get_settings

logger = logging.getLogger("app.notifications")
_settings = get_settings()


@dataclass
class SendResult:
    ok: bool
    provider_message_id: str | None = None
    error: str | None = None
    # Whether trying again could plausibly work. A mailbox that does not exist
    # will never exist; a 503 might be gone in a second. Retrying the first
    # forever is how a queue fills up with garbage and real messages starve.
    retryable: bool = True


class NotificationProvider(Protocol):
    name: str

    async def send(
        self, *, to: str, subject: str, body: str, channel: str
    ) -> SendResult: ...


class ConsoleProvider:
    """Writes to the log instead of sending. The default everywhere but prod.

    Returns a synthetic message id so the rest of the pipeline — status
    transitions, the delivery log, the idempotency constraint — is exercised
    exactly as it would be with a real provider. A no-op that skipped those would
    leave the interesting half of this module untested.
    """

    name = "console"

    async def send(self, *, to: str, subject: str, body: str, channel: str) -> SendResult:
        logger.info(
            "notification (console provider — NOT sent)",
            extra={
                "context": {
                    "channel": channel,
                    "to": to,
                    "subject": subject,
                    # The FULL body, not a preview. This provider exists so a
                    # developer can see what would have been sent, and some
                    # messages carry something they need — a grievance
                    # confirmation code sits past the 200-character mark, so
                    # truncating made the public filing flow impossible to
                    # complete locally.
                    #
                    # Logged here and nowhere persistent: the delivery log
                    # deliberately keeps no bodies, and this provider is refused
                    # in production.
                    "body": body,
                }
            },
        )
        return SendResult(ok=True, provider_message_id=f"console-{uuid.uuid4().hex[:16]}")


class AzureCommunicationEmailProvider:
    """Azure Communication Services Email.

    Chosen as the reference implementation because the rest of the platform is
    Azure — one vendor, one bill, one support relationship. Swapping it for SES or
    Resend means writing a sibling class and changing one config value.

    Inert without credentials: rather than throwing at import or startup, it
    reports a non-retryable failure per message so the reason lands on the row a
    DPO is looking at instead of in a log nobody reads.
    """

    name = "azure_acs"

    async def send(self, *, to: str, subject: str, body: str, channel: str) -> SendResult:
        if channel != "email":
            return SendResult(
                ok=False,
                error="This provider sends email only; SMS is not configured.",
                retryable=False,
            )
        endpoint = _settings.acs_endpoint
        key = _settings.acs_access_key
        sender = _settings.notification_from_address
        if not (endpoint and key and sender):
            return SendResult(
                ok=False,
                error=(
                    "Azure Communication Services is selected but not configured "
                    "(DS_ACS_ENDPOINT, DS_ACS_ACCESS_KEY, "
                    "DS_NOTIFICATION_FROM_ADDRESS)."
                ),
                retryable=False,
            )

        # NOTE: ACS uses HMAC request signing. Implemented here rather than
        # pulling in the azure-communication-email SDK, which brings a large
        # dependency tree for one call. TODO(acs): sign with the shared key —
        # this raises a clear, non-retryable error until that is done, instead of
        # silently appearing to send.
        return SendResult(
            ok=False,
            error=(
                "The Azure ACS provider is not finished: request signing is not "
                "implemented. Configure a provider that is, or complete "
                "TODO(acs) in notification_providers.py."
            ),
            retryable=False,
        )


class SmtpProvider:
    """A generic SMTP relay, for anyone who already has one.

    Useful precisely because it needs no vendor decision: a customer with an
    existing relay can be sending today.
    """

    name = "smtp"

    async def send(self, *, to: str, subject: str, body: str, channel: str) -> SendResult:
        if channel != "email":
            return SendResult(ok=False, error="SMTP sends email only.", retryable=False)
        host = _settings.smtp_host
        sender = _settings.notification_from_address
        if not (host and sender):
            return SendResult(
                ok=False,
                error="SMTP is selected but DS_SMTP_HOST / "
                      "DS_NOTIFICATION_FROM_ADDRESS are not set.",
                retryable=False,
            )

        import email.message
        import smtplib

        msg = email.message.EmailMessage()
        msg["From"] = sender
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)

        try:
            # Blocking, and deliberately so: this runs in a worker processing one
            # message at a time, and an async SMTP client is a dependency this
            # does not need. If throughput ever matters, that is the moment to
            # revisit — not now.
            import asyncio

            def _send() -> None:
                with smtplib.SMTP(host, _settings.smtp_port, timeout=15) as s:
                    if _settings.smtp_use_tls:
                        s.starttls()
                    if _settings.smtp_username:
                        s.login(_settings.smtp_username, _settings.smtp_password or "")
                    s.send_message(msg)

            await asyncio.to_thread(_send)
        except smtplib.SMTPRecipientsRefused as exc:
            # The mailbox does not exist. Retrying will not change that.
            return SendResult(ok=False, error=f"Recipient refused: {exc}", retryable=False)
        except Exception as exc:  # noqa: BLE001
            return SendResult(ok=False, error=f"{type(exc).__name__}: {exc}", retryable=True)

        return SendResult(ok=True, provider_message_id=f"smtp-{uuid.uuid4().hex[:16]}")


_PROVIDERS: dict[str, type] = {
    "console": ConsoleProvider,
    "azure_acs": AzureCommunicationEmailProvider,
    "smtp": SmtpProvider,
}


def get_provider() -> NotificationProvider:
    """The configured provider, defaulting to console.

    An unknown name falls back to console rather than raising: refusing to boot
    because of a typo in one setting would take the whole product down over a
    feature that can safely degrade to "logged, not sent" — and the log says
    loudly which provider is in use.
    """
    name = (_settings.notification_provider or "console").strip().lower()
    cls = _PROVIDERS.get(name)
    if cls is None:
        logger.error(
            "unknown notification provider; falling back to console",
            extra={"context": {"configured": name, "known": sorted(_PROVIDERS)}},
        )
        cls = ConsoleProvider
    return cls()
