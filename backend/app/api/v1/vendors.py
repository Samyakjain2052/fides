"""Vendor register routes.

`vendor:read` for looking, `vendor:manage` for changing. The auditor has the
first, for the same reason they have `assessment:read` — a processor list is
exactly what an audit inspects, and inspecting it changes nothing.

Status is deliberately not settable through the generic patch. A status change
is a decision about whether a third party may receive personal data, and it
carries a reason and an audit entry; folding it into a field update is how a
vendor becomes approved with nobody's name on it.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, require
from app.api.v1.dsar import _read_bounded
from app.core.permissions import Capability
from app.models.tenant import Tenant
from app.models.user import User
from app.models.vendor import VendorDocument
from app.services import file_service, vendor_service

router = APIRouter(prefix="/vendors", tags=["vendors"])


class VendorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=200)
    domain: str | None = Field(None, max_length=255)
    description: str | None = Field(None, max_length=4000)
    role: str = "processor"
    risk_tier: str = "medium"
    owner_user_id: uuid.UUID | None = None


class VendorPatch(BaseModel):
    #: `forbid`, so a typo is a 422 rather than a silently dropped field. This
    #: codebase has been bitten before by a `data_category` change that appeared
    #: to succeed because Pydantic discarded the field.
    model_config = ConfigDict(extra="forbid")

    domain: str | None = Field(None, max_length=255)
    description: str | None = Field(None, max_length=4000)
    role: str | None = None
    risk_tier: str | None = None
    owner_user_id: uuid.UUID | None = None
    dpa_signed: bool | None = None
    dpa_signed_on: date | None = None
    dpa_expires_on: date | None = None
    dsar_contact: str | None = Field(None, max_length=320)
    dsar_sla_days: int | None = Field(None, gt=0, le=365)
    breach_notice_hours: int | None = Field(None, gt=0, le=8760)
    data_categories: list[str] | None = None
    data_location: str | None = Field(None, max_length=4000)
    transfers_outside_india: bool | None = None
    subprocessors: list[dict[str, Any]] | None = None
    certifications: list[str] | None = None
    review_every_days: int | None = Field(None, gt=0, le=3650)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    #: Required for `rejected` and `conditional`. Enforced in the service.
    note: str | None = Field(None, max_length=4000)


class DocumentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    title: str = Field(..., min_length=1, max_length=200)
    url: str | None = Field(None, max_length=2000)
    note: str | None = Field(None, max_length=2000)


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The document's text as read, so its fingerprint can be recorded. Optional:
    #: somebody may simply be confirming they looked.
    content: str | None = Field(None, max_length=2_000_000)


class LinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: uuid.UUID


async def _full(current: CurrentUser, vendor) -> dict[str, Any]:
    owner = None
    if vendor.owner_user_id:
        owner = await current.session.scalar(
            select(User).where(User.id == vendor.owner_user_id)
        )
    tenant = await current.session.scalar(
        select(Tenant).where(Tenant.id == current.tenant_id)
    )
    return vendor_service.as_dict(
        vendor,
        owner=owner,
        concern_list=await vendor_service.concerns(
            current.session, vendor=vendor, tenant=tenant
        ),
        systems=await vendor_service.systems_for(
            current.session, vendor_id=vendor.id
        ),
        documents=await vendor_service.documents_for(
            current.session, vendor_id=vendor.id
        ),
    )


@router.get("", summary="The vendor register")
async def list_vendors(
    current: Annotated[CurrentUser, Depends(require(Capability.VENDOR_READ))],
) -> dict[str, Any]:
    """Every vendor, with their concerns.

    Concerns are computed per vendor rather than left to the screen, because the
    comparison against the tenant's own DSAR deadline needs the tenant — and a
    browser that computed it would have to be told the deadline, which is
    server-side policy.
    """
    rows = await vendor_service.list_all(current.session)
    tenant = await current.session.scalar(
        select(Tenant).where(Tenant.id == current.tenant_id)
    )

    owner_ids = {v.owner_user_id for v in rows if v.owner_user_id}
    people = {}
    if owner_ids:
        people = {
            u.id: u
            for u in (
                await current.session.execute(
                    select(User).where(User.id.in_(owner_ids))
                )
            ).scalars().all()
        }

    out = []
    for vendor in rows:
        out.append(
            vendor_service.as_dict(
                vendor,
                owner=people.get(vendor.owner_user_id),
                concern_list=await vendor_service.concerns(
                    current.session, vendor=vendor, tenant=tenant
                ),
            )
        )
    return {
        "items": out,
        # Counts, not a score. See services/vendor_service.py for why there is
        # deliberately no number per vendor.
        "counts": {
            "total": len(out),
            "in_use": sum(1 for v in out if v["in_use"]),
            "no_dpa": sum(1 for v in out if v["dpa_missing"]),
            "dpa_expired": sum(1 for v in out if v["dpa_expired"]),
            "review_overdue": sum(1 for v in out if v["review_overdue"]),
            "with_high_concerns": sum(
                1 for v in out if v.get("worst_severity") == "high"
            ),
        },
    }


@router.post("", status_code=201, summary="Add a vendor")
async def create_vendor(
    body: VendorIn,
    current: Annotated[CurrentUser, Depends(require(Capability.VENDOR_MANAGE))],
) -> dict[str, Any]:
    """Starts `prospective`.

    Recording that a vendor exists and deciding they may receive personal data
    are different acts — the same reason a connection starts `unverified`.
    """
    vendor = await vendor_service.create(
        current.session,
        tenant_id=current.tenant_id, actor=current.actor,
        name=body.name, domain=body.domain, description=body.description,
        role=body.role, risk_tier=body.risk_tier,
        owner_user_id=body.owner_user_id, created_by=current.user.id,
    )
    return await _full(current, vendor)


@router.get("/{vendor_id}", summary="One vendor, with concerns, systems and documents")
async def get_vendor(
    vendor_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.VENDOR_READ))],
) -> dict[str, Any]:
    vendor = await vendor_service.get(current.session, vendor_id=vendor_id)
    return await _full(current, vendor)


@router.patch("/{vendor_id}", summary="Edit the register entry")
async def update_vendor(
    vendor_id: uuid.UUID,
    body: VendorPatch,
    current: Annotated[CurrentUser, Depends(require(Capability.VENDOR_MANAGE))],
) -> dict[str, Any]:
    vendor = await vendor_service.get(current.session, vendor_id=vendor_id)
    # Only the fields actually sent, so a PATCH omitting `dpa_signed` does not
    # read as "set it to null".
    await vendor_service.update(
        current.session,
        tenant_id=current.tenant_id, actor=current.actor, vendor=vendor,
        **body.model_dump(exclude_unset=True),
    )
    return await _full(current, vendor)


@router.post("/{vendor_id}/decision", summary="Approve, refuse or retire a vendor")
async def decide_vendor(
    vendor_id: uuid.UUID,
    body: DecisionIn,
    current: Annotated[CurrentUser, Depends(require(Capability.VENDOR_MANAGE))],
) -> dict[str, Any]:
    """A refusal needs a reason, and so does a conditional approval.

    In the second case the note IS the record of what remains outstanding —
    which is the point of having the state at all, since most real vendor
    relationships are conditional rather than cleanly approved.
    """
    vendor = await vendor_service.get(current.session, vendor_id=vendor_id)
    await vendor_service.decide(
        current.session,
        tenant_id=current.tenant_id, actor=current.actor, vendor=vendor,
        status=body.status, note=body.note,
    )
    return await _full(current, vendor)


@router.post(
    "/{vendor_id}/documents", status_code=201, summary="Record a document"
)
async def add_document(
    vendor_id: uuid.UUID,
    body: DocumentIn,
    current: Annotated[CurrentUser, Depends(require(Capability.VENDOR_MANAGE))],
) -> dict[str, Any]:
    vendor = await vendor_service.get(current.session, vendor_id=vendor_id)
    document = await vendor_service.add_document(
        current.session,
        tenant_id=current.tenant_id, actor=current.actor, vendor=vendor,
        kind=body.kind, title=body.title, url=body.url, note=body.note,
    )
    return {"id": str(document.id), "title": document.title}


@router.post(
    "/{vendor_id}/documents/upload",
    status_code=201,
    summary="Upload a document — a signed DPA, a security report",
)
async def upload_document(
    vendor_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.VENDOR_MANAGE))],
    file: Annotated[UploadFile, File()],
    kind: str = "other",
) -> dict[str, Any]:
    vendor = await vendor_service.get(current.session, vendor_id=vendor_id)
    stored = await file_service.store(
        current.session,
        tenant_id=current.tenant_id,
        # Reuses the assessment-evidence purpose rather than adding a new one:
        # the access rule is identical (staff who may see assessments) and a
        # new purpose with the same rule is a second thing to keep in step.
        purpose="assessment_evidence",
        entity_type="vendor",
        entity_id=vendor.id,
        filename=file.filename or "document",
        data=await _read_bounded(file),
        declared_content_type=file.content_type,
        uploaded_by=current.user.id,
    )
    document = await vendor_service.add_document(
        current.session,
        tenant_id=current.tenant_id, actor=current.actor, vendor=vendor,
        kind=kind, title=stored.filename, file_id=stored.id,
    )
    return {"id": str(document.id), "file": file_service.as_dict(stored)}


@router.post(
    "/{vendor_id}/documents/{document_id}/review",
    summary="Record that somebody read this document",
)
async def review_document(
    vendor_id: uuid.UUID,
    document_id: uuid.UUID,
    body: ReviewIn,
    current: Annotated[CurrentUser, Depends(require(Capability.VENDOR_MANAGE))],
) -> dict[str, Any]:
    """Stores a fingerprint of what it said, so a later look can tell whether
    it changed. Nothing here fetches anything."""
    document = await current.session.scalar(
        select(VendorDocument).where(
            VendorDocument.id == document_id,
            VendorDocument.vendor_id == vendor_id,
        )
    )
    if document is None:
        from app.core.errors import NotFound

        raise NotFound("No such document.")

    await vendor_service.record_review(
        current.session,
        tenant_id=current.tenant_id, actor=current.actor,
        document=document, content=body.content,
    )
    return {
        "id": str(document.id),
        "last_seen_at": document.last_seen_at,
        "changed_since_review": document.changed_since_review,
    }


@router.post(
    "/{vendor_id}/documents/{document_id}/check",
    summary="Compare fresh content against what was last read",
)
async def check_document(
    vendor_id: uuid.UUID,
    document_id: uuid.UUID,
    body: ReviewIn,
    current: Annotated[CurrentUser, Depends(require(Capability.VENDOR_MANAGE))],
) -> dict[str, Any]:
    """Returns whether it moved.

    Takes the content rather than fetching it. Fetching an arbitrary
    customer-supplied URL from our own network is an SSRF primitive, and
    `connectors/hosts.py` exists because that lesson has already been learned
    here once — if this ever fetches, it goes through that fence.
    """
    if not (body.content or "").strip():
        from app.core.errors import ValidationProblem

        raise ValidationProblem("Send the document's current text to compare.")

    document = await current.session.scalar(
        select(VendorDocument).where(
            VendorDocument.id == document_id,
            VendorDocument.vendor_id == vendor_id,
        )
    )
    if document is None:
        from app.core.errors import NotFound

        raise NotFound("No such document.")

    changed = await vendor_service.note_content_changed(
        current.session,
        tenant_id=current.tenant_id, actor=current.actor,
        document=document, content=body.content,
    )
    return {"changed": changed, "title": document.title}


@router.post("/{vendor_id}/systems", status_code=201,
             summary="Say this vendor supplies a connected system")
async def link_system(
    vendor_id: uuid.UUID,
    body: LinkIn,
    current: Annotated[CurrentUser, Depends(require(Capability.VENDOR_MANAGE))],
) -> dict[str, Any]:
    """The join between the register and the data map.

    "This vendor is retired — which of our systems does that affect?" cannot be
    answered from two separate lists.
    """
    vendor = await vendor_service.get(current.session, vendor_id=vendor_id)
    await vendor_service.link_system(
        current.session, tenant_id=current.tenant_id, vendor=vendor,
        connection_id=body.connection_id,
    )
    return await _full(current, vendor)


@router.delete("/{vendor_id}/systems/{connection_id}", status_code=204,
               summary="Unlink a system from a vendor")
async def unlink_system(
    vendor_id: uuid.UUID,
    connection_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.VENDOR_MANAGE))],
) -> Response:
    # `-> Response`, not `-> None`: FastAPI asserts a 204 has no body and would
    # fail at import, taking every route with it.
    from sqlalchemy import delete

    from app.models.vendor import VendorSystem

    await current.session.execute(
        delete(VendorSystem).where(
            VendorSystem.vendor_id == vendor_id,
            VendorSystem.connection_id == connection_id,
        )
    )
    return Response(status_code=204)
