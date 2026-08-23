"""Authentication tests: rotation, reuse detection, lockout, enumeration resistance."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.errors import AuthenticationError
from app.db.session import set_tenant_context
from app.models.audit import AuditAction, AuditEvent
from app.models.user import RefreshToken
from app.services import auth_service


async def test_login_succeeds_and_issues_a_pair(app_session_factory, tenant_a):
    async with app_session_factory() as session:
        async with session.begin():
            pair = await auth_service.authenticate(
                session, tenant_slug=tenant_a["slug"],
                email=tenant_a["admin_email"], password=tenant_a["password"],
            )
    assert pair.access_token
    assert pair.refresh_token
    assert pair.user.email == tenant_a["admin_email"]


async def test_wrong_password_is_indistinguishable_from_unknown_email(
    app_session_factory, tenant_a
):
    """Both failures must produce the same message.

    A different error for "no such user" turns the login form into a
    user-enumeration oracle, and "this person has an account with this company" is
    itself personal data.
    """
    messages = []
    for email, password in [
        (tenant_a["admin_email"], "wrong-password-entirely"),
        ("nobody@nowhere.example.com", tenant_a["password"]),
    ]:
        async with app_session_factory() as session:
            with pytest.raises(AuthenticationError) as exc:
                async with session.begin():
                    await auth_service.authenticate(
                        session, tenant_slug=tenant_a["slug"], email=email, password=password
                    )
            messages.append(str(exc.value))

    assert messages[0] == messages[1], f"login leaks which emails exist: {messages}"


async def test_unknown_tenant_also_gives_the_same_error(app_session_factory, tenant_a):
    async with app_session_factory() as session:
        with pytest.raises(AuthenticationError) as exc:
            async with session.begin():
                await auth_service.authenticate(
                    session, tenant_slug="no-such-tenant",
                    email=tenant_a["admin_email"], password=tenant_a["password"],
                )
    assert "incorrect" in str(exc.value).lower()


async def test_refresh_rotates_and_consumes_the_old_token(app_session_factory, tenant_a):
    async with app_session_factory() as session:
        async with session.begin():
            first = await auth_service.authenticate(
                session, tenant_slug=tenant_a["slug"],
                email=tenant_a["admin_email"], password=tenant_a["password"],
            )
        async with session.begin():
            second = await auth_service.refresh(session, raw_token=first.refresh_token)

    assert second.refresh_token != first.refresh_token, "refresh token was not rotated"

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            rows = list((await session.execute(select(RefreshToken))).scalars())

    used = [r for r in rows if r.used_at is not None]
    assert len(used) == 1, "the presented token should be marked used exactly once"
    # Same family: rotation is a continuation of one session, not a new one.
    assert len({r.family_id for r in rows}) == 1


async def test_reusing_a_spent_token_revokes_the_whole_family(app_session_factory, tenant_a):
    """The critical one.

    A spent token presented again means two parties hold it. We cannot tell which
    is the legitimate user, so we trust neither: the whole lineage dies and both
    have to sign in again.
    """
    async with app_session_factory() as session:
        async with session.begin():
            first = await auth_service.authenticate(
                session, tenant_slug=tenant_a["slug"],
                email=tenant_a["admin_email"], password=tenant_a["password"],
            )
        async with session.begin():
            second = await auth_service.refresh(session, raw_token=first.refresh_token)

        # Attacker replays the stolen, already-spent token.
        with pytest.raises(AuthenticationError):
            async with session.begin():
                await auth_service.refresh(session, raw_token=first.refresh_token)

        # The legitimate user's newer token is dead too — deliberately.
        with pytest.raises(AuthenticationError):
            async with session.begin():
                await auth_service.refresh(session, raw_token=second.refresh_token)

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            rows = list((await session.execute(select(RefreshToken))).scalars())
            events = list(
                (
                    await session.execute(
                        select(AuditEvent).where(
                            AuditEvent.action == AuditAction.TOKEN_REUSE_DETECTED
                        )
                    )
                ).scalars()
            )

    assert all(r.revoked_at is not None for r in rows), "family was not fully revoked"
    assert len(events) == 1, "token reuse must be recorded in the audit trail"


async def test_lockout_after_repeated_failures(app_session_factory, tenant_a):
    from app.core.config import get_settings

    limit = get_settings().max_failed_logins

    for _ in range(limit):
        async with app_session_factory() as session:
            with pytest.raises(AuthenticationError):
                async with session.begin():
                    await auth_service.authenticate(
                        session, tenant_slug=tenant_a["slug"],
                        email=tenant_a["admin_email"], password="wrong-password-here",
                    )

    # Even the correct password is refused now, and the message says why.
    async with app_session_factory() as session:
        with pytest.raises(AuthenticationError) as exc:
            async with session.begin():
                await auth_service.authenticate(
                    session, tenant_slug=tenant_a["slug"],
                    email=tenant_a["admin_email"], password=tenant_a["password"],
                )
    assert "locked" in str(exc.value).lower()


async def test_failed_logins_never_record_the_password(app_session_factory, tenant_a):
    async with app_session_factory() as session:
        with pytest.raises(AuthenticationError):
            async with session.begin():
                await auth_service.authenticate(
                    session, tenant_slug=tenant_a["slug"],
                    email=tenant_a["admin_email"], password="hunter2-secret-value",
                )

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            rows = list(
                (
                    await session.execute(
                        select(AuditEvent).where(AuditEvent.action == AuditAction.LOGIN_FAILED)
                    )
                ).scalars()
            )

    assert rows, "a failed login must be recorded"
    blob = str([r.payload for r in rows])
    assert "hunter2" not in blob, "the attempted password leaked into the audit trail"


# --------------------------------------------------------------------------- #
# The session says which organisation it belongs to.
#
# Nothing carried this before: `UserOut` had a `tenant_id` and the login form
# knew the slug that was typed, but the organisation's display name existed only
# server-side. So the user-facing header showed a hardcoded
# "Example Fintech Pvt. Ltd." to every customer, and the footer named an invented
# Grievance Officer — a §13 statutory contact nobody could actually reach.
# --------------------------------------------------------------------------- #

async def test_authenticate_returns_the_tenant(app_session_factory, tenant_a):
    async with app_session_factory() as session:
        async with session.begin():
            pair = await auth_service.authenticate(
                session, tenant_slug=tenant_a["slug"],
                email=tenant_a["admin_email"], password=tenant_a["password"],
            )
    assert pair.tenant.id == tenant_a["id"]
    assert pair.tenant.slug == tenant_a["slug"]
    # The display name, which is the part the UI could not otherwise obtain.
    assert pair.tenant.name


async def test_refresh_keeps_reporting_the_same_tenant(app_session_factory, tenant_a):
    """A rotated session must not lose track of whose workspace it is.

    The header re-renders from whatever the refresh returns, so dropping the
    tenant here would blank the organisation name on every token rotation —
    intermittently, and only after the access token expired.
    """
    async with app_session_factory() as session:
        async with session.begin():
            first = await auth_service.authenticate(
                session, tenant_slug=tenant_a["slug"],
                email=tenant_a["admin_email"], password=tenant_a["password"],
            )
            raw = first.refresh_token

    async with app_session_factory() as session:
        async with session.begin():
            second = await auth_service.refresh(session, raw_token=raw)

    assert second.tenant.id == first.tenant.id
    assert second.tenant.name == first.tenant.name


async def test_two_tenants_report_their_own_name(
    app_session_factory, tenant_a, tenant_b
):
    """The whole point. If this ever returned one name for both, the product
    would be telling one customer's users they are somewhere else."""
    async with app_session_factory() as session:
        async with session.begin():
            a = await auth_service.authenticate(
                session, tenant_slug=tenant_a["slug"],
                email=tenant_a["admin_email"], password=tenant_a["password"],
            )
    async with app_session_factory() as session:
        async with session.begin():
            b = await auth_service.authenticate(
                session, tenant_slug=tenant_b["slug"],
                email=tenant_b["admin_email"], password=tenant_b["password"],
            )
    assert a.tenant.id != b.tenant.id
    assert a.tenant.name != b.tenant.name
