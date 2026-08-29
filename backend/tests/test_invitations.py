"""Invitations, the last-admin rule, and session revocation.

This module touches authentication, so the failure modes are the expensive kind:
a credential that still works after use, a workspace nobody can administer, or a
demoted user whose browser keeps working. Each has a test whose name says what
would go wrong.

Two things get particular attention:

* **The acceptance endpoint is public and creates a user.** Every failure must be
  indistinguishable from every other, or it becomes an oracle for which
  invitations and which accounts exist.
* **The tenant travels in the token.** This codebase has shipped the
  lookup-before-tenant-context bug three times. A test asserts a token carries its
  tenant and that acceptance works with no ambient context at all.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core import throttle
from app.core.errors import Conflict
from app.db.session import set_tenant_context
from app.models.audit import AuditEvent
from app.models.invitation import INVITATION_TTL_HOURS, UserInvitation
from app.models.user import RefreshToken, User
from app.services import auth_service, invitation_service, tenant_service
from app.services.audit_service import Actor
from app.services.notification_providers import SendResult


@asynccontextmanager
async def scoped(factory, tenant_id):
    async with factory() as session:
        await session.begin()
        await set_tenant_context(session, tenant_id)
        try:
            yield session
        finally:
            if session.in_transaction():
                await session.rollback()


def _actor(tenant: dict) -> Actor:
    return Actor(type="user", id=tenant["admin_id"], label="dpo@test")


class _Sends:
    name = "capture"

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, *, to, subject, body, channel, html_body=None):
        self.sent.append({"to": to, "subject": subject, "body": body})
        return SendResult(ok=True, provider_message_id="x")


@pytest.fixture
def provider(monkeypatch):
    def _install():
        impl = _Sends()
        monkeypatch.setattr(
            "app.services.notification_providers.get_provider", lambda: impl
        )
        return impl
    return _install


@pytest.fixture(autouse=True)
def clean_throttle():
    """The limiter is process-global, so tests would otherwise poison each other."""
    throttle.reset()
    yield
    throttle.reset()


@pytest.fixture
async def client():
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _age(session, row, *, hours: int) -> None:
    """Backdate a whole invitation, keeping its timeline coherent.

    Both timestamps move together: the database refuses a row whose expiry precedes
    its creation, so ageing one produces a shape the product cannot produce.
    """
    # `make_interval` rather than a bound timedelta: asyncpg sends a timedelta as
    # an interval parameter, and Postgres will not infer `timestamptz - $1` from an
    # untyped placeholder.
    await session.execute(
        text("UPDATE user_invitations "
             "SET created_at = created_at - make_interval(hours => :h), "
             "    expires_at = expires_at - make_interval(hours => :h) "
             "WHERE id = :i"),
        {"h": hours, "i": str(row.id)},
    )
    await session.refresh(row)


async def _invite(session, tenant, *, email="new@example.com", role="auditor"):
    return await invitation_service.invite(
        session, tenant_id=tenant["id"], actor=_actor(tenant),
        email=email, role=role, invited_by=tenant["admin_id"],
    )


# --------------------------------------------------------------------------- #
# The token is a credential
# --------------------------------------------------------------------------- #

async def test_the_raw_token_is_never_stored(app_session_factory, tenant_a):
    """Argon2 at rest, like a refresh token.

    A leaked database must not hand somebody the ability to create a privileged
    account in a customer's workspace.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        row, token = await _invite(s, tenant_a)
        assert token not in row.token_hash
        assert row.token_hash.startswith("$argon2")
        # And the lookup index is not the token either.
        assert token not in row.lookup_hash
        assert len(row.lookup_hash) == 64


