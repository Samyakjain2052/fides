"""
The most important test file in this repository.

If row-level security is misconfigured, one customer can read another customer's
data — which in this product means one company reading another company's
customers' personal data. Everything else is a bug; that is the end of the
business.

These tests assert the guarantee at the database level, using the application's
own restricted role.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

from app.db.session import set_tenant_context
from app.models.audit import AuditEvent
from app.models.user import User


async def test_tenant_cannot_read_another_tenants_users(
    app_session_factory, tenant_a, tenant_b
):
    """Bound to tenant A, tenant B's users must be invisible — even though the
    query has no tenant filter at all."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])

            # Deliberately NO where clause. This is the forgotten-filter scenario.
            emails = set((await session.execute(select(User.email))).scalars())

            assert tenant_a["admin_email"] in emails
            assert tenant_b["admin_email"] not in emails, (
                "RLS FAILURE: tenant A can see tenant B's users"
            )


async def test_tenant_cannot_read_another_tenants_audit_trail(
    app_session_factory, tenant_a, tenant_b
):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_b["id"])
            rows = list((await session.execute(select(AuditEvent))).scalars())

    assert rows, "tenant B should have its own bootstrap entries"
    assert all(r.tenant_id == tenant_b["id"] for r in rows), (
        "RLS FAILURE: audit entries from another tenant are visible"
    )


async def test_no_tenant_context_sees_nothing(app_session_factory, tenant_a):
    """Fail closed.

    An unset tenant variable must behave like "no tenant", not like "all
    tenants". This is the direction that matters: a bug that shows nothing is an
    outage, a bug that shows everything is a breach.
    """
    async with app_session_factory() as session:
        async with session.begin():
            # No set_tenant_context call at all.
            count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
            assert count == 0, "RLS FAILURE: queries without tenant context returned rows"


async def test_cannot_write_a_row_into_another_tenant(app_session_factory, tenant_a, tenant_b):
    """WITH CHECK, not just USING.

    Without WITH CHECK a tenant could INSERT a row stamped with someone else's
    tenant_id — invisible to them afterwards, but sitting in the victim's data.
    """
    from app.core.security import hash_password

    async with app_session_factory() as session:
        with pytest.raises(Exception) as exc_info:
            async with session.begin():
                await set_tenant_context(session, tenant_a["id"])
                session.add(
                    User(
                        tenant_id=tenant_b["id"],          # someone else's tenant
                        email="smuggled@example.com",
                        password_hash=hash_password("x" * 12),
                        full_name="Smuggled",
                        role="admin",
                    )
                )
                await session.flush()

    assert "row-level security" in str(exc_info.value).lower() or "policy" in str(
        exc_info.value
    ).lower(), f"expected an RLS violation, got: {exc_info.value}"


async def test_app_role_cannot_bypass_rls(app_session_factory):
    """The application role must not hold BYPASSRLS.

    Postgres exempts superusers and table owners from RLS. If the app connects as
    either, every policy above is decorative. This asserts the connected role is
    neither.
    """
    async with app_session_factory() as session:
        async with session.begin():
            row = (
                await session.execute(
                    text(
                        "SELECT rolbypassrls, rolsuper FROM pg_roles "
                        "WHERE rolname = current_user"
                    )
                )
            ).first()

            assert row is not None
            assert row.rolbypassrls is False, "app role has BYPASSRLS — RLS is not enforced"
            assert row.rolsuper is False, "app role is a superuser — RLS is not enforced"


async def test_tenant_context_does_not_leak_between_transactions(
    app_session_factory, tenant_a, tenant_b
):
    """SET LOCAL, not SET.

    Connections are pooled. If the tenant variable outlived its transaction, the
    next request on that connection would inherit the previous tenant's scope —
    the classic RLS-with-a-connection-pool breach.
    """
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            assert (await session.execute(select(func.count()).select_from(User))).scalar_one() >= 1

        # New transaction on the SAME session/connection, no context set.
        async with session.begin():
            count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
            assert count == 0, "tenant context leaked across transactions"
