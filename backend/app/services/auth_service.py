"""
Authentication: password login, refresh rotation, logout.

Two behaviours here are security decisions rather than mechanics, and both are
easy to get wrong:

* **Login failures are indistinguishable.** Unknown tenant, unknown email, wrong
  password and deactivated account all produce the same error and the same
  timing. Distinguishing them turns the login form into a user-enumeration oracle
  — "this email has an account here" is itself personal data.

* **Refresh tokens are single use, and reuse revokes the family.** Presenting a
  token that was already spent means it leaked, so every token in that lineage
  dies. The legitimate user gets logged out too; that is the correct trade — a
  forced re-login beats an attacker holding a live session.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AuthenticationError
from app.core.security import (
    api_key_lookup_hash,
    create_access_token,
    generate_refresh_token,
    hash_password,
    needs_rehash,
    parse_refresh_token,
    verify_password,
    verify_refresh_token,
)
from app.models.audit import AuditAction
from app.models.tenant import Tenant
from app.models.user import RefreshToken, User
from app.services import audit_service
from app.services.audit_service import Actor

logger = logging.getLogger("app.auth")
_settings = get_settings()

# One message for every failure mode. See the module docstring.
_GENERIC_FAILURE = "Email or password is incorrect."


@dataclass
class TokenPair:
    access_token: str
    access_expires_at: datetime
    refresh_token: str        # returned to be set as an HttpOnly cookie, never stored
    refresh_expires_at: datetime
    user: User


async def authenticate(
    session: AsyncSession,
    *,
    tenant_slug: str,
    email: str,
    password: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> TokenPair:
    """Verify a password and issue a token pair.

    Runs on an *unscoped* session because we cannot know the tenant until the slug
    is resolved. Every subsequent query is explicitly filtered by that tenant id,
    and the tenant context is set before the audit write.
    """
    tenant = (
        await session.execute(select(Tenant).where(Tenant.slug == tenant_slug, Tenant.is_active))
    ).scalar_one_or_none()

    if tenant is None:
        # Still spend time hashing, so a bad tenant slug is not faster than a bad
        # password. A timing difference here leaks which companies are customers.
        hash_password(password)
        raise AuthenticationError(_GENERIC_FAILURE)

    from app.db.session import set_tenant_context

    await set_tenant_context(session, tenant.id)

    user = (
        await session.execute(
            select(User).where(User.tenant_id == tenant.id, User.email == email.lower())
        )
    ).scalar_one_or_none()

    if user is None:
        hash_password(password)
        raise AuthenticationError(_GENERIC_FAILURE)

    now = datetime.now(UTC)

    if user.locked_until and user.locked_until > now:
        # Told plainly: the account exists (they proved that by locking it), and
        # silence here just generates support tickets.
        raise AuthenticationError(
            f"Account locked after too many failed attempts. Try again after "
            f"{user.locked_until.isoformat()}."
        )

    if not user.is_active or not user.password_hash or not verify_password(password, user.password_hash):
        # Recorded in its OWN transaction, which commits before we raise.
        #
        # This matters more than it looks. Writing the counter on the request's
        # transaction and then raising rolls the write back — so the lockout
        # counter never increments, brute-force protection silently does nothing,
        # and no failed login is ever recorded. Both were live bugs here until the
        # test suite caught them.
        await _record_failed_attempt(
            tenant_id=tenant.id, user_id=user.id, email=user.email, ip=ip, user_agent=user_agent
        )
        raise AuthenticationError(_GENERIC_FAILURE)

    # Success: clear the counters and transparently upgrade the hash if our cost
    # parameters have increased since it was written.
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    pair = await _issue_tokens(
        session, user=user, family_id=uuid.uuid4(), ip=ip, user_agent=user_agent
    )

    await audit_service.record(
        session,
        tenant_id=tenant.id,
        actor=Actor(type="user", id=user.id, label=user.email, ip=ip, user_agent=user_agent),
        action=AuditAction.LOGIN_SUCCEEDED,
        entity_type="user",
        entity_id=user.id,
        payload={"role": user.role, "mfa": user.mfa_enabled},
    )
    return pair


async def refresh(
    session: AsyncSession,
    *,
    raw_token: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> TokenPair:
    """Exchange a refresh token for a new pair, consuming the old one."""
    from app.db.session import set_tenant_context

    # The token carries its tenant, and it must: `refresh_tokens` is under RLS, so
    # a lookup with no tenant context matches nothing. Bind the session first,
    # then search inside that tenant.
    tenant_id = parse_refresh_token(raw_token)
    if tenant_id is None:
        raise AuthenticationError("Invalid session. Please sign in again.")
    await set_tenant_context(session, tenant_id)

    lookup = api_key_lookup_hash(raw_token)
    row = (
        await session.execute(select(RefreshToken).where(RefreshToken.lookup_hash == lookup))
    ).scalar_one_or_none()

    # A forged tenant id simply finds no row — the hash is still what authenticates.
    if row is None or not verify_refresh_token(raw_token, row.token_hash):
        raise AuthenticationError("Invalid session. Please sign in again.")

    now = datetime.now(UTC)

    # ---- reuse detection -------------------------------------------------
    # A spent token being presented again means two parties hold it. We cannot
    # tell which one is legitimate, so we trust neither and kill the lineage.
    if row.used_at is not None:
        # Same reasoning as the failed-login path: this has to commit, because we
        # are about to raise. A revocation that rolls back leaves the stolen token
        # live, which is the opposite of the intent.
        await _revoke_family(
            tenant_id=row.tenant_id, family_id=row.family_id, user_id=row.user_id,
            ip=ip, user_agent=user_agent,
        )
        logger.warning("refresh token reuse detected; family revoked")
        raise AuthenticationError("Session expired. Please sign in again.")

    if row.revoked_at is not None or row.expires_at <= now:
        raise AuthenticationError("Session expired. Please sign in again.")

    user = (await session.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthenticationError("Session expired. Please sign in again.")

    # Consume this token, then mint the next in the same family.
    row.used_at = now

    pair = await _issue_tokens(
        session, user=user, family_id=row.family_id, ip=ip, user_agent=user_agent
    )

    await audit_service.record(
        session,
        tenant_id=row.tenant_id,
        actor=Actor(type="user", id=user.id, label=user.email, ip=ip, user_agent=user_agent),
        action=AuditAction.TOKEN_REFRESHED,
        entity_type="user",
        entity_id=user.id,
        payload={"family": str(row.family_id)},
    )
    return pair


async def logout(
    session: AsyncSession, *, raw_token: str | None, user: User, ip: str | None = None
) -> None:
    """Revoke the presented token's whole family.

    The family, not just the one token: the user asked to end this session, and
    leaving its lineage alive would let a stolen token keep refreshing.
    """
    if raw_token:
        lookup = api_key_lookup_hash(raw_token)
        row = (
            await session.execute(select(RefreshToken).where(RefreshToken.lookup_hash == lookup))
        ).scalar_one_or_none()
        if row is not None:
            await session.execute(
                update(RefreshToken)
                .where(RefreshToken.family_id == row.family_id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=datetime.now(UTC), revoked_reason="logout")
            )

    await audit_service.record(
        session,
        tenant_id=user.tenant_id,
        actor=Actor(type="user", id=user.id, label=user.email, ip=ip),
        action=AuditAction.LOGOUT,
        entity_type="user",
        entity_id=user.id,
    )


async def _issue_tokens(
    session: AsyncSession,
    *,
    user: User,
    family_id: uuid.UUID,
    ip: str | None,
    user_agent: str | None,
) -> TokenPair:
    access, access_exp = create_access_token(
        user_id=user.id, tenant_id=user.tenant_id, role=user.role
    )
    raw_refresh, refresh_hash = generate_refresh_token(user.tenant_id)
    refresh_exp = datetime.now(UTC) + timedelta(days=_settings.refresh_token_ttl_days)

    session.add(
        RefreshToken(
            tenant_id=user.tenant_id,
            user_id=user.id,
            family_id=family_id,
            token_hash=refresh_hash,
            lookup_hash=api_key_lookup_hash(raw_refresh),
            expires_at=refresh_exp,
            user_agent=(user_agent or "")[:512] or None,
            ip_address=ip,
        )
    )
    await session.flush()

    return TokenPair(
        access_token=access,
        access_expires_at=access_exp,
        refresh_token=raw_refresh,
        refresh_expires_at=refresh_exp,
        user=user,
    )


# --------------------------------------------------------------------------
# Out-of-band writes
#
# Both of these run in their own session and commit, because the request they
# belong to is about to fail. Anything written on the failing transaction is
# rolled back with it — which would silently disable lockout and lose the
# evidence of the attempt.
# --------------------------------------------------------------------------
async def _record_failed_attempt(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    email: str,
    ip: str | None,
    user_agent: str | None,
) -> None:
    from app.db.session import tenant_session

    async with tenant_session(tenant_id) as s:
        user = (await s.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if user is None:
            return
        now = datetime.now(UTC)
        user.failed_login_count += 1
        locked = user.failed_login_count >= _settings.max_failed_logins
        if locked:
            user.locked_until = now + timedelta(minutes=_settings.lockout_minutes)
            user.failed_login_count = 0
        await s.flush()
        await audit_service.record(
            s,
            tenant_id=tenant_id,
            actor=Actor(type="user", id=user_id, label=email, ip=ip, user_agent=user_agent),
            action=AuditAction.ACCOUNT_LOCKED if locked else AuditAction.LOGIN_FAILED,
            entity_type="user",
            entity_id=user_id,
            # No password, no hash, not even its length. An audit trail that helps
            # an attacker who later reads it is a liability.
            payload={"reason": "invalid_credentials", "attempt": user.failed_login_count},
        )


async def _revoke_family(
    *,
    tenant_id: uuid.UUID,
    family_id: uuid.UUID,
    user_id: uuid.UUID,
    ip: str | None,
    user_agent: str | None,
) -> None:
    from app.db.session import tenant_session

    async with tenant_session(tenant_id) as s:
        await s.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC), revoked_reason="reuse_detected")
        )
        await audit_service.record(
            s,
            tenant_id=tenant_id,
            actor=Actor(type="user", id=user_id, ip=ip, user_agent=user_agent),
            action=AuditAction.TOKEN_REUSE_DETECTED,
            entity_type="refresh_token_family",
            entity_id=family_id,
            payload={"revoked_family": str(family_id)},
        )
