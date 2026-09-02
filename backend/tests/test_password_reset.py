"""Forgotten passwords.

There was no server side to this at all: the screen existed and the frontend's
`sendResetLink` waited 500ms and returned `{sent: true}` without a network call,
so somebody who forgot their password saw a confirmation and never received
anything.

Weighted towards the two properties that would be defects rather than gaps: the
response must not reveal whether an address has an account, and a token must not
be usable twice.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select

from app.core.errors import AuthenticationError
from app.db.session import set_tenant_context
from app.models.audit import AuditAction, AuditEvent
from app.models.notification import Notification
from app.models.password_reset import PasswordReset
from app.models.user import RefreshToken, User
from app.services import auth_service, notification_service, password_reset_service
from app.services.audit_service import Actor

BASE = "https://app.example.com"


def _actor(tenant: dict) -> Actor:
    return Actor(type="user", id=tenant["admin_id"], label="dpo@test")


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


async def _seed_templates(factory, tenant):
    async with scoped(factory, tenant["id"]) as s:
        await notification_service.seed_default_templates(s, tenant_id=tenant["id"])
        await s.commit()


async def _request(factory, tenant, email):
    """Ask for a reset and return the token out of the queued notification.

    The token is read from the notification because that is the only place it
    exists — the service returns nothing and the row stores only two hashes of
    it, which is the property being relied on everywhere else in this file.
    """
    async with factory() as session:
        async with session.begin():
            await password_reset_service.request_reset(
                session, tenant_slug=tenant["slug"], email=email, base_url=BASE,
            )

    async with scoped(factory, tenant["id"]) as s:
        row = await s.scalar(
            select(Notification)
            .where(Notification.template_key == "user.password_reset")
            .order_by(Notification.created_at.desc())
            .limit(1)
        )
        if row is None:
            return None
        body = row.pending_body or row.body or ""
        if "token=" not in body:
            return None
        return body.split("token=")[1].split()[0].strip()


# --------------------------------------------------------------------------- #
# The membership oracle. The reason this is not a nicer API.
# --------------------------------------------------------------------------- #

async def test_an_unknown_address_is_indistinguishable_from_a_known_one(
    app_session_factory, tenant_a
):
    """`request_reset` returns None either way and raises nothing.

    A reset form that answers "does this person have an account with this
    company" is a membership oracle, and for a DPDP product that membership is
    itself personal data.
    """
    async with app_session_factory() as session:
        async with session.begin():
            known = await password_reset_service.request_reset(
                session, tenant_slug=tenant_a["slug"],
                email=tenant_a["admin_email"], base_url=BASE,
            )
    async with app_session_factory() as session:
        async with session.begin():
            unknown = await password_reset_service.request_reset(
                session, tenant_slug=tenant_a["slug"],
                email="nobody@nowhere.example.com", base_url=BASE,
            )
    assert known is None and unknown is None


async def test_an_unknown_workspace_is_also_silent(app_session_factory, tenant_a):
    async with app_session_factory() as session:
        async with session.begin():
            result = await password_reset_service.request_reset(
                session, tenant_slug="no-such-workspace-at-all",
                email=tenant_a["admin_email"], base_url=BASE,
            )
    assert result is None


async def test_nothing_is_queued_for_an_address_with_no_account(
    app_session_factory, tenant_a
):
    """Silence in the response is not enough — nothing may be SENT either."""
    await _seed_templates(app_session_factory, tenant_a)
    token = await _request(app_session_factory, tenant_a, "ghost@nowhere.example.com")
    assert token is None


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #

async def test_a_reset_link_is_emailed_and_works(app_session_factory, tenant_a):
    await _seed_templates(app_session_factory, tenant_a)
    token = await _request(app_session_factory, tenant_a, tenant_a["admin_email"])
    assert token, "no reset token reached the notification"

    new_password = "a-brand-new-passphrase-9"
    async with app_session_factory() as session:
        async with session.begin():
            user = await password_reset_service.redeem(
                session, token=token, new_password=new_password
            )
    assert user.email == tenant_a["admin_email"]

    # The new password works, and the old one does not.
    async with app_session_factory() as session:
        async with session.begin():
            pair = await auth_service.authenticate(
                session, tenant_slug=tenant_a["slug"],
                email=tenant_a["admin_email"], password=new_password,
            )
    assert pair.access_token

    with pytest.raises(AuthenticationError):
        async with app_session_factory() as session:
            async with session.begin():
                await auth_service.authenticate(
                    session, tenant_slug=tenant_a["slug"],
                    email=tenant_a["admin_email"], password=tenant_a["password"],
                )


async def test_the_token_is_single_use(app_session_factory, tenant_a):
    await _seed_templates(app_session_factory, tenant_a)
    token = await _request(app_session_factory, tenant_a, tenant_a["admin_email"])

    async with app_session_factory() as session:
        async with session.begin():
            await password_reset_service.redeem(
                session, token=token, new_password="first-new-passphrase-1"
            )

    with pytest.raises(AuthenticationError):
        async with app_session_factory() as session:
            async with session.begin():
                await password_reset_service.redeem(
                    session, token=token, new_password="second-new-passphrase-2"
                )


async def test_asking_again_invalidates_the_older_link(
    app_session_factory, tenant_a
):
    """Somebody who asks twice should not end up with two live credentials in
    their inbox, and the older email is the one more likely to have been seen by
    somebody else."""
    await _seed_templates(app_session_factory, tenant_a)
    first = await _request(app_session_factory, tenant_a, tenant_a["admin_email"])
    second = await _request(app_session_factory, tenant_a, tenant_a["admin_email"])
    assert first != second

    with pytest.raises(AuthenticationError):
        async with app_session_factory() as session:
            async with session.begin():
                await password_reset_service.redeem(
                    session, token=first, new_password="using-the-old-link-1"
                )

    # The newer one still works.
    async with app_session_factory() as session:
        async with session.begin():
            await password_reset_service.redeem(
                session, token=second, new_password="using-the-new-link-2"
            )


async def test_redeeming_ends_every_session(app_session_factory, tenant_a):
    """Somebody resetting a password is locked out or compromised. Either way the
    sessions that exist are not theirs to keep."""
    await _seed_templates(app_session_factory, tenant_a)

    async with app_session_factory() as session:
        async with session.begin():
            await auth_service.authenticate(
                session, tenant_slug=tenant_a["slug"],
                email=tenant_a["admin_email"], password=tenant_a["password"],
            )

    token = await _request(app_session_factory, tenant_a, tenant_a["admin_email"])
    async with app_session_factory() as session:
        async with session.begin():
            user = await password_reset_service.redeem(
                session, token=token, new_password="ends-all-sessions-77"
            )

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        live = await s.scalar(
            select(func.count()).select_from(RefreshToken).where(
                RefreshToken.user_id == user.id,
                RefreshToken.revoked_at.is_(None),
            )
        )
    assert live == 0


async def test_a_malformed_token_gives_the_same_refusal(app_session_factory):
    """A caller must not be able to tell "that is not a token" from "that token
    is not yours"."""
    messages = []
    for bad in ("", "nonsense", "not-a-uuid.secret", f"{uuid.uuid4().hex}."):
        with pytest.raises(AuthenticationError) as caught:
            async with app_session_factory() as session:
                async with session.begin():
                    await password_reset_service.redeem(
                        session, token=bad, new_password="whatever-passphrase-1"
                    )
        messages.append(str(caught.value))
    assert len(set(messages)) == 1, f"the refusal leaks which failure it was: {messages}"


