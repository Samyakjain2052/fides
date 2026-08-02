"""Auth routes: login, refresh, logout, me."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Cookie, Request, Response, status

from app.api.deps import CurrentUserDep, UnscopedSession, client_ip
from app.core.config import get_settings
from app.core.errors import AuthenticationError
from app.core.permissions import capabilities_for
from app.schemas.auth import LoginRequest, RegisterRequest, SlugCheck, TokenResponse, UserOut
from app.services import auth_service, registration_service

router = APIRouter(prefix="/auth", tags=["auth"])
_settings = get_settings()


def _set_refresh_cookie(response: Response, token: str, expires: datetime) -> None:
    """HttpOnly + Secure + SameSite=Strict, all three deliberately.

    HttpOnly    JavaScript cannot read it, so XSS cannot steal the session.
    Secure      never sent over plain HTTP.
    SameSite    Strict blocks it being sent on cross-site requests, which is the
                CSRF defence for the refresh endpoint.
    path        scoped to <external_path_prefix>/v1/auth so it is not attached
                to every API call. The prefix matters: behind a proxy that mounts
                this API at /api, a cookie scoped to /v1/auth is never sent back,
                and every reload silently signs the user out.
    """
    response.set_cookie(
        key=_settings.refresh_cookie_name,
        value=token,
        httponly=True,
        secure=_settings.cookie_secure,
        samesite="strict",
        domain=_settings.cookie_domain,
        path=f"{_settings.external_path_prefix}{_settings.api_prefix}/auth",
        expires=int((expires - datetime.now(UTC)).total_seconds()),
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=_settings.refresh_cookie_name,
        path=f"{_settings.external_path_prefix}{_settings.api_prefix}/auth",
        domain=_settings.cookie_domain,
    )


@router.get("/workspace-available", response_model=SlugCheck,
            summary="Is this workspace id free?")
async def workspace_available(workspace: str, session: UnscopedSession) -> SlugCheck:
    """Live feedback for the signup form.

    Unlike the registration error, this does tell you a workspace is taken. That
    is deliberate: a form that only reveals it on submit is hostile, and the same
    fact is obtainable by trying to register. Rate-limited at the edge.
    """
    available = await registration_service.slug_available(session, workspace)
    return SlugCheck(
        workspace=workspace.strip().lower(),
        available=available,
        reason=None if available else "That workspace name is not available.",
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED,
             summary="Create an organisation and its first admin")
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    session: UnscopedSession,
) -> TokenResponse:
    """Self-serve signup.

    Creates the tenant and its first Admin/DPO in one transaction, writes both
    audit entries, and signs the new admin straight in — there is nothing useful
    they could do on a second screen before logging in anyway.
    """
    reg = await registration_service.register_tenant(
        session,
        company_name=payload.company_name,
        slug=payload.workspace,
        admin_name=payload.admin_name,
        admin_email=payload.admin_email,
        password=payload.password,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    # Issue the session directly rather than calling authenticate(), which would
    # re-verify the password we just hashed — a second Argon2 pass for nothing.
    pair = await auth_service.issue_session(
        session,
        user=reg.admin,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_refresh_cookie(response, pair.refresh_token, pair.refresh_expires_at)
    return TokenResponse(
        access_token=pair.access_token,
        expires_at=pair.access_expires_at,
        user=UserOut.model_validate(reg.admin),
        capabilities=sorted(c.value for c in capabilities_for(reg.admin.role)),
    )


@router.post("/login", response_model=TokenResponse, summary="Sign in with a password")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: UnscopedSession,
) -> TokenResponse:
    """Authenticate against one tenant.

    The tenant slug is part of the credentials: the same email may exist for
    several customers, and we must not guess which one the caller meant.
    """
    pair = await auth_service.authenticate(
        session,
        tenant_slug=payload.tenant_slug,
        email=payload.email,
        password=payload.password,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_refresh_cookie(response, pair.refresh_token, pair.refresh_expires_at)
    return TokenResponse(
        access_token=pair.access_token,
        expires_at=pair.access_expires_at,
        user=UserOut.model_validate(pair.user),
        capabilities=sorted(c.value for c in capabilities_for(pair.user.role)),
    )


@router.post("/refresh", response_model=TokenResponse, summary="Rotate the session")
async def refresh(
    request: Request,
    response: Response,
    session: UnscopedSession,
    ds_refresh: Annotated[str | None, Cookie()] = None,
) -> TokenResponse:
    """Single-use rotation. Presenting a spent token revokes the whole family —
    see auth_service.refresh."""
    if not ds_refresh:
        raise AuthenticationError("No session cookie.")

    pair = await auth_service.refresh(
        session,
        raw_token=ds_refresh,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_refresh_cookie(response, pair.refresh_token, pair.refresh_expires_at)
    return TokenResponse(
        access_token=pair.access_token,
        expires_at=pair.access_expires_at,
        user=UserOut.model_validate(pair.user),
        capabilities=sorted(c.value for c in capabilities_for(pair.user.role)),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="End the session")
async def logout(
    request: Request,
    response: Response,
    current: CurrentUserDep,
    ds_refresh: Annotated[str | None, Cookie()] = None,
) -> Response:
    await auth_service.logout(
        current.session,
        raw_token=ds_refresh,
        user=current.user,
        ip=client_ip(request),
    )
    _clear_refresh_cookie(response)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=TokenResponse, summary="Who am I, and what may I do")
async def me(current: CurrentUserDep) -> TokenResponse:
    """Re-issues nothing; returns the caller's identity and capability list so a
    reloading UI can rebuild its navigation without guessing."""
    from app.core.security import create_access_token

    token, expires = create_access_token(
        user_id=current.user.id, tenant_id=current.tenant_id, role=current.user.role
    )
    return TokenResponse(
        access_token=token,
        expires_at=expires,
        user=UserOut.model_validate(current.user),
        capabilities=sorted(c.value for c in capabilities_for(current.user.role)),
    )
