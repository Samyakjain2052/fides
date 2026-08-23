"""Consent routes — purposes, notices, principals, and the consent lifecycle.

Routers parse and serialise; the rules live in the services. No route writes a
`tenant_id` filter: RLS applies it, so a forgotten WHERE returns nothing rather
than everything.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from pydantic import BaseModel

from app.api.deps import CurrentUser, require, require_any
from app.core.errors import NotFound, PermissionDenied
from app.core.permissions import Capability, role_can
from app.models.audit import AuditAction
from app.models.consent import Consent, DataPrincipal, Notice, Purpose
from app.schemas.consent import (
    ConsentCheckOut,
    ConsentDetail,
    ConsentGrant,
    ConsentHistoryEntry,
    ConsentOut,
    ConsentWithdraw,
    NoticeCreate,
    NoticeOut,
    NoticeRevise,
    PrincipalCreate,
    PrincipalOut,
    PurposeCreate,
    PurposeOut,
)
from app.services import audit_service, consent_service, notice_service

router = APIRouter(tags=["consent"])


# --------------------------------------------------------------------------- #
# Self-service access
# --------------------------------------------------------------------------- #

async def _self_principal(current: CurrentUser) -> DataPrincipal:
    """The signed-in user as a Data Principal of their own workspace.

    Same convention and same `user:<id>` key as the DSAR routes, deliberately:
    two different external_ids for one person would give them two consent
    ledgers, and each screen would show half of their own history.
    """
    external_id = f"user:{current.user.id}"
    principal = await current.session.scalar(
        select(DataPrincipal).where(DataPrincipal.external_id == external_id)
    )
    if principal is None:
        principal = DataPrincipal(
            tenant_id=current.tenant_id,
            external_id=external_id,
            email=current.user.email,
        )
        current.session.add(principal)
        await current.session.flush()
    return principal


async def _authorise_principal(current: CurrentUser, principal_id: uuid.UUID) -> None:
    """Staff may read anyone in their tenant; everybody else, only themselves.

    This is the scoping half of `require_any`, and it must not be omitted. Without
    it, granting a Data Principal access to these routes would let them read every
    other person's consent ledger by changing one query parameter — a worse bug
    than the 403 it replaces.
    """
    if role_can(current.user.role, Capability.CONSENT_READ):
        return
    mine = await _self_principal(current)
    if principal_id != mine.id:
        raise PermissionDenied(
            "You can only read your own consent record.",
            required=[Capability.CONSENT_READ.value],
            role=current.user.role,
        )


@router.get(
    "/principals/me", response_model=PrincipalOut,
    summary="My own Data Principal record",
)
async def my_principal(
    current: Annotated[CurrentUser, Depends(require(Capability.SELF_READ))],
) -> DataPrincipal:
    """Created on first read.

    Exists so the user-facing screens stop calling `POST /principals`, which is a
    staff route requiring `consent:read`. That call is why the Preference Centre
    failed for the only role it was built for.
    """
    return await _self_principal(current)


# --------------------------------------------------------------------------- #
# Purposes
# --------------------------------------------------------------------------- #

@router.get("/purposes", response_model=list[PurposeOut], summary="List purposes")
async def list_purposes(
    # A person needs the purpose list to see what they have agreed to. It is
    # tenant configuration, not anybody's personal data, and the publishable-key
    # banner already serves it to anonymous visitors.
    current: Annotated[
        CurrentUser,
        Depends(require_any(Capability.CONSENT_READ, Capability.SELF_READ)),
    ],
    include_inactive: bool = False,
) -> list[Purpose]:
    return await notice_service.list_purposes(
        current.session, current.tenant_id, include_inactive=include_inactive
    )


@router.post(
    "/purposes", response_model=PurposeOut, status_code=201, summary="Create a purpose"
)
async def create_purpose(
    body: PurposeCreate,
    current: Annotated[CurrentUser, Depends(require(Capability.PURPOSE_MANAGE))],
) -> Purpose:
    return await notice_service.create_purpose(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        key=body.key,
        name=body.name,
        category=body.category,
        is_mandatory=body.is_mandatory,
        legal_basis=body.legal_basis,
        retention_days=body.retention_days,
    )


# --------------------------------------------------------------------------- #
# Notices
# --------------------------------------------------------------------------- #

@router.get("/notices", response_model=list[NoticeOut], summary="List notices")
async def list_notices(
    current: Annotated[
        CurrentUser,
        Depends(require_any(Capability.CONSENT_READ, Capability.SELF_READ)),
    ],
    purpose_id: uuid.UUID | None = None,
    published_only: bool = False,
) -> list[Notice]:
    return await notice_service.list_notices(
        current.session,
        current.tenant_id,
        purpose_id=purpose_id,
        published_only=published_only,
    )


@router.post(
    "/notices", response_model=NoticeOut, status_code=201,
    summary="Draft a notice (the next version for its purpose and language)",
)
async def draft_notice(
    body: NoticeCreate,
    current: Annotated[CurrentUser, Depends(require(Capability.PURPOSE_MANAGE))],
) -> Notice:
    return await notice_service.draft_notice(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        purpose_id=body.purpose_id,
        content=body.content,
        data_collected=body.data_collected,
        user_rights=body.user_rights,
        withdrawal_policy=body.withdrawal_policy,
        language=body.language,
    )


@router.post(
    "/notices/{notice_id}/publish", response_model=NoticeOut,
    summary="Publish a notice — after this its wording is frozen",
)
async def publish_notice(
    notice_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.PURPOSE_MANAGE))],
) -> Notice:
    """Publishing is one-way.

    A published notice is the text somebody agreed to. It cannot be edited or
    un-published — a database trigger enforces that, not just this service — and
    a change produces the next version instead.
    """
    return await notice_service.publish_notice(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        notice_id=notice_id,
    )


@router.patch(
    "/notices/{notice_id}", response_model=NoticeOut,
    summary="Edit a draft, or supersede a published notice with a new version",
)
async def revise_notice(
    notice_id: uuid.UUID,
    body: NoticeRevise,
    current: Annotated[CurrentUser, Depends(require(Capability.PURPOSE_MANAGE))],
) -> Notice:
    return await notice_service.revise_notice(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        notice_id=notice_id,
        content=body.content,
        data_collected=body.data_collected,
        user_rights=body.user_rights,
        withdrawal_policy=body.withdrawal_policy,
    )


# --------------------------------------------------------------------------- #
# Data principals
# --------------------------------------------------------------------------- #

@router.get("/principals", response_model=list[PrincipalOut], summary="List data principals")
async def list_principals(
    current: Annotated[CurrentUser, Depends(require(Capability.CONSENT_READ))],
    limit: int = Query(100, ge=1, le=500),
) -> list[DataPrincipal]:
    rows = await current.session.execute(
        select(DataPrincipal).order_by(DataPrincipal.created_at.desc()).limit(limit)
    )
    return list(rows.scalars().all())


@router.post(
    "/principals", response_model=PrincipalOut, status_code=201,
    summary="Register a data principal",
)
async def create_principal(
    body: PrincipalCreate,
    current: Annotated[CurrentUser, Depends(require(Capability.CONSENT_READ))],
) -> DataPrincipal:
    """Idempotent on `external_id`.

    A customer syncing their user base will call this repeatedly for the same
    people; making that an error would push them into "create, catch 409,
    update" for the normal case.
    """
    existing = await current.session.scalar(
        select(DataPrincipal).where(DataPrincipal.external_id == body.external_id)
    )
    if existing is not None:
        existing.email = body.email or existing.email
        existing.phone = body.phone or existing.phone
        existing.is_minor = body.is_minor
        existing.guardian_email = body.guardian_email or existing.guardian_email
        await current.session.flush()
        return existing

    principal = DataPrincipal(
        tenant_id=current.tenant_id,
        external_id=body.external_id,
        email=body.email,
        phone=body.phone,
        is_minor=body.is_minor,
        guardian_email=body.guardian_email,
    )
    current.session.add(principal)
    await current.session.flush()
    return principal


# --------------------------------------------------------------------------- #
# Consents
# --------------------------------------------------------------------------- #

@router.get(
    "/consents", response_model=list[ConsentDetail],
    summary="A principal's consents, with the notice version each was given against",
)
async def list_consents(
    principal_id: uuid.UUID,
    current: Annotated[
        CurrentUser,
        Depends(require_any(Capability.CONSENT_READ, Capability.SELF_READ)),
    ],
) -> list[ConsentDetail]:
    await _authorise_principal(current, principal_id)
    rows = await consent_service.for_principal(
        current.session, current.tenant_id, principal_id
    )
    return [
        ConsentDetail(
            **{k: getattr(c, k) for k in ConsentOut.model_fields},
            purpose_key=p.key,
            purpose_name=p.name,
            is_mandatory=p.is_mandatory,
            notice_version=n.version,
            notice_content=n.content,
        )
        for c, p, n in rows
    ]


@router.post("/consents", response_model=ConsentOut, status_code=201, summary="Record consent")
async def grant_consent(
    body: ConsentGrant,
    current: Annotated[CurrentUser, Depends(require(Capability.CONSENT_READ))],
) -> Consent:
    """Recording a yes.

    Fails with 409 if the purpose has no published notice — a consent that
    cannot name the wording behind it is not evidence of anything, so refusing
    is better than storing something unusable.
    """
    return await consent_service.grant(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        principal_id=body.principal_id,
        purpose_id=body.purpose_id,
        language=body.language,
        method=body.method,
        source=body.source,
        notice_id=body.notice_id,
    )


@router.post(
    "/consents/withdraw", response_model=ConsentOut,
    summary="Withdraw consent — one call, as easy as granting (DPDP §6(4))",
)
async def withdraw_consent(
    body: ConsentWithdraw,
    current: Annotated[CurrentUser, Depends(require(Capability.CONSENT_READ))],
) -> Consent:
    return await consent_service.withdraw(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        principal_id=body.principal_id,
        purpose_id=body.purpose_id,
        reason=body.reason,
    )


@router.get(
    "/consents/check", response_model=ConsentCheckOut,
    summary="Do I have consent right now?",
)
async def check_consent(
    principal_id: uuid.UUID,
    purpose: str = Query(..., description="The purpose key, e.g. marketing_email."),
    current: Annotated[CurrentUser, Depends(require(Capability.CONSENT_VALIDATE))] = None,
) -> ConsentCheckOut:
    """Expiry is evaluated against the clock at read time, not by a nightly job.

    A sweep leaves a window in which an expired consent still reads as active,
    and processing in that window is unlawful.
    """
    result = await consent_service.check(
        current.session,
        tenant_id=current.tenant_id,
        principal_id=principal_id,
        purpose_key=purpose,
    )

    # Audited — and only here, on the console route.
    #
    # `AuditAction.CONSENT_VALIDATED` existed as a constant that nothing wrote, so
    # a human asking "were we allowed to process this person's data?" left no trace
    # at all. That is the one question this endpoint exists to answer, and being
    # able to show you asked it is most of its value.
    #
    # Deliberately NOT on the public API's equivalent. Machine callers hit that
    # before every processing operation, and each audit entry takes a per-tenant
    # advisory lock — auditing a hot path would serialise it and bury the chain in
    # noise. Those calls are already recorded in `api_request_log`, which is the
    # right place for high-volume access logging.
    await audit_service.record(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        action=AuditAction.CONSENT_VALIDATED,
        entity_type="consent",
        entity_id=principal_id,
        payload={
            "purpose_key": purpose,
            "status": result["status"],
            "allowed": result["allowed"],
        },
    )
    return ConsentCheckOut(**{k: v for k, v in result.items() if k != "notice_version"})


@router.get(
    "/consents/history", response_model=list[ConsentHistoryEntry],
    summary="Consent history, read from the audit chain",
)
async def consent_history(
    principal_id: uuid.UUID,
    current: Annotated[
        CurrentUser,
        Depends(require_any(Capability.CONSENT_READ, Capability.SELF_READ)),
    ],
) -> list[ConsentHistoryEntry]:
    """Not a history table — a query over `audit_events`.

    One source of truth. Each entry carries the hash it was chained with, so the
    history a customer shows a regulator is the same evidence the integrity
    check verifies.
    """
    await _authorise_principal(current, principal_id)
    events = await consent_service.history(
        current.session, current.tenant_id, principal_id
    )
    return [
        ConsentHistoryEntry(
            seq=e.seq,
            action=e.action,
            occurred_at=e.created_at,
            actor_type=e.actor_type,
            payload=e.payload or {},
            hash=e.hash,
        )
        for e in events
    ]


# ------------------------------------------------------------------ overview --

class ConsentOverviewOut(BaseModel):
    """Dashboard totals.

    `active` counts consents whose status says active AND whose expiry has not
    passed — the same judgement `/consents/check` makes. `lapsed_not_yet_marked` is
    the gap between the two: rows still reading `active` that the product will
    already refuse. A dashboard claiming more active consents than validation
    would honour is worse than no dashboard.
    """

    active: int
    lapsed_not_yet_marked: int
    withdrawn_30d: int
    granted_30d: int
    expiring_30d: int
    expiring_7d: int
    total: int


class ConsentSliceOut(BaseModel):
    label: str
    value: int


class ExpiringConsentOut(BaseModel):
    principal_ref: str | None
    principal_email: str | None
    purpose_key: str
    purpose_name: str
    expires_at: Any


class ConsentDashboardOut(BaseModel):
    overview: ConsentOverviewOut
    # Only non-zero slices. A chart that draws a shape over an empty set is the
    # fabrication this codebase has already removed twice.
    status_split: list[ConsentSliceOut]
    expiring_soon: list[ExpiringConsentOut]


@router.get("/consents/overview", response_model=ConsentDashboardOut,
            summary="Consent totals, counted against the clock")
async def consent_overview(
    current: Annotated[CurrentUser, Depends(require(Capability.CONSENT_READ))],
) -> Any:
    """What the admin dashboard's consent tiles read.

    They were the last sample data on that page while the consent module itself was
    live — marked SAMPLE, so disclosed rather than dishonest, but the only figures
    a DPO could not act on.

    Expiry is evaluated per request rather than by a sweep, for the same reason
    `/consents/check` does it: a nightly job leaves a window in which an expired
    consent still counts as active, and processing in that window is unlawful.
    """
    return {
        "overview": await consent_service.overview(
            current.session, tenant_id=current.tenant_id
        ),
        "status_split": await consent_service.status_split(
            current.session, tenant_id=current.tenant_id
        ),
        "expiring_soon": await consent_service.expiring_soon(
            current.session, tenant_id=current.tenant_id, days=7
        ),
    }
