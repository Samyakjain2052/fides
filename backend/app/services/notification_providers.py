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

    SIGNING
    -------
    Signed with the resource's shared key rather than via
    `azure-communication-email`, which pulls a large dependency tree for one HTTP
    call. The scheme is Azure's standard HMAC one, and it is worth writing down
    because every part of it is load-bearing:

        StringToSign = VERB + "\n" + path?query + "\n" +
                       x-ms-date + ";" + host + ";" + x-ms-content-sha256
        Authorization: HMAC-SHA256 SignedHeaders=<those three>&Signature=<sig>

    The key is base64 and must be decoded before use; signing the base64 text
    itself produces a valid-looking signature that is always rejected. The body
    hash covers the exact bytes sent, so the body is serialised once and both
    hashed and posted — re-serialising could reorder keys and invalidate it.
    """

    name = "azure_acs"

    # The API version the request shape below was written against. Pinned rather
    # than tracking latest: a silently newer version can change required fields.
    API_VERSION = "2023-03-31"

    @staticmethod
    def _sign(
        *, method: str, url: str, body: bytes, access_key: str, date: str
    ) -> dict[str, str]:
        """Build the three headers ACS authenticates on."""
        import base64
        import hashlib
        import hmac
        from urllib.parse import urlparse

        parsed = urlparse(url)
        path_and_query = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        host = parsed.netloc

        content_hash = base64.b64encode(hashlib.sha256(body).digest()).decode()
        string_to_sign = (
            f"{method.upper()}\n{path_and_query}\n{date};{host};{content_hash}"
        )
        signature = base64.b64encode(
            hmac.new(
                base64.b64decode(access_key),
                string_to_sign.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode()

        return {
            "x-ms-date": date,
            "x-ms-content-sha256": content_hash,
            "Authorization": (
                "HMAC-SHA256 SignedHeaders=x-ms-date;host;x-ms-content-sha256"
                f"&Signature={signature}"
            ),
        }

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

        import json
        from email.utils import formatdate

        url = (
            f"{endpoint.rstrip('/')}/emails:send"
            f"?api-version={self.API_VERSION}"
        )
        payload = {
            "senderAddress": sender,
            "recipients": {"to": [{"address": to}]},
            "content": {"subject": subject, "plainText": body},
        }
        # Serialised ONCE. The signature covers these exact bytes, so hashing one
        # serialisation and posting another would fail authentication in a way
        # that looks like a wrong key.
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")

        # RFC 1123 in GMT, which is what ACS expects. `formatdate(usegmt=True)`
        # produces it; a local-timezone stamp is rejected as clock skew.
        headers = {
            "Content-Type": "application/json",
            **self._sign(
                method="POST", url=url, body=raw, access_key=key,
                date=formatdate(timeval=None, localtime=False, usegmt=True),
            ),
        }

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(url, content=raw, headers=headers)
        except Exception as exc:  # noqa: BLE001
            # Transport failure. Might be gone in a second.
            return SendResult(
                ok=False, error=f"{type(exc).__name__}: {exc}", retryable=True
            )

        if resp.status_code in (200, 202):
            # ACS is asynchronous: 202 means accepted for delivery, and the
            # operation id is how a bounce is traced later. Recorded as the
            # provider message id so the delivery log can be reconciled against
            # the ACS side.
            op_id = resp.headers.get("operation-location", "")
            message_id = (
                resp.headers.get("x-ms-request-id")
                or (op_id.rsplit("/", 1)[-1].split("?")[0] if op_id else None)
                or f"acs-{uuid.uuid4().hex[:16]}"
            )
            return SendResult(ok=True, provider_message_id=message_id)

        # 401/403 is a credential or clock problem; 400 is a malformed request or
        # an unverified sender domain. None of those improve by trying again, and
        # retrying a 401 every minute is how a queue turns into a log flood.
        retryable = resp.status_code not in (400, 401, 403, 404)
        detail = resp.text[:300].replace("\n", " ")
        return SendResult(
            ok=False,
            error=f"ACS returned {resp.status_code}: {detail}",
            retryable=retryable,
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
