"""Issuing, accepting and revoking invitations; and the rules around a user's access.

Read the model docstring for why invitations exist at all rather than
admin-set passwords. The short version: an administrator who knows a colleague's
password makes every audit entry attributed to that colleague arguable, and the
audit chain is this product's central claim.

Three rules in this file are worth stating up front.

**A workspace must keep one active admin.** Refused here with a sentence somebody
can act on, and enforced by a trigger on `users` so it holds whichever code path
tries. A workspace with no admin is unrecoverable without support access, which is
the worst possible support ticket.

**Losing access ends existing sessions.** The signed-in role is re-read from the
database on every request, so a *demotion* takes effect immediately — but refresh
tokens outlive it, and a demoted user's browser would keep minting new access
tokens until the family expired. Demotion and deactivation therefore revoke the
user's refresh-token families, reusing the machinery reuse-detection already has.

**The invite link is returned exactly once.** It is also emailed, but the console
shows it because the default notification provider writes to a log instead of
sending — an invitation nobody can deliver is a dead end, and pretending it was
sent would be worse.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import Conflict, NotFound
from app.core.permissions import Role, capabilities_for
from app.core.security import hash_password, verify_password
from app.models.audit import AuditAction
from app.models.invitation import INVITATION_TTL_HOURS, UserInvitation
from app.models.user import RefreshToken, User
from app.services import audit_service
from app.services.audit_service import Actor

logger = logging.getLogger("app.invitations")
_settings = get_settings()


class InvitationRefused(Conflict):
    """A reason this invitation operation must not proceed."""


class LastAdminRefused(Conflict):
    """The change would leave the workspace with no active administrator."""


# --------------------------------------------------------------------------- #
# Token handling — the same two-hash pattern as API keys
# --------------------------------------------------------------------------- #

def _lookup_hash(secret: str) -> str:
    """A deterministic index for finding the candidate row.

    Argon2 is salted, so an invitation cannot be looked up by its Argon2 hash.
    Keyed SHA-256 gives a stable index; Argon2 still does the verifying.
    """
    return hmac.new(
        _settings.jwt_secret.encode(), secret.encode(), hashlib.sha256
    ).hexdigest()


def _mint_token(tenant_id: uuid.UUID) -> tuple[str, str, str]:
    """Returns (full token, argon2 hash, lookup hash).

    The tenant is *inside* the token, as `<tenant-hex>.<secret>`. Acceptance
    happens before any tenant context exists and `user_invitations` is under RLS,
    so a lookup that does not already know the tenant matches zero rows and every
    valid invitation is rejected. That bug has been shipped three times in this
    codebase — refresh tokens, `ds_live_`, `pk_live_` — and this is the fourth
    place it would have happened.
    """
    secret = secrets.token_urlsafe(32)
    full = f"{tenant_id.hex}.{secret}"
    return full, hash_password(secret), _lookup_hash(secret)


def split_token(token: str) -> tuple[uuid.UUID, str]:
    """Pull the tenant back out of a presented token.

    A malformed token raises the same refusal as a wrong one — a caller must not
    be able to tell "that is not a token" from "that token is not yours".
    """
    generic = InvitationRefused(
        "That invitation link is not valid. It may have expired, already been "
        "used, or been withdrawn."
    )
    if "." not in token:
        raise generic
    tenant_hex, _, secret = token.partition(".")
    try:
        tenant_id = uuid.UUID(hex=tenant_hex)
    except ValueError:
        raise generic from None
    if not secret:
        raise generic
    return tenant_id, secret


# --------------------------------------------------------------------------- #
# Inviting
# --------------------------------------------------------------------------- #

async def invite(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    email: str,
    role: str,
    invited_by: uuid.UUID | None = None,
) -> tuple[UserInvitation, str]:
    """Issue an invitation. Returns the row and the raw token, once.

    The raw token is never stored and never returned again. If it is lost, the
    invitation is revoked and a new one issued — which is the same thing a
    password reset does, and for the same reason.
    """
    email = (email or "").strip().lower()
    if "@" not in email:
        raise InvitationRefused("That does not look like an email address.")
    try:
        Role(role)
    except ValueError:
        raise InvitationRefused(
            f"Unknown role {role!r}. One of: "
            f"{', '.join(r.value for r in Role)}."
        ) from None

    existing_user = await session.scalar(
        select(User).where(User.tenant_id == tenant_id, User.email == email)
    )
    if existing_user is not None:
        # Safe to say inside the console: the caller can already list every user
        # in this workspace, so this discloses nothing they cannot see. The
        # PUBLIC acceptance path is where non-disclosure matters.
        raise InvitationRefused(
            f"{email} already has an account in this workspace. Change their role "
            "instead, or deactivate them first."
        )

    now = datetime.now(UTC)
    full, token_hash, lookup = _mint_token(tenant_id)
    row = UserInvitation(
        tenant_id=tenant_id,
        email=email,
        role=role,
        token_hash=token_hash,
        lookup_hash=lookup,
        invited_by=invited_by,
        expires_at=now + timedelta(hours=INVITATION_TTL_HOURS),
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise InvitationRefused(
            f"{email} already has an invitation waiting. Revoke it first if you "
            "need to change the role or send a fresh link."
        ) from exc

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.INVITATION_SENT,
        entity_type="user_invitation", entity_id=row.id,
        payload={
            "email": email,
            "role": role,
            "expires_at": row.expires_at.isoformat(),
            # Deliberately not the token, nor its hash. An audit entry that
            # carried the credential would put it somewhere append-only forever.
        },
    )
    return row, full


async def send_invitation_email(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    invitation: UserInvitation,
    accept_url: str,
) -> None:
    """Email the link. Failure here does not undo the invitation.

    The link is also returned to the console for exactly this reason: the default
    provider writes to a log rather than sending, so an invitation that relied
    solely on email would be undeliverable in the shipped configuration. The
    delivery log records what happened either way.
    """
    from app.services import notification_service

    queued = await notification_service.enqueue(
        session,
        tenant_id=tenant_id,
        key="user.invitation",
        to_address=invitation.email,
        context={
            "role": invitation.role.replace("_", " "),
            "accept_url": accept_url,
            "expires_in": f"{INVITATION_TTL_HOURS} hours",
        },
        entity_type="user_invitation",
        entity_id=invitation.id,
    )
    await notification_service.send_now(session, notification=queued)


async def revoke(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    invitation_id: uuid.UUID,
    reason: str = "withdrawn by an administrator",
) -> UserInvitation:
    """Withdraw an invitation. The row is kept.

    No delete: the fact that a credential was issued, and what became of it, is
    worth more than a tidy table. An accepted invitation cannot be revoked —
    revoking it would suggest the resulting account is somehow provisional.
    """
    row = await session.scalar(
        select(UserInvitation).where(
            UserInvitation.tenant_id == tenant_id, UserInvitation.id == invitation_id
        )
    )
    if row is None:
        raise NotFound("No such invitation.")
    if row.accepted_at is not None:
        raise InvitationRefused(
            "That invitation was already accepted. Deactivate the resulting "
            "account instead."
        )
    if row.revoked_at is not None:
        return row

    row.revoked_at = datetime.now(UTC)
    row.revoked_reason = reason
    await session.flush()

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.INVITATION_REVOKED,
        entity_type="user_invitation", entity_id=row.id,
        payload={"email": row.email, "role": row.role, "reason": reason},
    )
    return row


async def list_invitations(
    session: AsyncSession, tenant_id: uuid.UUID, *, pending_only: bool = False
) -> list[UserInvitation]:
    stmt = select(UserInvitation).where(UserInvitation.tenant_id == tenant_id)
    if pending_only:
        stmt = stmt.where(
            UserInvitation.accepted_at.is_(None),
            UserInvitation.revoked_at.is_(None),
            UserInvitation.expires_at > datetime.now(UTC),
        )
    rows = await session.execute(stmt.order_by(UserInvitation.created_at.desc()))
    return list(rows.scalars().all())


# --------------------------------------------------------------------------- #
# Accepting — public, unauthenticated, creates a user
# --------------------------------------------------------------------------- #

async def accept(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    secret: str,
    full_name: str,
    password: str,
) -> User:
    """Turn an invitation into an account, in one transaction.

    Runs with tenant context already bound by the caller (the tenant came out of
    the token). Everything happens together — the user is created, the invitation
    is marked accepted and linked to the new user, and both audit entries are
    written — because a half-accepted invitation is either a credential that still
    works after use, or an account nobody can explain.

    Password policy is `registration_service.validate_password`, not a second
    implementation. One policy, or the weaker one becomes the real one.
    """
    from app.services.registration_service import validate_password

    generic = InvitationRefused(
        "That invitation link is not valid. It may have expired, already been "
        "used, or been withdrawn."
    )

    row = await session.scalar(
        select(UserInvitation).where(
            UserInvitation.tenant_id == tenant_id,
            UserInvitation.lookup_hash == _lookup_hash(secret),
        )
    )
    if row is None:
        raise generic
    # Argon2 verify even after the lookup matched: the lookup hash is keyed but
    # deterministic, and this is the check that actually authenticates.
    if not verify_password(secret, row.token_hash):
        raise generic
    if not row.is_usable:
        # Expired, accepted or revoked — all one message. Distinguishing them
        # would let somebody probe which invitations exist.
        raise generic

    full_name = (full_name or "").strip()
    if len(full_name) < 2:
        raise InvitationRefused("Please give the name this account should be under.")
    validate_password(password, email=row.email, name=full_name)

    now = datetime.now(UTC)
    user = User(
        tenant_id=tenant_id,
        email=row.email,
        password_hash=hash_password(password),
        full_name=full_name,
        # The invited role, not one the acceptor chooses. The whole point of the
        # token is that an administrator decided what this account may do.
        role=row.role,
        password_changed_at=now,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        # Somebody registered this address between the invitation and its
        # acceptance. Deliberately vague: this endpoint is public, and confirming
        # that an address has an account here is a disclosure.
        raise generic from exc

    row.accepted_at = now
    row.accepted_user_id = user.id
    await session.flush()

    actor = Actor(type="user", id=user.id, label=user.email)
    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.INVITATION_ACCEPTED,
        entity_type="user_invitation", entity_id=row.id,
        payload={"email": row.email, "role": row.role, "user_id": str(user.id)},
    )
    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.USER_CREATED,
        entity_type="user", entity_id=user.id,
        payload={"role": user.role, "via": "invitation", "invitation_id": str(row.id)},
    )
    return user


# --------------------------------------------------------------------------- #
# The last-admin rule
# --------------------------------------------------------------------------- #

async def assert_not_last_admin(
    session: AsyncSession, *, tenant_id: uuid.UUID, user: User, becoming_role: str | None,
    staying_active: bool,
) -> None:
    """Refuse a change that would leave nobody able to administer the workspace.

    Also enforced by a trigger on `users`, which is what makes it true. This exists
    so the caller gets a sentence they can act on instead of a raw integrity error
    — the trigger's message is for developers, this one is for a DPO.
    """
    losing_admin = user.role == "admin" and user.is_active and (
        becoming_role not in (None, "admin") or not staying_active
    )
    if not losing_admin:
        return

    remaining = await session.scalar(
        select(func.count())
        .select_from(User)
        .where(
            User.tenant_id == tenant_id,
            User.role == "admin",
            User.is_active.is_(True),
            User.id != user.id,
        )
    )
    if not remaining:
        raise LastAdminRefused(
            f"{user.email} is the only active administrator of this workspace. "
            "Promote somebody else first — a workspace with no administrator "
            "cannot be recovered without our support team."
        )


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #

async def list_sessions(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> list[dict]:
    """The user's live refresh-token families.

    One entry per family, not per token: a family is a browser, and the rotation
    within it is machinery nobody needs to see. What matters is "this person is
    signed in on three devices, one of them from an address you do not recognise".
    """
    now = datetime.now(UTC)
    rows = await session.execute(
        select(
            RefreshToken.family_id,
            func.min(RefreshToken.created_at).label("started_at"),
            func.max(RefreshToken.created_at).label("last_used_at"),
            func.max(RefreshToken.expires_at).label("expires_at"),
            func.count().label("rotations"),
        )
        .where(
            RefreshToken.tenant_id == tenant_id,
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now,
        )
        .group_by(RefreshToken.family_id)
        .order_by(func.max(RefreshToken.created_at).desc())
    )
    families = list(rows.all())

    out = []
    for fam in families:
        # The newest token in the family carries the context worth showing.
        newest = await session.scalar(
            select(RefreshToken)
            .where(
                RefreshToken.family_id == fam.family_id,
                RefreshToken.revoked_at.is_(None),
            )
            .order_by(RefreshToken.created_at.desc())
            .limit(1)
        )
        out.append({
            "family_id": fam.family_id,
            "started_at": fam.started_at,
            "last_used_at": fam.last_used_at,
            "expires_at": fam.expires_at,
            "rotations": fam.rotations,
            "ip_address": newest.ip_address if newest else None,
            "user_agent": newest.user_agent if newest else None,
        })
    return out


async def revoke_sessions(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    user: User,
    reason: str,
) -> int:
    """Sign a user out everywhere. Returns how many families were ended.

    Called directly by an administrator, and automatically on demotion or
    deactivation. The signed-in role is re-read per request so a demotion bites
    immediately, but a refresh token outlives it — without this, a demoted user's
    browser keeps minting access tokens until the family expires.
    """
    now = datetime.now(UTC)
    families = await session.execute(
        select(func.count(func.distinct(RefreshToken.family_id))).where(
            RefreshToken.tenant_id == tenant_id,
            RefreshToken.user_id == user.id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now,
        )
    )
    count = families.scalar() or 0

    await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.tenant_id == tenant_id,
            RefreshToken.user_id == user.id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now, revoked_reason=reason[:64])
    )
    await session.flush()

    if count:
        await audit_service.record(
            session, tenant_id=tenant_id, actor=actor,
            action=AuditAction.SESSIONS_REVOKED,
            entity_type="user", entity_id=user.id,
            payload={"email": user.email, "families": count, "reason": reason},
        )
    return count


# --------------------------------------------------------------------------- #
# The capability matrix
# --------------------------------------------------------------------------- #

def capability_matrix() -> dict:
    """What each role may do, generated from the enforcement itself.

    Read from `permissions.capabilities_for` rather than restated for display. A
    permissions screen that can disagree with the code enforcing it is worse than
    no permissions screen: it tells an administrator their workspace is configured
    one way while it behaves another.

    Note what is absent: there is no audit-write or audit-delete capability
    anywhere in the enum, so no role can be misconfigured into holding one. That
    is why the audit chain is append-only in fact and not merely by policy.
    """
    roles = [r.value for r in Role]
    all_caps = sorted({c.value for r in Role for c in capabilities_for(r)})
    return {
        "roles": roles,
        "capabilities": all_caps,
        "matrix": {
            r: sorted(c.value for c in capabilities_for(r)) for r in roles
        },
        "note": (
            "Generated from the capability matrix the API enforces, not a copy of "
            "it. There is deliberately no audit-write or audit-delete capability "
            "in the product at all, so no role can be configured to hold one."
        ),
    }
