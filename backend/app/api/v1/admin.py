"""Tenant administration: users and API keys."""

from __future__ import annotations

import uuid
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, require
from app.core.config import get_settings
from app.core.errors import Conflict
from app.core.permissions import Capability, capabilities_for
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
from app.services import (
    api_key_service,
    invitation_service,
    publishable_key_service,
    scheduler,
    tenant_service,
)

logger = logging.getLogger("app.admin")

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

    target = await tenant_service.get_user(
        current.session, tenant_id=current.tenant_id, user_id=user_id
    )
    # Refused here with a sentence a DPO can act on. Also refused by a trigger on
    # `users`, which is what makes it true regardless of code path.
    await invitation_service.assert_not_last_admin(
        current.session, tenant_id=current.tenant_id, user=target,
        becoming_role=payload.role, staying_active=target.is_active,
    )
    # Any role change that REDUCES what they can do, not just losing admin.
    #
    # The first version of this checked `role == "admin"`, which missed
    # grievance_officer -> data_principal entirely: a live session kept working
    # after the privileges behind it were taken away. Derived from the same
    # capability matrix the API enforces, so it cannot drift from it: if the new
    # role's capabilities are not a superset of the old, something was removed.
    losing_ground = not (
        capabilities_for(target.role) <= capabilities_for(payload.role)
    )

    user = await tenant_service.change_role(
        current.session, tenant_id=current.tenant_id, user_id=user_id,
        new_role=payload.role, actor=current.actor,
    )

    # Losing privileges has to end their sessions.
    #
    # The role is re-read from the database on every request, so the change itself
    # bites immediately — but a refresh token outlives it, and the browser would
    # keep minting access tokens until the family expired. Only on a reduction: a
    # promotion takes effect on the next request anyway, and signing somebody out
    # for being given more access would be baffling.
    if losing_ground:
        await invitation_service.revoke_sessions(
            current.session, tenant_id=current.tenant_id, actor=current.actor,
            user=user, reason="role_changed",
        )
    return UserOut.model_validate(user)


@router.post("/users/{user_id}/deactivate", response_model=UserOut, summary="Revoke access")
async def deactivate_user(
    user_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.USER_MANAGE))],
) -> UserOut:
    if user_id == current.user.id:
        raise Conflict("You cannot deactivate your own account.")

    target = await tenant_service.get_user(
        current.session, tenant_id=current.tenant_id, user_id=user_id
    )
    await invitation_service.assert_not_last_admin(
        current.session, tenant_id=current.tenant_id, user=target,
        becoming_role=target.role, staying_active=False,
    )

    user = await tenant_service.deactivate_user(
        current.session, tenant_id=current.tenant_id, user_id=user_id, actor=current.actor
    )
    # Revoking access has to mean now, not when their refresh token expires.
    await invitation_service.revoke_sessions(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        user=user, reason="deactivated",
    )
    return UserOut.model_validate(user)


@router.post("/users/{user_id}/reactivate", response_model=UserOut,
             summary="Restore a revoked account")
