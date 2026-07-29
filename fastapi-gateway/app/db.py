"""
Direct connections to the two APPLICATION databases (app-postgres, app-mongo).

This is the one part of the gateway that does NOT go through Fides. It exists so
you can create a data subject on demand — insert a person into both databases in
one call — and then immediately DSAR them. Fides never writes data; it only
reads and masks.

Deliberately the same credentials the Fides ConnectionConfigs use (see
fides-config/connections/connections.yml), so if an insert works here, Fides can
reach that row too.
"""

from __future__ import annotations

import asyncio
import logging
import os
from decimal import Decimal
from typing import Any

import asyncpg
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger("gateway.db")


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    """Make a database row JSON-safe.

    Mongo hands back BSON `ObjectId`s and Postgres hands back `Decimal`s; both
    are stringified rather than coerced to float, which matches how the same
    values appear in a Fides access package (`"amount": "49.99"`) so the two
    views line up field for field. Nested dicts (events.metadata) recurse.
    """
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, ObjectId):
            out[key] = str(value)
        elif isinstance(value, Decimal):
            out[key] = str(value)
        elif isinstance(value, dict):
            out[key] = _clean(value)
        else:
            out[key] = value
    return out


class AppDatabases:
    """Lazy connections to app-postgres and app-mongo.

    Both are opened on first use rather than at startup: a database being down
    should fail the one request that needs it, not stop the gateway from booting
    and serving /health and /dsar.
    """

    def __init__(self) -> None:
        # Kept as attributes so /data/subject/{email} can report *where* each
        # record lives, not just that it exists.
        self.pg_host = (
            f"{os.environ.get('APP_POSTGRES_HOST', 'app-postgres')}:"
            f"{os.environ.get('APP_POSTGRES_PORT', '5432')}"
        )
        self.pg_database = os.environ.get("APP_POSTGRES_DB", "appdb")
        self.mongo_host = (
            f"{os.environ.get('APP_MONGO_HOST', 'app-mongo')}:"
            f"{os.environ.get('APP_MONGO_PORT', '27017')}"
        )

        self._pg_dsn = (
            f"postgresql://{os.environ.get('APP_POSTGRES_USER', 'appuser')}:"
            f"{os.environ.get('APP_POSTGRES_PASSWORD', 'apppassword')}@"
            f"{os.environ.get('APP_POSTGRES_HOST', 'app-postgres')}:"
            f"{os.environ.get('APP_POSTGRES_PORT', '5432')}/"
            f"{os.environ.get('APP_POSTGRES_DB', 'appdb')}"
        )

        # NOTE: APP_MONGO_DB is `app_mongo_dataset`, not `appdb` — Fides' mongodb
        # connector queries the database named after the dataset's fides_key.
        # See the header of fides-config/resources/app_mongo_dataset.yml.
        self.mongo_database = os.environ.get("APP_MONGO_DB", "app_mongo_dataset")
        self._mongo_db_name = self.mongo_database
        self._mongo_uri = (
            f"mongodb://{os.environ.get('APP_MONGO_USER', 'mongouser')}:"
            f"{os.environ.get('APP_MONGO_PASSWORD', 'mongopassword')}@"
            f"{os.environ.get('APP_MONGO_HOST', 'app-mongo')}:"
            f"{os.environ.get('APP_MONGO_PORT', '27017')}/"
            # The database in the path doubles as the auth source. The app user
            # lives inside this database, not in `admin`, so both must be it.
            f"{self._mongo_db_name}"
        )

        self._pool: asyncpg.Pool | None = None
        self._mongo: AsyncIOMotorClient | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- lifecycle --
    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
        if self._mongo is not None:
            self._mongo.close()

    async def _pg(self) -> asyncpg.Pool:
        async with self._lock:
            if self._pool is None:
                logger.info("opening app-postgres pool")
                self._pool = await asyncpg.create_pool(
                    self._pg_dsn, min_size=1, max_size=5, command_timeout=30
                )
        return self._pool

    def _events(self) -> Any:
        if self._mongo is None:
            logger.info("opening app-mongo client (db=%s)", self._mongo_db_name)
            self._mongo = AsyncIOMotorClient(
                self._mongo_uri, serverSelectionTimeoutMS=10_000
            )
        return self._mongo[self._mongo_db_name]["events"]

    # ---------------------------------------------------------- app-postgres --
    async def upsert_user(
        self, email: str, full_name: str | None, phone: str | None
    ) -> tuple[int, str]:
        """Ensure a `users` row exists for this email.

        Returns (id, "inserted" | "updated").

        Matching on email means a row whose email was NULLed by a previous
        erasure is NOT found, so re-adding an erased subject inserts a fresh row
        and leaves the anonymised one behind — which is the honest outcome: the
        erasure is not undone, a new person record is created.
        """
        pool = await self._pg()
        async with pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchval(
                    "SELECT id FROM users WHERE email = $1 ORDER BY id LIMIT 1", email
                )
                if existing is not None:
                    # COALESCE so omitting a field leaves the stored value alone
                    # rather than wiping it.
                    await conn.execute(
                        """
                        UPDATE users
                           SET full_name = COALESCE($2, full_name),
                               phone     = COALESCE($3, phone)
                         WHERE id = $1
                        """,
                        existing,
                        full_name,
                        phone,
                    )
                    return existing, "updated"

                new_id = await conn.fetchval(
                    """
                    INSERT INTO users (email, full_name, phone)
                    VALUES ($1, $2, $3)
                    RETURNING id
                    """,
                    email,
                    full_name,
                    phone,
                )
                return new_id, "inserted"

    async def insert_orders(
        self, email: str, orders: list[tuple[Decimal, str]]
    ) -> list[int]:
        """Append `orders` rows. `orders` is denormalised on the email, exactly
        as the dataset declares (`user_email` is its own identity entrypoint)."""
        if not orders:
            return []
        pool = await self._pg()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                INSERT INTO orders (user_email, amount, item)
                SELECT $1, x.amount, x.item
                  FROM UNNEST($2::numeric[], $3::text[]) AS x(amount, item)
                RETURNING id
                """,
                email,
                [amount for amount, _ in orders],
                [item for _, item in orders],
            )
        return [r["id"] for r in rows]

    # ------------------------------------------------------------- app-mongo --
    async def insert_events(self, email: str, events: list[dict[str, Any]]) -> int:
        """Append documents to `events`.

        Shape must match fides-config/resources/app_mongo_dataset.yml — Fides
        only reads and masks fields declared there, so an undeclared field would
        be invisible to a DSAR and survive an erasure.
        """
        if not events:
            return 0
        result = await self._events().insert_many(events)
        return len(result.inserted_ids)

    # ------------------------------------------------------------- lookups --
    # Read-only; used by GET /data/subject/{email} to answer "where is my data?"
    # without going through Fides. Fides' own answer to that question is an
    # access DSAR — this is the raw view you can check it against.
    #
    # MAX_ROWS caps each collection so a fat subject cannot blow up the response.
    MAX_ROWS = 500

    async def find_in_postgres(self, email: str) -> dict[str, list[dict[str, Any]]]:
        """Rows in app-postgres whose email matches, per table."""
        pool = await self._pg()
        async with pool.acquire() as conn:
            users = await conn.fetch(
                """
                SELECT id, email, full_name, phone, created_at
                  FROM users
                 WHERE email = $1
                 ORDER BY id
                 LIMIT $2
                """,
                email,
                self.MAX_ROWS,
            )
            orders = await conn.fetch(
                """
                SELECT id, user_email, amount, item, created_at
                  FROM orders
                 WHERE user_email = $1
                 ORDER BY id
                 LIMIT $2
                """,
                email,
                self.MAX_ROWS,
            )
        return {
            "users": [_clean(dict(r)) for r in users],
            "orders": [_clean(dict(r)) for r in orders],
        }

    async def find_in_mongo(self, email: str) -> dict[str, list[dict[str, Any]]]:
        """Documents in app-mongo whose email matches."""
        cursor = self._events().find({"email": email}).limit(self.MAX_ROWS)
        events = await cursor.to_list(length=self.MAX_ROWS)
        return {"events": [_clean(doc) for doc in events]}

    async def count_masked(self, email: str) -> dict[str, int]:
        """Rows whose identifying email is NULL — i.e. previously erased.

        Matching on email can never find these (that is the point of the
        erasure), so without this count a lookup after an erasure looks
        identical to a lookup for someone who was never in the system. The
        `email` argument is unused; masked rows are by definition no longer
        attributable to anyone.
        """
        pool = await self._pg()
        async with pool.acquire() as conn:
            users = await conn.fetchval("SELECT count(*) FROM users WHERE email IS NULL")
            orders = await conn.fetchval(
                "SELECT count(*) FROM orders WHERE user_email IS NULL"
            )
        events = await self._events().count_documents({"email": None})
        return {"users": users or 0, "orders": orders or 0, "events": events}

    # ----------------------------------------------------------------- checks --
    async def ping(self) -> dict[str, str]:
        """Used by /health so a broken app-database surfaces there, not mid-POST."""
        status = {}
        try:
            pool = await self._pg()
            await pool.fetchval("SELECT 1")
            status["app_postgres"] = "ok"
        except Exception as exc:  # noqa: BLE001 - report, never raise, from health
            status["app_postgres"] = f"unreachable: {type(exc).__name__}"
        try:
            await self._events().database.command("ping")
            status["app_mongo"] = "ok"
        except Exception as exc:  # noqa: BLE001
            status["app_mongo"] = f"unreachable: {type(exc).__name__}"
        return status
