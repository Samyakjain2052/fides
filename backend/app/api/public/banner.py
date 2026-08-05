"""Consent collection from a browser banner, using a publishable key.

Separate router from the secret-key public API because the caller is different in
kind. A key here ships inside a page's JavaScript, so the design assumes it is
public and extractable, and takes its safety from three places:

1. **The key cannot do harm.** `consent:collect` only — enforced as a ceiling
   when the key is issued *and* by a CHECK constraint on the table. It cannot
   withdraw a consent, cannot read anybody's answers, cannot touch a DSAR.
2. **Provenance on every record.** Origin, hashed IP, user agent, notice version
   and a server-minted receipt id are observed by the server and hashed into the
   audit chain. A forged record is still attributable.
3. **Origin pinning**, which is defence-in-depth and not the boundary — `Origin`
   is whatever a non-browser client says it is.

What is deliberately *not* here: any way to withdraw. Withdrawal of an arbitrary
principal needs a verified session (the preference centre, after identity
verification) or a secret key holding `consent:withdraw`. A published credential
that can destroy real consent would be worse than one that can forge it.
"""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentPublishableKey, client_ip, require_allowed_origin
from app.core.errors import NotFound
from app.core.permissions import Scope
from app.models.consent import DataPrincipal, Purpose
from app.services import consent_service, public_api_service, publishable_key_service

router = APIRouter(prefix="/public/v1/banner", tags=["public API — banner"])


class BannerConsent(BaseModel):
    """One purpose's answer from a banner.

    There is no `granted: false`. A banner may record consent and may not take it
    away; declining simply means no consent is collected for that purpose. Making
    refusal a *write* would give a published key a destructive capability by the
    back door.
    """

    principal_ref: str = Field(
        ..., min_length=1, max_length=255,
        description="Your identifier for the visitor. Without a signed token this "
                    "is ASSERTED by the page, not verified — see `consent_token`.",
    )
    purpose: str = Field(..., examples=["marketing_email"])
    language: str = Field("English", max_length=32)
    source: str | None = Field(None, max_length=128, examples=["cookie-banner"])

    consent_token: str | None = Field(
        None,
        description="Optional step-up. Your server mints a short-lived signed "
                    "token binding principal_ref; when present and valid, the "
                    "record is marked strongly bound and the principal_ref is "
                    "trusted rather than merely asserted.",
    )


class BannerConsentOut(BaseModel):
    principal_ref: str
    purpose: str
    status: str
    given_at: Any | None
    expires_at: Any | None
    language: str
    notice_version: int | None

    # Provenance the caller can quote back to us in a dispute.
    server_receipt_id: str
    collection_method: str
    strongly_bound: bool


