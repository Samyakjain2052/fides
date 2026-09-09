"""Rights-request routes — raise, track, triage.

No route writes a `tenant_id` filter: RLS applies it, so a forgotten WHERE
returns nothing rather than everything.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.api.deps import CurrentUser, require
from app.core.config import get_settings
from app.core.errors import NotFound, PermissionDenied, ValidationProblem
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
from app.services import (
    data_map_service,
    dsar_fulfilment_service,
    dsar_service,
    file_service,
)

router = APIRouter(prefix="/dsar", tags=["rights requests"])


async def _read_bounded(file: UploadFile) -> bytes:
    """Read an upload, refusing one that is over the ceiling.

    Read in chunks and abandoned as soon as the limit is passed, rather than
    `await file.read()` and checking the length afterwards. The difference
    matters: the naive version pulls the whole body into memory before deciding
    it was too big, which makes the size limit an invitation rather than a
    defence.
    """
    limit = get_settings().max_upload_bytes
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(1024 * 256):
        total += len(chunk)
        if total > limit:
            raise ValidationProblem(
                f"Files must be {limit // (1024 * 1024)} MB or smaller."
            )
        chunks.append(chunk)
    return b"".join(chunks)


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
    summary="Download the assembled disclosure package — audited, and it expires",
)
async def get_package(
    request_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.SELF_READ))],
) -> Response:
    """One person's complete personal data, as the stored artifact.

    This used to proxy live JSON from the engine, which had three problems: a
    request fulfilled through the connections path produced nothing at all, the
    response was not something anybody could keep, and the engine had to retain
    the data indefinitely for the endpoint to keep working. It now serves the
    package that was assembled, stored and hashed — so what the person receives
    is exactly what an administrator reviewed and delivered.

    Only the person it belongs to. `dsar:process` does NOT open this: staff get
    the redacted preview instead, and reviewing a disclosure does not require
    reading somebody's government ID. See services/disclosure.py.
    """
    row = await dsar_service.get(current.session, current.tenant_id, request_id)

    mine = await _self_principal(current)
    if row.principal_id != mine.id:
        # 404, not 403 — the same reasoning as every other file refusal.
        raise NotFound("No such request.")

    stored, data = await dsar_fulfilment_service.package_for_principal(
        current.session, request=row
    )
    await dsar_fulfilment_service.note_package_downloaded(
        current.session, tenant_id=current.tenant_id, actor=current.actor, request=row
    )

    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{row.reference}.zip"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


# --------------------------------------------------------------------------- #
# Identity verification
# --------------------------------------------------------------------------- #

class IdentityReview(BaseModel):
    accept: bool
    #: Required on a refusal. Enforced in the service, not just here — this is
    #: the refusal a person is most likely to challenge.
    reason: str | None = Field(default=None, max_length=2000)


@router.post(
    "/{request_id}/identity",
    status_code=201,
    summary="Attach a document proving who is asking",
)
async def upload_identity_document(
    request_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.SELF_READ))],
    file: Annotated[UploadFile, File(description="Photo or scan of an ID")],
) -> dict[str, Any]:
    """Upload identity proof, for your own request or — with `dsar:process` — anyone's.

    Read fully into memory and bounded by `max_upload_bytes`. Streaming to disk
    first would mean an unvalidated file existing on the filesystem before
    anything had decided whether we accept it at all.
    """
    row = await dsar_service.get(current.session, current.tenant_id, request_id)
    staff = Capability.DSAR_PROCESS.value in set(current.capabilities)

    principal_id = None
    if not staff:
        mine = await _self_principal(current)
        if row.principal_id != mine.id:
            raise NotFound("No such request.")
        principal_id = mine.id

    stored = await dsar_fulfilment_service.submit_identity_document(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        request=row,
        filename=file.filename or "identity",
        data=await _read_bounded(file),
        declared_content_type=file.content_type,
        principal_id=principal_id,
        uploaded_by=current.user.id if staff else None,
    )
    return file_service.as_dict(stored)


@router.get(
    "/{request_id}/identity/document",
    summary="Open the submitted identity document — recorded every time",
)
async def download_identity_document(
    request_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.DSAR_PROCESS))],
) -> Response:
    """`dsar:process` only, and every look is audited.

    Looking at a photograph of somebody's government ID is itself processing,
    and the person whose ID it is has a right to know who looked. Recorded here
    rather than at the review decision, so a look that leads to no decision is
    still on the record.
    """
    row = await dsar_service.get(current.session, current.tenant_id, request_id)
    if row.identity_document_id is None:
        raise NotFound("No identity document has been submitted for this request.")

    stored, data = await file_service.fetch(
        current.session, file_id=row.identity_document_id
    )
    await dsar_fulfilment_service.note_identity_viewed(
        current.session, tenant_id=current.tenant_id, actor=current.actor, request=row
    )
    return Response(
        content=data,
        media_type=stored.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="identity-{row.reference}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


@router.post(
    "/{request_id}/identity/review",
    response_model=DsarDetail,
    summary="Accept or refuse the submitted identity document",
)
async def review_identity_document(
    request_id: uuid.UUID,
    body: IdentityReview,
    current: Annotated[CurrentUser, Depends(require(Capability.DSAR_PROCESS))],
) -> Any:
    """Accepting destroys the document; its purpose is then served — §8(7).

    Refusing requires a reason, which the person is entitled to be told.
    """
    row = await dsar_service.get(current.session, current.tenant_id, request_id)
    await dsar_fulfilment_service.review_identity(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        request=row,
        reviewer_id=current.user.id,
        accept=body.accept,
        reason=body.reason,
    )
    return await _detail(current, row)


# --------------------------------------------------------------------------- #
# The message thread
# --------------------------------------------------------------------------- #

class MessageBody(BaseModel):
    body: str = Field(..., min_length=1, max_length=20_000)


@router.get("/{request_id}/messages", summary="The correspondence on this request")
async def get_messages(
    request_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.SELF_READ))],
) -> dict[str, Any]:
    """Both sides of the thread. Reading marks the other side's messages seen."""
    row = await dsar_service.get(current.session, current.tenant_id, request_id)
    staff = Capability.DSAR_READ.value in set(current.capabilities)
    if not staff:
        mine = await _self_principal(current)
        if row.principal_id != mine.id:
            raise NotFound("No such request.")

    messages = await dsar_fulfilment_service.thread(
        current.session, request_id=row.id
    )
    await dsar_fulfilment_service.mark_read(
        current.session, request_id=row.id, reader_is_staff=staff
    )

    out = []
    for message in messages:
        attachments = await file_service.for_entity(
            current.session, entity_type="dsar_message", entity_id=message.id
        )
        out.append(dsar_fulfilment_service.message_as_dict(message, attachments))
    return {"reference": row.reference, "messages": out}