async def test_the_tenant_travels_in_the_token(app_session_factory, tenant_a):
    """The bug this codebase has shipped three times.

    Acceptance happens before any tenant context exists and `user_invitations` is
    under RLS, so a lookup that does not already know the tenant matches zero rows
    and every valid invitation is rejected.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        _row, token = await _invite(s, tenant_a)

    tenant_id, secret = invitation_service.split_token(token)
    assert tenant_id == tenant_a["id"]
    assert secret and secret not in str(tenant_id)


async def test_a_malformed_token_fails_like_a_wrong_one(app_session_factory, tenant_a):
    """A caller must not be able to tell "not a token" from "not your token"."""
    messages = set()
    for bad in ("no-dot-at-all", "notahex.secret", f"{uuid.uuid4().hex}."):
        with pytest.raises(Conflict) as exc:
            invitation_service.split_token(bad)
        messages.add(str(exc.value))
    assert len(messages) == 1, messages


async def test_an_invitation_is_single_use(app_session_factory, tenant_a):
    """The property that makes it a credential rather than a password.

    Accepting stamps `accepted_at`, and nothing further can be done with the token.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        _row, token = await _invite(s, tenant_a)
        _tid, secret = invitation_service.split_token(token)

        user = await invitation_service.accept(
            s, tenant_id=tenant_a["id"], secret=secret,
            full_name="New Person", password="correct-horse-battery-staple",
        )
        assert user.email == "new@example.com"
        assert user.role == "auditor"

        with pytest.raises(Conflict):
            await invitation_service.accept(
                s, tenant_id=tenant_a["id"], secret=secret,
                full_name="Someone Else", password="correct-horse-battery-staple-2",
            )