@router.post(
    "/consent",
    response_model=BannerConsentOut,
    status_code=201,
    summary="Collect consent from a banner (publishable key, collect-only)",
)
async def collect_from_banner(
    request: Request,
    response: Response,
    body: BannerConsent,
    caller: Annotated[
        CurrentPublishableKey,
        # Capability AND origin in one dependency, so neither can be forgotten on
        # a future endpoint added to this router.
        Depends(require_allowed_origin(Scope.CONSENT_COLLECT)),
    ],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Any:
    started = time.monotonic()
    origin = request.headers.get("origin")
    ip_hash = caller.ip_hash

    # Per key AND per IP. An unauthenticated public write path needs both: per key
    # alone lets one client exhaust a customer's allowance and take their banner
    # down for everyone; per IP alone lets a distributed caller through.
    limit, remaining = await public_api_service.enforce_publishable_rate_limits(
        caller.session, key=caller.key, ip_hash=ip_hash
    )
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)

    endpoint = "POST /public/v1/banner/consent"
    payload = body.model_dump(mode="json")

    if idempotency_key:
        replay = await public_api_service.replay_or_reserve(
            caller.session,
            tenant_id=caller.tenant_id,
            publishable_key_id=caller.key.id,
            key=idempotency_key,
            endpoint=endpoint,
            body=payload,
        )
        if replay is not None:
            stored_status, stored_body = replay
            response.status_code = stored_status
            response.headers["Idempotent-Replay"] = "true"
            await _log(caller, request, endpoint, stored_status, started, body, ip_hash)
            return stored_body

    # --- the step-up ------------------------------------------------------- #
    # With a valid signed token the principal_ref is verified: the integrator's
    # own server, which actually authenticated the person, vouched for it. Without
    # one it is asserted by a page anybody can call, and the record says so.
    collection_method = "publishable_key"
    strongly_bound = False
    principal_ref = body.principal_ref

    if body.consent_token:
        principal_ref = await publishable_key_service.resolve_bound_principal(
            caller.session, tenant_id=caller.tenant_id, token=body.consent_token
        )
        collection_method = "signed_token"
        strongly_bound = True
    elif caller.key.require_signed_token:
        from app.core.errors import PermissionDenied

        raise PermissionDenied(
            "This publishable key requires a signed consent token, because the "
            "purposes it collects need a verified principal.",
            required=["consent_token"],
        )

    purpose = await caller.session.scalar(
        select(Purpose).where(Purpose.key == body.purpose)
    )
    if purpose is None:
        raise NotFound(f"No purpose with key {body.purpose!r}.")

    # A mandatory purpose does not rest on consent, so collecting "consent" for it
    # from a banner would be recording something that is not what it claims to be.
    if purpose.is_mandatory:
        from app.core.errors import Conflict

        raise Conflict(
            f"{purpose.name} is not collected by consent (its basis is "
            f"{purpose.legal_basis.replace('_', ' ')}), so a banner cannot record "
            "consent for it."
        )

    principal = await caller.session.scalar(
        select(DataPrincipal).where(DataPrincipal.external_id == principal_ref)
    )
    if principal is None:
        principal = DataPrincipal(
            tenant_id=caller.tenant_id, external_id=principal_ref
        )
        caller.session.add(principal)
        await caller.session.flush()

    notice = await consent_service.current_notice(
        caller.session, caller.tenant_id, purpose.id, body.language
    )

    consent = await consent_service.grant(
        caller.session,
        tenant_id=caller.tenant_id,
        actor=caller.actor,
        principal_id=principal.id,
        purpose_id=purpose.id,
        language=body.language,
        method="banner",
        source=body.source or origin,
        notice_id=notice.id if notice else None,
    )

    # Where trust in this record actually comes from.
    provenance = await consent_service.record_provenance(
        caller.session,
        tenant_id=caller.tenant_id,
        actor=caller.actor,
        consent=consent,
        collection_method=collection_method,
        strongly_bound=strongly_bound,
        origin=origin,
        ip_hash=ip_hash,
        user_agent=request.headers.get("user-agent"),
        notice_id=notice.id if notice else None,
        notice_version=notice.version if notice else None,
        publishable_key_id=caller.key.id,
    )

    out = BannerConsentOut(
        principal_ref=principal.external_id,
        purpose=purpose.key,
        status=consent.status,
        given_at=consent.given_at,
        expires_at=consent.expires_at,
        language=consent.language,
        notice_version=notice.version if notice else None,
        server_receipt_id=provenance.server_receipt_id,
        collection_method=collection_method,
        strongly_bound=strongly_bound,
    )
    out_body = out.model_dump(mode="json")

    if idempotency_key:
        await public_api_service.store_response(
            caller.session,
            tenant_id=caller.tenant_id,
            publishable_key_id=caller.key.id,
            key=idempotency_key,
            endpoint=endpoint,
            body=payload,
            status_code=201,
            response_body=out_body,
        )

    await _log(caller, request, endpoint, 201, started, body, ip_hash)
    return out_body


@router.get(
    "/purposes",
    summary="Purposes a banner may collect, with their current notice",
)
async def banner_purposes(
    caller: Annotated[
        CurrentPublishableKey, Depends(require_allowed_origin(Scope.CONSENT_COLLECT))
    ],
) -> list[dict[str, Any]]:
    """What a banner needs in order to render itself honestly.

    Returns the notice wording so the page can show what someone is agreeing to,
    and excludes mandatory purposes — those are not consent-based, and offering a
    toggle for them would be the dark pattern the DPDP Act is written against.
    """
    rows = await caller.session.execute(
        select(Purpose)
        .where(Purpose.is_active.is_(True), Purpose.is_mandatory.is_(False))
        .order_by(Purpose.name)
    )
    out: list[dict[str, Any]] = []
    for purpose in rows.scalars().all():
        notice = await consent_service.current_notice(
            caller.session, caller.tenant_id, purpose.id
        )
        if notice is None:
            # No published notice means consent cannot lawfully be collected, so
            # the banner must not offer it.
            continue
        out.append(
            {
                "key": purpose.key,
                "name": purpose.name,
                "category": purpose.category,
                "retention_days": purpose.retention_days,
                "notice_version": notice.version,
                "language": notice.language,
                "content": notice.content,
                "data_collected": notice.data_collected,
                "user_rights": notice.user_rights,
                "withdrawal_policy": notice.withdrawal_policy,
            }
        )
    return out


async def _log(caller, request, endpoint, status_code, started, body, ip_hash) -> None:
    await public_api_service.log_request(
        caller.session,
        tenant_id=caller.tenant_id,
        publishable_key_id=caller.key.id,
        method="POST",
        path=endpoint,
        status_code=status_code,
        duration_ms=int((time.monotonic() - started) * 1000),
        ip_address=client_ip(request),
        ip_hash=ip_hash,
        user_agent=request.headers.get("user-agent"),
        principal_ref=body.principal_ref,
        purpose_key=body.purpose,
    )
