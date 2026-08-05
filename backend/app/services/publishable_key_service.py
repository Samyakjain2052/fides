"""Issuing, resolving and revoking publishable keys.

Kept separate from `api_key_service` on purpose. The two share a shape but not a
threat model, and a single module handling both is how a secret key ends up
returned twice or a publishable key ends up Argon2-hashed and unrecoverable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AuthenticationError, Conflict, NotFound, PermissionDenied
from app.core.permissions import PUBLISHABLE_SCOPES, Scope
from app.core.security import (
    ConsentTokenError,
    generate_publishable_key,
    generate_signing_secret,
    publishable_lookup_hash,
    verify_consent_token,
)
from app.models.audit import AuditAction
from app.models.publishable_key import PublishableKey
from app.models.tenant import Tenant
from app.services import audit_service
from app.services.audit_service import Actor


def _normalise_origin(origin: str) -> str:
    """Scheme + host + port, lowercased, no trailing slash.

    An origin is not a URL: `https://example.com/banner` and
    `https://example.com` are the same origin, and storing the first would make a
    perfectly valid request fail for a reason nobody would guess.
    """
    value = (origin or "").strip().rstrip("/").lower()
    if not value:
        raise Conflict("An allowed origin cannot be blank.")
    if not value.startswith(("http://", "https://")):
        raise Conflict(
            f"Origin {origin!r} must include a scheme, e.g. https://example.com"
        )
    if value.count("/") > 2:
        raise Conflict(
            f"Origin {origin!r} looks like a URL with a path. An origin is scheme, "
            "host and port only."
        )
    return value


async def create_key(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    name: str,
    allowed_origins: list[str],
    environment: str = "live",
    rate_limit_per_minute: int = 60,
    rate_limit_per_ip_per_minute: int = 10,
    require_signed_token: bool = False,
) -> tuple[PublishableKey, str]:
    """Issue a key. Returned in full, and retrievable again later — it is public.

    Capabilities are not a parameter. A publishable key holds `consent:collect`
    and nothing else; letting a caller pass a list would be the first step toward
    a browser bundle holding a withdraw scope. The database enforces the same
    ceiling independently.
    """
    origins = [_normalise_origin(o) for o in allowed_origins]
    if not origins:
        # An empty allowlist would mean the key works from nowhere, which is a
        # confusing thing to hand someone. Refuse at creation instead.
        raise Conflict(
            "A publishable key needs at least one allowed origin — the site that "
            "will use it."
        )

    full_key, prefix, lookup = generate_publishable_key(environment, tenant_id=tenant_id)

    row = PublishableKey(
        tenant_id=tenant_id,
        name=name.strip(),
        prefix=prefix,
        environment=environment,
        key=full_key,
        lookup_hash=lookup,
        capabilities=sorted(s.value for s in PUBLISHABLE_SCOPES),
        allowed_origins=origins,
        rate_limit_per_minute=rate_limit_per_minute,
        rate_limit_per_ip_per_minute=rate_limit_per_ip_per_minute,
        require_signed_token=require_signed_token,
        created_by=actor.id if actor.type == "user" else None,
    )
    session.add(row)
    await session.flush()

    # Mint the tenant's consent-token secret on first publishable key, so the
    # signed-token step-up is available without a separate setup step.
    tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
    if tenant is not None and not tenant.consent_token_secret:
        tenant.consent_token_secret = generate_signing_secret()
        await session.flush()

    await audit_service.record(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action=AuditAction.APIKEY_CREATED,
        entity_type="publishable_key",
        entity_id=row.id,
        payload={
            "name": row.name,
            "prefix": prefix,
            "capabilities": row.capabilities,
            "allowed_origins": origins,
            "require_signed_token": require_signed_token,
            # The key itself goes in the trail. It is public by design, and having
            # it there means "which key collected this?" is answerable from the
            # audit chain alone.
            "key": full_key,
        },
    )
    return row, full_key


async def resolve_key(session: AsyncSession, *, full_key: str) -> PublishableKey:
    """Find a publishable key. The caller must already have bound tenant context.

    There is no secret to verify — the key is published — so this is a lookup and
    a status check, not an authentication in the Argon2 sense. What makes it safe
    is that the key cannot do anything harmful, not that it was hard to obtain.
    """
    row = await session.scalar(
        select(PublishableKey).where(
            PublishableKey.lookup_hash == publishable_lookup_hash(full_key)
        )
    )
    if row is None:
        raise AuthenticationError("Invalid publishable key.")
    if row.revoked_at is not None:
        raise AuthenticationError("This publishable key has been revoked.")

    row.last_used_at = datetime.now(UTC)
    return row


def assert_capability(key: PublishableKey, scope: Scope) -> None:
    """403 in the same shape the secret-key scope guard uses, so an integrator
    reading either error learns the same thing."""
    if scope.value not in (key.capabilities or []):
        raise PermissionDenied(
            "This publishable key does not have the required scope.",
            required=[scope.value],
            granted=sorted(key.capabilities or []),
        )


def assert_origin_allowed(key: PublishableKey, origin: str | None) -> str | None:
    """Origin pinning.

    **This is defence-in-depth, not the security boundary.** The `Origin` header
    is set by browsers and trivially forged by anything that is not one — curl
    sends whatever you tell it to. It raises the cost of casual misuse of a key
    lifted from a bundle, and it does not stop a determined caller.

    What actually protects the data is elsewhere: the key can only collect (never
    withdraw, never read), and every record it creates is stamped with
    server-observed provenance so a forged one is still attributable.
    """
    allowed = key.allowed_origins or []
    if not allowed:
        raise PermissionDenied(
            "This publishable key has no allowed origins configured, so it cannot "
            "be used from a browser.",
            allowed_origins=[],
        )

    if origin is None:
        # A browser always sends Origin on a cross-origin POST. Its absence means
        # a non-browser caller, which is refused here and would in any case be
        # limited to collect-only.
        raise PermissionDenied(
            "A publishable key requires an Origin header.",
            allowed_origins=allowed,
        )

    if _normalise_origin(origin) not in allowed:
        raise PermissionDenied(
            f"Origin {origin!r} is not allowed for this publishable key.",
            allowed_origins=allowed,
        )
    return _normalise_origin(origin)


async def revoke_key(
    session: AsyncSession, *, tenant_id: uuid.UUID, key_id: uuid.UUID, actor: Actor
) -> PublishableKey:
    row = await session.scalar(
        select(PublishableKey).where(
            PublishableKey.id == key_id, PublishableKey.tenant_id == tenant_id
        )
    )
    if row is None:
        raise NotFound("No such publishable key.")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        await session.flush()
        await audit_service.record(
            session,
            tenant_id=tenant_id,
            actor=actor,
            action=AuditAction.APIKEY_REVOKED,
            entity_type="publishable_key",
            entity_id=row.id,
            payload={"name": row.name, "prefix": row.prefix},
        )
    return row


async def list_keys(session: AsyncSession, tenant_id: uuid.UUID) -> list[PublishableKey]:
    rows = await session.execute(
        select(PublishableKey)
        .where(PublishableKey.tenant_id == tenant_id)
        .order_by(PublishableKey.created_at.desc())
    )
    return list(rows.scalars().all())


# --------------------------------------------------------------------------- #
# The signed-token step-up
# --------------------------------------------------------------------------- #

async def resolve_bound_principal(
    session: AsyncSession, *, tenant_id: uuid.UUID, token: str
) -> str:
    """Verify a signed consent token and return the principal_ref it binds.

    Raises PermissionDenied on anything wrong with the token. Deliberately not a
    401: the caller's publishable key is fine, it is the step-up assertion that
    failed, and telling them "unauthenticated" would send them to debug the wrong
    credential.
    """
    tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
    secret = tenant.consent_token_secret if tenant else None
    if not secret:
        raise PermissionDenied(
            "This workspace has no consent-token secret configured, so signed "
            "tokens cannot be verified."
        )
    try:
        return verify_consent_token(secret=secret, token=token)
    except ConsentTokenError as exc:
        raise PermissionDenied(str(exc)) from exc