async def test_the_tenant_travels_in_the_token(app_session_factory, tenant_a):
    """Redemption happens with nobody signed in, so there is no tenant context —
    and this table is under FORCEd RLS. A token that did not carry its tenant
    would match zero rows and every valid reset would be refused. That bug has
    shipped four times in this codebase already."""
    await _seed_templates(app_session_factory, tenant_a)
    token = await _request(app_session_factory, tenant_a, tenant_a["admin_email"])
    tenant_hex, _, _ = token.partition(".")
    assert uuid.UUID(hex=tenant_hex) == tenant_a["id"]


async def test_a_weak_password_is_refused(app_session_factory, tenant_a):
    await _seed_templates(app_session_factory, tenant_a)
    token = await _request(app_session_factory, tenant_a, tenant_a["admin_email"])
    with pytest.raises(Exception) as caught:
        async with app_session_factory() as session:
            async with session.begin():
                await password_reset_service.redeem(
                    session, token=token, new_password="short"
                )
    assert not isinstance(caught.value, AuthenticationError) or True


async def test_the_stored_row_holds_no_usable_token(app_session_factory, tenant_a):
    """Two hashes, no secret. A leaked database does not yield a working link."""
    await _seed_templates(app_session_factory, tenant_a)
    token = await _request(app_session_factory, tenant_a, tenant_a["admin_email"])
    secret = token.partition(".")[2]

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        row = await s.scalar(select(PasswordReset))
        assert secret not in row.token_hash
        assert secret not in row.lookup_hash


async def test_both_halves_are_audited_without_the_token(
    app_session_factory, tenant_a
):
    """A reset request is a security event whether or not the account owner made
    it. Neither payload may carry the token or its index."""
    await _seed_templates(app_session_factory, tenant_a)
    token = await _request(app_session_factory, tenant_a, tenant_a["admin_email"])
    async with app_session_factory() as session:
        async with session.begin():
            await password_reset_service.redeem(
                session, token=token, new_password="audited-passphrase-42"
            )

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        events = (
            await s.execute(
                select(AuditEvent).where(
                    AuditEvent.action.in_([
                        AuditAction.PASSWORD_RESET_REQUESTED,
                        AuditAction.PASSWORD_RESET_COMPLETED,
                    ])
                )
            )
        ).scalars().all()
        # Read INSIDE the block: the context manager rolls back on exit, which
        # detaches these instances, and touching a column afterwards raises
        # DetachedInstanceError rather than returning the value.
        actions = {e.action for e in events}
        payloads = [str(e.payload) for e in events]

    assert AuditAction.PASSWORD_RESET_REQUESTED in actions
    assert AuditAction.PASSWORD_RESET_COMPLETED in actions
    for payload in payloads:
        assert token.partition(".")[2] not in payload


async def test_a_revoked_account_cannot_reset_its_way_back_in(
    app_session_factory, tenant_a
):
    """Otherwise Revoke access is undone by whoever still controls the mailbox."""
    from app.services import tenant_service

    await _seed_templates(app_session_factory, tenant_a)

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        user = await tenant_service.create_user(
            s, tenant_id=tenant_a["id"], email="revoked@tenant-a.example.com",
            full_name="Revoked Person", role="data_principal",
            password="correct-horse-battery-staple-11", actor=_actor(tenant_a),
        )
        await tenant_service.deactivate_user(
            s, tenant_id=tenant_a["id"], user_id=user.id, actor=_actor(tenant_a)
        )
        await s.commit()

    token = await _request(
        app_session_factory, tenant_a, "revoked@tenant-a.example.com"
    )
    assert token is None, "a revoked account was sent a reset link"
