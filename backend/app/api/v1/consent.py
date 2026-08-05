"""Consent routes — purposes, notices, principals, and the consent lifecycle.

Routers parse and serialise; the rules live in the services. No route writes a
`tenant_id` filter: RLS applies it, so a forgotten WHERE returns nothing rather
than everything.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.api.deps import CurrentUser, require
from app.core.errors import NotFound
from app.core.permissions import Capability
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
from app.services import consent_service, notice_service

router = APIRouter(tags=["consent"])


# --------------------------------------------------------------------------- #
# Purposes
# --------------------------------------------------------------------------- #

@router.get("/purposes", response_model=list[PurposeOut], summary="List purposes")
async def list_purposes(
    current: Annotated[CurrentUser, Depends(require(Capability.CONSENT_READ))],
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
    current: Annotated[CurrentUser, Depends(require(Capability.CONSENT_READ))],
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
    current: Annotated[CurrentUser, Depends(require(Capability.CONSENT_READ))],
) -> list[ConsentDetail]:
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
    return ConsentCheckOut(**{k: v for k, v in result.items() if k != "notice_version"})


@router.get(
    "/consents/history", response_model=list[ConsentHistoryEntry],
    summary="Consent history, read from the audit chain",
)
async def consent_history(
    principal_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.CONSENT_READ))],
) -> list[ConsentHistoryEntry]:
    """Not a history table — a query over `audit_events`.

    One source of truth. Each entry carries the hash it was chained with, so the
    history a customer shows a regulator is the same evidence the integrity
    check verifies.
    """
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