@router.post(
    "/{request_id}/messages", status_code=201, summary="Send a message on this request"
)
async def post_message(
    request_id: uuid.UUID,
    body: MessageBody,
    current: Annotated[CurrentUser, Depends(require(Capability.SELF_READ))],
) -> dict[str, Any]:
    """Direction is derived from who is asking, never from the request body.

    A caller who could set `direction` themselves could forge a message that
    appears to have come from the data principal — which in a statutory
    correspondence record is evidence fabrication.
    """
    row = await dsar_service.get(current.session, current.tenant_id, request_id)
    staff = Capability.DSAR_PROCESS.value in set(current.capabilities)

    if staff:
        message = await dsar_fulfilment_service.post_message(
            current.session,
            tenant_id=current.tenant_id, actor=current.actor, request=row,
            body=body.body, direction="to_principal",
            author_user_id=current.user.id,
            author_label=current.user.full_name or current.user.email,
        )
    else:
        mine = await _self_principal(current)
        if row.principal_id != mine.id:
            raise NotFound("No such request.")
        message = await dsar_fulfilment_service.post_message(
            current.session,
            tenant_id=current.tenant_id, actor=current.actor, request=row,
            body=body.body, direction="from_principal",
            author_principal_id=mine.id,
            author_label=current.user.email,
            notify=False,
        )
    return dsar_fulfilment_service.message_as_dict(message, [])


# --------------------------------------------------------------------------- #
# Assembling and delivering the disclosure
# --------------------------------------------------------------------------- #

class AssembleBody(BaseModel):
    #: Typed back, like the retention live run and the connected erasure. This
    #: gathers one person's entire record into a single object.
    confirm_reference: str = Field(..., max_length=32)


class DeliverBody(BaseModel):
    covering_note: str | None = Field(default=None, max_length=5000)


@router.get(
    "/{request_id}/disclosure/preview",
    summary="What would be disclosed — sensitive values excluded",
)
async def preview_disclosure(
    request_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.DSAR_PROCESS))],
) -> dict[str, Any]:
    """Field names, locations, and the values that identify nobody on their own.

    Government ID, financial and health values read `<SENSITIVE VALUE EXCLUDED>`,
    and credential material is withheld outright. This is the only view of the
    disclosure staff get — the assembled package itself is readable by its
    subject and by nobody else.
    """
    row = await dsar_service.get(current.session, current.tenant_id, request_id)
    return await dsar_fulfilment_service.preview(
        current.session, tenant_id=current.tenant_id, actor=current.actor, request=row
    )


@router.post(
    "/{request_id}/disclosure/assemble",
    summary="Build and store the disclosure package. Does not send it.",
)
async def assemble_disclosure(
    request_id: uuid.UUID,
    body: AssembleBody,
    current: Annotated[CurrentUser, Depends(require(Capability.DSAR_PROCESS))],
) -> dict[str, Any]:
    """Assembling is not delivering, and the two must stay distinguishable.

    A package can be assembled, checked against the preview, and found wrong.
    "We prepared it" must never read as "they received it" in an audit.
    """
    row = await dsar_service.get(current.session, current.tenant_id, request_id)
    stored, summary = await dsar_fulfilment_service.assemble(
        current.session,
        tenant_id=current.tenant_id, actor=current.actor, request=row,
        confirm_reference=body.confirm_reference,
    )
    return {
        "package": file_service.as_dict(stored),
        "fields": summary["field_count"],
        "collections": summary["collections"],
        "counts": summary["counts"],
        "expires_at": row.package_available_until,
        "delivered": False,
    }


@router.post(
    "/{request_id}/disclosure/deliver",
    summary="Put the assembled package in the thread and notify the requester",
)
async def deliver_disclosure(
    request_id: uuid.UUID,
    body: DeliverBody,
    current: Annotated[CurrentUser, Depends(require(Capability.DSAR_PROCESS))],
) -> dict[str, Any]:
    """The package is never emailed. The notification says one is waiting."""
    row = await dsar_service.get(current.session, current.tenant_id, request_id)
    message = await dsar_fulfilment_service.deliver(
        current.session,
        tenant_id=current.tenant_id, actor=current.actor, request=row,
        author_user_id=current.user.id,
        author_label=current.user.full_name or current.user.email,
        covering_note=body.covering_note,
    )
    return {
        "delivered_at": row.package_delivered_at,
        "message": dsar_fulfilment_service.message_as_dict(message, []),
    }
