"""Rights-request routes — raise, track, triage.

No route writes a `tenant_id` filter: RLS applies it, so a forgotten WHERE
returns nothing rather than everything.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.api.deps import CurrentUser, require
from app.core.errors import PermissionDenied
from app.core.permissions import Capability
from app.models.consent import DataPrincipal
from app.models.dsar import DsarRequest
from app.schemas.dsar import (
    DsarDetail,
    DsarEventOut,
    DsarOut,
    DsarPage,
    DsarStatusChange,
    DsarSubmit,
)
from app.services import data_map_service, dsar_service

router = APIRouter(prefix="/dsar", tags=["rights requests"])


async def _detail(current: CurrentUser, row: DsarRequest, *, with_timeline: bool = True):
    principal = await current.session.scalar(
        select(DataPrincipal).where(DataPrincipal.id == row.principal_id)
    )
    events = (
        await dsar_service.timeline(current.session, current.tenant_id, row.id)
        if with_timeline
        else []
    )
    now = datetime.now(UTC)
    return DsarDetail(
        **{k: getattr(row, k) for k in DsarOut.model_fields},
        principal_ref=principal.external_id if principal else None,
        principal_email=principal.email if principal else None,
        timeline=[DsarEventOut.model_validate(e) for e in events],
        allowed_transitions=sorted(dsar_service.ALLOWED_TRANSITIONS.get(row.status, set())),
        # Evaluated against the clock on read, not by a nightly job. A request
        # that is overdue must read as overdue the moment a DPO looks at it.
        overdue=row.is_open and row.deadline_at <= now,
        days_remaining=(
            (row.deadline_at - now).days if row.is_open else None
        ),
    )


async def _self_principal(current: CurrentUser) -> DataPrincipal:
    """The signed-in user as a Data Principal of their own workspace.

    They are different tables on purpose — an operator of the console is not a
    subject of processing — but a person raising their own request needs to be
    both. Created on first use rather than requiring a separate step.
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


# --------------------------------------------------------------------------- #
# Raising
# --------------------------------------------------------------------------- #

@router.post("", response_model=DsarDetail, status_code=201, summary="Raise a rights request")
async def submit_request(
    body: DsarSubmit,
    current: Annotated[CurrentUser, Depends(require(Capability.SELF_DSAR_WRITE))],
) -> Any:
    """Raise your own request, or someone else's if you have `dsar:process`.

    A DPO acting on a phone call is a real workflow. "Staff can erase anyone" is
    also how somebody gets erased maliciously, so which of the two happened is
    recorded on the request and in the audit trail rather than inferred later.
    """
    requested_by = "principal"

    if body.principal_id is not None:
        held = set(current.capabilities)
        if Capability.DSAR_PROCESS.value not in held:
            raise PermissionDenied(
                "Raising a request on someone else's behalf needs dsar:process.",
                required=[Capability.DSAR_PROCESS.value],
                granted=sorted(held),
            )
        principal_id = body.principal_id
        requested_by = "staff"
    else:
        principal_id = (await _self_principal(current)).id

    request = await dsar_service.submit(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        principal_id=principal_id,
        type=body.type,
        verification_method=body.verification_method,
        verified=body.verification_method is not None,
        correction_payload=body.correction_payload,
        requested_by_actor=requested_by,
    )
    # Access and erasure go to the engine; correction stays a manual workflow.
    await dsar_service.dispatch_to_engine(
        current.session, tenant_id=current.tenant_id, actor=current.actor, request=request
    )
    return await _detail(current, request)


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

@router.get("/mine", response_model=list[DsarDetail], summary="My own requests")
async def my_requests(
    current: Annotated[CurrentUser, Depends(require(Capability.SELF_READ))],
) -> Any:
    principal = await _self_principal(current)
    rows = await current.session.execute(
        select(DsarRequest)
        .where(DsarRequest.principal_id == principal.id)
        .order_by(DsarRequest.submitted_at.desc())
    )
    out = []
    for row in rows.scalars().all():
        await dsar_service.refresh_from_engine(
            current.session, tenant_id=current.tenant_id, request=row
        )
        out.append(await _detail(current, row))
    return out


@router.get("", response_model=DsarPage, summary="The triage queue")
async def list_requests(
    current: Annotated[CurrentUser, Depends(require(Capability.DSAR_READ))],
    status: str | None = None,
    type: str | None = None,
    overdue_only: bool = False,
    limit: int = Query(100, ge=1, le=500),
) -> Any:
    stmt = select(DsarRequest)
    count_stmt = select(func.count()).select_from(DsarRequest)
    if status:
        stmt = stmt.where(DsarRequest.status == status)
        count_stmt = count_stmt.where(DsarRequest.status == status)
    if type:
        stmt = stmt.where(DsarRequest.type == type)
        count_stmt = count_stmt.where(DsarRequest.type == type)
    if overdue_only:
        now = datetime.now(UTC)
        open_states = ("received", "verifying", "in_progress")
        stmt = stmt.where(
            DsarRequest.deadline_at <= now, DsarRequest.status.in_(open_states)
        )
        count_stmt = count_stmt.where(
            DsarRequest.deadline_at <= now, DsarRequest.status.in_(open_states)
        )

    total = (await current.session.scalar(count_stmt)) or 0
    rows = await current.session.execute(
        stmt.order_by(DsarRequest.deadline_at).limit(limit)
    )
    items = []
    for row in rows.scalars().all():
        await dsar_service.refresh_from_engine(
            current.session, tenant_id=current.tenant_id, request=row
        )
        items.append(await _detail(current, row, with_timeline=False))
    return DsarPage(items=items, total=total)


class EraseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The request's own reference, typed back. Same guard the retention live
    #: run uses — an irreversible action should not follow from one click.
    confirm_reference: str = Field(..., max_length=32)
    #: Optional "<connection_id>:<table>" allow-list, so an admin who must
    #: retain one table for a statutory reason can erase the rest.
    only: list[str] | None = None


@router.get("/{request_id}/data-map",
            summary="Where this person's data is, across connected systems")
async def data_map(
    request_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.DSAR_PROCESS))],
) -> dict[str, Any]:
    """Metadata only: systems, tables, row counts, categories, matched column.

    No values. A rights request authorises acting on somebody's data, not
    reading it — see data_map_service for the reasoning, and for why an
    unverified connection is reported as *unknown* rather than empty.
    """
    return await data_map_service.build(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        request_id=request_id,
    )


@router.post("/{request_id}/erase",
             summary="Mask this person out of the connected systems")
async def erase_across_systems(
    request_id: uuid.UUID,
    body: EraseBody,
    current: Annotated[CurrentUser, Depends(require(Capability.DSAR_PROCESS))],
) -> dict[str, Any]:
    """Irreversible. Refuses without the reference, and refuses under a legal hold.

    Does not mark the request completed: erasing the connected systems is one
    part of fulfilling it, and whether everything in scope was reached is the
    admin's judgement to record.
    """
    return await data_map_service.erase(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        request_id=request_id,
        confirm_reference=body.confirm_reference,
        only=body.only,
    )


@router.get("/{request_id}", response_model=DsarDetail, summary="One request and its timeline")
async def get_request(
    request_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.SELF_READ))],
) -> Any:
    row = await dsar_service.get(current.session, current.tenant_id, request_id)

    # Your own request, or you hold dsar:read. Without this check any signed-in
    # user could read another person's request by id — RLS scopes to the tenant,
    # not to the individual.
    if Capability.DSAR_READ.value not in set(current.capabilities):
        mine = await _self_principal(current)
        if row.principal_id != mine.id:
            raise PermissionDenied("That request belongs to someone else.")

    await dsar_service.refresh_from_engine(
        current.session, tenant_id=current.tenant_id, request=row
    )
    return await _detail(current, row)


# --------------------------------------------------------------------------- #
# Triage
# --------------------------------------------------------------------------- #

@router.patch(
    "/{request_id}/status", response_model=DsarDetail,
    summary="Advance, reject or cancel a request",
)
async def change_status(
    request_id: uuid.UUID,
    body: DsarStatusChange,
    current: Annotated[CurrentUser, Depends(require(Capability.DSAR_PROCESS))],
) -> Any:
    row = await dsar_service.get(current.session, current.tenant_id, request_id)
    await dsar_service.change_status(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        request=row,
        to_status=body.to_status,
        reason=body.reason,
        note=body.note,
    )
    return await _detail(current, row)


@router.post(
    "/{request_id}/retry", response_model=DsarDetail,
    summary="Re-dispatch a request whose engine call failed",
)
async def retry_dispatch(
    request_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.DSAR_PROCESS))],
) -> Any:
    """Because a downstream being briefly down should not cost someone their
    rights request — it stays at `received` with the failure recorded, and this
    is how it gets picked back up."""
    row = await dsar_service.get(current.session, current.tenant_id, request_id)
    await dsar_service.dispatch_to_engine(
        current.session, tenant_id=current.tenant_id, actor=current.actor, request=row
    )
    return await _detail(current, row)


# --------------------------------------------------------------------------- #
# The access package
# --------------------------------------------------------------------------- #

@router.get(
    "/{request_id}/package",
    summary="Download the access package — audited, and it expires",
)
async def get_package(
    request_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.SELF_READ))],
) -> Any:
    """One person's complete personal data in a single response.

    Every retrieval writes an audit entry, and the package expires. Both matter
    more here than anywhere else in the API: this is the object that would do the
    most damage if it leaked, and "who downloaded it, and when" has to be
    answerable.
    """
    row = await dsar_service.get(current.session, current.tenant_id, request_id)

    if Capability.DSAR_PROCESS.value not in set(current.capabilities):
        mine = await _self_principal(current)
        if row.principal_id != mine.id:
            raise PermissionDenied("That request belongs to someone else.")

    return await dsar_service.package(
        current.session, tenant_id=current.tenant_id, actor=current.actor, request=row
    )