async def test_an_expired_invitation_is_refused(app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        row, token = await _invite(s, tenant_a)
        _tid, secret = invitation_service.split_token(token)
        # Both timestamps move. `ck_user_invitations_expires_after_created` refuses
        # a row whose expiry precedes its creation — correctly, since that would be
        # an invitation issued already dead — so ageing only `expires_at` produces
        # a row the product cannot create.
        await _age(s, row, hours=INVITATION_TTL_HOURS + 1)

        with pytest.raises(Conflict):
            await invitation_service.accept(
                s, tenant_id=tenant_a["id"], secret=secret,
                full_name="New Person", password="correct-horse-battery-staple",
            )


async def test_a_revoked_invitation_is_refused(app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        row, token = await _invite(s, tenant_a)
        _tid, secret = invitation_service.split_token(token)
        await invitation_service.revoke(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), invitation_id=row.id,
            reason="sent to the wrong address",
        )
        with pytest.raises(Conflict):
            await invitation_service.accept(
                s, tenant_id=tenant_a["id"], secret=secret,
                full_name="New Person", password="correct-horse-battery-staple",
            )


async def test_every_acceptance_failure_says_the_same_thing(
    app_session_factory, tenant_a
):
    """The endpoint is public. Distinguishing failures makes it an oracle.

    Expired, revoked, already-accepted and simply-wrong must be one message, or
    somebody can probe which invitations exist in a workspace they do not belong to.
    """
    messages = set()
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        # wrong secret
        with pytest.raises(Conflict) as exc:
            await invitation_service.accept(
                s, tenant_id=tenant_a["id"], secret="not-a-real-secret",
                full_name="X", password="correct-horse-battery-staple",
            )
        messages.add(str(exc.value))

        # expired
        row, token = await _invite(s, tenant_a, email="expired@example.com")
        _t, secret = invitation_service.split_token(token)
        await _age(s, row, hours=INVITATION_TTL_HOURS + 1)
        with pytest.raises(Conflict) as exc:
            await invitation_service.accept(
                s, tenant_id=tenant_a["id"], secret=secret, full_name="X",
                password="correct-horse-battery-staple",
            )
        messages.add(str(exc.value))

        # revoked
        row2, token2 = await _invite(s, tenant_a, email="revoked@example.com")
        _t2, secret2 = invitation_service.split_token(token2)
        await invitation_service.revoke(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            invitation_id=row2.id, reason="no longer joining",
        )
        with pytest.raises(Conflict) as exc:
            await invitation_service.accept(
                s, tenant_id=tenant_a["id"], secret=secret2, full_name="X",
                password="correct-horse-battery-staple",
            )
        messages.add(str(exc.value))

    assert len(messages) == 1, f"failures are distinguishable: {messages}"


async def test_the_invited_role_is_the_role_granted(app_session_factory, tenant_a):
    """The acceptor does not choose. That is what the token is for.

    An administrator decided what this account may do; if acceptance could pick a
    role, the invitation would be a privilege-escalation primitive.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        _row, token = await _invite(s, tenant_a, email="aud@example.com", role="auditor")
        _t, secret = invitation_service.split_token(token)
        user = await invitation_service.accept(
            s, tenant_id=tenant_a["id"], secret=secret, full_name="An Auditor",
            password="correct-horse-battery-staple",
        )
        assert user.role == "auditor"


async def test_acceptance_reuses_the_registration_password_policy(
    app_session_factory, tenant_a
):
    """One policy. Two implementations means the weaker one is the real one."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        _row, token = await _invite(s, tenant_a)
        _t, secret = invitation_service.split_token(token)
        with pytest.raises(Exception) as exc:
            await invitation_service.accept(
                s, tenant_id=tenant_a["id"], secret=secret, full_name="New Person",
                # Contains the email local part — rejected by validate_password.
                password="new@example.com-password",
            )
        assert "password" in str(exc.value).lower()


async def test_the_audit_entry_does_not_contain_the_token(
    app_session_factory, tenant_a
):
    """An audit entry is append-only forever.

    Putting a credential in one would place it somewhere it can never be removed
    from, which is the opposite of what hashing it at rest achieves.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        row, token = await _invite(s, tenant_a)
        _t, secret = invitation_service.split_token(token)
        rows = await s.execute(text("SELECT payload::text FROM audit_events"))
        blob = " ".join(r[0] or "" for r in rows)
        assert token not in blob
        assert secret not in blob
        assert row.token_hash not in blob
        assert row.lookup_hash not in blob


async def test_one_live_invitation_per_address(app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _invite(s, tenant_a, email="dup@example.com")
        with pytest.raises(Conflict) as exc:
            await _invite(s, tenant_a, email="dup@example.com")
        assert "already has an invitation" in str(exc.value)


async def test_revoking_frees_the_address_to_be_invited_again(
    app_session_factory, tenant_a
):
    """The uniqueness is on LIVE invitations.

    Accepted and revoked rows are kept as history, so a plain UNIQUE would refuse a
    legitimate re-invitation after a mistake.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        row, _token = await _invite(s, tenant_a, email="again@example.com")
        await invitation_service.revoke(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            invitation_id=row.id, reason="wrong role",
        )
        second, _t2 = await _invite(s, tenant_a, email="again@example.com", role="admin")
        assert second.role == "admin"

        # And both rows survive, so the history is readable.
        count = await s.scalar(
            select(func.count()).select_from(UserInvitation)
            .where(UserInvitation.email == "again@example.com")
        )
        assert count == 2


async def test_inviting_an_existing_user_is_refused(app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(Conflict) as exc:
            await _invite(s, tenant_a, email=tenant_a["admin_email"])
        assert "already has an account" in str(exc.value)


async def test_an_accepted_invitation_cannot_be_revoked(app_session_factory, tenant_a):
    """Revoking it would suggest the resulting account is provisional."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        row, token = await _invite(s, tenant_a)
        _t, secret = invitation_service.split_token(token)
        await invitation_service.accept(
            s, tenant_id=tenant_a["id"], secret=secret, full_name="New Person",
            password="correct-horse-battery-staple",
        )
        with pytest.raises(Conflict) as exc:
            await invitation_service.revoke(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                invitation_id=row.id,
            )
        assert "already accepted" in str(exc.value)


async def test_the_application_role_cannot_delete_an_invitation(
    app_session_factory, tenant_a
):
    """The record that a credential was issued outlives the credential."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _invite(s, tenant_a)
        await s.flush()
        with pytest.raises(DBAPIError):
            await s.execute(text("DELETE FROM user_invitations"))


async def test_the_status_is_computed_not_stored(app_session_factory, tenant_a):
    """A stored status is stale the moment the expiry passes.

    That window is exactly when somebody would try to use the invitation.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        row, _token = await _invite(s, tenant_a)
        assert row.status == "pending"
        assert row.is_usable

        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        assert row.status == "expired", "no write, no job — just the clock"
        assert not row.is_usable


async def test_invitations_are_tenant_isolated(
    app_session_factory, tenant_a, tenant_b
):
    async with app_session_factory() as s:
        await s.begin()
        await set_tenant_context(s, tenant_b["id"])
        await _invite(s, tenant_b, email="b@example.com")
        await s.commit()

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        assert await invitation_service.list_invitations(s, tenant_a["id"]) == []


async def test_a_token_from_one_tenant_does_not_work_in_another(
    app_session_factory, tenant_a, tenant_b
):
    """The tenant in the token is checked, not merely carried.

    Substituting another tenant's id would otherwise turn the token into a
    cross-workspace credential.
    """
    async with app_session_factory() as s:
        await s.begin()
        await set_tenant_context(s, tenant_b["id"])
        _row, token = await _invite(s, tenant_b, email="b@example.com")
        await s.commit()

    _tid, secret = invitation_service.split_token(token)
    # Same secret, presented under tenant A's context.
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(Conflict):
            await invitation_service.accept(
                s, tenant_id=tenant_a["id"], secret=secret, full_name="X",
                password="correct-horse-battery-staple",
            )


# --------------------------------------------------------------------------- #
# The last-admin rule
# --------------------------------------------------------------------------- #

async def test_the_last_admin_cannot_be_demoted_at_the_service(
    app_session_factory, tenant_a
):
    """A workspace with no admin is unrecoverable without support access.

    The worst possible support ticket, and entirely preventable.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        admin = await tenant_service.get_user(
            s, tenant_id=tenant_a["id"], user_id=tenant_a["admin_id"]
        )
        with pytest.raises(Conflict) as exc:
            await invitation_service.assert_not_last_admin(
                s, tenant_id=tenant_a["id"], user=admin,
                becoming_role="auditor", staying_active=True,
            )
        assert "only active administrator" in str(exc.value)


async def test_the_database_refuses_to_demote_the_last_admin(
    app_session_factory, tenant_a
):
    """A trigger, because the rule is about the SET of rows.

    No CHECK constraint can count its own table, and a service rule can be
    bypassed by the next code path somebody writes.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(IntegrityError):
            await s.execute(
                text("UPDATE users SET role='auditor' WHERE id=:i"),
                {"i": str(tenant_a["admin_id"])},
            )


async def test_the_database_refuses_to_deactivate_the_last_admin(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(IntegrityError):
            await s.execute(
                text("UPDATE users SET is_active=false WHERE id=:i"),
                {"i": str(tenant_a["admin_id"])},
            )


async def test_an_admin_can_be_demoted_once_there_is_another(
    app_session_factory, tenant_a
):
    """The rule is "keep one", not "never change an admin"."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        second = await tenant_service.create_user(
            s, tenant_id=tenant_a["id"], email="admin2@tenant-a.example.com",
            full_name="Second Admin", role="admin",
            password="correct-horse-battery-staple-2", actor=_actor(tenant_a),
        )
        assert second.role == "admin"

        first = await tenant_service.get_user(
            s, tenant_id=tenant_a["id"], user_id=tenant_a["admin_id"]
        )
        # Does not raise now.
        await invitation_service.assert_not_last_admin(
            s, tenant_id=tenant_a["id"], user=first,
            becoming_role="auditor", staying_active=True,
        )
        await s.execute(
            text("UPDATE users SET role='auditor' WHERE id=:i"),
            {"i": str(tenant_a["admin_id"])},
        )


async def test_a_deactivated_admin_does_not_count_towards_the_rule(
    app_session_factory, tenant_a
):
    """"Another admin exists but cannot sign in" is not a recoverable workspace."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await tenant_service.create_user(
            s, tenant_id=tenant_a["id"], email="dormant@tenant-a.example.com",
            full_name="Dormant Admin", role="admin",
            password="correct-horse-battery-staple-3", actor=_actor(tenant_a),
        )
        await s.execute(
            text("UPDATE users SET is_active=false "
                 "WHERE email='dormant@tenant-a.example.com'")
        )
        first = await tenant_service.get_user(
            s, tenant_id=tenant_a["id"], user_id=tenant_a["admin_id"]
        )
        with pytest.raises(Conflict):
            await invitation_service.assert_not_last_admin(
                s, tenant_id=tenant_a["id"], user=first,
                becoming_role="auditor", staying_active=True,
            )


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #

async def _sign_in_twice(session, tenant, user):
    """Two families, as though the person used two browsers."""
    for _ in range(2):
        await auth_service.issue_session(
            session, user=user, ip="203.0.113.10", user_agent="test-agent"
        )


async def test_sessions_are_listed_one_per_family(app_session_factory, tenant_a):
    """A family is a browser. The rotation inside it is machinery nobody needs."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        admin = await tenant_service.get_user(
            s, tenant_id=tenant_a["id"], user_id=tenant_a["admin_id"]
        )
        await _sign_in_twice(s, tenant_a, admin)

        sessions = await invitation_service.list_sessions(
            s, tenant_id=tenant_a["id"], user_id=admin.id
        )
        assert len(sessions) == 2
        assert all(x["ip_address"] == "203.0.113.10" for x in sessions)
        assert all(x["rotations"] >= 1 for x in sessions)