async def reactivate_user(
    user_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.USER_MANAGE))],
) -> UserOut:
    """The way back from Revoke access.

    Without this, revoking was one-way: a misclick in a table of similar rows
    locked somebody out permanently and the only remedy was a database edit.
    Their sessions are not restored — see `tenant_service.reactivate_user`.
    """
    user = await tenant_service.reactivate_user(
        current.session, tenant_id=current.tenant_id, user_id=user_id,
        actor=current.actor,
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


# ------------------------------------------------------------- invitations --
#
# Why invitations exist rather than admin-set passwords: an administrator who
# knows a colleague's password makes every audit entry attributed to that
# colleague arguable, and the audit chain is this product's central claim. The
# existing `POST /users` with a password is kept for provisioning and scripts, but
# invitations are the intended path for people.


class InvitationCreate(BaseModel):
    email: EmailStr
    role: str = Field(
        ..., pattern="^(admin|auditor|grievance_officer|data_principal)$",
        description="What the account may do. Chosen by you, not by the person "
                    "accepting — that is the point of the token.",
    )


class InvitationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    role: str
    # Computed against the clock, never stored: a stored status is stale the moment
    # the expiry passes, and that window is exactly when somebody would use it.
    status: str
    expires_at: Any
    accepted_at: Any | None
    revoked_at: Any | None
    revoked_reason: str | None
    created_at: Any


class InvitationCreated(BaseModel):
    invitation: InvitationOut
    # Returned exactly once and never stored. Also emailed — but the default
    # notification provider writes to a log instead of sending, so an invitation
    # that relied only on email would be undeliverable in the shipped
    # configuration.
    accept_url: str
    shown_once: str
    emailed: bool


class RevokeInvitation(BaseModel):
    reason: str = Field("withdrawn by an administrator", max_length=2000)


@router.get("/invitations", response_model=list[InvitationOut],
            summary="Invitations, with what became of each")
async def list_invitations(
    current: Annotated[CurrentUser, Depends(require(Capability.USER_MANAGE))],
    pending_only: bool = False,
) -> Any:
    return await invitation_service.list_invitations(
        current.session, current.tenant_id, pending_only=pending_only
    )


@router.post("/invitations", response_model=InvitationCreated, status_code=201,
             summary="Invite somebody to this workspace")
async def create_invitation(
    body: InvitationCreate,
    request: Request,
    current: Annotated[CurrentUser, Depends(require(Capability.USER_MANAGE))],
) -> Any:
    """Issue an invitation and email it. The link comes back once.

    Nobody, including the administrator sending this, ever learns the invited
    person's password — they set it themselves when they accept.
    """
    row, token = await invitation_service.invite(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        email=str(body.email),
        role=body.role,
        invited_by=current.user.id,
    )

    # `public_base_url` first, and the request only as a local-dev fallback.
    #
    # This used to be request-derived on the reasoning that it "works behind the
    # proxy rather than a configured base URL that drifts". That held until the
    # backend moved to internal ingress, where nginx must send
    # `Host: $proxy_host` for Container Apps to route at all — so the request
    # origin became the backend's internal FQDN and every invitation emailed a
    # link to a host that resolves for nobody.
    #
    # It is also the safer default regardless of hosting: an emailed link built
    # from the Host header lets a caller choose where our email points.
    settings = get_settings()
    base = (settings.public_base_url or str(request.base_url)).rstrip("/")
    external = settings.external_path_prefix or ""
    if external and base.endswith(external):
        base = base[: -len(external)]
    accept_url = f"{base}/accept-invitation?token={token}"

    emailed = False
    try:
        await invitation_service.send_invitation_email(
            current.session, tenant_id=current.tenant_id, invitation=row,
            accept_url=accept_url,
        )
        emailed = True
    except Exception:  # noqa: BLE001
        # The invitation stands either way. Failing the whole call because a mail
        # provider was down would throw away a credential that was already issued
        # and audited — and the link is in this response.
        logger.exception("could not email an invitation; the link is in the response")

    return InvitationCreated(
        invitation=InvitationOut.model_validate(row),
        accept_url=accept_url,
        shown_once=(
            "This link is shown once and is not stored anywhere we can read it. "
            "If it is lost, revoke this invitation and send a new one."
        ),
        emailed=emailed,
    )


@router.post("/invitations/{invitation_id}/revoke", response_model=InvitationOut,
             summary="Withdraw an invitation (the record is kept)")
async def revoke_invitation(
    invitation_id: uuid.UUID,
    body: RevokeInvitation,
    current: Annotated[CurrentUser, Depends(require(Capability.USER_MANAGE))],
) -> Any:
    return await invitation_service.revoke(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        invitation_id=invitation_id, reason=body.reason,
    )


# ---------------------------------------------------------------- sessions --

class SessionOut(BaseModel):
    family_id: uuid.UUID
    started_at: Any
    last_used_at: Any
    expires_at: Any
    # How many times the token rotated in this family. Useful only as a rough
    # activity signal; the rotation itself is machinery.
    rotations: int
    ip_address: str | None
    user_agent: str | None


@router.get("/users/{user_id}/sessions", response_model=list[SessionOut],
            summary="A user's live sessions")
async def list_sessions(
    user_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.USER_MANAGE))],
) -> Any:
    """One entry per refresh-token family — that is, per browser.

    What an administrator needs when something has gone wrong: "this person is
    signed in on three devices, one of them from an address nobody recognises."
    """
    await tenant_service.get_user(
        current.session, tenant_id=current.tenant_id, user_id=user_id
    )
    return await invitation_service.list_sessions(
        current.session, tenant_id=current.tenant_id, user_id=user_id
    )


