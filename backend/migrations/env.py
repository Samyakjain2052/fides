"""
Alembic environment.

Runs migrations with the OWNER url, never the application url: the owner creates
tables and RLS policies, the application merely uses them. Keeping the two apart
is what makes row-level security meaningful — Postgres exempts table owners from
RLS, so an app connecting as owner would bypass every policy.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.db.base import Base

# Importing the models registers them on Base.metadata for autogenerate.
from app import models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
# asyncpg for the app, but Alembic drives it through SQLAlchemy's async engine.
config.set_main_option("sqlalchemy.url", settings.owner_url.replace("%", "%%"))

target_metadata = Base.metadata


def _run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,          # catch column type changes
        compare_server_default=True,
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    # Alembic builds its own engine, so it needs the same TLS treatment as the
    # app — otherwise migrations fail against Azure while the app connects fine.
    from app.db.session import build_ssl_context

    connect_args: dict = {"statement_cache_size": 0}
    ssl_ctx = build_ssl_context()
    if ssl_ctx is not None:
        connect_args["ssl"] = ssl_ctx

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_run_migrations)
    await connectable.dispose()


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(_run_async())
