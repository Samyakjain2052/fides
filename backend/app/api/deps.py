"""
Request dependencies — where authentication and authorisation actually happen.

This is the enforcement point referred to in ARCHITECTURE.md N3. The React
sidebar hiding a menu item is presentation; `require(Capability.X)` is the thing
that stops a request.

The flow for every authenticated request:

    bearer token → verify signature → load the user from the database
                 → open a transaction bound to that user's tenant (RLS)
                 → check the route's capability against the user's role

Note the "load the user from the database" step. The JWT carries `role` and
`tenant_id`, but they are not trusted on their own: a role revoked two minutes ago
must not keep working until the token expires. The token proves identity; the
database decides what that identity may do.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AuthenticationError, PermissionDenied
from app.core.permissions import Capability, Scope, role_can
from app.core.security import decode_access_token
from app.db.session import get_session_factory, set_tenant_context, unscoped_session
from app.models.api_key import ApiKey
from app.models.user import User
from app.services import api_key_service
from app.services.audit_service import Actor


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
async def get_unscoped_session() -> AsyncIterator[AsyncSession]:
    """For login and other pre-tenant operations only."""
    async with unscoped_session() as session:
        yield session


# --------------------------------------------------------------------------
# Human callers
# --------------------------------------------------------------------------
@dataclass
class CurrentUser:
    user: User
    session: AsyncSession
    request: Request

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.user.tenant_id

    @property
    def actor(self) -> Actor:
        return Actor(
            type="user",
            id=self.user.id,
            label=self.user.email,
            ip=client_ip(self.request),
            user_agent=self.request.headers.get("user-agent"),
        )


def client_ip(request: Request) -> str | None:
    """Trust X-Forwarded-For only for its first hop, and only because we run
    behind our own proxy. Without a proxy in front this header is caller-supplied
    and must not be believed."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


async def get_current_user(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> AsyncIterator[CurrentUser]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError("Missing bearer token.")

    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = decode_access_token(token)
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Access token expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Invalid access token.") from exc

    if claims.get("typ") != "access":
        # A refresh token presented as a bearer token would otherwise be accepted.
        raise AuthenticationError("Wrong token type.")

    tenant_id = uuid.UUID(claims["tenant_id"])
    user_id = uuid.UUID(claims["sub"])

    # One transaction for the whole request, bound to the tenant. Everything the
    # handler touches is inside it, so RLS is in force for every query.
    async with get_session_factory()() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_id, actor_id=user_id)

            user = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one_or_none()

            # RLS makes this the tenant check as well: a token for tenant A cannot
            # load a user from tenant B, because the row is invisible.
            if user is None or not user.is_active:
                raise AuthenticationError("Account is no longer active.")

            yield CurrentUser(user=user, session=session, request=request)


def require(*capabilities: Capability):
    """Route guard. Every protected route declares what it needs.

    Requires ALL listed capabilities, not any — a route that both reads and
    mutates should say so.
    """

    async def _guard(
        current: Annotated[CurrentUser, Depends(get_current_user)],
    ) -> CurrentUser:
        missing = [c for c in capabilities if not role_can(current.user.role, c)]
        if missing:
            raise PermissionDenied(
                "Your role does not permit this action.",
                required=[c.value for c in missing],
                role=current.user.role,
            )
        return current

    return _guard


# --------------------------------------------------------------------------
# Machine callers
# --------------------------------------------------------------------------
@dataclass
class CurrentApiKey:
    key: ApiKey
    session: AsyncSession
    request: Request

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.key.tenant_id

    @property
    def actor(self) -> Actor:
        return Actor(
            type="api_key",
            id=self.key.id,
            label=f"{self.key.prefix}/{self.key.name}",
            ip=client_ip(self.request),
            user_agent=self.request.headers.get("user-agent"),
        )


async def get_current_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> AsyncIterator[CurrentApiKey]:
    """Accepts `X-API-Key: ds_live_…` or `Authorization: Bearer ds_live_…`.

    The lookup runs unscoped — we do not know the tenant until the key resolves —
    then the transaction is rebound to that key's tenant before the handler runs.
    """
    raw = x_api_key
    if not raw and authorization and authorization.lower().startswith("bearer "):
        candidate = authorization.split(" ", 1)[1].strip()
        if candidate.startswith("ds_"):
            raw = candidate
    if not raw:
        raise AuthenticationError("Missing API key.")

    async with get_session_factory()() as session:
        async with session.begin():
            key = await api_key_service.authenticate_key(session, full_key=raw)
            await set_tenant_context(session, key.tenant_id, actor_id=key.id)
            yield CurrentApiKey(key=key, session=session, request=request)


def require_scope(*scopes: Scope):
    """Scope guard for the public API. Least privilege per key."""

    async def _guard(
        caller: Annotated[CurrentApiKey, Depends(get_current_api_key)],
    ) -> CurrentApiKey:
        held = set(caller.key.scopes)
        missing = [s.value for s in scopes if s.value not in held]
        if missing:
            raise PermissionDenied(
                "This API key does not have the required scope.",
                required=missing,
                granted=sorted(held),
            )
        return caller

    return _guard


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]
UnscopedSession = Annotated[AsyncSession, Depends(get_unscoped_session)]
