"""
Structured JSON logging with a request id, and no personal data.

Two rules that matter for a privacy product:
  * logs are JSON so they are queryable in aggregate, and
  * logs never contain personal data. A log line with an email address in it is
    a copy of personal data outside the consent model, in a system with a
    different retention policy. `scrub()` exists to make the safe path the easy
    one; the audit trail is where identity-linked facts belong.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

# Set by the request-id middleware, read by the formatter, so every line emitted
# while handling a request is correlatable without threading a logger around.
request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
tenant_id_ctx: ContextVar[str | None] = ContextVar("tenant_id", default=None)

_SENSITIVE_KEYS = {
    "password", "password_hash", "token", "token_hash", "refresh_token",
    "api_key", "key_hash", "secret", "authorization", "cookie",
    "email", "phone", "full_name", "aadhaar", "pan", "ip",
}


def scrub(data: Any) -> Any:
    """Recursively redact anything that looks like a credential or personal data."""
    if isinstance(data, dict):
        return {
            k: ("[redacted]" if k.lower() in _SENSITIVE_KEYS else scrub(v))
            for k, v in data.items()
        }
    if isinstance(data, (list, tuple)):
        return [scrub(v) for v in data]
    return data


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = request_id_ctx.get()
        if rid:
            payload["request_id"] = rid
        tid = tenant_id_ctx.get()
        if tid:
            payload["tenant_id"] = tid
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Anything attached via logger.info(..., extra={"context": {...}})
        if hasattr(record, "context"):
            payload["context"] = scrub(record.context)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # uvicorn's access log duplicates our middleware's line, with a raw path and
    # no request id. Ours is better; silence theirs.
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("uvicorn.access").propagate = False
