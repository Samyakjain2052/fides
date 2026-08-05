"""The machinery that makes the public API safe to integrate against.

Three concerns, all of which exist because the network between a customer's
systems and ours is unreliable and their retry logic is not our code:

* **Idempotency** — replay the first response for a repeated key.
* **Rate limiting** — a fixed window counted in Postgres, so the limit is not a
  per-process fiction.
* **Request logging** — append-only, and the rate limiter's counter.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, RateLimited
from app.models.api_key import ApiKey
from app.models.publishable_key import PublishableKey
from app.models.public_api import ApiRequestLog, IdempotencyKey

# How long a stored response stays replayable. Long enough to cover an overnight
# retry queue, short enough that the table does not grow without bound.
IDEMPOTENCY_TTL = timedelta(hours=24)

# Fixed window. Deliberately not a token bucket: a bucket needs either shared
# in-memory state (wrong across replicas) or a Redis round trip on every call,
# and a fixed window in the table we already write is honest about its edges —
# a caller can get 2N requests across a window boundary, which for a
# consent-check API is not worth a second datastore to prevent.
RATE_WINDOW = timedelta(minutes=1)
DEFAULT_RATE_LIMIT = 600  # per key per minute


class IdempotencyConflict(Conflict):
    """Same key, different request body. A client bug, surfaced rather than hidden."""


def request_fingerprint(endpoint: str, body: Any) -> str:
    """Stable hash of endpoint + body.

    `sort_keys` matters: two JSON objects with the same fields in a different
    order are the same request, and treating them as different would reject
    legitimate retries from any client that does not preserve key order.
    """
    payload = json.dumps(body, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(f"{endpoint}\n{payload}".encode()).hexdigest()


async def replay_or_reserve(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    api_key_id: uuid.UUID | None = None,
    publishable_key_id: uuid.UUID | None = None,
    key: str,
    endpoint: str,
    body: Any,
) -> tuple[int, dict[str, Any]] | None:
    """Return the stored response for a repeated key, or None to proceed.

    Raises IdempotencyConflict when the key was seen with a different body: the
    customer has reused a key for a different request, and silently replaying the
    old response would hide a bug that costs them a missing consent record.
    """
    fingerprint = request_fingerprint(endpoint, body)
    now = datetime.now(UTC)

    # Scoped to whichever credential made the call. A publishable key and a secret
    # key are different callers, and a collision between their key spaces would be
    # a confusing cross-talk bug with no cause visible from either side.
    existing = await session.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.tenant_id == tenant_id,
            IdempotencyKey.api_key_id == api_key_id,
            IdempotencyKey.publishable_key_id == publishable_key_id,
            IdempotencyKey.key == key,
        )
    )

    if existing is None:
        return None

    if existing.expires_at <= now:
        # Expired: treat the key as fresh and let the new request overwrite it.
        await session.delete(existing)
        await session.flush()
        return None

    if existing.request_hash != fingerprint:
        raise IdempotencyConflict(
            "This Idempotency-Key was already used for a different request. "
            "Use a new key for a new request."
        )

    return existing.status_code, existing.response_body


async def store_response(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    api_key_id: uuid.UUID | None = None,
    publishable_key_id: uuid.UUID | None = None,
    key: str,
    endpoint: str,
    body: Any,
    status_code: int,
    response_body: dict[str, Any],
) -> None:
    session.add(
        IdempotencyKey(
            tenant_id=tenant_id,
            api_key_id=api_key_id,
            publishable_key_id=publishable_key_id,
            key=key,
            request_hash=request_fingerprint(endpoint, body),
            endpoint=endpoint,
            status_code=status_code,
            response_body=response_body,
            expires_at=datetime.now(UTC) + IDEMPOTENCY_TTL,
        )
    )
    await session.flush()


async def enforce_rate_limit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    api_key: ApiKey,
    limit: int = DEFAULT_RATE_LIMIT,
) -> tuple[int, int]:
    """Count this key's requests in the current window; raise if over.

    Returns (limit, remaining) so the handler can put them in response headers —
    a caller that can see how close it is does not have to discover the limit by
    being refused.
    """
    since = datetime.now(UTC) - RATE_WINDOW
    used = (
        await session.scalar(
            select(func.count())
            .select_from(ApiRequestLog)
            .where(
                ApiRequestLog.api_key_id == api_key.id,
                ApiRequestLog.created_at >= since,
            )
        )
    ) or 0

    if used >= limit:
        raise RateLimited(
            f"Rate limit exceeded: {limit} requests per "
            f"{int(RATE_WINDOW.total_seconds())} seconds for this API key.",
            retry_after=int(RATE_WINDOW.total_seconds()),
        )
    return limit, max(0, limit - used - 1)


async def enforce_publishable_rate_limits(
    session: AsyncSession,
    *,
    key: "PublishableKey",
    ip_hash: str | None,
) -> tuple[int, int]:
    """Two windows, both required: per key and per IP.

    Per key alone would let one abusive client consume a customer's whole
    allowance and take the banner down for everybody. Per IP alone would let a
    distributed caller sail past. The tighter of the two remainders is reported,
    because that is the one the caller will actually hit.

    Both windows count the same `api_request_log` rows the secret-key limiter
    uses — this is the same machinery, not a parallel one, and it compares
    timezone-aware datetimes against `timestamptz` columns (the mismatch that
    made the first version of this query 500).
    """
    since = datetime.now(UTC) - RATE_WINDOW

    used_key = (
        await session.scalar(
            select(func.count())
            .select_from(ApiRequestLog)
            .where(
                ApiRequestLog.publishable_key_id == key.id,
                ApiRequestLog.created_at >= since,
            )
        )
    ) or 0
    if used_key >= key.rate_limit_per_minute:
        raise RateLimited(
            f"Rate limit exceeded for this publishable key: "
            f"{key.rate_limit_per_minute} requests per "
            f"{int(RATE_WINDOW.total_seconds())} seconds.",
            retry_after=int(RATE_WINDOW.total_seconds()),
            scope="key",
        )

    remaining_ip = key.rate_limit_per_ip_per_minute
    if ip_hash:
        used_ip = (
            await session.scalar(
                select(func.count())
                .select_from(ApiRequestLog)
                .where(
                    ApiRequestLog.publishable_key_id == key.id,
                    ApiRequestLog.ip_hash == ip_hash,
                    ApiRequestLog.created_at >= since,
                )
            )
        ) or 0
        if used_ip >= key.rate_limit_per_ip_per_minute:
            raise RateLimited(
                f"Rate limit exceeded for this client: "
                f"{key.rate_limit_per_ip_per_minute} requests per "
                f"{int(RATE_WINDOW.total_seconds())} seconds.",
                retry_after=int(RATE_WINDOW.total_seconds()),
                scope="ip",
            )
        remaining_ip = max(0, key.rate_limit_per_ip_per_minute - used_ip - 1)

    remaining_key = max(0, key.rate_limit_per_minute - used_key - 1)
    if remaining_ip <= remaining_key:
        return key.rate_limit_per_ip_per_minute, remaining_ip
    return key.rate_limit_per_minute, remaining_key


async def log_request(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    api_key_id: uuid.UUID | None = None,
    publishable_key_id: uuid.UUID | None = None,
    method: str,
    path: str,
    status_code: int,
    duration_ms: int,
    ip_address: str | None = None,
    ip_hash: str | None = None,
    user_agent: str | None = None,
    principal_ref: str | None = None,
    purpose_key: str | None = None,
) -> None:
    """Append one row. Bodies are deliberately not stored.

    A request log holding request bodies is a second copy of everyone's personal
    data, with none of the consent machinery around it. The principal reference
    and purpose key are enough to answer "when did you check this person?".
    """
    session.add(
        ApiRequestLog(
            tenant_id=tenant_id,
            api_key_id=api_key_id,
            publishable_key_id=publishable_key_id,
            method=method,
            path=path[:255],
            status_code=status_code,
            duration_ms=duration_ms,
            ip_address=ip_address,
            ip_hash=ip_hash,
            user_agent=(user_agent or "")[:1000] or None,
            principal_ref=principal_ref,
            purpose_key=purpose_key,
        )
    )
    await session.flush()