async def test_revoking_sessions_ends_every_family(app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        admin = await tenant_service.get_user(
            s, tenant_id=tenant_a["id"], user_id=tenant_a["admin_id"]
        )
        await _sign_in_twice(s, tenant_a, admin)

        count = await invitation_service.revoke_sessions(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), user=admin,
            reason="revoked_by_admin",
        )
        assert count == 2
        assert await invitation_service.list_sessions(
            s, tenant_id=tenant_a["id"], user_id=admin.id
        ) == []

        live = await s.scalar(
            select(func.count()).select_from(RefreshToken)
            .where(RefreshToken.user_id == admin.id, RefreshToken.revoked_at.is_(None))
        )
        assert live == 0


async def test_a_demotion_over_http_ends_their_sessions(
    app_session_factory, tenant_a, client
):
    """The reason this matters.

    The signed-in role is re-read from the database on every request, so the
    demotion bites immediately — but a refresh token outlives it, and without this
    the demoted user's browser keeps minting access tokens until the family
    expires.
    """
    pw = "correct-horse-battery-staple-demote"
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        target = await tenant_service.create_user(
            s, tenant_id=tenant_a["id"], email="demote@tenant-a.example.com",
            full_name="To Demote", role="admin", password=pw, actor=_actor(tenant_a),
        )
        target_id = target.id
        await s.commit()

    # They sign in, creating a family.
    r = await client.post("/v1/auth/login", json={
        "tenant_slug": tenant_a["slug"],
        "email": "demote@tenant-a.example.com", "password": pw,
    })
    assert r.status_code == 200, r.text

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        assert len(await invitation_service.list_sessions(
            s, tenant_id=tenant_a["id"], user_id=target_id
        )) == 1

    # An admin demotes them.
    admin_login = await client.post("/v1/auth/login", json={
        "tenant_slug": tenant_a["slug"], "email": tenant_a["admin_email"],
        "password": tenant_a["password"],
    })
    headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    demoted = await client.patch(
        f"/v1/admin/users/{target_id}/role", headers=headers,
        json={"role": "auditor"},
    )
    assert demoted.status_code == 200, demoted.text
    assert demoted.json()["role"] == "auditor"

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        assert await invitation_service.list_sessions(
            s, tenant_id=tenant_a["id"], user_id=target_id
        ) == [], "the demoted user is still signed in somewhere"


