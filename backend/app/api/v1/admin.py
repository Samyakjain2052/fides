"""Tenant administration: users and API keys."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import select

from app.api.deps import CurrentUser, require
from app.core.errors import Conflict
from app.core.permissions import Capability
from app.models.user import User
from app.schemas.auth import UserOut
from app.models.publishable_key import PublishableKey
from app.schemas.tenant import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    ConsentTokenSecretOut,
    PublishableKeyCreate,
    PublishableKeyOut,
    RoleChange,
    UserCreate,
)
from app.services import api_key_service, publishable_key_service, tenant_service

router = APIRouter(prefix="/admin", tags=["admin"])


# ------------------------------------------------------------------- users --
@router.get("/users", response_model=list[UserOut], summary="List users in this tenant")
async def list_users(
    current: Annotated[CurrentUser, Depends(require(Capability.USER_MANAGE))],
) -> list[UserOut]:
    rows = (
        await current.session.execute(select(User).order_by(User.created_at.desc()))
    ).scalars()
    return [UserOut.model_validate(r) for r in rows]


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED,
             summary="Add a user")
async def create_user(
    payload: UserCreate,
    current: Annotated[CurrentUser, Depends(require(Capability.USER_MANAGE))],
) -> UserOut:
    user = await tenant_service.create_user(
        current.session,
        tenant_id=current.tenant_id,
        email=payload.email,
        full_name=payload.full_name,
        role=payload.role,
        password=payload.password,
        actor=current.actor,
    )
    return UserOut.model_validate(user)


@router.patch("/users/{user_id}/role", response_model=UserOut, summary="Change a user's role")
async def change_role(
    user_id: uuid.UUID,
    payload: RoleChange,
    current: Annotated[CurrentUser, Depends(require(Capability.USER_MANAGE))],
) -> UserOut:
    # Guard against an admin removing their own last privilege and locking the
    # tenant out of its own console.
    if user_id == current.user.id and payload.role != current.user.role:
        raise Conflict("You cannot change your own role. Ask another admin.")

    user = await tenant_service.change_role(
        current.session, tenant_id=current.tenant_id, user_id=user_id,
        new_role=payload.role, actor=current.actor,
    )
    return UserOut.model_validate(user)


@router.post("/users/{user_id}/deactivate", response_model=UserOut, summary="Revoke access")
async def deactivate_user(
    user_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.USER_MANAGE))],
) -> UserOut:
    if user_id == current.user.id:
        raise Conflict("You cannot deactivate your own account.")

    user = await tenant_service.deactivate_user(
        current.session, tenant_id=current.tenant_id, user_id=user_id, actor=current.actor
    )
    return UserOut.model_validate(user)


# ---------------------------------------------------------------- api keys --
@router.get("/api-keys", response_model=list[ApiKeyOut], summary="List API keys")
async def list_api_keys(
    current: Annotated[CurrentUser, Depends(require(Capability.APIKEY_MANAGE))],
) -> list[ApiKeyOut]:
    rows = await api_key_service.list_keys(current.session, tenant_id=current.tenant_id)
    return [ApiKeyOut.model_validate(r) for r in rows]


@router.post("/api-keys", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED,
             summary="Mint an API key (shown once)")
async def create_api_key(
    payload: ApiKeyCreate,
    current: Annotated[CurrentUser, Depends(require(Capability.APIKEY_MANAGE))],
) -> ApiKeyCreated:
    """The plaintext key is in this response and nowhere else, ever again."""
    row, plaintext = await api_key_service.create_key(
        current.session,
        tenant_id=current.tenant_id,
        name=payload.name,
        scopes=payload.scopes,
        environment=payload.environment,
        expires_in_days=payload.expires_in_days,
        actor=current.actor,
    )
    return ApiKeyCreated(**ApiKeyOut.model_validate(row).model_dump(), api_key=plaintext)


@router.post("/api-keys/{key_id}/revoke", response_model=ApiKeyOut, summary="Revoke an API key")
async def revoke_api_key(
    key_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.APIKEY_MANAGE))],
) -> ApiKeyOut:
    row = await api_key_service.revoke_key(
        current.session, tenant_id=current.tenant_id, key_id=key_id, actor=current.actor
    )
    return ApiKeyOut.model_validate(row)


# --------------------------------------------------------------------------- #
# Publishable keys — browser-safe, collect-only
# --------------------------------------------------------------------------- #

@router.get(
    "/publishable-keys", response_model=list[PublishableKeyOut],
    summary="List publishable keys",
)
async def list_publishable_keys(
    current: Annotated[CurrentUser, Depends(require(Capability.USER_MANAGE))],
) -> list[PublishableKey]:
    return await publishable_key_service.list_keys(current.session, current.tenant_id)


@router.post(
    "/publishable-keys", response_model=PublishableKeyOut,
    status_code=status.HTTP_201_CREATED,
    summary="Issue a publishable key for a consent banner",
)
async def create_publishable_key(
    body: PublishableKeyCreate,
    current: Annotated[CurrentUser, Depends(require(Capability.USER_MANAGE))],
) -> PublishableKey:
    """Returns the key in full, and will again on every list.

    That is the difference from a secret key: this one is designed to be published
    in a page, so hiding it after creation would protect nothing and would just
    make reinstalling a banner require issuing a new key.
    """
    row, _full = await publishable_key_service.create_key(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        name=body.name,
        allowed_origins=body.allowed_origins,
        environment=body.environment,
        rate_limit_per_minute=body.rate_limit_per_minute,
        rate_limit_per_ip_per_minute=body.rate_limit_per_ip_per_minute,
        require_signed_token=body.require_signed_token,
    )
    return row


@router.post(
    "/publishable-keys/{key_id}/revoke", response_model=PublishableKeyOut,
    summary="Revoke a publishable key",
)
async def revoke_publishable_key(
    key_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.USER_MANAGE))],
) -> PublishableKey:
    return await publishable_key_service.revoke_key(
        current.session, tenant_id=current.tenant_id, key_id=key_id, actor=current.actor
    )


@router.get(
    "/consent-token-secret", response_model=ConsentTokenSecretOut,
    summary="The signing secret for the signed-token step-up",
)
async def consent_token_secret(
    current: Annotated[CurrentUser, Depends(require(Capability.USER_MANAGE))],
) -> ConsentTokenSecretOut:
    """A real secret — it belongs on the integrator's server, never in a page.

    Minted on the first publishable key so the step-up needs no separate setup.
    """
    from sqlalchemy import select as _select

    from app.core.security import CONSENT_TOKEN_TTL_SECONDS, generate_signing_secret
    from app.models.tenant import Tenant

    tenant = await current.session.scalar(
        _select(Tenant).where(Tenant.id == current.tenant_id)
    )
    if tenant.consent_token_secret is None:
        tenant.consent_token_secret = generate_signing_secret()
        await current.session.flush()
    return ConsentTokenSecretOut(
        secret=tenant.consent_token_secret,
        token_ttl_seconds=CONSENT_TOKEN_TTL_SECONDS,
    )
