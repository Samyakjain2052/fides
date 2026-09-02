"""Does this connection actually work?

A stored credential proves nothing. The whole value of this feature is that a
connection reads "connected" only after something really answered, so a probe is
part of the contract rather than a diagnostic afterthought.

Every probe returns a `ProbeResult` and never raises: a failed connection is an
ordinary outcome to be shown to an admin, not a 500. And the message is written
for the person who typed the credentials — "password authentication failed for
user 'x'" is useful; a driver traceback is not.

Sync drivers, run through `asyncio.to_thread`, following the same reasoning
`SmtpProvider` already documents: a probe runs once, on demand, and an async
driver would be a dependency bought for nothing. asyncpg is the exception
because it is already here.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("app.connectors")

#: Kept short on purpose. An admin is watching a spinner, and a probe that hangs
#: for thirty seconds against an unroutable private host is indistinguishable
#: from a broken page.
TIMEOUT_SECONDS = 8.0


@dataclass
class ProbeResult:
    ok: bool
    message: str
    #: Safe, non-sensitive facts worth showing — server version, table count.
    #: Never any row of customer data: a connectivity check has no business
    #: reading personal data, and an admin screen has no business displaying it.
    detail: dict[str, Any] | None = None


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int(value: Any, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


# --------------------------------------------------------------------------- #
# PostgreSQL
# --------------------------------------------------------------------------- #

async def probe_postgresql(config: dict[str, Any]) -> ProbeResult:
    import asyncpg

    host = (config.get("host") or "").strip()
    if not host:
        return ProbeResult(False, "No host given.")

    ssl_arg: Any = "require" if _truthy(config.get("tls", "true")) else False

    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(
                host=host,
                port=_int(config.get("port"), 5432),
                user=(config.get("user") or "").strip() or None,
                password=config.get("password") or None,
                database=(config.get("database") or "").strip() or None,
                ssl=ssl_arg,
                # The suite's engines disable the statement cache for pgbouncer
                # compatibility; a one-shot probe has no use for it either.
                statement_cache_size=0,
            ),
            timeout=TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return ProbeResult(
            False,
            f"Timed out after {TIMEOUT_SECONDS:.0f}s connecting to {host}. If this "
            "database is on a private network, it is not reachable from here yet.",
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(False, _clean(exc))

    try:
        version = await conn.fetchval("SELECT version()")
        tables = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
        )
        return ProbeResult(
            True, "Connected.",
            {"server": (version or "").split(" on ")[0], "tables": tables},
        )
    except Exception as exc:  # noqa: BLE001
        # Connected but could not look around: a real and useful distinction,
        # because it means the credentials are right and the grants are not.
        return ProbeResult(
            False, f"Connected, but could not read the schema: {_clean(exc)}"
        )
    finally:
        await conn.close()


# --------------------------------------------------------------------------- #
# MySQL
# --------------------------------------------------------------------------- #

async def probe_mysql(config: dict[str, Any]) -> ProbeResult:
    import pymysql

    host = (config.get("host") or "").strip()
    if not host:
        return ProbeResult(False, "No host given.")

    def _run() -> ProbeResult:
        kwargs: dict[str, Any] = {
            "host": host,
            "port": _int(config.get("port"), 3306),
            "user": (config.get("user") or "").strip() or None,
            "password": config.get("password") or "",
            "database": (config.get("database") or "").strip() or None,
            "connect_timeout": int(TIMEOUT_SECONDS),
            "read_timeout": int(TIMEOUT_SECONDS),
        }
        if _truthy(config.get("tls", "true")):
            # pymysql enables TLS when given any ssl mapping.
            kwargs["ssl"] = {}
        conn = pymysql.connect(**kwargs)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION()")
                version = (cur.fetchone() or ["unknown"])[0]
                cur.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = DATABASE()"
                )
                tables = (cur.fetchone() or [0])[0]
            return ProbeResult(True, "Connected.",
                               {"server": f"MySQL {version}", "tables": tables})
        finally:
            conn.close()

    try:
        return await asyncio.wait_for(asyncio.to_thread(_run),
                                      timeout=TIMEOUT_SECONDS + 2)
    except TimeoutError:
        return ProbeResult(
            False,
            f"Timed out after {TIMEOUT_SECONDS:.0f}s connecting to {host}. If this "
            "database is on a private network, it is not reachable from here yet.",
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(False, _clean(exc))


# --------------------------------------------------------------------------- #
# MongoDB
# --------------------------------------------------------------------------- #

async def probe_mongodb(config: dict[str, Any]) -> ProbeResult:
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError

    host = (config.get("host") or "").strip()
    if not host:
        return ProbeResult(False, "No host given.")

    def _run() -> ProbeResult:
        ms = int(TIMEOUT_SECONDS * 1000)
        kwargs: dict[str, Any] = {
            "host": host,
            "port": _int(config.get("port"), 27017),
            "serverSelectionTimeoutMS": ms,
            "connectTimeoutMS": ms,
            "socketTimeoutMS": ms,
        }
        user = (config.get("user") or "").strip()
        if user:
            kwargs["username"] = user
            kwargs["password"] = config.get("password") or ""
            kwargs["authSource"] = (config.get("auth_source") or "admin").strip()
        if _truthy(config.get("tls", "true")):
            kwargs["tls"] = True

        client = MongoClient(**kwargs)
        try:
            # `ping` is the cheapest thing that proves auth and reachability;
            # anything heavier would be reading a customer's data to answer
            # "can we connect".
            info = client.admin.command("ping")
            if not info.get("ok"):
                return ProbeResult(False, "Server did not acknowledge a ping.")
            build = client.server_info().get("version", "unknown")
            db_name = (config.get("database") or "").strip()
            detail: dict[str, Any] = {"server": f"MongoDB {build}"}

            # Ping succeeded, so the server is reachable. Listing collections is
            # a separate permission, and failing it is reported as a failure
            # rather than a warning — for the same reason the PostgreSQL probe
            # does: a connector that cannot see the schema cannot fulfil a
            # DSAR, so "connected" would overstate what this connection can do.
            if db_name:
                try:
                    detail["collections"] = len(
                        client[db_name].list_collection_names()
                    )
                except PyMongoError as exc:
                    return ProbeResult(
                        False,
                        "Reached the server, but could not list collections in "
                        f"{db_name!r}: {_clean(exc)}. If this server requires "
                        "authentication, fill in the username and password.",
                        detail,
                    )
            return ProbeResult(True, "Connected.", detail)
        except PyMongoError as exc:
            return ProbeResult(False, _clean(exc))
        finally:
            client.close()

    try:
        return await asyncio.wait_for(asyncio.to_thread(_run),
                                      timeout=TIMEOUT_SECONDS + 2)
    except TimeoutError:
        return ProbeResult(
            False,
            f"Timed out after {TIMEOUT_SECONDS:.0f}s connecting to {host}. If this "
            "database is on a private network, it is not reachable from here yet.",
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(False, _clean(exc))


# --------------------------------------------------------------------------- #

PROBES = {
    "postgresql": probe_postgresql,
    "mysql": probe_mysql,
    "mongodb": probe_mongodb,
}


def _clean(exc: Exception) -> str:
    """A driver error, made fit to show an admin.

    Truncated because driver messages sometimes echo the connection string, and a
    connection string contains the password. Truncation is a blunt guard; the
    scrub below is the real one.
    """
    text = str(exc).strip() or type(exc).__name__
    for marker in ("password=", "pwd=", "Authentication failed for user"):
        if marker in text:
            head, _, _ = text.partition(marker)
            text = f"{head}{marker}…"
            break
    return text[:300]


async def run(connector_id: str, config: dict[str, Any]) -> ProbeResult:
    """Probe a connection, or say plainly that this connector cannot be probed."""
    probe = PROBES.get(connector_id)
    if probe is None:
        return ProbeResult(
            False,
            "No connection test exists for this connector yet, so there is "
            "nothing to verify. Credentials are stored but unproven.",
        )
    try:
        return await probe(config)
    except Exception as exc:  # noqa: BLE001
        # A probe must never take a request down.
        logger.exception("probe raised", extra={"context": {"connector": connector_id}})
        return ProbeResult(False, f"The test itself failed: {type(exc).__name__}")