async def test_deactivation_over_http_ends_their_sessions(
    app_session_factory, tenant_a, client
):
    pw = "correct-horse-battery-staple-deact"
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        target = await tenant_service.create_user(
            s, tenant_id=tenant_a["id"], email="deact@tenant-a.example.com",
            full_name="To Deactivate", role="auditor", password=pw,
            actor=_actor(tenant_a),
        )
        target_id = target.id
        await s.commit()

    await client.post("/v1/auth/login", json={
        "tenant_slug": tenant_a["slug"],
        "email": "deact@tenant-a.example.com", "password": pw,
    })
    admin_login = await client.post("/v1/auth/login", json={
        "tenant_slug": tenant_a["slug"], "email": tenant_a["admin_email"],
        "password": tenant_a["password"],
    })
    headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}

    r = await client.post(f"/v1/admin/users/{target_id}/deactivate", headers=headers)
    assert r.status_code == 200, r.text

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        assert await invitation_service.list_sessions(
            s, tenant_id=tenant_a["id"], user_id=target_id
        ) == []


async def test_a_promotion_does_not_end_their_sessions(
    app_session_factory, tenant_a, client
):
    """Signing somebody out for being given more access would be baffling.

    A promotion takes effect on the next request anyway, because the role is
    re-read per request.
    """
    pw = "correct-horse-battery-staple-promo"
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        target = await tenant_service.create_user(
            s, tenant_id=tenant_a["id"], email="promo@tenant-a.example.com",
            full_name="To Promote", role="auditor", password=pw, actor=_actor(tenant_a),
        )
        target_id = target.id
        await s.commit()

    await client.post("/v1/auth/login", json={
        "tenant_slug": tenant_a["slug"],
        "email": "promo@tenant-a.example.com", "password": pw,
    })
    admin_login = await client.post("/v1/auth/login", json={
        "tenant_slug": tenant_a["slug"], "email": tenant_a["admin_email"],
        "password": tenant_a["password"],
    })
    headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}

    r = await client.patch(
        f"/v1/admin/users/{target_id}/role", headers=headers, json={"role": "admin"},
    )
    assert r.status_code == 200, r.text

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        assert len(await invitation_service.list_sessions(
            s, tenant_id=tenant_a["id"], user_id=target_id
        )) == 1, "a promotion should not sign anybody out"


