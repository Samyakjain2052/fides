"""API key lifecycle — machine credentials for a customer's own systems."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AuthenticationError, NotFound
from app.core.permissions import validate_scopes
from app.core.security import api_key_lookup_hash, generate_api_key, verify_api_key
from app.models.api_key import ApiKey
from app.models.audit import AuditAction
from app.services import audit_service
from app.services.audit_service import Actor


async def create_key(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    scopes: list[str],
    environment: str = "live",
    expires_in_days: int | None = None,
    actor: Actor,
) -> tuple[ApiKey, str]:
    """Mint a key. Returns (row, plaintext).

    The plaintext is returned once, to be shown once. Only its Argon2 hash and a
    deterministic lookup hash are stored, so we genuinely cannot reveal it again —
    which is the property that makes a leak recoverable by rotation rather than
    by trusting that nobody copied it.
    """
    validated = validate_scopes(scopes)
    full_key, prefix, key_hash = generate_api_key(environment)

    row = ApiKey(
        tenant_id=tenant_id,
        name=name,
        prefix=prefix,
        environment=environment,
        key_hash=key_hash,
        lookup_hash=api_key_lookup_hash(full_key),
        scopes=[s.value for s in validated],
        expires_at=(
            datetime.now(UTC) + timedelta(days=expires_in_days) if expires_in_days else None
        ),
        created_by=actor.id,
    )
    session.add(row)
    await session.flush()

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor, action=AuditAction.APIKEY_CREATED,
        entity_type="api_key", entity_id=row.id,
        # Scopes and prefix are recorded; the key itself never is.
        payload={"name": name, "prefix": prefix, "scopes": row.scopes, "environment": environment},
    )
    return row, full_key


async def authenticate_key(session: AsyncSession, *, full_key: str) -> ApiKey:
    """Resolve an inbound key to its row, or refuse.

    Two-hash pattern: the deterministic lookup hash finds the candidate (Argon2 is
    salted, so you cannot query by it), then Argon2 actually verifies. Fast index,
    slow compare.
    """
    row = (
        await session.execute(
            select(ApiKey).where(ApiKey.lookup_hash == api_key_lookup_hash(full_key))
        )
    ).scalar_one_or_none()

    if row is None or not verify_api_key(full_key, row.key_hash):
        raise AuthenticationError("Invalid API key.")
    if row.revoked_at is not None:
        raise AuthenticationError("This API key has been revoked.")
    if row.expires_at is not None and row.expires_at <= datetime.now(UTC):
        raise AuthenticationError("This API key has expired.")

    # Surfaces dead keys in the console so customers can prune them. Best-effort:
    # not worth failing a request over.
    row.last_used_at = datetime.now(UTC)
    return row


async def revoke_key(
    session: AsyncSession, *, tenant_id: uuid.UUID, key_id: uuid.UUID, actor: Actor
) -> ApiKey:
    row = (
        await session.execute(
            select(ApiKey).where(ApiKey.id == key_id, ApiKey.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("API key not found.")

    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        await session.flush()
        await audit_service.record(
            session, tenant_id=tenant_id, actor=actor, action=AuditAction.APIKEY_REVOKED,
            entity_type="api_key", entity_id=row.id, payload={"name": row.name},
        )
    return row


async def list_keys(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[ApiKey]:
    return list(
        (
            await session.execute(
                select(ApiKey).where(ApiKey.tenant_id == tenant_id).order_by(ApiKey.created_at.desc())
            )
        ).scalars()
    )
