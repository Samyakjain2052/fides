"""
Test fixtures.

Tests run against a REAL PostgreSQL. Not SQLite, not a mock — the three things
this backend's security rests on (row-level security, an append-only trigger,
advisory locks) do not exist outside Postgres. A suite that stubs the database
would pass while the product leaked.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

# Set before importing app modules: config is read at import time, and the real
# secrets must never be needed to run tests.
os.environ.setdefault("DS_ENV", "test")
os.environ.setdefault("DS_JWT_SECRET", "t" * 40)
os.environ.setdefault("DS_AUDIT_HMAC_KEY", "a" * 40)
os.environ.setdefault("DS_COOKIE_SECURE", "false")
# Argon2 turned down so the suite runs in seconds, not minutes. Production cost
# comes from config, so this cannot leak into a deployment.
os.environ.setdefault("DS_ARGON2_TIME_COST", "1")
os.environ.setdefault("DS_ARGON2_MEMORY_COST", "8192")
os.environ.setdefault("DS_ARGON2_PARALLELISM", "1")

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

APP_URL = os.environ["DS_DATABASE_URL"]
OWNER_URL = os.environ.get("DS_DATABASE_OWNER_URL", APP_URL)


@pytest.fixture
async def owner_engine():
    """Owner connection — used only to clean up between tests.

    The owner can see across tenants (tables are FORCE RLS, but the owner sets the
    tenant variable itself), which is exactly why the application never uses this
    role.
    """
    eng = create_async_engine(OWNER_URL, poolclass=None)
    yield eng
    await eng.dispose()


@pytest.fixture
async def app_engine():
    """The application's restricted connection. RLS applies to this one."""
    eng = create_async_engine(APP_URL)
    yield eng
    await eng.dispose()


@pytest.fixture(autouse=True)
async def clean_db(owner_engine):
    """Truncate before each test.

    `audit_events` cannot be DELETEd (that is the point), so cleanup runs as the
    owner with the trigger temporarily disabled — a privilege the application
    does not have and a test asserts it does not have.
    """
    async with owner_engine.begin() as conn:
        await conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_no_update_delete"))
        await conn.execute(text(
                "TRUNCATE audit_events, refresh_tokens, api_keys, "
                "purge_run_items, purge_runs, retention_policies, "
                "dsar_events, dsar_requests, "
                "idempotency_keys, api_request_log, consent_provenance, "
                "publishable_keys, consents, notices, data_principals, purposes, "
                "users, tenants CASCADE"
            ))
        await conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_no_update_delete"))
    yield


@pytest.fixture
async def app_session_factory(app_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(app_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def tenant_a(app_session_factory) -> AsyncIterator[dict]:
    async for t in _make_tenant(app_session_factory, "tenant-a", "Tenant A"):
        yield t


@pytest.fixture
async def tenant_b(app_session_factory) -> AsyncIterator[dict]:
    async for t in _make_tenant(app_session_factory, "tenant-b", "Tenant B"):
        yield t


async def _make_tenant(factory, slug: str, name: str):
    """Create a tenant and COMMIT before yielding.

    Committing first is not a detail. Yielding inside an open transaction would:
      * hide the rows from every other connection (uncommitted), so isolation
        tests would pass for the wrong reason, and
      * hold the tenant's audit advisory lock for the whole test, deadlocking any
        test that writes an audit entry for that tenant.
    Both bit this suite before the fixture was fixed.
    """
    from app.services import tenant_service

    async with factory() as session:
        async with session.begin():
            tenant, admin = await tenant_service.create_tenant(
                session,
                slug=slug,
                name=name,
                admin_email=f"admin@{slug}.example.com",
                admin_password="correct-horse-battery-staple",
                admin_name=f"{name} Admin",
            )
            info = {
                "id": tenant.id,
                "slug": slug,
                "admin_id": admin.id,
                "admin_email": admin.email,
                "password": "correct-horse-battery-staple",
            }
        # transaction committed here, lock released
    yield info


@pytest.fixture
def unique_slug() -> str:
    return f"t-{uuid.uuid4().hex[:10]}"