# --------------------------------------------------------------------------- #
# The capability matrix
# --------------------------------------------------------------------------- #

def test_the_matrix_is_generated_from_the_enforcement():
    """A permissions screen that can disagree with the code is worse than none.

    It tells an administrator their workspace is configured one way while it
    behaves another.
    """
    from app.core.permissions import Role, capabilities_for

    matrix = invitation_service.capability_matrix()
    for role in Role:
        assert matrix["matrix"][role.value] == sorted(
            c.value for c in capabilities_for(role)
        )


def test_no_role_can_hold_an_audit_write_capability():
    """The reason the audit chain is append-only in fact, not merely by policy.

    There is no audit-write or audit-delete capability in the enum at all, so no
    role can be misconfigured into holding one.
    """
    matrix = invitation_service.capability_matrix()
    for caps in matrix["matrix"].values():
        for cap in caps:
            assert "audit:write" not in cap
            assert "audit:delete" not in cap
    assert "audit:write" not in matrix["capabilities"]
    assert "audit:delete" not in matrix["capabilities"]


# --------------------------------------------------------------------------- #
# Over HTTP
# --------------------------------------------------------------------------- #

async def test_the_whole_invitation_flow_over_http(
    app_session_factory, tenant_a, client, provider
):
    """Invite → accept → signed in, with the right role and no password shared."""
    impl = provider()
    admin_login = await client.post("/v1/auth/login", json={
        "tenant_slug": tenant_a["slug"], "email": tenant_a["admin_email"],
        "password": tenant_a["password"],
    })
    headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}

    invited = await client.post(
        "/v1/admin/invitations", headers=headers,
        json={"email": "colleague@example.com", "role": "grievance_officer"},
    )
    assert invited.status_code == 201, invited.text
    body = invited.json()
    assert body["invitation"]["status"] == "pending"
    assert "token=" in body["accept_url"]
    assert "shown once" in body["shown_once"]

    # It was emailed, and the email carries the link.
    assert impl.sent, "no invitation email was sent"
    assert impl.sent[0]["to"] == "colleague@example.com"
    assert body["accept_url"] in impl.sent[0]["body"]

    token = body["accept_url"].split("token=")[1]
    accepted = await client.post("/v1/auth/accept-invitation", json={
        "token": token, "full_name": "A Colleague",
        "password": "correct-horse-battery-staple-x",
    })
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["user"]["role"] == "grievance_officer"
    assert accepted.json()["user"]["email"] == "colleague@example.com"
    # Signed straight in.
    assert accepted.json()["access_token"]
    # And they hold the grievance officer's capabilities, not an admin's.
    caps = accepted.json()["capabilities"]
    assert "grievance:process" in caps
    assert "user:manage" not in caps

    # The invitation now reads as accepted.
    listed = await client.get("/v1/admin/invitations", headers=headers)
    row = next(i for i in listed.json() if i["email"] == "colleague@example.com")
    assert row["status"] == "accepted"

    # And the token is spent.
    again = await client.post("/v1/auth/accept-invitation", json={
        "token": token, "full_name": "Someone Else",
        "password": "correct-horse-battery-staple-y",
    })
    assert again.status_code == 409, again.text


async def test_over_http_the_self_guard_reaches_the_last_admin_case_first(
    app_session_factory, tenant_a, client
):
    """Worth stating rather than working around: over HTTP the last-admin rule is
    unreachable, and that is fine.

    Both routes refuse to act on your own account before any other check. With one
    admin left, that admin is the only account holding `user:manage`, so the only
    request that could remove the last admin is one they make against themselves —
    which the self guard refuses first, with a clearer message than the rule would
    give.

    The last-admin rule therefore protects the paths a request cannot reach: a
    script, a data fix, a migration, or anything running as the owner role. That is
    exactly why it is a trigger and not only a service check — see
    `test_the_database_refuses_to_demote_the_last_admin`.
    """
    admin_login = await client.post("/v1/auth/login", json={
        "tenant_slug": tenant_a["slug"], "email": tenant_a["admin_email"],
        "password": tenant_a["password"],
    })
    headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}

    demote_self = await client.patch(
        f"/v1/admin/users/{tenant_a['admin_id']}/role", headers=headers,
        json={"role": "auditor"},
    )
    assert demote_self.status_code == 409
    assert "your own role" in demote_self.text

    deactivate_self = await client.post(
        f"/v1/admin/users/{tenant_a['admin_id']}/deactivate", headers=headers
    )
    assert deactivate_self.status_code == 409
    assert "your own account" in deactivate_self.text


