"""Forgotten passwords.

There was no server side to this at all. The screen existed, and the frontend's
`sendResetLink` waited 500ms and returned `{sent: true}` without making a network
call — so somebody who forgot their password saw a confirmation and never
received anything. This is the missing half.

THREE RULES, and each one is the answer to a specific way this goes wrong.

1. **The response never reveals whether the address exists.** `request_reset`
   returns the same thing for a registered address and an unregistered one, and
   spends comparable time either way. A reset form that answers the question
   "does this person have an account with this company" is a membership oracle,
   and for a DPDP product that membership is itself personal data.

2. **The tenant travels inside the token.** Redemption happens with nobody
   signed in, so there is no tenant context, and `password_resets` is under
   FORCEd RLS — a lookup that does not already know the tenant matches zero rows
   and every valid token is rejected. That bug has shipped four times here
   (refresh tokens, `ds_live_`, `pk_live_`, invitations); this is the fifth place
   it would have.

3. **Redeeming one ends every session.** Somebody resetting a password is either
   locked out or compromised. In both cases the sessions that exist are not
   theirs to keep.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AuthenticationError
from app.core.security import hash_password, verify_password
from app.models.audit import AuditAction
from app.models.password_reset import RESET_TTL_MINUTES, PasswordReset
from app.models.tenant import Tenant
from app.models.user import RefreshToken, User
from app.services import audit_service, notification_service
from app.services.audit_service import Actor
from app.services.registration_service import validate_password

logger = logging.getLogger("app.password_reset")
_settings = get_settings()

#: Deliberately identical for every failure mode: expired, already used,
#: superseded, malformed, or simply wrong. A caller must not be able to tell
#: which, because each distinction is a fact about somebody else's account.
_GENERIC = (
    "That reset link is not valid. It may have expired, already been used, or "
    "been replaced by a newer one. Ask for another."
)


def _lookup_hash(secret: str) -> str:
    """Deterministic index. Argon2 is salted and cannot be searched by."""
    return hmac.new(
        _settings.jwt_secret.encode(), secret.encode(), hashlib.sha256
    ).hexdigest()


def _mint(tenant_id: uuid.UUID) -> tuple[str, str, str]:
    secret = secrets.token_urlsafe(32)
    return f"{tenant_id.hex}.{secret}", hash_password(secret), _lookup_hash(secret)


def _split(token: str) -> tuple[uuid.UUID, str]:
    if "." not in (token or ""):
        raise AuthenticationError(_GENERIC)
    tenant_hex, _, secret = token.partition(".")
    try:
        tenant_id = uuid.UUID(hex=tenant_hex)
    except ValueError:
        raise AuthenticationError(_GENERIC) from None
    if not secret:
        raise AuthenticationError(_GENERIC)
    return tenant_id, secret


async def request_reset(
    session: AsyncSession,
    *,
    tenant_slug: str,
    email: str,
    base_url: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Send a reset link, if that address belongs to an account.

    Returns None either way and raises nothing the caller can distinguish. The
    work done for an unknown address is deliberately similar to the work done for
    a known one — including a password hash — so response time does not answer
    the question the response refuses to.

    Runs on an unscoped session: the tenant is resolved from the slug here, the
    same way login does.
    """
    from app.db.session import set_tenant_context
    from app.services.registration_service import slugify

    typed = (tenant_slug or "").strip().lower()
    candidates = [typed]
    loose = slugify(typed)
    if loose and loose != typed:
        candidates.append(loose)

    found = (
        await session.execute(
            select(Tenant).where(Tenant.slug.in_(candidates), Tenant.is_active)
        )
    ).scalars().all()
    tenant = next((t for t in found if t.slug == typed), None) or (
        found[0] if found else None
    )

    if tenant is None:
        # Spend the time anyway. An unknown workspace returning instantly while a
        # known one takes 300ms is the same oracle by a different route.
        hash_password(secrets.token_urlsafe(16))
        return

    await set_tenant_context(session, tenant.id)

    user = (
        await session.execute(
            select(User).where(
                User.tenant_id == tenant.id,
                User.email == (email or "").strip().lower(),
            )
        )
    ).scalar_one_or_none()

    if user is None or not user.is_active:
        # Same silence for "no such address" and "that account is revoked". A
        # revoked user learning they are revoked from a reset form is a small
        # leak, but it is one this does not need to make.
        hash_password(secrets.token_urlsafe(16))
        return

    # Any outstanding link stops working. Somebody who asks twice should not end
    # up with two live credentials in their inbox, and the older email is the one
    # more likely to have been seen by somebody else.
    await session.execute(
        update(PasswordReset)
        .where(
            PasswordReset.user_id == user.id,
            PasswordReset.used_at.is_(None),
            PasswordReset.invalidated_at.is_(None),
        )
        .values(invalidated_at=datetime.now(UTC))
    )

    token, token_hash, lookup = _mint(tenant.id)
    reset = PasswordReset(
        tenant_id=tenant.id,
        user_id=user.id,
        token_hash=token_hash,
        lookup_hash=lookup,
        expires_at=datetime.now(UTC) + timedelta(minutes=RESET_TTL_MINUTES),
        requested_ip=ip,
        requested_user_agent=(user_agent or "")[:512] or None,
    )
    session.add(reset)
    await session.flush()

    await audit_service.record(
        session,
        tenant_id=tenant.id,
        # The actor is the account itself: nobody is authenticated, and
        # attributing this to "system" would lose who it was about.
        actor=Actor(type="user", id=user.id, label=user.email, ip=ip,
                    user_agent=user_agent),
        action=AuditAction.PASSWORD_RESET_REQUESTED,
        entity_type="user", entity_id=user.id,
        # No token, not even hashed. An audit trail readable by an auditor is
        # not a place to put a credential's index.
        payload={"email": user.email, "expires_in_minutes": RESET_TTL_MINUTES},
    )

    await notification_service.enqueue(
        session,
        tenant_id=tenant.id,
        key="user.password_reset",
        to_address=user.email,
        # Keyed to THIS RESET, not to the user.
        #
        # `enqueue` deduplicates on (tenant, template, entity) so that one state
        # change cannot send the same message twice. Keying this to the user made
        # every reset after the first look like a duplicate of it, so asking for
        # a second link queued nothing and the person waited for an email that
        # was never going to arrive. Each request is its own event and needs its
        # own identity.
        entity_type="password_reset",
        entity_id=reset.id,
        context={
            "reset_url": f"{base_url.rstrip('/')}/reset-password?token={token}",
            "expires_in": f"{RESET_TTL_MINUTES} minutes",
        },
    )


