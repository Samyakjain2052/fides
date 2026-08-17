"""Grievance routes — the queue, the filer's own view, and the published officer.

Two things about permissions here are worth stating, because both are easy to get
wrong in a way that looks fine:

* **A Grievance Officer must not be able to read anything else.** The role's nav
  is already restricted, but nav is presentation. The routes carry
  `GRIEVANCE_READ` / `GRIEVANCE_PROCESS` and nothing wider, so the restriction
  holds against anyone who types a URL. A test signs in as that role and asserts
  403 on consent, DSAR, audit and users.

* **`/mine` is scoped from the session**, never from a parameter. There is no id
  to pass and therefore none to tamper with. A `?principal_id=` on a self-service
  endpoint is how one person reads another person's complaints.

Listing the queue also runs the escalation sweep. That is not a side effect
smuggled into a GET for convenience — it is the fix for the window a nightly job
leaves open, and it is idempotent. See the service docstring.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.api.deps import CurrentUser, require
from app.core.permissions import Capability
from app.models.grievance import GRIEVANCE_CATEGORIES, Grievance
from app.services import grievance_service

router = APIRouter(prefix="/grievances", tags=["grievances"])


# --------------------------------------------------------------------------- #
# Shapes
# --------------------------------------------------------------------------- #

class GrievanceFile(BaseModel):
    category: str = Field(..., examples=list(GRIEVANCE_CATEGORIES)[:1])
    description: str = Field(
        ..., min_length=10, max_length=grievance_service.MAX_DESCRIPTION,
        description="What went wrong, in the person's own words. Capped here as "
                    "well as in the form, because the form is not the only way in.",
    )
    contact_email: EmailStr | None = Field(
        None,
        description="Only needed when filing on somebody else's behalf. A signed-in "
                    "person's account address is used otherwise.",
    )
    related_dsar_id: uuid.UUID | None = None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_status: str | None
    to_status: str | None
    note: str | None
    actor_label: str | None
    automated: bool
    created_at: Any


class GrievanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    reference: str
    category: str
    # Returned RAW. React escapes on render, which is the correct arrangement for
    # a browser; pre-escaping here would double-escape and show a DPO `&amp;lt;`.
    # Any non-HTML sink (PDF, CSV, plain-text email) must call
    # `grievance_service.safe_text` — see its docstring.
    description: str
    status: str
    contact_email: str | None
    contact_verified: bool
    assigned_to: uuid.UUID | None
    related_dsar_id: uuid.UUID | None
    submitted_at: Any
    deadline_at: Any
    escalate_at: Any
    acknowledged_at: Any | None
    resolved_at: Any | None
    escalated: bool
    escalated_at: Any | None
    resolution_notes: str | None
    rejection_reason: str | None
    satisfaction_rating: int | None
    satisfaction_comment: str | None

    # Computed against the clock on every read, never stored. A stored flag is
    # only as fresh as the last job that ran.
    is_overdue: bool
    days_open: int


class GrievanceDetail(GrievanceOut):
    timeline: list[EventOut]


class GrievancePage(BaseModel):
    items: list[GrievanceOut]
    counts: dict[str, int]


class StatusChange(BaseModel):
    to_status: str = Field(
        ..., pattern="^(acknowledged|in_progress|resolved|rejected|reopened)$"
    )
    resolution_notes: str | None = Field(
        None, max_length=8000,
        description="Required to resolve. A redressal mechanism that records no "
                    "redress is not one.",
    )
    rejection_reason: str | None = Field(
        None, max_length=4000,
        description="Required to reject. This is the point at which the person's "
                    "next step is the Data Protection Board.",
    )
    note: str | None = Field(None, max_length=2000)


class Assignment(BaseModel):
    user_id: uuid.UUID | None = Field(
        None, description="Null unassigns. Must be an active user in this workspace."
    )


class Escalation(BaseModel):
    reason: str | None = Field(None, max_length=1000)


class Feedback(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    comment: str | None = Field(None, max_length=4000)


class OfficerOut(BaseModel):
    name: str | None
    email: str | None
    # False when the contact has been cleared. Surfaced rather than rendered as a
    # blank line, because §13 requires this to be *published* and an empty field
    # on a public page is a compliance gap that looks like a design choice.
    published: bool
    sla_days: int
    escalation_days: int


class OfficerSave(BaseModel):
    name: str = Field(..., min_length=2, max_length=255)
    email: EmailStr
    sla_days: int | None = Field(None, ge=1, le=90)
    escalation_days: int | None = Field(None, ge=1, le=89)


def _out(row: Grievance) -> dict[str, Any]:
    """Serialise a **fully loaded** row.

    Safe only for rows that came from a SELECT. A row that came from an INSERT has
    its never-set nullable columns marked unloaded — SQLAlchemy cannot know what
    the database put there without asking — so reading them here would trigger a
    lazy SELECT from synchronous code and raise `MissingGreenlet`. Write paths must
    go through `_detail`, which refreshes first.
    """
    return {
        **{c.name: getattr(row, c.name) for c in row.__table__.columns},
        # Computed against the clock on every read, never stored. A stored flag is
        # only as fresh as the last job that ran, and the moment it is stale is the
        # moment somebody is looking at it.
        "is_overdue": row.is_overdue,
        "days_open": row.days_open,
    }


async def _detail(current: CurrentUser, row: Grievance) -> dict[str, Any]:
    """One grievance and its timeline, for a row that may have just been written.

    The refresh is not defensive tidiness — it is required. After a `file()` or a
    status change, the columns nobody assigned (`resolution_notes`, `assigned_to`,
    `updated_at`, …) are unloaded, and `_out` walks every column. Without this the
    route raises `MissingGreenlet` on the first write, which is exactly what it did
    until a test exercised the routes rather than the service.
    """
    await current.session.refresh(row)
    events = await grievance_service.timeline(current.session, current.tenant_id, row.id)
    return {**_out(row), "timeline": events}


# --------------------------------------------------------------------------- #
# Filing
# --------------------------------------------------------------------------- #

@router.post("", response_model=GrievanceDetail, status_code=201,
             summary="File a grievance")
async def file_grievance(
    body: GrievanceFile,
    current: Annotated[CurrentUser, Depends(require(Capability.SELF_GRIEVANCE_WRITE))],
) -> Any:
    """File as the signed-in person.

    Their account address is already ours and already confirmed, so no email
    round-trip: asking somebody to prove they own the address they just logged in
    with would be theatre.
    """
    from app.api.v1.dsar import _self_principal

    principal = await _self_principal(current)
    row, _token = await grievance_service.file(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        category=body.category,
        description=body.description,
        principal_id=principal.id,
        contact_email=body.contact_email,
        related_dsar_id=body.related_dsar_id,
    )
    return await _detail(current, row)


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

@router.get("/mine", response_model=list[GrievanceOut], summary="My own grievances")
async def my_grievances(
    current: Annotated[CurrentUser, Depends(require(Capability.SELF_READ))],
) -> Any:
    from app.api.v1.dsar import _self_principal

    principal = await _self_principal(current)
    rows = await grievance_service.list_for_principal(
        current.session, current.tenant_id, principal.id
    )
    return [_out(r) for r in rows]


@router.get("/officer", response_model=OfficerOut,
            summary="The published Grievance Officer and the SLA clocks")
async def get_officer(
    current: Annotated[CurrentUser, Depends(require(Capability.SELF_READ))],
) -> Any:
    """Readable by everyone in the workspace, including data principals.

    §13 requires this contact to be *published*. A person who has to ask an
    administrator who to complain to has not been given a redressal mechanism.
    """
    return await grievance_service.officer(current.session, current.tenant_id)


@router.put("/officer", response_model=OfficerOut,
            summary="Change the published officer and the SLA clocks")
async def put_officer(
    body: OfficerSave,
    current: Annotated[CurrentUser, Depends(require(Capability.TENANT_MANAGE))],
) -> Any:
    """Changing the clocks does not rewrite existing grievances.

    Their deadlines were stamped at filing. Retroactively moving somebody's
    statutory deadline because a setting changed today would make the record
    unreliable in exactly the direction that flatters the fiduciary.
    """
    return await grievance_service.set_officer(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        name=body.name,
        email=body.email,
        sla_days=body.sla_days,
        escalation_days=body.escalation_days,
    )


@router.get("", response_model=GrievancePage, summary="The redressal queue")
async def list_grievances(
    current: Annotated[CurrentUser, Depends(require(Capability.GRIEVANCE_READ))],
    status: str | None = Query(
        None, pattern="^(open|acknowledged|in_progress|resolved|rejected|reopened)$"
    ),
    category: str | None = None,
    assigned_to: uuid.UUID | None = None,
    escalated_only: bool = False,
    overdue_only: bool = False,
    limit: int = Query(100, ge=1, le=500),
) -> Any:
    """The queue, ordered by deadline — soonest statutory risk first.

    Runs the escalation sweep before reading. A nightly job leaves a window in
    which an overdue grievance still displays as fine, and that window is exactly
    when a DPO is looking at this screen. The sweep is idempotent, so doing it on
    every read costs a query and cannot double-notify.
    """
    await grievance_service.sweep_escalations(
        current.session, tenant_id=current.tenant_id
    )
    rows = await grievance_service.list_for_tenant(
        current.session,
        current.tenant_id,
        status=status,
        category=category,
        assigned_to=assigned_to,
        escalated_only=escalated_only,
        overdue_only=overdue_only,
        limit=limit,
    )
    return {
        "items": [_out(r) for r in rows],
        "counts": await grievance_service.counts(current.session, current.tenant_id),
    }


@router.get("/{grievance_id}", response_model=GrievanceDetail,
            summary="One grievance and its timeline")
async def get_grievance(
    grievance_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.GRIEVANCE_READ))],
) -> Any:
    row = await grievance_service.get(current.session, current.tenant_id, grievance_id)
    return await _detail(current, row)


# --------------------------------------------------------------------------- #
# Working it
# --------------------------------------------------------------------------- #

@router.patch("/{grievance_id}", response_model=GrievanceDetail,
              summary="Acknowledge, progress, resolve, reject or reopen")
async def change_status(
    grievance_id: uuid.UUID,
    body: StatusChange,
    current: Annotated[CurrentUser, Depends(require(Capability.GRIEVANCE_PROCESS))],
) -> Any:
    row = await grievance_service.get(current.session, current.tenant_id, grievance_id)
    await grievance_service.change_status(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        grievance=row,
        to_status=body.to_status,
        resolution_notes=body.resolution_notes,
        rejection_reason=body.rejection_reason,
        note=body.note,
    )
    return await _detail(current, row)


@router.post("/{grievance_id}/assign", response_model=GrievanceDetail,
             summary="Assign to a user, or unassign")
async def assign(
    grievance_id: uuid.UUID,
    body: Assignment,
    current: Annotated[CurrentUser, Depends(require(Capability.GRIEVANCE_PROCESS))],
) -> Any:
    row = await grievance_service.get(current.session, current.tenant_id, grievance_id)
    await grievance_service.assign(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        grievance=row, user_id=body.user_id,
    )
    return await _detail(current, row)


@router.post("/{grievance_id}/escalate", response_model=GrievanceDetail,
             summary="Escalate to the Grievance Officer")
async def escalate(
    grievance_id: uuid.UUID,
    body: Escalation,
    current: Annotated[CurrentUser, Depends(require(Capability.GRIEVANCE_ESCALATE))],
) -> Any:
    """Manual escalation. Idempotent — already-escalated is not an error.

    Notifies the published officer and raises the grievance in the queue. It does
    **not** contact the Data Protection Board: unattended regulator contact is a
    decision a person makes, and this flag is what tells them to make it.
    """
    row = await grievance_service.get(current.session, current.tenant_id, grievance_id)
    await grievance_service.escalate(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        grievance=row, reason=body.reason,
    )
    return await _detail(current, row)


@router.post("/{grievance_id}/feedback", response_model=GrievanceDetail,
             summary="Rate the resolution (the filer only)")
async def feedback(
    grievance_id: uuid.UUID,
    body: Feedback,
    current: Annotated[CurrentUser, Depends(require(Capability.SELF_GRIEVANCE_WRITE))],
) -> Any:
    """The filer's verdict, and only the filer's.

    Ownership is checked against the session's own principal record rather than
    trusted from the request — otherwise anybody with an account could rate
    anybody's grievance, and a rating of 1 reopens it.

    A rating of 1 or 2 reopens the grievance. That is deliberate: a satisfaction
    score that feeds a dashboard and changes nothing is a metric, not redress.
    """
    from app.api.v1.dsar import _self_principal
    from app.core.errors import NotFound

    principal = await _self_principal(current)
    row = await grievance_service.get(current.session, current.tenant_id, grievance_id)
    if row.principal_id != principal.id:
        # 404 rather than 403: confirming that a grievance exists but belongs to
        # somebody else is itself a disclosure.
        raise NotFound("No such grievance.")

    await grievance_service.rate(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        grievance=row, rating=body.rating, comment=body.comment,
    )
    return await _detail(current, row)