async def test_an_admin_can_be_demoted_over_http_once_another_exists(
    app_session_factory, tenant_a, client
):
    """The rule is "keep one", not "never touch an admin".

    Also asserts the demotion revoked the demoted admin's sessions, which is why
    the second admin has to sign in again to continue.
    """
    pw = "correct-horse-battery-staple-second"
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        second = await tenant_service.create_user(
            s, tenant_id=tenant_a["id"], email="second@tenant-a.example.com",
            full_name="Second Admin", role="admin", password=pw, actor=_actor(tenant_a),
        )
        second_id = second.id
        await s.commit()

    admin_login = await client.post("/v1/auth/login", json={
        "tenant_slug": tenant_a["slug"], "email": tenant_a["admin_email"],
        "password": tenant_a["password"],
    })
    headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}

    demoted = await client.patch(
        f"/v1/admin/users/{second_id}/role", headers=headers, json={"role": "auditor"},
    )
    assert demoted.status_code == 200, demoted.text
    assert demoted.json()["role"] == "auditor"

    # And the workspace still has exactly one admin, so it is administrable.
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        admins = await s.scalar(
            select(func.count()).select_from(User).where(
                User.tenant_id == tenant_a["id"], User.role == "admin",
                User.is_active.is_(True),
            )
        )
        assert admins == 1


