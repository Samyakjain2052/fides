"""Tenant and user provisioning."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.core.permissions import Role
from app.core.security import hash_password
from app.models.audit import AuditAction
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit_service
from app.services.audit_service import Actor


async def create_tenant(
    session: AsyncSession,
    *,
    slug: str,
    name: str,
    admin_email: str,
    admin_password: str,
    admin_name: str,
    legal_name: str | None = None,
    actor: Actor | None = None,
) -> tuple[Tenant, User]:
    """Provision a customer plus their first Admin/DPO.

    A tenant with no admin is unusable, so the two are created in one
    transaction — either both exist or neither does.
    """
    existing = (
        await session.execute(select(Tenant.id).where(Tenant.slug == slug))
    ).scalar_one_or_none()
    if existing:
        raise Conflict(f"A tenant with slug '{slug}' already exists.")

    tenant = Tenant(
        slug=slug,
        name=name,
        legal_name=legal_name or name,
        # DPDP §13 requires a *published* Grievance Officer. Defaulted to the
        # first admin so no workspace is ever non-compliant purely by omission —
        # and set here as well as in `registration_service` because whether a
        # statutory contact exists must not depend on which code path created the
        # tenant. Changeable afterwards; see grievance_service.set_officer.
        grievance_officer_name=admin_name,
        grievance_officer_email=admin_email.lower(),
    )
    session.add(tenant)
    await session.flush()

    # Tenant context has to be set before anything tenant-scoped is written —
    # RLS's WITH CHECK would otherwise reject the insert. Failing closed here is
    # the policy working, not a bug.
    from app.db.session import set_tenant_context

    await set_tenant_context(session, tenant.id)

    admin = User(
        tenant_id=tenant.id,
        email=admin_email.lower(),
        password_hash=hash_password(admin_password),
        full_name=admin_name,
        role=Role.ADMIN.value,
        password_changed_at=datetime.now(UTC),
    )
    session.add(admin)
    await session.flush()

    who = actor or Actor(type="system", label="provisioning")
    await audit_service.record(
        session, tenant_id=tenant.id, actor=who, action=AuditAction.TENANT_CREATED,
        entity_type="tenant", entity_id=tenant.id,
        payload={"slug": slug, "name": name},
    )
    await audit_service.record(
        session, tenant_id=tenant.id, actor=who, action=AuditAction.USER_CREATED,
        entity_type="user", entity_id=admin.id,
        payload={"role": admin.role, "bootstrap": True},
    )

    # Same reasoning as the officer default above: without these every statutory
    # notification this workspace tries to send suppresses with "no active
    # template". Honest, but useless — and a workspace that silently cannot tell
    # anybody anything is a trap regardless of which code path created it.
    from app.services import notification_service

    await notification_service.seed_default_templates(session, tenant_id=tenant.id)
    return tenant, admin


async def get_user(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> User:
    """One user in this tenant, or a 404.

    Scoped by tenant_id as well as id even though RLS already restricts the row.
    Belt and braces: the explicit predicate documents the intent, and a future
    caller running as the owner role would otherwise reach across tenants.
    """
    row = await session.scalar(
        select(User).where(User.tenant_id == tenant_id, User.id == user_id)
    )
    if row is None:
        raise NotFound("No such user in this workspace.")
    return row


async def create_user(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    email: str,
    full_name: str,
    role: str,
    password: str | None,
    actor: Actor,
) -> User:
    try:
        Role(role)
    except ValueError as exc:
        raise Conflict(f"Unknown role '{role}'.") from exc

    user = User(
        tenant_id=tenant_id,
        email=email.lower(),
        full_name=full_name,
        role=role,
        password_hash=hash_password(password) if password else None,
        password_changed_at=datetime.now(UTC) if password else None,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise Conflict("A user with that email already exists for this tenant.") from exc

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor, action=AuditAction.USER_CREATED,
        entity_type="user", entity_id=user.id, payload={"role": role},
    )
    return user


async def change_role(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID, new_role: str, actor: Actor
) -> User:
    try:
        Role(new_role)
    except ValueError as exc:
        raise Conflict(f"Unknown role '{new_role}'.") from exc

    user = (
        await session.execute(
            select(User).where(User.id == user_id, User.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if user is None:
        raise NotFound("User not found.")

    old_role = user.role
    if old_role == new_role:
        return user
    user.role = new_role
    await session.flush()

    # Both values recorded: a role change is only meaningful as a transition.
    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor, action=AuditAction.USER_ROLE_CHANGED,
        entity_type="user", entity_id=user.id,
        payload={"from": old_role, "to": new_role},
    )
    return user


async def reactivate_user(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID, actor: Actor
) -> User:
    """Restore a revoked account.

    Added because revoking was one-way: an admin who clicked the wrong row had
    locked somebody out with no path back except a database edit. That is a bad
    property for a destructive-looking action sitting in a table of similar rows.

    Sessions are deliberately NOT restored. Their refresh tokens were revoked
    when access was withdrawn and revocation is meant to be final — the person
    signs in again, which is also the only way we learn they still know their
    password.
    """
    user = (
        await session.execute(
            select(User).where(User.id == user_id, User.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if user is None:
        raise NotFound("User not found.")

    if user.is_active:
        return user

    user.is_active = True
    # Cleared so a lockout from before the revocation does not survive it: an
    # account restored into a locked state looks reactivated and is not.
    user.failed_login_count = 0
    user.locked_until = None
    await session.flush()

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.USER_REACTIVATED,
        entity_type="user", entity_id=user.id,
        payload={"sessions_restored": False},
    )
    return user


async def deactivate_user(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID, actor: Actor
) -> User:
    user = (
        await session.execute(
            select(User).where(User.id == user_id, User.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if user is None:
        raise NotFound("User not found.")

    user.is_active = False
    # Revoking access has to kill live sessions too, or the user keeps working
    # with an access token until it expires.
    from sqlalchemy import update

    from app.models.user import RefreshToken

    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC), revoked_reason="user_deactivated")
    )
    await session.flush()

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor, action=AuditAction.USER_DEACTIVATED,
        entity_type="user", entity_id=user.id, payload={"sessions_revoked": True},
    )
    return user
