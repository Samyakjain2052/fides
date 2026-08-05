"""The public API — what a customer's own systems call.

This is the product's real surface (non-negotiable N5). The admin console is a
console; *this* is the thing that sits in a customer's request path and answers
"do I have consent right now?" before they process someone's data.

Three properties matter more here than in the admin API:

* **Fast and honest.** A check that is slow gets cached by the caller, and a
  cached consent decision is a stale one. Expiry is evaluated against the clock
  on every call, never by a background sweep.
* **Safe to retry.** Timeouts happen. `Idempotency-Key` on writes replays the
  first response instead of recording a second consent.
* **Least privilege.** Per-key scopes: a key in a marketing service can read
  consent and cannot collect or erase anything.
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentApiKey, client_ip, require_scope
from app.core.errors import NotFound, PermissionDenied
from app.core.permissions import Scope
from app.models.consent import DataPrincipal, Purpose
from app.schemas.consent import ConsentCheckOut
from app.services import consent_service, public_api_service

router = APIRouter(prefix="/public/v1", tags=["public API"])


# --------------------------------------------------------------------------- #
# Shapes
# --------------------------------------------------------------------------- #

class PublicConsentCollect(BaseModel):
    """Collect consent for a person your systems already know about."""

    principal_ref: str = Field(
        ..., min_length=1, max_length=255,
        description="Your identifier for this person. We create the principal on "
                    "first use, so you do not need a separate registration step.",
        examples=["cust-8412"],
    )
    purpose: str = Field(..., examples=["marketing_email"], description="Purpose key.")
    granted: bool = Field(
        ...,
        description="true records consent, false records a withdrawal. Both are "
                    "explicit acts — there is no implied yes.",
    )
    email: str | None = Field(None, max_length=320)
    language: str = Field("English", max_length=32)
    method: str = Field(
        "api",
        description="How the answer was captured on your side: checkbox, banner, "
                    "verbal_logged, import.",
    )
    source: str | None = Field(
        None, max_length=128,
        description="Which of your systems asked. Shows up in the audit trail.",
        examples=["signup-flow"],
    )


class PublicConsentOut(BaseModel):
    principal_ref: str
    purpose: str
    status: str
    given_at: Any | None = None
    withdrawn_at: Any | None = None
    expires_at: Any | None = None
    notice_version: int | None = None
    language: str


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _rate_headers(response: Response, limit: int, remaining: int) -> None:
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)


async def _principal_by_ref(
    caller: CurrentApiKey, principal_ref: str, *, create: bool = False,
    email: str | None = None,
) -> DataPrincipal:
    principal = await caller.session.scalar(
        select(DataPrincipal).where(DataPrincipal.external_id == principal_ref)
    )
    if principal is not None:
        if email and not principal.email:
            principal.email = email
        return principal
    if not create:
        raise NotFound(f"No principal with reference {principal_ref!r}.")

    principal = DataPrincipal(
        tenant_id=caller.tenant_id, external_id=principal_ref, email=email
    )
    caller.session.add(principal)
    await caller.session.flush()
    return principal


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #

@router.get(
    "/consent/check",
    response_model=ConsentCheckOut,
    summary="Do I have consent right now?",
)
async def check_consent(
    request: Request,
    response: Response,
    principal_ref: Annotated[str, Query(description="Your identifier for the person.")],
    purpose: Annotated[str, Query(description="Purpose key.")],
    caller: Annotated[CurrentApiKey, Depends(require_scope(Scope.CONSENT_READ))],
) -> ConsentCheckOut:
    """The call that belongs in your request path, before you process.

    A `never_given` answer is not an error — it is the correct, common answer for
    someone who was never asked, and returning 404 for it would push callers into
    treating a missing consent as a failure to be retried.
    """
    started = time.monotonic()
    limit, remaining = await public_api_service.enforce_rate_limit(
        caller.session, tenant_id=caller.tenant_id, api_key=caller.key
    )
    _rate_headers(response, limit, remaining)

    status_code = 200
    try:
        principal = await caller.session.scalar(
            select(DataPrincipal).where(DataPrincipal.external_id == principal_ref)
        )
        if principal is None:
            # An unknown person has given no consent. Same shape as a known
            # person who never answered, because that is the same fact.
            result = {
                "allowed": False,
                "status": "never_given",
                "purpose": purpose,
                "reason": "No such principal, so no consent has been recorded.",
            }
        else:
            result = await consent_service.check(
                caller.session,
                tenant_id=caller.tenant_id,
                principal_id=principal.id,
                purpose_key=purpose,
            )
        return ConsentCheckOut(**{k: v for k, v in result.items() if k != "notice_version"})
    except NotFound:
        status_code = 404
        raise
    finally:
        await public_api_service.log_request(
            caller.session,
            tenant_id=caller.tenant_id,
            api_key_id=caller.key.id,
            method="GET",
            path="/public/v1/consent/check",
            status_code=status_code,
            duration_ms=int((time.monotonic() - started) * 1000),
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            principal_ref=principal_ref,
            purpose_key=purpose,
        )


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #

@router.post(
    "/consent",
    response_model=PublicConsentOut,
    status_code=201,
    summary="Record consent, or a withdrawal",
)
async def collect_consent(
    request: Request,
    response: Response,
    body: PublicConsentCollect,
    caller: Annotated[CurrentApiKey, Depends(require_scope(Scope.CONSENT_COLLECT))],
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            description="Send one per logical request. A retry with the same key "
                        "replays the first response instead of recording a second "
                        "consent.",
        ),
    ] = None,
) -> Any:
    started = time.monotonic()
    limit, remaining = await public_api_service.enforce_rate_limit(
        caller.session, tenant_id=caller.tenant_id, api_key=caller.key
    )
    _rate_headers(response, limit, remaining)

    endpoint = "POST /public/v1/consent"
    payload = body.model_dump(mode="json")

    if idempotency_key:
        replay = await public_api_service.replay_or_reserve(
            caller.session,
            tenant_id=caller.tenant_id,
            api_key_id=caller.key.id,
            key=idempotency_key,
            endpoint=endpoint,
            body=payload,
        )
        if replay is not None:
            stored_status, stored_body = replay
            response.status_code = stored_status
            # Tell the caller this was a replay. Without it, a client cannot
            # distinguish "recorded now" from "already recorded", which matters
            # when they are reconciling their own state.
            response.headers["Idempotent-Replay"] = "true"
            await public_api_service.log_request(
                caller.session,
                tenant_id=caller.tenant_id,
                api_key_id=caller.key.id,
                method="POST",
                path=endpoint,
                status_code=stored_status,
                duration_ms=int((time.monotonic() - started) * 1000),
                ip_address=client_ip(request),
                user_agent=request.headers.get("user-agent"),
                principal_ref=body.principal_ref,
                purpose_key=body.purpose,
            )
            return stored_body

    # Withdrawing needs its own scope, even on a secret key.
    #
    # `consent:collect` and `consent:withdraw` were one `consent:write` until the
    # publishable-key work forced the distinction: recording a consent that never
    # happened is bad, but destroying a real one is worse — it deletes genuine
    # evidence and stops the customer's downstream processing for someone who
    # never asked for that. A credential should have to be trusted separately for
    # the destructive half.
    if not body.granted:
        held = set(caller.key.scopes)
        if Scope.CONSENT_WITHDRAW.value not in held:
            raise PermissionDenied(
                "Withdrawing consent requires a separate scope.",
                required=[Scope.CONSENT_WITHDRAW.value],
                granted=sorted(held),
            )

    purpose = await caller.session.scalar(
        select(Purpose).where(Purpose.key == body.purpose)
    )
    if purpose is None:
        raise NotFound(f"No purpose with key {body.purpose!r}.")

    principal = await _principal_by_ref(
        caller, body.principal_ref, create=True, email=body.email
    )

    if body.granted:
        consent = await consent_service.grant(
            caller.session,
            tenant_id=caller.tenant_id,
            actor=caller.actor,
            principal_id=principal.id,
            purpose_id=purpose.id,
            language=body.language,
            method=body.method,
            source=body.source,
        )
    else:
        consent = await consent_service.withdraw(
            caller.session,
            tenant_id=caller.tenant_id,
            actor=caller.actor,
            principal_id=principal.id,
            purpose_id=purpose.id,
            reason=body.source,
        )

    out = PublicConsentOut(
        principal_ref=principal.external_id,
        purpose=purpose.key,
        status=consent.status,
        given_at=consent.given_at,
        withdrawn_at=consent.withdrawn_at,
        expires_at=consent.expires_at,
        notice_version=None,
        language=consent.language,
    )
    out_body = out.model_dump(mode="json")

    if idempotency_key:
        await public_api_service.store_response(
            caller.session,
            tenant_id=caller.tenant_id,
            api_key_id=caller.key.id,
            key=idempotency_key,
            endpoint=endpoint,
            body=payload,
            status_code=201,
            response_body=out_body,
        )

    await public_api_service.log_request(
        caller.session,
        tenant_id=caller.tenant_id,
        api_key_id=caller.key.id,
        method="POST",
        path=endpoint,
        status_code=201,
        duration_ms=int((time.monotonic() - started) * 1000),
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        principal_ref=body.principal_ref,
        purpose_key=body.purpose,
    )
    return out_body


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

@router.get(
    "/purposes",
    summary="The purposes you can ask about, and their published notices",
)
async def list_public_purposes(
    caller: Annotated[CurrentApiKey, Depends(require_scope(Scope.CONSENT_READ))],
) -> list[dict[str, Any]]:
    """So an integrator can discover the keys rather than hardcode them from a
    screenshot of the admin console."""
    rows = await caller.session.execute(
        select(Purpose).where(Purpose.is_active.is_(True)).order_by(Purpose.name)
    )
    out: list[dict[str, Any]] = []
    for purpose in rows.scalars().all():
        notice = await consent_service.current_notice(
            caller.session, caller.tenant_id, purpose.id
        )
        out.append(
            {
                "key": purpose.key,
                "name": purpose.name,
                "category": purpose.category,
                "is_mandatory": purpose.is_mandatory,
                "legal_basis": purpose.legal_basis,
                "retention_days": purpose.retention_days,
                # Null means no published notice, so consent cannot be collected
                # for it yet. Better to say so here than to fail the write.
                "current_notice_version": notice.version if notice else None,
                "collectable": notice is not None,
            }
        )
    return out
