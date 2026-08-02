"""
Database sessions, and the mechanism that makes tenant isolation real.

The critical function here is `tenant_session()`. It opens a transaction and
issues:

    SET LOCAL app.tenant_id = '<uuid>'

Every tenant-scoped table has an RLS policy reading that variable, so the
database appends the tenant filter to every query whether or not the application
remembered to. Three details make it trustworthy:

1. **SET LOCAL, not SET.** Scoped to the transaction. Connections are pooled and
   reused across requests; a plain SET would leak one tenant's context into the
   next request on that connection. This is the classic RLS-with-a-pool bug.
2. **The app role is not the table owner and has no BYPASSRLS.** Postgres exempts
   superusers and table owners from RLS, so an application connecting as the
   owner has decorative policies. Migrations run as the owner; the app does not.
3. **Fail closed.** `current_setting('app.tenant_id', true)` returns NULL when
   unset, and the policies compare against it, so a query with no tenant context
   matches zero rows rather than everything.
"""

from __future__ import annotations

import asyncio
import ssl
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_settings = get_settings()

TENANT_SETTING = "app.tenant_id"
ACTOR_SETTING = "app.actor_id"

# The engine is created lazily and cached PER EVENT LOOP.
#
# A module-level engine is bound to whichever loop imported it. In production
# there is one loop, so it never matters — but any code path that opens its own
# session (the out-of-band audit writes in auth_service) then fails with
# "Event loop is closed" under test, where each test gets a fresh loop. Keying the
# cache by loop makes the same code correct in both.
_engines: dict[int, AsyncEngine] = {}
_factories: dict[int, async_sessionmaker[AsyncSession]] = {}


def build_ssl_context() -> ssl.SSLContext | None:
    """TLS for asyncpg, from `db_ssl_mode`.

    Returns None for local plaintext. Otherwise an SSLContext, because asyncpg
    takes a context object — not libpq's `sslmode` URL parameter, which it
    rejects outright.

    `require` encrypts but deliberately does not verify the server, which stops
    passive sniffing and NOT an active man-in-the-middle. `verify-full` is the one
    to use against Azure.
    """
    mode = _settings.db_ssl_mode
    if mode == "disable":
        return None
    ctx = ssl.create_default_context(cafile=_settings.db_ssl_root_cert)
    if mode == "require":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _connect_args() -> dict:
    ctx = build_ssl_context()
    args: dict = {
        # Azure's gateway drops idle connections; a server-side timeout keeps a
        # dead socket from being handed to a request.
        "timeout": 10,
        # asyncpg caches prepared statements per connection, which breaks behind
        # PgBouncer in transaction pooling mode — statements outlive the session
        # they were prepared in. Disabling the cache is the standard fix, and we
        # WILL be behind PgBouncer on Azure Flexible Server.
        "statement_cache_size": 0,
    }
    if ctx is not None:
        args["ssl"] = ctx
    return args


def get_engine() -> AsyncEngine:
    loop_key = id(asyncio.get_event_loop())
    if loop_key not in _engines:
        _engines[loop_key] = create_async_engine(
            str(_settings.database_url),
            echo=_settings.db_echo,
            pool_size=_settings.db_pool_size,
            max_overflow=_settings.db_max_overflow,
            pool_pre_ping=True,
            connect_args=_connect_args(),
        )
    return _engines[loop_key]


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    loop_key = id(asyncio.get_event_loop())
    if loop_key not in _factories:
        _factories[loop_key] = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,  # objects stay usable after commit, for serialising
            autoflush=False,         # flush when we say so; keeps audit ordering predictable
        )
    return _factories[loop_key]


async def set_tenant_context(
    session: AsyncSession, tenant_id: uuid.UUID | None, actor_id: uuid.UUID | None = None
) -> None:
    """Bind the transaction to a tenant.

    Uses set_config(..., true) — the `true` is *is_local*, the parameterised
    equivalent of SET LOCAL. Parameterised because the tenant id, though a UUID
    here, must never be string-interpolated into SQL on principle.
    """
    await session.execute(
        text(f"SELECT set_config('{TENANT_SETTING}', :tid, true)"),
        {"tid": str(tenant_id) if tenant_id else ""},
    )
    if actor_id is not None:
        await session.execute(
            text(f"SELECT set_config('{ACTOR_SETTING}', :aid, true)"),
            {"aid": str(actor_id)},
        )


@asynccontextmanager
async def tenant_session(
    tenant_id: uuid.UUID | None, actor_id: uuid.UUID | None = None
) -> AsyncIterator[AsyncSession]:
    """The standard way to touch the database.

    Opens a transaction, binds the tenant context inside it, commits on success
    and rolls back on any exception. Because the context is transaction-local, it
    dies with the transaction — nothing to clean up, nothing to leak.
    """
    async with get_session_factory()() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_id, actor_id)
            yield session


@asynccontextmanager
async def unscoped_session() -> AsyncIterator[AsyncSession]:
    """For the handful of operations that legitimately precede tenant context:
    looking a tenant up by slug, authenticating a user before we know their
    tenant, platform health checks.

    Tables reachable this way are exactly those with no RLS policy (`tenants`) or
    with a policy that permits the lookup. Everything else still returns nothing,
    because fail-closed applies here too.
    """
    async with get_session_factory()() as session:
        async with session.begin():
            yield session


async def dispose_engine() -> None:
    """Close every cached engine. Called on application shutdown."""
    for eng in list(_engines.values()):
        await eng.dispose()
    _engines.clear()
    _factories.clear()
