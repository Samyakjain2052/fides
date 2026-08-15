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
from app.core.security import (
    decode_access_token,
    hash_ip,
    parse_api_key,
    parse_publishable_key,
)
from app.db.session import get_session_factory, set_tenant_context, unscoped_session
from app.models.api_key import ApiKey
from app.models.publishable_key import PublishableKey
from app.models.user import User
from app.services import api_key_service, publishable_key_service
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

    The tenant is read out of the key and bound BEFORE the lookup, because
    `api_keys` is under row-level security: a query made with no tenant context
    matches nothing, so authentication would fail for every valid key. Same
    reasoning as the refresh token, and the same fix.

    Reading the tenant from the key is not trusting it. The Argon2 verify below is
    what authenticates, and RLS means a key claiming another tenant simply finds
    no row there.
    """
    raw = x_api_key
    if not raw and authorization and authorization.lower().startswith("bearer "):
        candidate = authorization.split(" ", 1)[1].strip()
        if candidate.startswith("ds_"):
            raw = candidate
    if not raw:
        raise AuthenticationError("Missing API key.")

    claimed_tenant, raw = parse_api_key(raw)
    if claimed_tenant is None:
        # No tenant in the key: nothing to bind, so the lookup could only ever
        # return zero rows. Refuse with the same message as a bad secret rather
        # than leaking that the format was the problem.
        raise AuthenticationError("Invalid API key.")

    async with get_session_factory()() as session:
        async with session.begin():
            await set_tenant_context(session, claimed_tenant)
            key = await api_key_service.authenticate_key(session, full_key=raw)
            # Re-bind with the authenticated key as the actor, now that we know it.
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


# --------------------------------------------------------------------------
# Publishable-key callers (browser banners)
#
# A separate dependency chain from CurrentApiKey, not a flag on it. The two have
# different threat models, and a shared code path is how a publishable key ends
# up somewhere only a secret key belongs.
# --------------------------------------------------------------------------
@dataclass
class CurrentPublishableKey:
    key: "PublishableKey"
    session: AsyncSession
    request: Request

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.key.tenant_id

    @property
    def actor(self) -> Actor:
        return Actor(
            # A distinct actor type in the audit trail. "This came from a browser
            # banner using a published key" is materially different provenance
            # from "a server-side integration did this", and the trail should not
            # blur them.
            type="publishable_key",
            id=self.key.id,
            label=f"{self.key.prefix}/{self.key.name}",
            ip=client_ip(self.request),
            user_agent=self.request.headers.get("user-agent"),
        )

    @property
    def ip_hash(self) -> str | None:
        return hash_ip(client_ip(self.request))


async def get_current_publishable_key(
    request: Request,
    x_publishable_key: Annotated[str | None, Header(alias="X-Publishable-Key")] = None,
) -> AsyncIterator[CurrentPublishableKey]:
    """Resolve a `pk_live_…` key.

    Only accepted in its own header. Deliberately NOT via `Authorization: Bearer`,
    which is where secret keys go — a publishable key arriving in the same slot as
    a secret one invites confusing the two at every layer above.

    The tenant is read from the key and bound BEFORE the lookup, because
    `publishable_keys` is under RLS. This project has now hit that ordering bug
    twice (refresh tokens, then secret API keys); it is not going to be hit a
    third time.
    """
    raw = (x_publishable_key or "").strip()
    if not raw:
        raise AuthenticationError("Missing publishable key.")

    claimed_tenant = parse_publishable_key(raw)
    if claimed_tenant is None:
        raise AuthenticationError("Invalid publishable key.")

    async with get_session_factory()() as session:
        async with session.begin():
            await set_tenant_context(session, claimed_tenant)
            key = await publishable_key_service.resolve_key(session, full_key=raw)
            await set_tenant_context(session, key.tenant_id, actor_id=key.id)
            yield CurrentPublishableKey(key=key, session=session, request=request)


def require_publishable_scope(scope: Scope):
    """Capability guard, reporting in the same shape as the secret-key guard.

    The inner dependency is a plain default rather than part of an `Annotated`
    string. This module uses `from __future__ import annotations`, so annotations
    are strings FastAPI resolves at module scope — and a string containing a call
    on a closed-over variable cannot be resolved there. It fails at OpenAPI
    generation with a Pydantic "not fully defined" error that points nowhere near
    the cause.
    """

    async def _guard(
        caller: CurrentPublishableKey = Depends(get_current_publishable_key),
    ) -> CurrentPublishableKey:
        publishable_key_service.assert_capability(caller.key, scope)
        return caller

    return _guard


def require_allowed_origin(scope: Scope, *, origin_required: bool = True):
    """Capability AND origin, as one dependency.

    Origin pinning lives here rather than inline in the handler so it cannot be
    forgotten on the next endpoint someone adds to this router.

    It is **defence-in-depth, not the security boundary** — see
    `publishable_key_service.assert_origin_allowed` for why. The actual controls
    are the collect-only capability and provenance stamping.
    """
    inner = require_publishable_scope(scope)

    async def _guard(
        request: Request,
        caller: CurrentPublishableKey = Depends(inner),
    ) -> CurrentPublishableKey:
        origin = request.headers.get("origin")
        publishable_key_service.assert_origin_allowed(
            caller.key, origin, required=origin_required
        )
        return caller

    return _guard
