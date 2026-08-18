"""A small in-process throttle for unauthenticated endpoints.

**Read this before relying on it.** It is a sliding window held in this
process's memory. That means:

* It does not hold across replicas. Two API containers give an attacker twice
  the budget, and a restart resets it.
* It is therefore **not a security boundary** and nothing here should be
  described as one.

It is included anyway because it is the right shape for what it actually
defends against on the routes that use it. The acceptance endpoint's real
protection is the token itself: 256 bits of entropy behind a tenant UUID, which
is not brute-forceable regardless of request rate. What a limiter buys is
protection from a client stuck in a retry loop, and from an attacker filling the
logs faster than anybody can read them.

The durable version is a shared counter — Redis, or the `api_request_log` table
the public API already uses for key-based limits. That is worth doing when there
is more than one replica; doing it now would be a moving part with no current
purpose. `ARCHITECTURE.md` records the same reasoning about the job queue.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque

from app.core.errors import RateLimited

logger = logging.getLogger("app.throttle")

# bucket key -> timestamps of recent hits
_hits: dict[str, deque[float]] = defaultdict(deque)

# Above this many distinct keys we stop tracking new ones rather than grow without
# bound. An attacker rotating source addresses would otherwise turn a defence into
# a memory leak — which is a worse outcome than the requests it was meant to slow.
_MAX_KEYS = 10_000


def check(key: str, *, limit: int, window_seconds: int, what: str = "requests") -> None:
    """Record a hit and raise `RateLimited` if the window is full.

    Prunes as it goes, so an idle key costs nothing after its window passes.
    """
    now = time.monotonic()
    bucket = _hits.get(key)
    if bucket is None:
        if len(_hits) >= _MAX_KEYS:
            logger.warning(
                "throttle key table is full; not tracking new keys",
                extra={"context": {"keys": len(_hits), "limit": _MAX_KEYS}},
            )
            return
        bucket = _hits[key]

    cutoff = now - window_seconds
    while bucket and bucket[0] < cutoff:
        bucket.popleft()

    if len(bucket) >= limit:
        raise RateLimited(
            f"Too many {what}. Try again in {window_seconds} seconds.",
            retry_after=window_seconds,
        )
    bucket.append(now)


def reset() -> None:
    """For tests. Nothing in the application should call this."""
    _hits.clear()