@router.post("/users/{user_id}/sessions/revoke", summary="Sign a user out everywhere")
async def revoke_sessions(
    user_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.USER_MANAGE))],
) -> dict[str, Any]:
    """Ends every live session immediately.

    Their password still works, so this is not a lockout — it is what you reach
    for when a laptop is lost or a session looks wrong. Deactivation is the
    lockout, and it calls this too.
    """
    user = await tenant_service.get_user(
        current.session, tenant_id=current.tenant_id, user_id=user_id
    )
    count = await invitation_service.revoke_sessions(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        user=user, reason="revoked_by_admin",
    )
    return {
        "revoked_families": count,
        "note": (
            "Their password still works — this ends the sessions, it does not lock "
            "the account. Deactivate the user for that."
        ),
    }


# -------------------------------------------------------- capability matrix --

@router.get("/capabilities", summary="What each role may do, from the enforcement itself")
async def capabilities(
    current: Annotated[CurrentUser, Depends(require(Capability.USER_MANAGE))],
) -> dict[str, Any]:
    """Generated from the matrix the API enforces, not a copy of it.

    A permissions screen that can disagree with the code enforcing it is worse
    than no permissions screen — it tells an administrator their workspace is
    configured one way while it behaves another.
    """
    return invitation_service.capability_matrix()


# --------------------------------------------------------------- scheduled jobs --
#
# Why this endpoint exists: four modules previously disclosed "there is no
# scheduler", which was honest and, being visible in the UI, harmless. A scheduler
# creates a worse possibility — one that stopped weeks ago while every screen
# quietly claims escalation and retry are automatic. Nobody notices, because
# nothing looks broken.
#
# So `stale` is the important field here, and the module caveats quote this rather
# than asserting the scheduler is alive.


class JobStatusOut(BaseModel):
    job: str
    description: str
    interval_seconds: int
    last_status: str | None
    last_started_at: Any | None
    last_error: str | None
    last_success_at: Any | None
    last_success_items: int | None
    # True when nothing has succeeded in three intervals — or ever. "We have no
    # evidence this works" and "this stopped working" warrant the same response.
    stale: bool
    seconds_since_success: int | None


class JobRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job: str
    status: str
    started_at: Any
    finished_at: Any | None
    tenants_processed: int
    items_processed: int
    error: str | None


class JobsOut(BaseModel):
    jobs: list[JobStatusOut]
    recent: list[JobRunOut]
    note: str


@router.get("/jobs", response_model=JobsOut,
            summary="Is the scheduler running, and what has it done?")
async def scheduled_jobs(
    current: Annotated[CurrentUser, Depends(require(Capability.TENANT_MANAGE))],
) -> Any:
    """Platform-wide, not per tenant.

    The scheduler sweeps every workspace, so its health is a property of the
    deployment rather than of one customer. The log holds counts and job names only
    — no tenant is named, because "which customers had overdue complaints last
    night" is not a question this should make easy to ask.
    """
    return JobsOut(
        jobs=await scheduler.status(current.session),
        recent=await scheduler.recent_runs(current.session, limit=25),
        note=(
            "A job is stale when nothing has succeeded in three of its intervals, "
            "or when it has never succeeded at all. If everything here is stale the "
            "scheduler process is not running, and escalation, notification retries "
            "and pre-purge warnings are not happening — regardless of what any "
            "other screen implies."
        ),
    )


@router.post("/jobs/{job_name}/run", response_model=JobRunOut,
             summary="Run a scheduled job now")
async def run_job_now(
    job_name: str,
    current: Annotated[CurrentUser, Depends(require(Capability.TENANT_MANAGE))],
) -> Any:
    """Takes the same advisory lock the scheduler does.

    So this cannot double-run against a scheduler mid-sweep — it records
    `skipped_locked` and returns, which is also how you can tell the scheduler is
    genuinely working rather than merely deployed.
    """
    if job_name not in scheduler.JOBS:
        raise Conflict(
            f"Unknown job {job_name!r}. One of: {', '.join(sorted(scheduler.JOBS))}."
        )
    return await scheduler.run_job(job_name)
