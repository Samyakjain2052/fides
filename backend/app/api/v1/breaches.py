"""Breach routes — DPDP §8(6).

**Every route here requires `BREACH_MANAGE`, including the reads.** Breach detail
is the most sensitive combination in the product — who was affected by what — so
there is no wider read scope, and specifically an Auditor does not get the
affected list. An auditor's job is to check that the obligation was discharged,
which the counts and the timestamps answer; the names add nothing to that and
would spread the most sensitive join in the system to a second role.

Two shapes worth noting:

* **There is no route that sets `status = 'notified'`.** The status follows the
  work: it advances when both halves of the duty are actually recorded. A status
  the UI can assert independently of the work is a status that will eventually be
  wrong, and a CHECK constraint refuses it at the database too.

* **`notify-board` records a human's action.** It returns the content to submit
  and stores who submitted it and the reference they got back. This product does
  not transmit anything to the Board and the endpoint's name is the closest thing
  to a lie in this module, so the response says so explicitly.

There is no DELETE. A mistaken entry is voided with a reason and kept.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, require
from app.core.permissions import Capability
from app.models.breach import BOARD_NOTIFICATION_HOURS, SEVERITIES, Breach
from app.models.tenant import Tenant
from app.services import breach_service

router = APIRouter(prefix="/breaches", tags=["breaches"])


# --------------------------------------------------------------------------- #
# Shapes
# --------------------------------------------------------------------------- #

class BreachCreate(BaseModel):
    title: str = Field(..., min_length=3, max_length=255)
    description: str = Field(..., min_length=10, max_length=20000)
    severity: str = Field("medium", pattern="^(low|medium|high|critical)$")
    discovered_at: datetime | None = Field(
        None,
        description="When you became aware. The statutory clock runs from this, "
                    "not from when the breach happened. Optional only while the "
                    "entry is a draft.",
    )
    occurred_at: datetime | None = Field(
        None, description="Often genuinely unknown; leave it null rather than guess."
    )
    categories_affected: list[str] = Field(default_factory=list, max_length=50)
    estimated_affected_count: int | None = Field(None, ge=0)


class BreachUpdate(BaseModel):
    title: str | None = Field(None, min_length=3, max_length=255)
    description: str | None = Field(None, min_length=10, max_length=20000)
    severity: str | None = Field(None, pattern="^(low|medium|high|critical)$")
    discovered_at: datetime | None = None
    discovered_at_reason: str | None = Field(
        None, max_length=2000,
        description="Required when changing an already-recorded awareness date. "
                    "This is the field every deadline is measured from.",
    )
    occurred_at: datetime | None = None
    contained_at: datetime | None = None
    categories_affected: list[str] | None = Field(None, max_length=50)
    estimated_affected_count: int | None = Field(None, ge=0)
    root_cause: str | None = Field(None, max_length=20000)
    remediation: str | None = Field(None, max_length=20000)


class StatusChange(BaseModel):
    # `notified`, `closed` and `void` are absent on purpose — each has its own
    # endpoint, because each requires something the caller must supply.
    to_status: str = Field(..., pattern="^(investigating|contained)$")
    note: str | None = Field(None, max_length=2000)


class ProgressOut(BaseModel):
    total: int
    notified: int
    suppressed: int
    remaining: int
    complete: bool
    # The sentence the UI shows. "4,812 of 10,000 notified" is the truth; a green
    # tick at 48% is not.
    summary: str


class BreachOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    reference: str
    title: str
    description: str
    severity: str
    status: str
    occurred_at: Any | None
    discovered_at: Any | None
    contained_at: Any | None
    closed_at: Any | None
    categories_affected: list[str]
    estimated_affected_count: int | None
    root_cause: str | None
    remediation: str | None
    board_notified_at: Any | None
    board_reference: str | None
    board_submitted_by: str | None
    principals_notified_at: Any | None
    notification_exemption: str | None
    void_reason: str | None

    # Computed against the clock on every read, never stored.
    board_deadline_at: Any | None
    board_overdue: bool
    hours_since_discovery: float | None
    affected_count: int


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_status: str | None
    to_status: str | None
    note: str | None
    actor_label: str | None
    automated: bool
    created_at: Any


class BreachDetail(BreachOut):
    timeline: list[EventOut]
    progress: ProgressOut


class BreachPage(BaseModel):
    items: list[BreachOut]
    counts: dict[str, int]
    # Our operational reading of "without delay", surfaced rather than buried, so
    # nobody mistakes it for a figure from the statute.
    board_threshold_hours: int
    board_threshold_note: str


class AffectedQuery(BaseModel):
    """Attach by category query, by explicit list, or both."""

    categories: list[str] | None = Field(
        None,
        description="Attaches everyone holding a consent for a purpose in these "
                    "categories. Preview it first — notifying the wrong people "
                    "about a breach is itself an incident.",
    )
    principal_ids: list[uuid.UUID] | None = None


class AffectedPreviewOut(BaseModel):
    matched: int
    sample: list[dict[str, Any]]
    note: str


class AffectedRowOut(BaseModel):
    principal_id: uuid.UUID
    principal_ref: str | None
    email: str | None
    source: str
    notified_at: Any | None
    suppressed_reason: str | None


class BoardNotify(BaseModel):
    submitted_by: str = Field(
        ..., min_length=2, max_length=255,
        description="The person who submitted it. This product does not transmit "
                    "anything to the Board, so the record names who did.",
    )
    board_reference: str | None = Field(None, max_length=255)
    submitted_at: datetime | None = None


class BoardContentOut(BaseModel):
    content: str
    submitted: bool
    note: str


class CloseBody(BaseModel):
    root_cause: str = Field(..., min_length=5, max_length=20000)
    remediation: str = Field(..., min_length=5, max_length=20000)
    notification_exemption: str | None = Field(
        None, max_length=4000,
        description="Required only if affected people remain un-notified. A written "
                    "reason, because 'we decided not to tell them' is a decision "
                    "somebody may be asked to justify.",
    )


class VoidBody(BaseModel):
    reason: str = Field(..., min_length=5, max_length=2000)


async def _out(current: CurrentUser, row: Breach) -> dict[str, Any]:
    return {
        **{c.name: getattr(row, c.name) for c in row.__table__.columns},
        "board_deadline_at": row.board_deadline_at,
        "board_overdue": row.board_overdue,
        "hours_since_discovery": row.hours_since_discovery,
        "affected_count": await breach_service.affected_count(
            current.session, tenant_id=current.tenant_id, breach=row
        ),
    }


async def _detail(current: CurrentUser, row: Breach) -> dict[str, Any]:
    # Refresh first: after a write, the columns nobody assigned are unloaded, and
    # `_out` walks every column. Without this the route raises MissingGreenlet on
    # the first write — the same bug the grievance routes had.
    await current.session.refresh(row)
    progress = await breach_service.notify_progress(
        current.session, tenant_id=current.tenant_id, breach=row
    )
    return {
        **await _out(current, row),
        "timeline": await breach_service.timeline(
            current.session, current.tenant_id, row.id
        ),
        "progress": {**progress.__dict__, "summary": progress.summary},
    }


# --------------------------------------------------------------------------- #
# The register
# --------------------------------------------------------------------------- #

@router.get("", response_model=BreachPage, summary="The breach register")
async def list_breaches(
    current: Annotated[CurrentUser, Depends(require(Capability.BREACH_MANAGE))],
    status: str | None = Query(
        None, pattern="^(draft|investigating|contained|notified|closed|void)$"
    ),
    severity: str | None = Query(None, pattern="^(low|medium|high|critical)$"),
    open_only: bool = False,
    limit: int = Query(100, ge=1, le=500),
) -> Any:
    rows = await breach_service.list_for_tenant(
        current.session, current.tenant_id, status=status, severity=severity,
        open_only=open_only, limit=limit,
    )
    return {
        "items": [await _out(current, r) for r in rows],
        "counts": await breach_service.counts(current.session, current.tenant_id),
        "board_threshold_hours": BOARD_NOTIFICATION_HOURS,
        "board_threshold_note": (
            f"The Rules say the Board must be told 'without delay', which is not a "
            f"number. {BOARD_NOTIFICATION_HOURS} hours is this product's "
            "operational reading and what the countdown uses — it is not a "
            "statutory figure."
        ),
    }


@router.post("", response_model=BreachDetail, status_code=201,
             summary="Record a breach (opens as a draft)")
async def create_breach(
    body: BreachCreate,
    current: Annotated[CurrentUser, Depends(require(Capability.BREACH_MANAGE))],
) -> Any:
    row = await breach_service.record(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        title=body.title,
        description=body.description,
        severity=body.severity,
        discovered_at=body.discovered_at,
        occurred_at=body.occurred_at,
        categories_affected=body.categories_affected,
        estimated_affected_count=body.estimated_affected_count,
    )
    return await _detail(current, row)


@router.get("/{breach_id}", response_model=BreachDetail,
            summary="One breach, its timeline and its notification progress")
async def get_breach(
    breach_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.BREACH_MANAGE))],
) -> Any:
    row = await breach_service.get(current.session, current.tenant_id, breach_id)
    return await _detail(current, row)


@router.patch("/{breach_id}", response_model=BreachDetail, summary="Update a breach")
async def update_breach(
    breach_id: uuid.UUID,
    body: BreachUpdate,
    current: Annotated[CurrentUser, Depends(require(Capability.BREACH_MANAGE))],
) -> Any:
    """Changing an already-recorded `discovered_at` requires a reason.

    Both the old and new values go into the audit chain under their own action, so
    a quiet backdating of when you became aware is visible to anyone reading it.
    """
    row = await breach_service.get(current.session, current.tenant_id, breach_id)
    await breach_service.update(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        breach=row,
        discovered_at_reason=body.discovered_at_reason,
        **body.model_dump(exclude={"discovered_at_reason"}, exclude_none=True),
    )
    return await _detail(current, row)


@router.post("/{breach_id}/status", response_model=BreachDetail,
             summary="Move to investigating or contained")
async def change_status(
    breach_id: uuid.UUID,
    body: StatusChange,
    current: Annotated[CurrentUser, Depends(require(Capability.BREACH_MANAGE))],
) -> Any:
    row = await breach_service.get(current.session, current.tenant_id, breach_id)
    await breach_service.change_status(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        breach=row, to_status=body.to_status, note=body.note,
    )
    return await _detail(current, row)


# --------------------------------------------------------------------------- #
# Who was affected
# --------------------------------------------------------------------------- #

@router.post("/{breach_id}/affected/preview", response_model=AffectedPreviewOut,
             summary="Who a category query would attach — sends nothing")
async def preview_affected(
    breach_id: uuid.UUID,
    body: AffectedQuery,
    current: Annotated[CurrentUser, Depends(require(Capability.BREACH_MANAGE))],
) -> Any:
    """Review before attaching, let alone notifying.

    A DPO must be able to see and correct exactly who is about to be told.
    Notifying the wrong people about a breach is itself an incident, and it is not
    an incident you can undo.
    """
    await breach_service.get(current.session, current.tenant_id, breach_id)
    people = await breach_service.find_by_categories(
        current.session, tenant_id=current.tenant_id,
        categories=body.categories or [],
    )
    return AffectedPreviewOut(
        matched=len(people),
        sample=[
            {"principal_id": str(p.id), "principal_ref": p.external_id, "email": p.email}
            for p in people[:25]
        ],
        note=(
            "Everyone holding a consent for a purpose in these categories, "
            "excluding principals whose data has already been purged. Nothing is "
            "attached or sent by this call."
        ),
    )


@router.post("/{breach_id}/affected", response_model=BreachDetail,
             summary="Attach affected data principals")
async def attach_affected(
    breach_id: uuid.UUID,
    body: AffectedQuery,
    current: Annotated[CurrentUser, Depends(require(Capability.BREACH_MANAGE))],
) -> Any:
    row = await breach_service.get(current.session, current.tenant_id, breach_id)

    ids: list[uuid.UUID] = list(body.principal_ids or [])
    source = "manual" if ids else "query"
    if body.categories:
        found = await breach_service.find_by_categories(
            current.session, tenant_id=current.tenant_id, categories=body.categories
        )
        ids += [p.id for p in found]
        source = "query" if not body.principal_ids else "mixed"
    if not ids:
        from app.core.errors import Conflict

        raise Conflict(
            "Nobody to attach. Supply a list of principals, or categories to match."
        )

    await breach_service.attach_affected(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        breach=row, principal_ids=ids, source=source,
    )
    return await _detail(current, row)


@router.get("/{breach_id}/affected", response_model=list[AffectedRowOut],
            summary="Who is on the affected list, and whether they were told")
async def list_affected(
    breach_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.BREACH_MANAGE))],
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
) -> Any:
    """The most sensitive read in the product.

    `breach:manage` only — deliberately not readable by an Auditor. Checking that
    the obligation was discharged is answered by the counts and the timestamps;
    the names add nothing to that and would spread the who-was-affected-by-what
    join to a second role.
    """
    await breach_service.get(current.session, current.tenant_id, breach_id)
    rows = await breach_service.affected_list(
        current.session, current.tenant_id, breach_id, offset=offset, limit=limit
    )
    return [
        AffectedRowOut(
            principal_id=link.principal_id,
            principal_ref=principal.external_id if principal else None,
            email=principal.email if principal else None,
            source=link.source,
            notified_at=link.notified_at,
            suppressed_reason=link.suppressed_reason,
        )
        for link, principal in rows
    ]


# --------------------------------------------------------------------------- #
# The notification duty
# --------------------------------------------------------------------------- #

@router.get("/{breach_id}/board-notice", response_model=BoardContentOut,
            summary="The text to submit to the Board — generated, not sent")
async def board_notice(
    breach_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.BREACH_MANAGE))],
) -> Any:
    row = await breach_service.get(current.session, current.tenant_id, breach_id)
    tenant = await current.session.scalar(
        select(Tenant).where(Tenant.id == current.tenant_id)
    )
    return BoardContentOut(
        content=breach_service.board_notification_content(row, tenant),
        submitted=row.board_notified_at is not None,
        note=(
            "Copy this into the Board's portal yourself. This product does not "
            "transmit anything to the Data Protection Board — unattended software "
            "contacting a regulator is not something it should do — so record the "
            "submission afterwards with the reference you are given."
        ),
    )


@router.post("/{breach_id}/notify-board", response_model=BreachDetail,
             summary="Record that a human submitted the Board notification")
async def notify_board(
    breach_id: uuid.UUID,
    body: BoardNotify,
    current: Annotated[CurrentUser, Depends(require(Capability.BREACH_MANAGE))],
) -> Any:
    """Records somebody's action. It does not perform one.

    Requires the submitter's name for that reason: "the system reported it" would
    be false, and a compliance record whose most load-bearing claim is false is
    worse than no record at all.
    """
    row = await breach_service.get(current.session, current.tenant_id, breach_id)
    await breach_service.notify_board(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        breach=row, submitted_by=body.submitted_by,
        reference=body.board_reference, submitted_at=body.submitted_at,
    )
    return await _detail(current, row)


@router.post("/{breach_id}/notify-principals", response_model=BreachDetail,
             summary="Start or resume notifying the affected people")
async def notify_principals(
    breach_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.BREACH_MANAGE))],
    batch: int = Query(
        breach_service.NOTIFY_BATCH, ge=1, le=1000,
        description="How many to attempt in this call. Call again to continue — "
                    "every attempt is recorded per person, so resuming never "
                    "double-notifies.",
    ),
) -> Any:
    """Resumable by design, because a half-finished run is the normal case.

    Ten thousand people and a provider rate limit means the first attempt will not
    finish. Progress comes back as counts over the affected table rather than a
    total held in memory, so the figure survives a crash and is never optimistic.
    """
    row = await breach_service.get(current.session, current.tenant_id, breach_id)
    await breach_service.notify_principals(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        breach=row, batch=batch,
    )
    return await _detail(current, row)


# --------------------------------------------------------------------------- #
# Ending
# --------------------------------------------------------------------------- #

@router.post("/{breach_id}/close", response_model=BreachDetail,
             summary="Close, with the cause and the fix on the record")
async def close_breach(
    breach_id: uuid.UUID,
    body: CloseBody,
    current: Annotated[CurrentUser, Depends(require(Capability.BREACH_MANAGE))],
) -> Any:
    """Refuses while affected people remain un-notified, unless an exemption is written.

    Notifying the Board and notifying the individuals are separate obligations, and
    closing on the strength of the first would record compliance that did not
    happen.
    """
    row = await breach_service.get(current.session, current.tenant_id, breach_id)
    await breach_service.close(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        breach=row, root_cause=body.root_cause, remediation=body.remediation,
        notification_exemption=body.notification_exemption,
    )
    return await _detail(current, row)


@router.post("/{breach_id}/void", response_model=BreachDetail,
             summary="Mark an entry as recorded in error (it is kept)")
async def void_breach(
    breach_id: uuid.UUID,
    body: VoidBody,
    current: Annotated[CurrentUser, Depends(require(Capability.BREACH_MANAGE))],
) -> Any:
    """There is no delete in this module.

    A register whose entries can vanish is not a register, and "this was a mistake,
    here is why" is more useful to the next reader than an absence.
    """
    row = await breach_service.get(current.session, current.tenant_id, breach_id)
    await breach_service.void(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        breach=row, reason=body.reason,
    )
    return await _detail(current, row)
