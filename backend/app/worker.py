"""The scheduler process.

A separate process, not a thread inside the API. Two reasons:

* With N API replicas a thread would run N times. The advisory lock would stop the
  duplicate work, but every replica would still be waking up to contend for it.
* Background work would compete with request serving for the same event loop, so a
  large notification backlog would show up as slow page loads.

Run it with `python -m app.worker`. It ticks until killed, and it is safe to run
exactly one of.
"""

from __future__ import annotations

import asyncio
import logging

from app.core.logging import configure_logging
from app.services import scheduler


def main() -> None:
    configure_logging()
    logging.getLogger("app.worker").info("worker process starting")
    try:
        asyncio.run(scheduler.run_forever())
    except KeyboardInterrupt:
        logging.getLogger("app.worker").info("worker stopped")


if __name__ == "__main__":
    main()