async def redeem(
    session: AsyncSession,
    *,
    token: str,
    new_password: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> User:
    """Set a new password from a reset token, and end every session.

    Raises `AuthenticationError` with one message for every way a token can be
    unusable. The only exception is a password the policy rejects — that is the
    caller's own input and telling them what is wrong with it helps nobody else.
    """
    from app.db.session import set_tenant_context

    tenant_id, secret = _split(token)
    await set_tenant_context(session, tenant_id)

    row = (
        await session.execute(
            select(PasswordReset).where(
                PasswordReset.lookup_hash == _lookup_hash(secret)
            )
        )
    ).scalar_one_or_none()

    now = datetime.now(UTC)
    if (
        row is None
        or row.used_at is not None
        or row.invalidated_at is not None
        or row.expires_at <= now
        or not verify_password(secret, row.token_hash)
    ):
        raise AuthenticationError(_GENERIC)

    user = (
        await session.execute(select(User).where(User.id == row.user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthenticationError(_GENERIC)

    # Checked against the same policy registration uses, and against this
    # person's own name and address — the two words most likely to be in a
    # password somebody chooses under pressure.
    validate_password(new_password, email=user.email, name=user.full_name)

    user.password_hash = hash_password(new_password)
    # A reset is also the way out of a lockout, which is often why somebody is
    # here in the first place.
    user.failed_login_count = 0
    user.locked_until = None

    row.used_at = now

    # Every session ends. Somebody resetting a password is locked out or
    # compromised, and in both cases the live sessions are not theirs to keep.
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason="password_reset")
    )
    await session.flush()

    await audit_service.record(
        session,
        tenant_id=tenant_id,
        actor=Actor(type="user", id=user.id, label=user.email, ip=ip,
                    user_agent=user_agent),
        action=AuditAction.PASSWORD_RESET_COMPLETED,
        entity_type="user", entity_id=user.id,
        payload={"sessions_revoked": True},
    )
    return user