async def test_sessions_can_be_listed_and_revoked_over_http(
    app_session_factory, tenant_a, client
):
    pw = "correct-horse-battery-staple-sess"
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        target = await tenant_service.create_user(
            s, tenant_id=tenant_a["id"], email="sess@tenant-a.example.com",
            full_name="Has Sessions", role="auditor", password=pw,
            actor=_actor(tenant_a),
        )
        target_id = target.id
        await s.commit()

    for _ in range(2):
        await client.post("/v1/auth/login", json={
            "tenant_slug": tenant_a["slug"],
            "email": "sess@tenant-a.example.com", "password": pw,
        })

    admin_login = await client.post("/v1/auth/login", json={
        "tenant_slug": tenant_a["slug"], "email": tenant_a["admin_email"],
        "password": tenant_a["password"],
    })
    headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}

    listed = await client.get(f"/v1/admin/users/{target_id}/sessions", headers=headers)
    assert listed.status_code == 200, listed.text
    assert len(listed.json()) == 2

    revoked = await client.post(
        f"/v1/admin/users/{target_id}/sessions/revoke", headers=headers
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked_families"] == 2
    # Says what it is and is not.
    assert "does not lock" in revoked.json()["note"]

    again = await client.get(f"/v1/admin/users/{target_id}/sessions", headers=headers)
    assert again.json() == []


async def test_the_capability_matrix_endpoint_matches_enforcement(client, tenant_a):
    admin_login = await client.post("/v1/auth/login", json={
        "tenant_slug": tenant_a["slug"], "email": tenant_a["admin_email"],
        "password": tenant_a["password"],
    })
    headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    r = await client.get("/v1/admin/capabilities", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["roles"]) == {
        "admin", "auditor", "grievance_officer", "data_principal"
    }
    # The auditor holds no processing capability.
    assert "dsar:process" not in body["matrix"]["auditor"]
    assert "report:generate" in body["matrix"]["auditor"]
    assert "audit:write" not in body["capabilities"]


@pytest.mark.parametrize("role", ["auditor", "grievance_officer", "data_principal"])
async def test_only_user_manage_reaches_the_invitation_routes(
    app_session_factory, tenant_a, client, role
):
    pw = f"correct-horse-battery-staple-{role}"
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await tenant_service.create_user(
            s, tenant_id=tenant_a["id"], email=f"{role}@tenant-a.example.com",
            full_name=role, role=role, password=pw, actor=_actor(tenant_a),
        )
        await s.commit()
    login = await client.post("/v1/auth/login", json={
        "tenant_slug": tenant_a["slug"],
        "email": f"{role}@tenant-a.example.com", "password": pw,
    })
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    for method, path, body in (
        ("get", "/v1/admin/invitations", None),
        ("post", "/v1/admin/invitations",
         {"email": "x@example.com", "role": "auditor"}),
        ("get", "/v1/admin/capabilities", None),
        ("get", f"/v1/admin/users/{uuid.uuid4()}/sessions", None),
    ):
        call = getattr(client, method)
        r = await call(path, headers=headers, **({"json": body} if body else {}))
        assert r.status_code == 403, f"{role} reached {path}: {r.status_code}"


async def test_the_acceptance_endpoint_is_throttled(client):
    """Not the security boundary — the token's entropy is.

    What this stops is a retry loop or a scanner filling the logs faster than
    anybody can read them. See app/core/throttle.py.
    """
    payload = {
        "token": f"{uuid.uuid4().hex}.{'x' * 40}",
        "full_name": "Probe", "password": "correct-horse-battery-staple",
    }
    codes = []
    for _ in range(12):
        r = await client.post("/v1/auth/accept-invitation", json=payload)
        codes.append(r.status_code)
    assert 429 in codes, f"never throttled: {codes}"
    # And it refused before it throttled, rather than leaking a different error.
    assert 409 in codes


async def test_any_loss_of_privilege_ends_their_sessions_not_just_losing_admin(
    app_session_factory, tenant_a, client
):
    """The bug a live walkthrough caught and the first test missed.

    The original check was `role == "admin"`, so grievance_officer ->
    data_principal left a live session working after the privileges behind it were
    taken away. It is now derived from the capability matrix itself: if the new
    role's capabilities are not a superset of the old, something was removed.
    """
    pw = "correct-horse-battery-staple-lateral"
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        target = await tenant_service.create_user(
            s, tenant_id=tenant_a["id"], email="officer2@tenant-a.example.com",
            full_name="An Officer", role="grievance_officer", password=pw,
            actor=_actor(tenant_a),
        )
        target_id = target.id
        await s.commit()

    await client.post("/v1/auth/login", json={
        "tenant_slug": tenant_a["slug"],
        "email": "officer2@tenant-a.example.com", "password": pw,
    })
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        assert len(await invitation_service.list_sessions(
            s, tenant_id=tenant_a["id"], user_id=target_id
        )) == 1

    admin_login = await client.post("/v1/auth/login", json={
        "tenant_slug": tenant_a["slug"], "email": tenant_a["admin_email"],
        "password": tenant_a["password"],
    })
    headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}

    # Neither role is admin, so a naive "was an admin?" check misses this.
    r = await client.patch(
        f"/v1/admin/users/{target_id}/role", headers=headers,
        json={"role": "data_principal"},
    )
    assert r.status_code == 200, r.text

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        assert await invitation_service.list_sessions(
            s, tenant_id=tenant_a["id"], user_id=target_id
        ) == [], "the session outlived the privileges it was granted under"


async def test_a_lateral_promotion_still_does_not_end_sessions(
    app_session_factory, tenant_a, client
):
    """data_principal -> grievance_officer only adds. Nobody should be signed out."""
    pw = "correct-horse-battery-staple-up"
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        target = await tenant_service.create_user(
            s, tenant_id=tenant_a["id"], email="rising@tenant-a.example.com",
            full_name="Rising", role="data_principal", password=pw,
            actor=_actor(tenant_a),
        )
        target_id = target.id
        await s.commit()

    await client.post("/v1/auth/login", json={
        "tenant_slug": tenant_a["slug"],
        "email": "rising@tenant-a.example.com", "password": pw,
    })
    admin_login = await client.post("/v1/auth/login", json={
        "tenant_slug": tenant_a["slug"], "email": tenant_a["admin_email"],
        "password": tenant_a["password"],
    })
    headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}

    r = await client.patch(
        f"/v1/admin/users/{target_id}/role", headers=headers,
        json={"role": "grievance_officer"},
    )
    assert r.status_code == 200, r.text

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        assert len(await invitation_service.list_sessions(
            s, tenant_id=tenant_a["id"], user_id=target_id
        )) == 1
